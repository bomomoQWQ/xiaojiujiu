"""CPU 推理基准：为弱 VPS 选型提供实测数据。

架构文档 §74 的核心判断是"可用性 > 推理速度"，但"可用"需要量化：
一次事件评价要在多久内返回？Q4 和 Q3 差多少？线程数该怎么限制
才不会把整台 VPS 打死？

本模块测四件事：
  1. **延迟**：端到端每次调用耗时（p50 / p90 / p99），区分冷启动与稳态；
  2. **吞吐**：completion tokens/s；
  3. **质量**：每次调用的 schema 合法率（低量化可能把 JSON 输出打坏，
     这是比速度更致命的退化）；
  4. **内存/线程**：按线程数扫描，给出"建议线程数"。

无模型时可用 :class:`~qboss_training.inference.EchoBackend` 跑通全流程
（用于回归测试与报告格式验证）。
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import CONTRACTS
from .errors import TrainingError
from .inference import (
    GenerationRequest,
    InferenceBackend,
    build_inference_messages,
)
from .utils.io import write_json
from .utils.secrets import utc_now_iso
from .eval.evaluator import EvalConfig, evaluate_text

LOGGER = logging.getLogger("qboss_training.benchmark")


@dataclass
class BenchmarkConfig:
    """基准配置。"""

    warmup_runs: int = 2
    repeat: int = 5
    max_new_tokens: int = 256
    temperature: float = 0.0
    top_p: float = 1.0
    #: 每个 task 采样多少条不同的输入
    samples_per_task: int = 5
    #: 线程扫描（llama.cpp 后端）
    thread_sweep: tuple[int, ...] = ()
    #: 目标：单次调用延迟上限（秒），超过则判定不达标
    target_latency_s: float = 8.0
    #: 目标：schema 合法率下限
    target_schema_pass_rate: float = 0.98


@dataclass
class LatencyStats:
    """延迟统计（秒）。"""

    runs: int = 0
    mean: float = 0.0
    median: float = 0.0
    p90: float = 0.0
    p99: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0
    stdev: float = 0.0

    @classmethod
    def from_samples(cls, samples: Sequence[float]) -> "LatencyStats":
        values = sorted(float(item) for item in samples)
        if not values:
            return cls()
        return cls(
            runs=len(values),
            mean=statistics.fmean(values),
            median=statistics.median(values),
            p90=_percentile(values, 0.90),
            p99=_percentile(values, 0.99),
            minimum=values[0],
            maximum=values[-1],
            stdev=statistics.pstdev(values) if len(values) > 1 else 0.0,
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "runs": float(self.runs),
            "mean_s": round(self.mean, 4),
            "median_s": round(self.median, 4),
            "p90_s": round(self.p90, 4),
            "p99_s": round(self.p99, 4),
            "min_s": round(self.minimum, 4),
            "max_s": round(self.maximum, 4),
            "stdev_s": round(self.stdev, 4),
        }


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[index]


@dataclass
class BenchmarkResult:
    """单个配置下的基准结果。"""

    label: str = ""
    backend: str = ""
    model: str = ""
    threads: int | None = None
    calls: int = 0
    latency: LatencyStats = field(default_factory=LatencyStats)
    throughput_tokens_per_s: float = 0.0
    prompt_tokens_mean: float = 0.0
    completion_tokens_mean: float = 0.0
    cold_start_s: float = 0.0
    schema_pass_rate: float = 0.0
    text_constraint_rate: float = 0.0
    invariant_pass_rate: float = 0.0
    extraction_rate: float = 0.0
    failures: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)

    def meets_targets(self, config: BenchmarkConfig) -> list[str]:
        """返回未达标项。"""
        problems: list[str] = []
        if self.latency.p90 > config.target_latency_s:
            problems.append(
                f"p90 延迟 {self.latency.p90:.2f}s 超过目标 {config.target_latency_s:.2f}s"
            )
        if self.schema_pass_rate < config.target_schema_pass_rate:
            problems.append(
                f"schema 合法率 {self.schema_pass_rate:.2%} 低于目标 "
                f"{config.target_schema_pass_rate:.2%}"
            )
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "backend": self.backend,
            "model": self.model,
            "threads": self.threads,
            "calls": self.calls,
            "latency": self.latency.to_dict(),
            "throughput_tokens_per_s": round(self.throughput_tokens_per_s, 2),
            "prompt_tokens_mean": round(self.prompt_tokens_mean, 1),
            "completion_tokens_mean": round(self.completion_tokens_mean, 1),
            "cold_start_s": round(self.cold_start_s, 4),
            "schema_pass_rate": round(self.schema_pass_rate, 4),
            "extraction_rate": round(self.extraction_rate, 4),
            "invariant_pass_rate": round(self.invariant_pass_rate, 4),
            "text_constraint_rate": round(self.text_constraint_rate, 4),
            "failures": self.failures[:20],
            "created_at": self.created_at,
        }


@dataclass
class BenchmarkReport:
    """基准总报告。"""

    results: list[BenchmarkResult] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def best_by_latency(self) -> BenchmarkResult | None:
        if not self.results:
            return None
        return min(self.results, key=lambda item: item.latency.p90)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": dict(self.config),
            "results": [item.to_dict() for item in self.results],
            "created_at": self.created_at,
        }


def _select_inputs(
    records: Sequence[Mapping[str, Any]], samples_per_task: int
) -> list[Mapping[str, Any]]:
    """为每个 task 选若干条输入。"""
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        task = str(record.get("task", ""))
        if task in CONTRACTS:
            buckets.setdefault(task, []).append(record)

    if not buckets:
        raise TrainingError(
            "基准数据为空：请提供包含 input/task 的 JSONL（可用 data/splits/test.jsonl）。"
        )

    selected: list[Mapping[str, Any]] = []
    for task in sorted(buckets):
        selected.extend(buckets[task][:samples_per_task])
    return selected


def benchmark_backend(
    backend: InferenceBackend,
    records: Sequence[Mapping[str, Any]],
    config: BenchmarkConfig,
    *,
    label: str = "",
    threads: int | None = None,
    eval_config: EvalConfig | None = None,
) -> BenchmarkResult:
    """对一个后端跑基准。"""
    eval_config = eval_config or EvalConfig()
    inputs = _select_inputs(records, config.samples_per_task)
    result = BenchmarkResult(
        label=label or getattr(backend, "name", "backend"),
        backend=getattr(backend, "name", ""),
        threads=threads,
    )
    describe = backend.describe() if hasattr(backend, "describe") else {}
    result.model = str(describe.get("model_path") or describe.get("gguf_path") or "")

    latencies: list[float] = []
    prompt_tokens: list[int] = []
    completion_tokens: list[int] = []

    # 冷启动单独记录；暖机轮不计入延迟统计（否则首次加载会污染稳态延迟）
    first = True
    schema_ok = 0
    invariant_ok = 0
    text_ok = 0
    extracted = 0
    total = 0

    def _call(record: Mapping[str, Any], *, measure: bool) -> None:
        nonlocal first, total, schema_ok, invariant_ok, text_ok, extracted
        task = str(record.get("task"))
        messages = build_inference_messages(task, record.get("input") or {})
        request = GenerationRequest(
            messages=messages,
            max_new_tokens=config.max_new_tokens,
            temperature=config.temperature,
            top_p=config.top_p,
        )
        try:
            response = backend.generate(request)
        except Exception as exc:
            result.failures.append(f"{task}: 生成失败 {type(exc).__name__}: {exc}")
            return

        if first:
            result.cold_start_s = response.latency_s
            first = False

        prompt_tokens.append(response.prompt_tokens)
        completion_tokens.append(response.completion_tokens)

        evaluated = evaluate_text(
            record, response.text, eval_config, latency_s=response.latency_s
        )
        if measure:
            latencies.append(response.latency_s)
            total += 1
            if evaluated.parsed is not None:
                extracted += 1
            if evaluated.schema_ok:
                schema_ok += 1
            if evaluated.invariant_ok:
                invariant_ok += 1
            if evaluated.text_ok:
                text_ok += 1

    for _ in range(max(0, config.warmup_runs)):
        for record in inputs:
            _call(record, measure=False)

    for _ in range(max(1, config.repeat)):
        for record in inputs:
            _call(record, measure=True)

    result.calls = total
    result.latency = LatencyStats.from_samples(latencies)
    if total:
        result.extraction_rate = extracted / total
        result.schema_pass_rate = schema_ok / total
        result.invariant_pass_rate = invariant_ok / total
        result.text_constraint_rate = text_ok / total
    if prompt_tokens:
        result.prompt_tokens_mean = statistics.fmean(prompt_tokens)
    if completion_tokens:
        result.completion_tokens_mean = statistics.fmean(completion_tokens)
    if result.latency.mean > 0 and completion_tokens:
        result.throughput_tokens_per_s = result.completion_tokens_mean / result.latency.mean

    return result


def benchmark_thread_sweep(
    build_backend: Any,
    records: Sequence[Mapping[str, Any]],
    config: BenchmarkConfig,
) -> list[BenchmarkResult]:
    """按线程数扫描。``build_backend(threads)`` 负责构造对应线程数的后端。"""
    results: list[BenchmarkResult] = []
    for threads in config.thread_sweep:
        LOGGER.info("扫描线程数 = %d", threads)
        backend = build_backend(threads)
        result = benchmark_backend(
            backend, records, config, label=f"threads={threads}", threads=threads
        )
        results.append(result)
    return results


def run_benchmark(
    backend: InferenceBackend,
    records: Sequence[Mapping[str, Any]],
    config: BenchmarkConfig,
    *,
    output_dir: str | Path | None = None,
    label: str = "",
) -> BenchmarkReport:
    """执行基准并按需落盘。"""
    overall_started = time.perf_counter()
    result = benchmark_backend(backend, records, config, label=label)
    report = BenchmarkReport(
        results=[result],
        config={
            "warmup_runs": config.warmup_runs,
            "repeat": config.repeat,
            "max_new_tokens": config.max_new_tokens,
            "samples_per_task": config.samples_per_task,
            "target_latency_s": config.target_latency_s,
            "target_schema_pass_rate": config.target_schema_pass_rate,
            "wall_time_s": round(time.perf_counter() - overall_started, 2),
        },
    )
    if output_dir:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "benchmark_report.json", report.to_dict())
    return report


def render_benchmark_summary(report: BenchmarkReport, config: BenchmarkConfig | None = None) -> str:
    """渲染 Markdown 摘要。"""
    config = config or BenchmarkConfig()
    lines = [
        "# CPU 推理基准",
        "",
        "| 配置 | 线程 | 平均延迟 | p90 | p99 | tok/s | schema | 不变量 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.results:
        lines.append(
            f"| {item.label} | {item.threads if item.threads is not None else '-'} | "
            f"{item.latency.mean:.3f}s | {item.latency.p90:.3f}s | {item.latency.p99:.3f}s | "
            f"{item.throughput_tokens_per_s:.1f} | {item.schema_pass_rate:.1%} | "
            f"{item.invariant_pass_rate:.1%} |"
        )
    lines.append("")

    for item in report.results:
        problems = item.meets_targets(config)
        status = "达标" if not problems else "未达标"
        lines.append(f"## {item.label}：{status}")
        lines.append("")
        lines.append(f"- 冷启动：{item.cold_start_s:.3f}s")
        lines.append(f"- 调用次数：{item.calls}")
        lines.append(f"- 平均 prompt token：{item.prompt_tokens_mean:.0f}")
        lines.append(f"- 平均 completion token：{item.completion_tokens_mean:.0f}")
        if problems:
            lines.extend(f"- ⚠️ {problem}" for problem in problems)
        if item.failures:
            lines.append(f"- 失败 {len(item.failures)} 次，示例：{item.failures[0][:160]}")
        lines.append("")

    lines.extend(
        [
            "## 判读建议",
            "",
            f"- 延迟目标 p90 <= {config.target_latency_s}s；弱 VPS 上建议限制线程数，",
            "  并把 2B worker 降优先级（nice / cgroup），避免影响主 Bot。",
            "- **低量化最先坏掉的通常是结构化输出**：Q3/Q2 时务必重跑本基准，",
            "  重点看 schema 合法率而不是速度。",
            "- 模型常驻内存，不要每轮重新加载（架构文档 §74.4）。",
        ]
    )
    return "\n".join(lines)


def estimate_memory_from_backend(backend: InferenceBackend) -> float | None:
    """尽量估算后端内存占用（MB）。取不到返回 None。"""
    llm = getattr(backend, "llm", None)
    if llm is None:
        return None
    for attribute in ("n_ctx", "n_vocab", "n_embd"):
        if not hasattr(llm, attribute):
            continue
    try:  # llama-cpp-python 不暴露统一 RSS 接口，这里用 /proc 或 psutil 兜底
        import psutil  # type: ignore

        process = psutil.Process()
        return process.memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def benchmark_report_to_jsonl(report: BenchmarkReport, path: str | Path) -> None:
    """把多配置结果打平成 JSONL，便于跨机器汇总对比。"""
    import json

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for item in report.results:
            handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
