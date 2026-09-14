"""命令行入口：``python -m qboss_training.cli <command>``。

命令一览
--------
``gen``          用 DeepSeek 生成训练数据（预算 / 批次 / 断点续跑 / 去重）
``validate``     离线校验数据集（schema + 不变量 + 文本约束）
``split``        离线切分 train/val/test（确定性、防泄漏）
``sft``          构建聊天 SFT jsonl（completion-only）
``eval``         评测（schema 合法率 / 字段准确率与误差 / 不变量 / 文本约束）
``train``        BF16 LoRA 训练（``--qlora`` 切换 QLoRA fallback，``--smoke`` 冒烟）
``merge``        合并 adapter 到基座权重
``gguf``         导出 llama.cpp GGUF（含量化矩阵）
``bench``        CPU 推理基准
``plan``         打印本次要执行的命令（不调用任何 API / 不加载模型）

约定：**任何命令都不接受 API key 参数**，只从环境变量 DEEPSEEK_API_KEY 读取。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from .benchmark import (
    BenchmarkConfig,
    benchmark_backend,
    render_benchmark_summary,
    run_benchmark,
)
from .config import GenerationConfig, ProjectConfig, SplitConfig, load_project_config
from .contracts import TASK_NAMES
from .data.generator import dry_run_plan, generation_summary, run_generation
from .data.io import ensure_dir, read_jsonl, write_json
from .data.splitter import (
    SPLITS,
    assert_task_coverage,
    describe_splits,
    load_and_split,
)
from .errors import BudgetExceeded, MissingCredentialError, TrainingError
from .eval.evaluator import (
    EvalConfig,
    evaluate_records,
    evaluate_with_backend,
    limit_records_by_task,
    load_eval_records,
    render_eval_summary,
    threshold_gate,
    write_eval_report,
)
from .inference import build_gold_replay_backend, load_backend
from .sft.format import (
    DEFAULT_PRETTY_JSON,
    SFTBuildConfig,
    build_sft_dataset,
    build_messages,
    load_tokenizer,
    masking_preview,
    write_sft_jsonl,
)
from .training.config import (
    FreezeConfig,
    LoraConfigSpec,
    ModelConfig,
    QuantizationConfig,
    TrainingConfig,
    apply_qlora_fallback,
    describe_training_config,
)
from .training.export_gguf import (
    ExportConfig,
    describe_matrix,
    estimate_ram_mb,
    run_export,
)
from .training.merge import merge_adapter, verify_merged_model
from .utils.secrets import redact, setup_logging
from .validators import (
    bundle_fingerprint,
    schema_bundle,
    validate_records,
)

LOGGER = logging.getLogger("qboss_training.cli")


# --------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qboss",
        description="理解痞老板 Runtime 的 2B 模型训练工程（事件评价 + 情绪解释）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  qboss gen --task event_eval --target 200\n"
            "  qboss validate --input data/raw/event_eval.jsonl\n"
            "  qboss split --input data/raw/event_eval.jsonl data/raw/emotion_explain.jsonl\n"
            "  qboss sft --split-dir data/splits --output-dir data/sft\n"
            "  qboss eval --input data/splits/test.jsonl --backend transformers "
            "--model-path outputs/merged\n"
            "  qboss train --config configs/lora_bf16.yaml --smoke\n"
            "  qboss bench --input data/splits/test.jsonl --backend llama_cpp "
            "--gguf-path out/qboss-2b-Q4_K_M.gguf\n"
        ),
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("--log-json", action="store_true", help="日志输出为 JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_gen(subparsers)
    _add_validate(subparsers)
    _add_split(subparsers)
    _add_sft(subparsers)
    _add_eval(subparsers)
    _add_train(subparsers)
    _add_merge(subparsers)
    _add_gguf(subparsers)
    _add_bench(subparsers)
    _add_plan(subparsers)

    return parser


def _common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="YAML 配置文件")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="覆盖配置项，可多次，如 --set generation.concurrency=8",
    )


def _parse_overrides(items: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--set 需要 KEY=VALUE 形式，收到：{item!r}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = value.strip()
    return overrides


def _load_config(args: argparse.Namespace) -> ProjectConfig:
    return load_project_config(
        getattr(args, "config", None), _parse_overrides(getattr(args, "overrides", []) or [])
    )


# --------------------------------------------------------------------------
# 子命令定义
# --------------------------------------------------------------------------

def _add_gen(sub: Any) -> None:
    parser = sub.add_parser("gen", help="用 DeepSeek 生成训练数据")
    _common_config_args(parser)
    parser.add_argument("--task", choices=list(TASK_NAMES), help="生成哪个任务")
    parser.add_argument("--target", type=int, help="目标样本数")
    parser.add_argument("--batch-size", type=int, help="每批条数")
    parser.add_argument("--concurrency", type=int, help="并发请求数")
    parser.add_argument("--output-dir", help="输出目录")
    parser.add_argument("--model", help="教师模型名")
    parser.add_argument("--base-url", help="OpenAI 兼容 base_url")
    parser.add_argument("--max-usd", type=float, help="预算上限（美元）")
    parser.add_argument("--max-requests", type=int, help="请求数上限")
    parser.add_argument("--seed", type=int, help="种子场景随机种子")
    parser.add_argument("--self-verify", action="store_true", help="开启教师自检修正（成本翻倍）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划与凭据状态，不调用 API",
    )
    parser.set_defaults(func=cmd_gen)


def _add_validate(sub: Any) -> None:
    parser = sub.add_parser("validate", help="离线校验数据集")
    parser.add_argument("--input", nargs="+", required=True, help="待校验 JSONL")
    parser.add_argument("--report", help="报告输出路径（JSON）")
    parser.add_argument(
        "--no-invariants", action="store_true", help="只查 schema，不跑跨字段不变量"
    )
    parser.add_argument(
        "--no-input-schema", action="store_true", help="跳过输入契约校验"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="有任何失败即以非零码退出（CI 用）",
    )
    parser.add_argument("--dump-failures", help="把失败样本写到该 JSONL")
    parser.set_defaults(func=cmd_validate)


def _add_split(sub: Any) -> None:
    parser = sub.add_parser("split", help="离线切分数据集")
    _common_config_args(parser)
    parser.add_argument("--input", nargs="+", required=True, help="输入 JSONL（可多个）")
    parser.add_argument("--output-dir", required=True, help="切分输出目录")
    parser.add_argument("--seed", type=int, help="随机种子")
    parser.add_argument("--train-ratio", type=float, help="train 比例")
    parser.add_argument("--val-ratio", type=float, help="val 比例")
    parser.add_argument("--test-ratio", type=float, help="test 比例")
    parser.add_argument(
        "--stratify-by",
        help="分层键，逗号分隔（task,direction,event_kind）",
    )
    parser.add_argument(
        "--no-validate", action="store_true", help="跳过切分前的 schema 校验"
    )
    parser.set_defaults(func=cmd_split)


def _add_sft(sub: Any) -> None:
    parser = sub.add_parser("sft", help="构建聊天 SFT jsonl")
    parser.add_argument("--split-dir", help="切分目录（读取 train/val/test.jsonl）")
    parser.add_argument("--input", nargs="*", help="直接指定记录文件（替代 --split-dir）")
    parser.add_argument("--output-dir", required=True, help="SFT 输出目录")
    parser.add_argument("--model-path", help="tokenizer 来源（本地路径或 HF id）")
    parser.add_argument("--max-seq-length", type=int, default=1024)
    parser.add_argument(
        "--enable-thinking", action="store_true", help="渲染 think 段（默认关闭并掩码）"
    )
    parser.add_argument(
        "--pretty-json",
        action="store_true",
        default=DEFAULT_PRETTY_JSON,
        help="JSON 缩进（默认开启，与训练/推理默认一致）",
    )
    parser.add_argument("--compact-json", action="store_true", help="改为紧凑 JSON")
    parser.add_argument(
        "--no-tokenize",
        action="store_true",
        help="只用 messages 形态输出，不加载 tokenizer（离线可用）",
    )
    parser.add_argument(
        "--preview-masking", action="store_true", help="打印掩码预览（需要 tokenizer）"
    )
    parser.add_argument("--limit", type=int, help="只处理前 N 条（调试用）")
    parser.set_defaults(func=cmd_sft)


def _add_eval(sub: Any) -> None:
    parser = sub.add_parser("eval", help="评测")
    parser.add_argument("--input", nargs="+", required=True, help="评测数据 JSONL")
    parser.add_argument(
        "--backend",
        choices=("transformers", "llama_cpp", "echo"),
        default="transformers",
    )
    parser.add_argument("--model-path", help="模型路径（transformers 后端）")
    parser.add_argument("--adapter-path", help="LoRA adapter 路径")
    parser.add_argument("--gguf-path", help="GGUF 文件路径（llama_cpp 后端）")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--n-threads", type=int)
    parser.add_argument("--n-ctx", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--tolerance", type=float, default=0.15, help="数值误差阈值")
    parser.add_argument("--limit", type=int, help="每任务最多评测条数")
    parser.add_argument("--output-dir", help="报告输出目录")
    parser.add_argument("--markdown", help="Markdown 摘要输出路径")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="启用上线门禁：指标不达标则非零退出",
    )
    parser.add_argument(
        "--echo-from",
        help="echo 后端：用 JSONL 里的 output 当作模型回复（离线回归测试用）",
    )
    parser.add_argument(
        "--echo-fence",
        action="store_true",
        help="echo 后端：把回复包进 ```json 代码块（测试 JSON 抽取鲁棒性）",
    )
    parser.set_defaults(func=cmd_eval)


def _add_train(sub: Any) -> None:
    parser = sub.add_parser("train", help="LoRA / QLoRA 训练")
    _common_config_args(parser)
    parser.add_argument("--train-file")
    parser.add_argument("--eval-file")
    parser.add_argument("--output-dir")
    parser.add_argument("--model-path", help="基座模型（默认 Qwen/Qwen3.5-2B）")
    parser.add_argument("--max-seq-length", type=int)
    parser.add_argument("--learning-rate", type=float)
    parser.add_argument("--num-epochs", type=float)
    parser.add_argument("--batch-size", type=int, help="per_device_train_batch_size")
    parser.add_argument("--grad-accum", type=int, help="gradient_accumulation_steps")
    parser.add_argument("--lora-r", type=int)
    parser.add_argument("--lora-alpha", type=int)
    parser.add_argument("--include-mlp", action="store_true", help="LoRA 也挂在 MLP 上")
    parser.add_argument("--qlora", action="store_true", help="使用 4bit QLoRA fallback")
    parser.add_argument("--smoke", action="store_true", help="冒烟测试（4 步）")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--resume", help="从 checkpoint 续训")
    parser.add_argument("--report-to", help="如 tensorboard")
    parser.add_argument(
        "--plan-only", action="store_true", help="只打印配置与冻结策略，不加载模型"
    )
    parser.set_defaults(func=cmd_train)


def _add_merge(sub: Any) -> None:
    parser = sub.add_parser("merge", help="合并 LoRA adapter 到基座")
    parser.add_argument("--adapter-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--base-model", help="基座模型（默认读 adapter_config.json）")
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument(
        "--method", choices=("auto", "peft", "manual"), default="auto"
    )
    parser.set_defaults(func=cmd_merge)


def _add_gguf(sub: Any) -> None:
    parser = sub.add_parser("gguf", help="导出 llama.cpp GGUF")
    parser.add_argument("--model-dir", required=True, help="合并后的 HF 权重目录")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--llama-cpp-dir", help="llama.cpp 仓库根目录")
    parser.add_argument(
        "--convert-script", help="直接指定 convert_hf_to_gguf.py 路径（替代 --llama-cpp-dir）"
    )
    parser.add_argument(
        "--quantize-binary", help="直接指定 llama-quantize 可执行文件路径"
    )
    parser.add_argument("--name", default="qboss-2b", help="GGUF 文件名前缀")
    parser.add_argument("--outtype", default="f16")
    parser.add_argument(
        "--quants",
        default="Q4_K_M",
        help="量化类型，逗号分隔（Q8_0,Q6_K,Q5_K_M,Q4_K_M,Q3_K_M,Q2_K）",
    )
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--drop-f16", action="store_true", help="量化后删除 f16 中间文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    parser.add_argument("--matrix", action="store_true", help="只打印量化矩阵说明")
    parser.add_argument(
        "--params-billion", type=float, default=2.0, help="用于内存估算的参数量（B）"
    )
    parser.set_defaults(func=cmd_gguf)


def _add_bench(sub: Any) -> None:
    parser = sub.add_parser("bench", help="CPU 推理基准")
    parser.add_argument("--input", nargs="+", required=True, help="基准输入 JSONL")
    parser.add_argument(
        "--backend", choices=("llama_cpp", "transformers", "echo"), default="llama_cpp"
    )
    parser.add_argument("--gguf-path")
    parser.add_argument("--model-path")
    parser.add_argument("--adapter-path")
    parser.add_argument("--n-ctx", type=int, default=2048)
    parser.add_argument(
        "--threads",
        default="",
        help="线程数，逗号分隔以扫描（如 2,4,6）；留空则用后端默认",
    )
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--samples-per-task", type=int, default=5)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--target-latency", type=float, default=8.0)
    parser.add_argument("--output-dir")
    parser.add_argument("--markdown")
    parser.add_argument("--label", default="")
    parser.set_defaults(func=cmd_bench)


def _add_plan(sub: Any) -> None:
    parser = sub.add_parser("plan", help="打印完整流水线计划（不执行）")
    parser.add_argument("--task", default="event_eval", choices=list(TASK_NAMES))
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--config", help="YAML 配置")
    parser.set_defaults(func=cmd_plan)


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------

def cmd_gen(args: argparse.Namespace) -> int:
    config = _load_config(args)
    gen: GenerationConfig = config.generation
    if args.task:
        gen.task = args.task
    if args.target:
        gen.target_samples = args.target
    if args.batch_size:
        gen.batch_size = args.batch_size
    if args.concurrency:
        gen.concurrency = args.concurrency
    if args.output_dir:
        gen.output_dir = args.output_dir
    if args.model:
        gen.client.model = args.model
    if args.base_url:
        gen.client.base_url = args.base_url
    if args.max_usd is not None:
        gen.budget.max_usd = args.max_usd
    if args.max_requests is not None:
        gen.budget.max_requests = args.max_requests
    if args.seed is not None:
        gen.seed = args.seed
    if args.self_verify:
        gen.self_verify = True

    if args.dry_run:
        print(json.dumps(dry_run_plan(gen), ensure_ascii=False, indent=2))
        return 0

    def _progress(stats: Any, cursor: int) -> None:
        print(
            f"  ... 已接受 {stats.accepted} 条（游标 {cursor}，请求 {stats.requested}）",
            flush=True,
        )

    try:
        records, stats = asyncio.run(run_generation(gen, progress=_progress))
    except MissingCredentialError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 2
    except BudgetExceeded as exc:
        print(f"\n预算中止：{exc}", file=sys.stderr)
        return 3

    summary = generation_summary(gen, stats)
    summary_path = Path(gen.output_dir) / "generation_summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary["stats"], ensure_ascii=False, indent=2))
    print(f"\n输出：{summary['output_file']}")
    print(f"摘要：{summary_path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    records: list[dict[str, Any]] = []
    for path in args.input:
        loaded = read_jsonl(path)
        LOGGER.info("读取 %s：%d 条", path, len(loaded))
        records.extend(loaded)

    report = validate_records(
        records,
        check_invariants=not args.no_invariants,
        require_input_schema=not args.no_input_schema,
    )
    payload = report.to_dict()
    payload["schema_fingerprint"] = bundle_fingerprint()

    print(f"总样本：{report.total}")
    print(f"通过：{report.passed}    失败：{report.failed}    通过率：{report.pass_rate:.2%}")
    for task, counts in sorted(report.by_task.items()):
        print(
            f"  {task}: {counts['passed']}/{counts['total']} "
            f"({counts['passed'] / max(1, counts['total']):.2%})"
        )
    if report.violation_counts:
        print("\n违规分布：")
        for code, count in sorted(report.violation_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {code}: {count}")

    if args.report:
        write_json(args.report, payload)
        print(f"\n报告：{args.report}")

    if args.dump_failures and report.failed_reports:
        failures = [
            records[item.index]
            for item in report.failed_reports
            if 0 <= item.index < len(records)
        ]
        from .data.io import write_jsonl

        write_jsonl(args.dump_failures, failures)
        print(f"失败样本：{args.dump_failures}")

    if args.strict and report.failed:
        return 1
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    config = _load_config(args)
    split_config: SplitConfig = config.split
    if args.seed is not None:
        split_config.seed = args.seed
    if args.train_ratio is not None:
        split_config.train_ratio = args.train_ratio
    if args.val_ratio is not None:
        split_config.val_ratio = args.val_ratio
    if args.test_ratio is not None:
        split_config.test_ratio = args.test_ratio
    if args.stratify_by:
        split_config.stratify_by = tuple(
            item.strip() for item in args.stratify_by.split(",") if item.strip()
        )

    splits, report, paths = load_and_split(
        args.input,
        split_config,
        args.output_dir,
        validate=not args.no_validate,
    )

    print("切分结果：")
    print(describe_splits(splits))
    print(f"\n去重移除：{report.duplicates_removed}")
    print(f"校验失败丢弃：{report.invalid_records}")
    print(f"防泄漏移动：{report.leakage_moved}")

    problems = assert_task_coverage(splits)
    for problem in problems:
        print(f"警告：{problem}")

    print("\n输出：")
    for name, path in paths.items():
        print(f"  {name}: {path}")
    return 0


def cmd_sft(args: argparse.Namespace) -> int:
    records: list[dict[str, Any]] = []
    sources: dict[str, list[dict[str, Any]]] = {}

    if args.split_dir:
        for name in SPLITS:
            path = Path(args.split_dir) / f"{name}.jsonl"
            if path.exists():
                sources[name] = read_jsonl(path)
                records.extend(sources[name])
    if args.input:
        for path in args.input:
            loaded = read_jsonl(path)
            sources[Path(path).stem] = loaded
            records.extend(loaded)

    if not records:
        print("错误：没有找到任何输入记录（用 --split-dir 或 --input）", file=sys.stderr)
        return 2

    if args.limit:
        records = records[: args.limit]

    pretty = not args.compact_json
    output_dir = ensure_dir(args.output_dir)

    # 1) 无 tokenizer 路径：messages 形态
    for name, items in sources.items():
        target = output_dir / f"{name}.jsonl"
        count = write_sft_jsonl(items, str(target), pretty=pretty, mode="messages")
        print(f"写出 {target}：{count} 条")

    # 2) 有 tokenizer 路径：真 tokenize + 掩码统计
    if not args.no_tokenize:
        if not args.model_path:
            print(
                "\n提示：未提供 --model-path，跳过 tokenize 与掩码统计。"
                "（如需验证 completion-only 掩码，请指定 tokenizer 路径）",
                file=sys.stderr,
            )
        else:
            build_config = SFTBuildConfig(
                model_name_or_path=args.model_path,
                max_seq_length=args.max_seq_length,
                enable_thinking=args.enable_thinking,
                pretty_json=pretty,
            )
            tokenizer = load_tokenizer(build_config)
            samples, stats = build_sft_dataset(records, tokenizer, build_config)
            stats_payload = stats.to_dict()
            write_json(output_dir / "sft_stats.json", stats_payload)
            print("\nSFT token 统计：")
            print(json.dumps(stats_payload, ensure_ascii=False, indent=2))
            if stats.total and not samples:
                print(
                    "警告：所有样本都被丢弃，请检查 --max-seq-length 是否过小",
                    file=sys.stderr,
                )
            if args.preview_masking and records:
                print("\n掩码预览：")
                print(masking_preview(records[0], tokenizer, build_config))
    else:
        # 离线模式也输出一份可用于 train 的 token 无关统计
        write_json(
            output_dir / "sft_stats.json",
            {"mode": "no_tokenize", "records": len(records), "by_task": _count_by_task(records)},
        )

    return 0


def _count_by_task(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("task", "?"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def cmd_eval(args: argparse.Namespace) -> int:
    records = limit_records_by_task(load_eval_records(args.input), args.limit)
    if not records:
        print("错误：评测数据为空", file=sys.stderr)
        return 2
    LOGGER.info("评测样本：%d 条", len(records))

    config = EvalConfig(
        tolerance=args.tolerance,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
    )

    if args.backend == "echo" or args.echo_from:
        if args.echo_from:
            replay_source = read_jsonl(args.echo_from)
        else:
            # 默认用 gold 回放：等价于"模型完全正确"的上界基线
            replay_source = records
        backend = build_gold_replay_backend(
            replay_source, wrap_in_fence=args.echo_fence
        )
        report = evaluate_with_backend(records, backend, config)
    else:
        backend = load_backend(
            args.backend,
            model_path=args.model_path,
            adapter_path=args.adapter_path,
            gguf_path=args.gguf_path,
            device=args.device,
            dtype=args.dtype,
            n_threads=args.n_threads,
            n_ctx=args.n_ctx,
        )
        report = evaluate_with_backend(records, backend, config)

    summary = render_eval_summary(report)
    print(summary)

    if args.output_dir:
        paths = write_eval_report(report, args.output_dir, include_samples=True)
        for name, path in paths.items():
            print(f"{name}: {path}")
    if args.markdown:
        Path(args.markdown).write_text(summary, encoding="utf-8")
        print(f"markdown: {args.markdown}")

    if args.gate:
        failures = threshold_gate(report)
        if failures:
            print("\n门禁未通过：", file=sys.stderr)
            for item in failures:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print("\n门禁通过。")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    config = _load_training_config(args)

    if args.qlora:
        config = apply_qlora_fallback(config)

    if args.plan_only:
        print("训练计划：")
        print(f"  {describe_training_config(config)}")
        print(f"  train: {config.train_file}")
        print(f"  eval:  {config.eval_file or '(无)'}")
        print(f"  out:   {config.output_dir}")
        print("  冻结策略：")
        print(f"    视觉:      {'冻结' if config.freeze.freeze_vision else '不冻结'}")
        print(f"    embedding: {'冻结' if config.freeze.freeze_embeddings else '不冻结'}")
        print(f"    lm_head:   {'冻结' if config.freeze.freeze_lm_head else '不冻结'}")
        print(f"    norm:      {'冻结' if config.freeze.freeze_norms else '不冻结'}")
        print(f"  LoRA 目标: {', '.join(config.lora.resolved_target_modules())}")
        return 0

    from .training.train import run_training

    try:
        report = run_training(config, smoke=args.smoke)
    except (TrainingError, FileNotFoundError) as exc:
        print(f"训练失败：{redact(exc)}", file=sys.stderr)
        return 1

    print(json.dumps(report.get("metrics", {}), ensure_ascii=False, indent=2))
    print(f"\nadapter: {config.output_dir}")
    return 0


def _load_training_config(args: argparse.Namespace) -> TrainingConfig:
    """从 YAML 构造训练配置（train 命令用，字段比 ProjectConfig 多）。"""
    config = TrainingConfig()
    document: dict[str, Any] = {}
    if getattr(args, "config", None):
        import yaml

        with Path(args.config).open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}

    # 只取训练相关的节，避免与生成器配置混用
    section = document.get("training", document)
    if "model" in section:
        model_section = section["model"]
        config.model = ModelConfig(
            **{
                key: value
                for key, value in model_section.items()
                if key in ModelConfig.__dataclass_fields__
            }
        )
    if "lora" in section:
        lora_section = section["lora"]
        coerced: dict[str, Any] = {}
        for key, value in lora_section.items():
            if key not in LoraConfigSpec.__dataclass_fields__:
                continue
            if "modules" in key and isinstance(value, list):
                value = tuple(value)
            coerced[key] = value
        config.lora = LoraConfigSpec(**coerced)
    if "freeze" in section:
        config.freeze = FreezeConfig(
            **{
                key: value
                for key, value in section["freeze"].items()
                if key in FreezeConfig.__dataclass_fields__
            }
        )
    if "quantization" in section:
        config.quantization = QuantizationConfig(
            **{
                key: value
                for key, value in section["quantization"].items()
                if key in QuantizationConfig.__dataclass_fields__
            }
        )

    for key, value in section.items():
        if key in ("model", "lora", "freeze", "quantization"):
            continue
        if key not in TrainingConfig.__dataclass_fields__:
            continue
        if key == "report_to" and isinstance(value, list):
            value = tuple(value)
        setattr(config, key, value)

    # CLI 覆盖
    cli_map = {
        "train_file": args.train_file,
        "eval_file": args.eval_file,
        "output_dir": args.output_dir,
        "max_seq_length": args.max_seq_length,
        "learning_rate": args.learning_rate,
        "num_train_epochs": args.num_epochs,
        "per_device_train_batch_size": args.batch_size,
        "gradient_accumulation_steps": args.grad_accum,
        "max_steps": args.max_steps,
        "resume_from_checkpoint": args.resume,
    }
    for key, value in cli_map.items():
        if value is not None:
            setattr(config, key, value)
    if args.model_path:
        config.model.name_or_path = args.model_path
    if args.lora_r:
        config.lora.r = args.lora_r
    if args.lora_alpha:
        config.lora.lora_alpha = args.lora_alpha
    if args.include_mlp:
        config.lora.include_mlp = True
    if args.report_to:
        config.report_to = tuple(
            item.strip() for item in args.report_to.split(",") if item.strip()
        )
    return config


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        report = merge_adapter(
            args.adapter_path,
            args.output_dir,
            base_model=args.base_model,
            dtype=args.dtype,
            method=args.method,
        )
    except (TrainingError, ImportError) as exc:
        print(f"合并失败：{redact(exc)}", file=sys.stderr)
        return 1

    print(f"合并方式：{report.method}")
    print(f"输出：{report.output_dir}")
    print(f"文件：{', '.join(report.files[:8])}")
    for warning in report.warnings:
        print(f"警告：{warning}")

    problems = verify_merged_model(report.output_dir)
    if problems:
        print("\n产物检查发现问题：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\n产物检查通过，可用于 GGUF 转换。")
    return 0


def cmd_gguf(args: argparse.Namespace) -> int:
    if args.matrix:
        print(describe_matrix())
        print("\n内存估算（2B，2K 上下文）：")
        for quant in ("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q3_K_M", "Q2_K"):
            print(f"  {quant}: 约 {estimate_ram_mb(args.params_billion, quant):.0f} MB")
        return 0

    quants = tuple(item.strip() for item in args.quants.split(",") if item.strip())
    config = ExportConfig(
        model_dir=args.model_dir,
        output_dir=args.output_dir,
        llama_cpp_dir=args.llama_cpp_dir,
        convert_script=args.convert_script,
        quantize_binary=args.quantize_binary,
        name=args.name,
        outtype=args.outtype,
        quant_types=quants,
        threads=args.threads,
        keep_f16=not args.drop_f16,
        dry_run=args.dry_run,
    )

    try:
        report = run_export(config)
    except TrainingError as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("将要执行的命令（dry-run）：")
        for command in report.commands:
            print("  " + " ".join(str(item) for item in command))
        for note in report.notes:
            print(f"说明：{note}")
        return 0

    print(f"f16: {report.f16_path}")
    for quant, path in report.quantized.items():
        print(f"{quant}: {path}")
    print()
    print(describe_matrix())
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    records = load_eval_records(args.input)
    if not records:
        print("错误：基准数据为空", file=sys.stderr)
        return 2

    threads_list: list[int] = []
    if args.threads:
        try:
            threads_list = [int(item.strip()) for item in args.threads.split(",") if item.strip()]
        except ValueError:
            print("错误：--threads 需要整数或逗号分隔的整数列表", file=sys.stderr)
            return 2

    config = BenchmarkConfig(
        warmup_runs=args.warmup,
        repeat=args.repeat,
        max_new_tokens=args.max_new_tokens,
        samples_per_task=args.samples_per_task,
        target_latency_s=args.target_latency,
        thread_sweep=tuple(threads_list),
    )

    if threads_list and args.backend == "llama_cpp" and len(threads_list) > 1:
        results = []
        from .benchmark import BenchmarkReport, benchmark_thread_sweep

        def _build(threads: int) -> Any:
            return load_backend(
                "llama_cpp",
                gguf_path=args.gguf_path or args.model_path,
                n_threads=threads,
                n_ctx=args.n_ctx,
            )

        results = benchmark_thread_sweep(_build, records, config)
        report = BenchmarkReport(
            results=results,
            config={"thread_sweep": list(threads_list), "repeat": args.repeat},
        )
    elif args.backend == "echo":
        backend = build_gold_replay_backend(records)
        report = run_benchmark(
            backend, records, config, label=args.label or "echo-gold-replay"
        )
    else:
        backend = load_backend(
            args.backend,
            model_path=args.model_path,
            adapter_path=args.adapter_path,
            gguf_path=args.gguf_path,
            n_threads=threads_list[0] if threads_list else None,
            n_ctx=args.n_ctx,
        )
        report = run_benchmark(backend, records, config, label=args.label or args.backend)

    summary = render_benchmark_summary(report, config)
    print(summary)

    if args.output_dir:
        from .utils.io import write_json

        directory = ensure_dir(args.output_dir)
        write_json(
            Path(directory) / "benchmark_report.json",
            {"config": report.config, "results": [item.to_dict() for item in report.results]},
        )
        print(f"报告：{Path(directory) / 'benchmark_report.json'}")
    if args.markdown:
        Path(args.markdown).write_text(summary, encoding="utf-8")
        print(f"markdown: {args.markdown}")

    failing = [
        item for item in report.results if item.meets_targets(config)
    ]
    if failing:
        print("\n有配置未达标（见上），弱 VPS 部署前请调整量化等级或线程数。", file=sys.stderr)
        return 1
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    config = load_project_config(args.config)
    gen: GenerationConfig = config.generation
    gen.task = args.task
    gen.target_samples = args.target
    plan = {
        "schema_fingerprint": bundle_fingerprint(),
        "tasks": {
            task: {
                "required_fields": list(schema_bundle()[task]["output_schema"]["required"]),
                "enums": {
                    key: spec["enum"]
                    for key, spec in schema_bundle()[task]["output_schema"]["properties"].items()
                    if "enum" in spec
                },
            }
            for task in TASK_NAMES
        },
        "pipeline": [
            f"1. qboss gen --task {gen.task} --target {gen.target_samples}",
            f"2. qboss validate --input {gen.output_dir}/{gen.task}.jsonl --strict",
            f"3. qboss split --input {gen.output_dir}/*.jsonl --output-dir data/splits",
            "4. qboss sft --split-dir data/splits --output-dir data/sft --model-path <tokenizer>",
            "5. qboss train --config configs/lora_bf16.yaml --smoke",
            "6. qboss train --config configs/lora_bf16.yaml",
            "7. qboss eval --input data/splits/test.jsonl --backend transformers "
            "--adapter-path <adapter>",
            "8. qboss merge --adapter-path <adapter> --output-dir outputs/merged",
            "9. qboss gguf --model-dir outputs/merged --output-dir outputs/gguf "
            "--quants Q4_K_M --llama-cpp-dir <llama.cpp>",
            "10. qboss bench --input data/splits/test.jsonl --backend llama_cpp "
            "--gguf-path outputs/gguf/qboss-2b-Q4_K_M.gguf --threads 2,4,6",
            "11. qboss eval --input data/splits/test.jsonl --backend llama_cpp "
            "--gguf-path outputs/gguf/qboss-2b-Q4_K_M.gguf --gate",
        ],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, json_output=args.log_json)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。断点已保留，可重新运行同一命令续跑。", file=sys.stderr)
        return 130
    except Exception as exc:  # 顶层兜底：保证密钥不出现在 traceback 里
        LOGGER.error("执行失败：%s", redact(exc))
        if LOGGER.isEnabledFor(logging.DEBUG):
            raise
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
