"""训练配置、参数冻结策略与模型体检。

本工程的目标模型是 **Qwen/Qwen3.5-2B 纯文本**用途：只做事件评价与情绪解释。
因此这里有一条硬性策略 —— **冻结视觉与 embedding/lm_head**：

* **视觉塔**（vision tower / vision projector / merger）：纯文本任务完全用不到，
  训练它们既浪费显存又会让 adapter 带上无用的视觉梯度。Qwen-VL 系列在
  ``from_pretrained`` 时甚至不该实例化视觉部分（见 :func:`resolve_model_kwargs`）。
* **embedding 与 lm_head**：2B 模型的词表很大（Qwen 系列约 15 万 token），
  embedding + lm_head 往往占掉全部参数的 15~25%。LoRA 场景下解冻它们
  会显著增加优化器状态与过拟合风险，且我们的输出空间被 schema 严格约束，
  没有学新词表的必要。
* **norm 层**（``layernorm`` / ``rmsnorm`` / ``model.norm``）：
  LoRA 的 scaling 已经提供了调节幅度，再解冻 norm 会让训练更不稳定。
  默认冻结；如确实需要可用 ``unfreeze_norms: true`` 打开。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ConfigError

LOGGER = logging.getLogger("qboss_training.training")

#: 视觉相关模块名子串（命中即冻结 / 不加载）
VISION_PATTERNS: tuple[str, ...] = (
    "visual",
    "vision",
    "vision_model",
    "vision_tower",
    "vision_projection",
    "vision_projector",
    "merger",
    "multi_modal_projector",
    "mm_projector",
    "image_encoder",
    "img_encoder",
    "patch_embed",
    "resampler",
    "perceiver",
    "vae",
    "audio",
    "speech",
)

#: embedding / 输出头相关模块名子串
EMBEDDING_PATTERNS: tuple[str, ...] = (
    "embed_tokens",
    "embeddings",
    "embedding",
    "wte",
    "wpe",
    "lm_head",
    "output_projection",
    "score",
)

#: norm 层相关模块名子串
NORM_PATTERNS: tuple[str, ...] = (
    "norm",
    "layernorm",
    "layer_norm",
    "ln_f",
    "ln_1",
    "ln_2",
)


@dataclass
class ModelConfig:
    """模型加载配置。"""

    name_or_path: str = "Qwen/Qwen3.5-2B"
    #: 权重大小：2B 用 bf16（RTX 4060 8G 可放下 + LoRA）
    torch_dtype: str = "bfloat16"
    #: auto / cuda / cpu
    device: str = "auto"
    #: 是否加载视觉模块。纯文本任务必须为 False。
    load_vision: bool = False
    trust_remote_code: bool = False
    #: attention 实现：sdpa / flash_attention_2 / eager
    attn_implementation: str = "sdpa"
    #: 从本地目录加载（离线环境）
    local_files_only: bool = False
    extra_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoraConfigSpec:
    """LoRA 超参。默认值针对"小模型 + 严格结构化输出"场景。"""

    r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    bias: str = "none"
    #: 默认只挂在 attention 的 q/k/v/o 投影上。
    #: 2B 结构下加 FFN 收益有限但显存涨得明显，因此默认不加。
    target_modules: tuple[str, ...] = (
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
    )
    #: 是否把 MLP 也纳入 LoRA
    include_mlp: bool = False
    mlp_target_modules: tuple[str, ...] = ("gate_proj", "up_proj", "down_proj")
    use_rslora: bool = False
    use_dora: bool = False
    init_lora_weights: str = "gaussian"
    task_type: str = "CAUSAL_LM"

    def resolved_target_modules(self) -> list[str]:
        modules = list(self.target_modules)
        if self.include_mlp:
            modules.extend(self.mlp_target_modules)
        # 去重且保序
        seen: set[str] = set()
        ordered: list[str] = []
        for module in modules:
            if module not in seen:
                seen.add(module)
                ordered.append(module)
        return ordered


@dataclass
class FreezeConfig:
    """冻结策略。"""

    freeze_vision: bool = True
    freeze_embeddings: bool = True
    freeze_lm_head: bool = True
    freeze_norms: bool = True
    #: 额外按名字子串冻结
    freeze_patterns: tuple[str, ...] = ()
    #: 强制解冻的名字子串（优先级最高）
    unfreeze_patterns: tuple[str, ...] = ()


@dataclass
class QuantizationConfig:
    """QLoRA 4bit 量化配置（fallback 路径）。"""

    enabled: bool = False
    load_in_4bit: bool = True
    bnb_4bit_quant_type: str = "nf4"
    bnb_4bit_compute_dtype: str = "bfloat16"
    bnb_4bit_use_double_quant: bool = True
    #: 4bit 基座必须配合 gradient checkpointing + paged optimizer
    optim: str = "paged_adamw_8bit"


@dataclass
class TrainingConfig:
    """训练总配置。"""

    model: ModelConfig = field(default_factory=ModelConfig)
    lora: LoraConfigSpec = field(default_factory=LoraConfigSpec)
    freeze: FreezeConfig = field(default_factory=FreezeConfig)
    quantization: QuantizationConfig = field(default_factory=QuantizationConfig)

    train_file: str = "data/sft/train.jsonl"
    eval_file: str = "data/sft/val.jsonl"
    output_dir: str = "outputs/lora-event-eval"

    max_seq_length: int = 1024
    per_device_train_batch_size: int = 2
    per_device_eval_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    learning_rate: float = 8.0e-5
    lr_scheduler_type: str = "cosine"
    warmup_ratio: float = 0.03
    num_train_epochs: float = 2.0
    max_steps: int = -1
    weight_decay: float = 0.0
    max_grad_norm: float = 1.0
    optim: str = "adamw_torch"
    bf16: bool = True
    fp16: bool = False
    gradient_checkpointing: bool = True
    gradient_checkpointing_kwargs: dict[str, Any] = field(
        default_factory=lambda: {"use_reentrant": False}
    )
    logging_steps: int = 10
    save_steps: int = 200
    eval_steps: int = 200
    save_total_limit: int = 3
    seed: int = 42
    data_seed: int = 42
    report_to: tuple[str, ...] = ()
    resume_from_checkpoint: str | None = None
    enable_thinking: bool = False
    pretty_json: bool = True
    packing: bool = False
    dataloader_num_workers: int = 0
    #: 训练前是否做一次模型体检（打印可训练参数与冻结命中情况）
    verify_freeze: bool = True


# --------------------------------------------------------------------------
# 冻结
# --------------------------------------------------------------------------

def matches_any(name: str, patterns: Sequence[str]) -> bool:
    lowered = name.lower()
    return any(pattern in lowered for pattern in patterns)


def classify_parameter(name: str) -> str:
    """把参数名归类：vision / embedding / norm / other。

    顺序很重要 —— ``model.embed_tokens`` 同时像 embedding，
    而 ``vision_model.embeddings`` 应该优先判为 vision。
    """
    lowered = name.lower()
    if matches_any(lowered, VISION_PATTERNS):
        return "vision"
    if matches_any(lowered, EMBEDDING_PATTERNS):
        return "embedding"
    if matches_any(lowered, NORM_PATTERNS):
        return "norm"
    return "other"


def should_freeze(name: str, config: FreezeConfig) -> tuple[bool, str]:
    """判断单个参数是否应冻结，返回 ``(是否冻结, 原因)``。"""
    lowered = name.lower()

    if config.unfreeze_patterns and matches_any(lowered, config.unfreeze_patterns):
        return False, "unfreeze_patterns"

    if config.freeze_patterns and matches_any(lowered, config.freeze_patterns):
        return True, "freeze_patterns"

    kind = classify_parameter(lowered)
    if kind == "vision" and config.freeze_vision:
        return True, "vision"
    if kind == "embedding" and config.freeze_embeddings:
        # lm_head 与 embed_tokens 在 tie_word_embeddings 时是同一份权重，
        # 单独区分一下原因便于排查
        reason = "lm_head" if "lm_head" in lowered or "output_projection" in lowered else "embedding"
        if reason == "lm_head" and not config.freeze_lm_head:
            return False, ""
        return True, reason
    if kind == "norm" and config.freeze_norms:
        return True, "norm"
    return False, ""


def apply_freeze_policy(model: Any, config: FreezeConfig) -> dict[str, Any]:
    """对**基座模型**应用冻结策略（LoRA 之前调用）。

    返回统计报告：每个类别的参数量与冻结命中数。
    """
    report: dict[str, Any] = {
        "total_params": 0,
        "trainable_before": 0,
        "trainable_after": 0,
        "frozen_params": 0,
        "by_kind": {},
        "examples": [],
    }

    for name, parameter in model.named_parameters():
        count = parameter.numel()
        report["total_params"] += count
        kind = classify_parameter(name)
        bucket = report["by_kind"].setdefault(
            kind, {"params": 0, "frozen_params": 0, "tensors": 0}
        )
        bucket["params"] += count
        bucket["tensors"] += 1

        if parameter.requires_grad:
            report["trainable_before"] += count

        freeze, reason = should_freeze(name, config)
        if freeze:
            parameter.requires_grad = False
            bucket["frozen_params"] += count
            report["frozen_params"] += count
            if len(report["examples"]) < 12:
                report["examples"].append({"name": name, "reason": reason, "numel": count})
        if parameter.requires_grad:
            report["trainable_after"] += count

    return report


def iter_trainable_names(model: Any) -> list[str]:
    return [name for name, parameter in model.named_parameters() if parameter.requires_grad]


def assert_freeze_invariants(model: Any, config: FreezeConfig) -> list[str]:
    """校验冻结策略是否真的生效。返回问题列表（空 = 正常）。

    这是"配置写了但没生效"的兜底：LoRA 注入顺序、tie_weights、
    自定义实现都可能让 embedding 悄悄保持可训练。
    """
    problems: list[str] = []
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad:
            continue
        kind = classify_parameter(name)
        if kind == "vision" and config.freeze_vision:
            problems.append(f"视觉参数仍可训练：{name}")
        elif kind == "embedding" and config.freeze_embeddings:
            problems.append(f"embedding/lm_head 仍可训练：{name}")
        elif kind == "norm" and config.freeze_norms:
            problems.append(f"norm 仍可训练：{name}")
    return problems


# --------------------------------------------------------------------------
# 模型加载
# --------------------------------------------------------------------------

def resolve_model_kwargs(config: ModelConfig) -> dict[str, Any]:
    """构造 ``from_pretrained`` 关键字参数。

    关键点：纯文本任务下，对支持多模态的 checkpoint 要显式关闭视觉塔。
    """
    kwargs: dict[str, Any] = {
        "trust_remote_code": config.trust_remote_code,
        "local_files_only": config.local_files_only,
    }
    if not config.load_vision:
        # Qwen-VL 系列认这两个开关；不支持的模型会忽略未知 kwargs 报错，
        # 因此调用方需要能容忍失败（见 load_base_model）。
        kwargs["_disable_vision"] = True
        kwargs["vision_config"] = None

    if config.attn_implementation:
        kwargs["attn_implementation"] = config.attn_implementation

    kwargs.update(config.extra_kwargs)
    return kwargs


def load_base_model(config: ModelConfig, *, quantization: QuantizationConfig | None = None) -> Any:
    """加载基座模型（延迟 import torch/transformers）。"""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    dtype_map = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    torch_dtype = dtype_map.get(config.torch_dtype, torch.bfloat16)

    kwargs = resolve_model_kwargs(config)

    if quantization is not None and quantization.enabled:
        from transformers import BitsAndBytesConfig

        compute_dtype = dtype_map.get(
            quantization.bnb_4bit_compute_dtype, torch.bfloat16
        )
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=quantization.load_in_4bit,
            bnb_4bit_quant_type=quantization.bnb_4bit_quant_type,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=quantization.bnb_4bit_use_double_quant,
        )
        kwargs.pop("torch_dtype", None)
    else:
        kwargs["torch_dtype"] = torch_dtype

    if config.device == "auto":
        kwargs["device_map"] = "auto"

    model = _from_pretrained_with_vision_fallback(
        AutoModelForCausalLM, config, kwargs
    )

    tokenizer = AutoTokenizer.from_pretrained(
        config.name_or_path,
        trust_remote_code=config.trust_remote_code,
        local_files_only=config.local_files_only,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if config.device == "cpu":
        model = model.to("cpu")

    return model, tokenizer


def _from_pretrained_with_vision_fallback(
    auto_class: Any, config: ModelConfig, kwargs: dict[str, Any]
) -> Any:
    """先尝试带"关闭视觉"参数的加载；失败则回退到普通加载。

    不同 transformers 版本对未知 kwargs 的处理不一致，这里做兼容。
    """
    try:
        return auto_class.from_pretrained(config.name_or_path, **kwargs)
    except (TypeError, ValueError, KeyError) as exc:
        fallback = dict(kwargs)
        fallback.pop("_disable_vision", None)
        fallback.pop("vision_config", None)
        message = str(exc)
        if not any(
            token in message
            for token in ("vision_config", "_disable_vision", "unexpected keyword")
        ):
            raise
        LOGGER.warning(
            "模型不接受关闭视觉的参数，回退到普通加载（视觉模块仍会被冻结）：%s",
            message[:200],
        )
        return auto_class.from_pretrained(config.name_or_path, **fallback)


# --------------------------------------------------------------------------
# smoke / QLoRA 覆盖
# --------------------------------------------------------------------------

def apply_smoke_overrides(config: TrainingConfig) -> TrainingConfig:
    """smoke test：几十步、极小批次，只为验证管线能跑通。"""
    import copy

    smoke = copy.deepcopy(config)
    smoke.output_dir = str(Path(config.output_dir) / "smoke")
    smoke.max_steps = 4
    smoke.num_train_epochs = 1.0
    smoke.per_device_train_batch_size = 1
    smoke.per_device_eval_batch_size = 1
    smoke.gradient_accumulation_steps = 1
    smoke.logging_steps = 1
    smoke.save_steps = 4
    smoke.eval_steps = 4
    smoke.save_total_limit = 1
    smoke.max_seq_length = min(config.max_seq_length, 512)
    smoke.lora.r = min(config.lora.r, 8)
    smoke.lora.lora_alpha = min(config.lora.lora_alpha, 16)
    smoke.verify_freeze = True
    smoke.report_to = ()
    return smoke


def apply_qlora_fallback(config: TrainingConfig) -> TrainingConfig:
    """QLoRA fallback：4bit 基座 + paged optimizer。"""
    import copy

    qlora = copy.deepcopy(config)
    qlora.quantization.enabled = True
    qlora.quantization.load_in_4bit = True
    qlora.optim = qlora.quantization.optim
    qlora.gradient_checkpointing = True
    # 4bit 基座 + bf16 计算：不要开 fp16
    qlora.bf16 = config.bf16
    qlora.fp16 = False
    # QLoRA 下解冻 embedding 会破坏量化收益，强制冻结
    qlora.freeze.freeze_embeddings = True
    qlora.freeze.freeze_lm_head = True
    return qlora


def validate_training_config(config: TrainingConfig) -> list[str]:
    """静态检查明显不合理的组合。返回问题列表。"""
    problems: list[str] = []
    if config.model.load_vision:
        problems.append(
            "model.load_vision=true：本任务是纯文本，加载视觉塔只会浪费显存"
        )
    if config.quantization.enabled and not config.optim.startswith("paged_"):
        problems.append(
            "QLoRA 建议使用 paged optimizer（optim: paged_adamw_8bit）以避免显存峰值 OOM"
        )
    if config.quantization.enabled and config.fp16 and not config.bf16:
        problems.append("4bit 量化 + fp16 训练容易数值不稳，建议 bf16")
    if config.bf16 and config.fp16:
        problems.append("bf16 与 fp16 不能同时开启")
    if config.per_device_train_batch_size <= 0:
        problems.append("per_device_train_batch_size 必须为正")
    if config.learning_rate <= 0:
        problems.append("learning_rate 必须为正")
    if config.max_steps == -1 and config.num_train_epochs <= 0:
        problems.append("num_train_epochs 必须为正，或显式设置 max_steps")
    if not config.freeze.freeze_embeddings:
        problems.append(
            "freeze_embeddings=false：2B 词表 embedding 占参数比例很高，"
            "解冻会显著增加过拟合与显存风险"
        )
    if config.packing:
        problems.append(
            "packing=true 会跨样本拼接，可能破坏 completion-only 掩码边界，"
            "本工程默认关闭"
        )
    return problems


def describe_training_config(config: TrainingConfig) -> str:
    """单行摘要，便于日志与实验记录。"""
    mode = "QLoRA(4bit)" if config.quantization.enabled else "BF16-LoRA"
    targets = ",".join(config.lora.resolved_target_modules())
    return (
        f"{mode} | model={config.model.name_or_path} | r={config.lora.r} "
        f"alpha={config.lora.lora_alpha} | targets={targets} | "
        f"bs={config.per_device_train_batch_size}x{config.gradient_accumulation_steps} "
        f"| lr={config.learning_rate} | seq={config.max_seq_length}"
    )
