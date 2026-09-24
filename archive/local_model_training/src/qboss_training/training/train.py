"""LoRA / QLoRA 训练脚本（BF16 优先，QLoRA 作为 fallback）。

核心保证
--------
1. **completion-only**：labels 由 :mod:`qboss_training.sft.format` 预先算好，
   只覆盖 assistant 的 JSON 段；本脚本会把 tokenized 数据集直接喂给 trainer
   （``skip_prepare_dataset``），避免 TRL 二次加工破坏掩码。
2. **冻结视觉与 embedding/lm_head**：见 :mod:`qboss_training.training.config`。
   LoRA 注入前后各校验一次，任何"该冻结却可训练"的参数都会让脚本报错退出。
3. **版本鲁棒**：TRL / transformers 的 ``SFTConfig`` 参数名在版本间变动频繁，
   这里按签名过滤关键字，而不是硬编码某个版本的 API。
4. **smoke test**：``--smoke`` 用 4 步、batch=1 跑通管线，
   在真正花钱训练之前先证明数据、掩码、adapter、保存链路都是通的。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ConfigError, TrainingError
from ..sft.format import SFTBuildConfig, build_sft_dataset, load_tokenizer
from ..training.config import (
    FreezeConfig,
    TrainingConfig,
    apply_freeze_policy,
    apply_smoke_overrides,
    assert_freeze_invariants,
    classify_parameter,
    describe_training_config,
    load_base_model,
    validate_training_config,
)
from ..utils.io import read_jsonl, write_json
from ..utils.secrets import utc_now_iso

LOGGER = logging.getLogger("qboss_training.train")


def _filter_kwargs(target: Any, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """只保留 ``target`` 能接受的关键字，其余丢弃并告警。

    这让脚本能同时跑在多个 TRL / transformers 版本上：
    参数名变了就退化，而不是直接 TypeError 崩掉。
    """
    import inspect

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):  # pragma: no cover
        return dict(kwargs)

    parameters = signature.parameters
    accepts_var_kwargs = any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    )
    if accepts_var_kwargs:
        return dict(kwargs)

    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in kwargs.items():
        if key in parameters:
            kept[key] = value
        else:
            dropped.append(key)
    if dropped:
        LOGGER.warning("目标不接受这些参数，已忽略：%s", ", ".join(sorted(dropped)))
    return kept


def build_lora_config(spec: Any) -> Any:
    """构造 peft.LoraConfig（按签名过滤，兼容 use_rslora/use_dora 等新参数）。"""
    from peft import LoraConfig, TaskType

    task_type = getattr(TaskType, spec.task_type, TaskType.CAUSAL_LM)
    kwargs = {
        "r": spec.r,
        "lora_alpha": spec.lora_alpha,
        "lora_dropout": spec.lora_dropout,
        "bias": spec.bias,
        "target_modules": spec.resolved_target_modules(),
        "task_type": task_type,
        "use_rslora": spec.use_rslora,
        "use_dora": spec.use_dora,
        "init_lora_weights": spec.init_lora_weights,
    }
    return LoraConfig(**_filter_kwargs(LoraConfig.__init__, kwargs))


def inject_lora(model: Any, config: TrainingConfig) -> Any:
    """注入 LoRA adapter。"""
    from peft import get_peft_model

    if config.quantization.enabled:
        from peft import prepare_model_for_kbit_training

        model = prepare_model_for_kbit_training(
            model, use_gradient_checkpointing=config.gradient_checkpointing
        )

    lora_config = build_lora_config(config.lora)
    peft_model = get_peft_model(model, lora_config)
    return peft_model


def summarize_trainable(peft_model: Any, _freeze: FreezeConfig | None = None) -> dict[str, Any]:
    """统计可训练参数，并按类别拆分。"""
    from collections import defaultdict

    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"tensors": 0, "params": 0})
    total_trainable = 0
    total = 0
    lora_params = 0

    for name, parameter in peft_model.named_parameters():
        count = parameter.numel()
        total += count
        if not parameter.requires_grad:
            continue
        total_trainable += count
        if "lora_" in name.lower():
            lora_params += count
            kind = "lora"
        else:
            kind = classify_parameter(name)
        buckets[kind]["tensors"] += 1
        buckets[kind]["params"] += count

    return {
        "total_params": total,
        "trainable_params": total_trainable,
        "trainable_ratio": round(total_trainable / total, 6) if total else 0.0,
        "lora_params": lora_params,
        "by_kind": {key: dict(value) for key, value in sorted(buckets.items())},
    }


def load_training_records(config: TrainingConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    train_path = Path(config.train_file)
    if not train_path.exists():
        raise ConfigError(
            f"训练文件不存在：{train_path}。请先运行 `qboss sft` 生成 SFT 数据。"
        )
    train_records = read_jsonl(train_path)
    eval_records: list[dict[str, Any]] = []
    eval_path = Path(config.eval_file) if config.eval_file else None
    if eval_path and eval_path.exists():
        eval_records = read_jsonl(eval_path)
    if not train_records:
        raise ConfigError(f"训练文件为空：{train_path}")
    return train_records, eval_records


def _detect_resume_checkpoint(config: TrainingConfig) -> str | None:
    """自动断点续训：输出目录里已有 checkpoint 时从最新的继续。"""
    if config.resume_from_checkpoint:
        return config.resume_from_checkpoint
    output_dir = Path(config.output_dir)
    if not output_dir.exists():
        return None
    checkpoints = [
        path
        for path in output_dir.glob("checkpoint-*")
        if path.is_dir() and (path / "trainer_state.json").exists()
    ]
    if not checkpoints:
        return None

    def _step(path: Path) -> int:
        try:
            return int(path.name.split("-")[-1])
        except (ValueError, IndexError):
            return -1

    latest = max(checkpoints, key=_step)
    LOGGER.info("检测到断点，将从 %s 继续训练", latest)
    return str(latest)


def build_training_arguments(config: TrainingConfig, *, has_eval: bool) -> Any:
    """构造 SFTConfig / TrainingArguments（按签名过滤）。"""
    evaluation_strategy = "steps" if has_eval else "no"
    kwargs: dict[str, Any] = {
        "output_dir": config.output_dir,
        "per_device_train_batch_size": config.per_device_train_batch_size,
        "per_device_eval_batch_size": config.per_device_eval_batch_size,
        "gradient_accumulation_steps": config.gradient_accumulation_steps,
        "learning_rate": config.learning_rate,
        "lr_scheduler_type": config.lr_scheduler_type,
        "warmup_ratio": config.warmup_ratio,
        "num_train_epochs": config.num_train_epochs,
        "max_steps": config.max_steps,
        "weight_decay": config.weight_decay,
        "max_grad_norm": config.max_grad_norm,
        "optim": config.optim,
        "bf16": config.bf16,
        "fp16": config.fp16,
        "gradient_checkpointing": config.gradient_checkpointing,
        "gradient_checkpointing_kwargs": config.gradient_checkpointing_kwargs,
        "logging_steps": config.logging_steps,
        "save_steps": config.save_steps,
        "eval_steps": config.eval_steps if has_eval else None,
        "save_total_limit": config.save_total_limit,
        "seed": config.seed,
        "data_seed": config.data_seed,
        "report_to": list(config.report_to),
        "dataloader_num_workers": config.dataloader_num_workers,
        # SFT 专用
        "max_seq_length": config.max_seq_length,
        "packing": False,  # 必须关闭：packing 会破坏 completion-only 掩码
        "completion_only_loss": False,  # labels 已由我们算好，避免二次掩码
        "dataset_kwargs": {"skip_prepare_dataset": True},
        "dataset_text_field": "text",
        "evaluation_strategy": evaluation_strategy,
        "eval_strategy": evaluation_strategy,
    }

    try:
        from trl import SFTConfig

        return SFTConfig(**_filter_kwargs(SFTConfig.__init__, kwargs))
    except ImportError:
        from transformers import TrainingArguments

        LOGGER.warning("未安装 trl，退化为 TrainingArguments（无 SFT 专用参数）")
        return TrainingArguments(**_filter_kwargs(TrainingArguments.__init__, kwargs))


def build_trainer(
    model: Any,
    tokenizer: Any,
    train_dataset: Any,
    eval_dataset: Any | None,
    config: TrainingConfig,
    *,
    training_args: Any | None = None,
) -> Any:
    """构造 SFTTrainer。"""
    from trl import SFTTrainer

    args = training_args or build_training_arguments(config, has_eval=eval_dataset is not None)

    kwargs: dict[str, Any] = {
        "model": model,
        "args": args,
        "train_dataset": train_dataset,
        "eval_dataset": eval_dataset,
        "processing_class": tokenizer,
        "tokenizer": tokenizer,
        "data_collator": build_data_collator(tokenizer),
    }
    return SFTTrainer(**_filter_kwargs(SFTTrainer.__init__, kwargs))


def build_data_collator(tokenizer: Any) -> Any:
    """padding collator：``input_ids`` 用 pad，``labels`` 用 -100。"""
    from transformers import DataCollatorForSeq2Seq

    return DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        model=None,
        padding=True,
        label_pad_token_id=-100,
        pad_to_multiple_of=8,
        return_tensors="pt",
    )


def prepare_datasets(
    train_records: Sequence[Mapping[str, Any]],
    eval_records: Sequence[Mapping[str, Any]],
    tokenizer: Any,
    config: TrainingConfig,
) -> tuple[Any, Any | None, dict[str, Any]]:
    """编码数据集并产出统计。"""
    build_config = SFTBuildConfig(
        model_name_or_path=config.model.name_or_path,
        max_seq_length=config.max_seq_length,
        enable_thinking=config.enable_thinking,
        pretty_json=config.pretty_json,
        drop_over_length=True,
    )

    train_samples, train_stats = build_sft_dataset(train_records, tokenizer, build_config)
    if not train_samples:
        raise TrainingError(
            "没有可用训练样本：全部被丢弃。请检查 max_seq_length "
            f"（当前 {config.max_seq_length}）与数据长度分布。"
        )

    eval_samples: list[dict[str, Any]] = []
    eval_stats: dict[str, Any] = {}
    if eval_records:
        eval_samples, stats = build_sft_dataset(
            eval_records, tokenizer, build_config
        )
        eval_stats = stats.to_dict()

    from datasets import Dataset

    train_dataset = Dataset.from_list(train_samples)
    eval_dataset = Dataset.from_list(eval_samples) if eval_samples else None

    stats_payload = {
        "train": train_stats.to_dict(),
        "eval": eval_stats,
        "train_samples": len(train_samples),
        "eval_samples": len(eval_samples),
        "dropped_train": len(train_records) - len(train_samples),
    }
    return train_dataset, eval_dataset, stats_payload


def run_training(config: TrainingConfig, *, smoke: bool = False) -> dict[str, Any]:
    """执行训练。返回运行报告。"""
    if smoke:
        config = apply_smoke_overrides(config)
        LOGGER.info("SMOKE 模式：max_steps=%d, batch=%d", config.max_steps, config.per_device_train_batch_size)

    problems = validate_training_config(config)
    hard_problems = [item for item in problems if "load_vision" in item or "必须为正" in item]
    if hard_problems:
        raise ConfigError("训练配置不合法：\n  - " + "\n  - ".join(hard_problems))
    for item in problems:
        LOGGER.warning("配置提醒：%s", item)

    LOGGER.info("训练配置：%s", describe_training_config(config))
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) 基座 + tokenizer
    model, tokenizer = load_base_model(config.model, quantization=config.quantization)

    # 2) 冻结策略（LoRA 之前）
    freeze_report = apply_freeze_policy(model, config.freeze)
    LOGGER.info(
        "冻结：总参数 %.1fM，冻结 %.1fM，冻结后基座可训练 %.1fM",
        freeze_report["total_params"] / 1e6,
        freeze_report["frozen_params"] / 1e6,
        freeze_report["trainable_after"] / 1e6,
    )

    # 3) LoRA
    peft_model = inject_lora(model, config)
    trainable_report = summarize_trainable(peft_model, config.freeze)

    # 4) 再次校验：LoRA 注入后 embedding/视觉仍不得可训练
    violations = assert_freeze_invariants(peft_model, config.freeze)
    if violations:
        raise TrainingError(
            "冻结策略未生效，拒绝开始训练：\n  - " + "\n  - ".join(violations[:10])
        )

    if hasattr(peft_model, "print_trainable_parameters"):
        peft_model.print_trainable_parameters()

    # 5) 数据
    train_records, eval_records = load_training_records(config)
    train_dataset, eval_dataset, data_stats = prepare_datasets(
        train_records, eval_records, tokenizer, config
    )
    LOGGER.info(
        "数据：train=%d（丢弃 %d），eval=%d，平均 completion token=%.1f",
        data_stats["train_samples"],
        data_stats["dropped_train"],
        data_stats["eval_samples"],
        data_stats["train"].get("mean_completion_tokens", 0.0),
    )

    # 6) 训练
    trainer = build_trainer(peft_model, tokenizer, train_dataset, eval_dataset, config)
    resume = _detect_resume_checkpoint(config)
    started = utc_now_iso()
    train_result = trainer.train(resume_from_checkpoint=resume)

    # 7) 保存
    trainer.save_model(config.output_dir)
    tokenizer.save_pretrained(config.output_dir)

    metrics = dict(getattr(train_result, "metrics", {}) or {})
    try:
        from ..validators import bundle_fingerprint

        schema_fingerprint = bundle_fingerprint()
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("计算 schema 指纹失败：%s", exc)
        schema_fingerprint = ""

    report: dict[str, Any] = {
        "mode": "QLoRA" if config.quantization.enabled else "BF16-LoRA",
        "smoke": smoke,
        "started_at": started,
        "finished_at": utc_now_iso(),
        "output_dir": config.output_dir,
        "resumed_from": resume,
        "schema_fingerprint": schema_fingerprint,
        "config": _config_as_dict(config),
        "freeze": freeze_report,
        "trainable": trainable_report,
        "data": data_stats,
        "metrics": metrics,
        "world_size": 1,
    }

    write_json(output_dir / "training_report.json", report)
    write_json(output_dir / "freeze_report.json", freeze_report)
    write_json(
        output_dir / "schema_fingerprint.json",
        {"schema_fingerprint": schema_fingerprint, "created_at": utc_now_iso()},
    )

    LOGGER.info("训练完成，adapter 已保存到 %s", config.output_dir)
    return report


def _config_as_dict(config: TrainingConfig) -> dict[str, Any]:
    def _convert(node: Any) -> Any:
        if isinstance(node, tuple):
            return list(node)
        if isinstance(node, dict):
            return {key: _convert(value) for key, value in node.items()}
        return node

    return _convert(asdict(config))
