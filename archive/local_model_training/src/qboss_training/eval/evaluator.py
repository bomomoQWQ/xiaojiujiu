"""评测：schema 合法率 · 字段准确率/误差 · 不变量 · 文本约束。

评测指标与"这个模型能不能上线"直接对应：

====================  ====================================================
指标                  含义
====================  ====================================================
schema_pass_rate     JSON 能否抽出来、字段/类型/取值域是否合法
extraction_rate      原始回复能抽出 JSON 的比例（格式遵循能力）
enum_accuracy        类别字段（direction/relation_signal/responsibility）准确率
mae / mae_within     数值字段平均绝对误差，以及误差 <= 阈值的比例
invariant_pass_rate  架构文档硬约束（§8.1 / §11.3）通过率
text_constraint_rate 文本约束（长度、无数字、无台词、无括号动作）通过率
====================  ====================================================

所有指标都是**离线**可算的：只要有一份 ``(记录, 模型回复)`` 就能算，
不需要 GPU，也不需要网络。
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..contracts import CONTRACTS, get_contract
from ..inference import (
    GenerationRequest,
    InferenceBackend,
    build_inference_messages,
    extract_output,
)
from ..utils.io import read_jsonl, write_json
from ..utils.jsonx import extract_json
from ..utils.secrets import utc_now_iso
from ..validators import validate_output
from ..validators.invariants import (
    ERROR,
    Violation,
    has_dialogue,
    numeric_assertions,
)

LOGGER = logging.getLogger("qboss_training.eval")

#: 数值字段误差阈值：|pred - gold| <= 该值算"命中"
DEFAULT_TOLERANCE = 0.15


@dataclass
class EvalConfig:
    """评测配置。"""

    tolerance: float = DEFAULT_TOLERANCE
    max_new_tokens: int = 384
    temperature: float = 0.0
    top_p: float = 1.0
    #: 每类最多评测多少条（便于快速看趋势）
    limit_per_task: int | None = None
    #: 输出预测明细
    save_predictions: bool = True
    #: 文本约束：字段长度上下限（覆盖 schema 之外的额外检查）
    max_text_chars: int = 80
    min_text_chars: int = 4


@dataclass
class FieldStats:
    """单字段统计。

    类别字段看 ``accuracy``（精确匹配率）；
    数值字段看 ``mae`` 与 ``within_rate``，而 ``accuracy`` 表示**完全相等**的比例
    （浮点字段上通常很低，仅作参考，不要误读为"准确率"）。
    文本字段的 ``accuracy`` 表示"非空且长度达标"的比例（文本没有唯一正解，
    不做字符串比对）。
    """

    total: int = 0
    correct: int = 0
    value_correct: int = 0
    abs_error_sum: float = 0.0
    within_tolerance: int = 0
    missing: int = 0
    #: 该字段是数值型（用于报告里区分口径）
    numeric: bool = False

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def exact_accuracy(self) -> float:
        """数值字段的完全相等比例。"""
        return self.value_correct / self.total if self.total else 0.0

    @property
    def mae(self) -> float:
        return self.abs_error_sum / self.total if self.total else 0.0

    @property
    def within_rate(self) -> float:
        return self.within_tolerance / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"total": self.total, "missing": self.missing}
        if self.numeric:
            payload.update(
                {
                    "mae": round(self.mae, 4),
                    "within_tolerance_rate": round(self.within_rate, 4),
                    "exact_match_rate": round(self.exact_accuracy, 4),
                }
            )
        else:
            payload["accuracy"] = round(self.accuracy, 4)
        return payload


@dataclass
class SampleResult:
    """单条样本的评测结果。"""

    record_id: str
    task: str
    raw_text: str = ""
    parsed: dict[str, Any] | None = None
    gold: dict[str, Any] = field(default_factory=dict)
    extraction_error: str | None = None
    violations: list[Violation] = field(default_factory=list)
    text_constraint_errors: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    completion_tokens: int = 0

    @property
    def schema_ok(self) -> bool:
        if self.parsed is None:
            return False
        return not any(item.code == "SCHEMA" for item in self.violations)

    @property
    def invariant_ok(self) -> bool:
        return not any(item.code != "SCHEMA" and item.severity == ERROR for item in self.violations)

    @property
    def text_ok(self) -> bool:
        return not self.text_constraint_errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.record_id,
            "task": self.task,
            "parsed": self.parsed,
            "gold": self.gold,
            "extraction_error": self.extraction_error,
            "schema_ok": self.schema_ok,
            "invariant_ok": self.invariant_ok,
            "text_ok": self.text_ok,
            "violations": [item.to_dict() for item in self.violations],
            "text_constraint_errors": self.text_constraint_errors,
            "latency_s": round(self.latency_s, 4),
            "completion_tokens": self.completion_tokens,
        }


@dataclass
class EvalReport:
    """评测汇总。"""

    backend: str = ""
    model: str = ""
    total: int = 0
    extracted: int = 0
    schema_ok: int = 0
    invariant_ok: int = 0
    text_ok: int = 0
    by_task: dict[str, dict[str, Any]] = field(default_factory=dict)
    field_stats: dict[str, dict[str, FieldStats]] = field(default_factory=dict)
    violation_counts: dict[str, int] = field(default_factory=dict)
    text_error_counts: dict[str, int] = field(default_factory=dict)
    samples: list[SampleResult] = field(default_factory=list)
    latency_mean_s: float = 0.0
    created_at: str = field(default_factory=utc_now_iso)

    def rates(self) -> dict[str, float]:
        def _rate(value: int) -> float:
            return round(value / self.total, 4) if self.total else 0.0

        return {
            "extraction_rate": _rate(self.extracted),
            "schema_pass_rate": _rate(self.schema_ok),
            "invariant_pass_rate": _rate(self.invariant_ok),
            "text_constraint_rate": _rate(self.text_ok),
        }

    def to_dict(self, *, include_samples: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "backend": self.backend,
            "model": self.model,
            "total": self.total,
            "rates": self.rates(),
            "counts": {
                "extracted": self.extracted,
                "schema_ok": self.schema_ok,
                "invariant_ok": self.invariant_ok,
                "text_ok": self.text_ok,
            },
            "by_task": self.by_task,
            "field_stats": {
                task: {name: stats.to_dict() for name, stats in fields.items()}
                for task, fields in self.field_stats.items()
            },
            "violation_counts": dict(
                sorted(self.violation_counts.items(), key=lambda kv: -kv[1])
            ),
            "text_error_counts": dict(
                sorted(self.text_error_counts.items(), key=lambda kv: -kv[1])
            ),
            "latency_mean_s": round(self.latency_mean_s, 4),
            "created_at": self.created_at,
        }
        if include_samples:
            payload["samples"] = [item.to_dict() for item in self.samples]
        return payload


# --------------------------------------------------------------------------
# 文本约束
# --------------------------------------------------------------------------

def check_text_constraints(
    task: str, output: Mapping[str, Any], config: EvalConfig
) -> list[str]:
    """schema 之外的文本约束。返回错误信息列表。"""
    contract = get_contract(task)
    problems: list[str] = []

    for name in contract.text_fields:
        value = output.get(name)
        if not isinstance(value, str):
            continue
        if len(value) > config.max_text_chars:
            problems.append(f"{name}: 长度 {len(value)} 超过 {config.max_text_chars}")
        if len(value) < config.min_text_chars and value.strip():
            problems.append(f"{name}: 长度 {len(value)} 低于 {config.min_text_chars}")
        if "\n" in value:
            problems.append(f"{name}: 含换行（应为单句）")

    if task == "emotion_explain":
        joined = " ".join(
            str(output.get(name, "")) for name in contract.text_fields
        )
        # 情绪解释不得出现数字（自己发明情绪强度）
        numbers = numeric_assertions(joined)
        if numbers:
            problems.append(f"出现输入外的数字：{numbers[:3]}")
        for name in contract.text_fields:
            value = output.get(name)
            if isinstance(value, str) and has_dialogue(value):
                problems.append(f"{name}: 含引号/台词/动作描写")
                break

    if task == "event_eval":
        evidence = output.get("evidence")
        if isinstance(evidence, str) and len(evidence) > 60:
            problems.append(f"evidence: 长度 {len(evidence)} 超过 60")

    return problems


# --------------------------------------------------------------------------
# 字段比对
# --------------------------------------------------------------------------

def compare_fields(
    task: str,
    predicted: Mapping[str, Any],
    gold: Mapping[str, Any],
    config: EvalConfig,
    stats: dict[str, FieldStats],
) -> None:
    """把一条预测与标注比对，累计到 ``stats``（原地修改）。"""
    contract = get_contract(task)

    for name in contract.categorical_fields:
        bucket = stats.setdefault(name, FieldStats())
        bucket.total += 1
        gold_value = gold.get(name)
        predicted_value = predicted.get(name)
        if gold_value is None:
            bucket.missing += 1
            continue
        if predicted_value == gold_value:
            bucket.correct += 1

    for name in contract.numeric_fields:
        bucket = stats.setdefault(name, FieldStats(numeric=True))
        bucket.numeric = True
        bucket.total += 1
        gold_value = gold.get(name)
        predicted_value = predicted.get(name)
        if not isinstance(gold_value, (int, float)) or isinstance(gold_value, bool):
            bucket.missing += 1
            continue
        if not isinstance(predicted_value, (int, float)) or isinstance(
            predicted_value, bool
        ):
            bucket.missing += 1
            # 类型错的预测按最大误差计入，避免 MAE 被"缺失即忽略"美化
            bucket.abs_error_sum += 1.0
            continue
        error = abs(float(predicted_value) - float(gold_value))
        bucket.abs_error_sum += error
        if error <= config.tolerance:
            bucket.within_tolerance += 1
        if error <= 1e-9:
            bucket.value_correct += 1

    for name in contract.text_fields:
        bucket = stats.setdefault(name, FieldStats())
        bucket.total += 1
        value = predicted.get(name)
        if not isinstance(value, str) or not value.strip():
            bucket.missing += 1
            continue
        if len(value) >= config.min_text_chars:
            bucket.correct += 1


# --------------------------------------------------------------------------
# 主评测流程
# --------------------------------------------------------------------------

def evaluate_text(
    record: Mapping[str, Any],
    raw_text: str,
    config: EvalConfig,
    *,
    latency_s: float = 0.0,
    completion_tokens: int = 0,
) -> SampleResult:
    """对一条"已有回复文本"做完整评测（无需模型）。"""
    task = str(record.get("task", ""))
    result = SampleResult(
        record_id=str(record.get("id", "?")),
        task=task,
        raw_text=raw_text,
        gold=dict(record.get("output") or {}),
        latency_s=latency_s,
        completion_tokens=completion_tokens,
    )

    if task not in CONTRACTS:
        result.extraction_error = f"未知 task={task!r}"
        return result

    parsed, error = extract_output(raw_text, task)
    if parsed is None:
        result.extraction_error = error
        return result

    result.parsed = parsed
    model_input = record.get("input")
    result.violations = validate_output(
        task,
        parsed,
        model_input=model_input if isinstance(model_input, Mapping) else None,
    )
    result.text_constraint_errors = check_text_constraints(task, parsed, config)
    return result


def evaluate_records(
    records: Sequence[Mapping[str, Any]],
    config: EvalConfig,
    *,
    respond: Callable[[Mapping[str, Any]], tuple[str, float, int]],
    backend_name: str = "",
    model_name: str = "",
) -> EvalReport:
    """按 ``respond`` 回调评测一组记录。

    Args:
        respond: ``(record) -> (回复文本, 延迟秒, completion token 数)``。
                 由它屏蔽"模型来自哪里"，因此测试可以注入假回复。
    """
    report = EvalReport(backend=backend_name, model=model_name)
    per_task: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "extracted": 0, "schema_ok": 0, "invariant_ok": 0, "text_ok": 0}
    )
    latency_total = 0.0

    for record in records:
        task = str(record.get("task", ""))
        started = time.perf_counter()
        try:
            text, latency, completion_tokens = respond(record)
        except Exception as exc:  # 后端异常不应中断整轮评测
            LOGGER.warning("记录 %s 生成失败：%s", record.get("id"), exc)
            text, latency, completion_tokens = "", 0.0, 0
        wall = time.perf_counter() - started
        result = evaluate_text(
            record,
            text,
            config,
            latency_s=latency or wall,
            completion_tokens=completion_tokens,
        )

        report.total += 1
        latency_total += result.latency_s
        bucket = per_task[task]
        bucket["total"] += 1

        if result.parsed is not None:
            report.extracted += 1
            bucket["extracted"] += 1
        if result.schema_ok:
            report.schema_ok += 1
            bucket["schema_ok"] += 1
        if result.invariant_ok:
            report.invariant_ok += 1
            bucket["invariant_ok"] += 1
        if result.text_ok:
            report.text_ok += 1
            bucket["text_ok"] += 1

        for violation in result.violations:
            report.violation_counts[violation.code] = (
                report.violation_counts.get(violation.code, 0) + 1
            )
        for problem in result.text_constraint_errors:
            key = problem.split(":")[0]
            report.text_error_counts[key] = report.text_error_counts.get(key, 0) + 1

        if result.parsed is not None:
            field_stats = report.field_stats.setdefault(task, {})
            compare_fields(task, result.parsed, result.gold, config, field_stats)

        report.samples.append(result)

    report.by_task = {
        task: {
            **counts,
            "extraction_rate": round(counts["extracted"] / counts["total"], 4)
            if counts["total"]
            else 0.0,
            "schema_pass_rate": round(counts["schema_ok"] / counts["total"], 4)
            if counts["total"]
            else 0.0,
            "invariant_pass_rate": round(counts["invariant_ok"] / counts["total"], 4)
            if counts["total"]
            else 0.0,
            "text_constraint_rate": round(counts["text_ok"] / counts["total"], 4)
            if counts["total"]
            else 0.0,
        }
        for task, counts in per_task.items()
    }
    report.latency_mean_s = latency_total / report.total if report.total else 0.0
    return report


def evaluate_with_backend(
    records: Sequence[Mapping[str, Any]],
    backend: InferenceBackend,
    config: EvalConfig,
) -> EvalReport:
    """用真实后端评测。"""

    def _respond(record: Mapping[str, Any]) -> tuple[str, float, int]:
        task = str(record.get("task"))
        messages = build_inference_messages(task, record.get("input") or {})
        response = backend.generate(
            GenerationRequest(
                messages=messages,
                max_new_tokens=config.max_new_tokens,
                temperature=config.temperature,
                top_p=config.top_p,
            )
        )
        return response.text, response.latency_s, response.completion_tokens

    describe = backend.describe() if hasattr(backend, "describe") else {}
    return evaluate_records(
        records,
        config,
        respond=_respond,
        backend_name=getattr(backend, "name", ""),
        model_name=str(
            describe.get("model_path") or describe.get("gguf_path") or ""
        ),
    )


def limit_records_by_task(
    records: Iterable[Mapping[str, Any]], limit: int | None
) -> list[dict[str, Any]]:
    """按 task 均衡截断，避免只截到第一个任务的样本。"""
    if not limit:
        return [dict(record) for record in records]
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        buckets[str(record.get("task", "?"))].append(dict(record))
    if not buckets:
        return []
    per_task = max(1, limit // len(buckets))
    out: list[dict[str, Any]] = []
    for task in sorted(buckets):
        out.extend(buckets[task][:per_task])
    return out


def load_eval_records(paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for path in paths:
        collected.extend(read_jsonl(path))
    return collected


def write_eval_report(
    report: EvalReport,
    output_dir: str | Path,
    *,
    include_samples: bool = True,
) -> dict[str, Path]:
    """落盘 JSON 报告 + 预测明细。"""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    paths["report"] = write_json(
        directory / "eval_report.json", report.to_dict(include_samples=False)
    )
    if include_samples:
        prediction_path = directory / "predictions.jsonl"
        from ..utils.io import write_jsonl

        write_jsonl(prediction_path, [item.to_dict() for item in report.samples])
        paths["predictions"] = prediction_path
    return paths


def render_eval_summary(report: EvalReport) -> str:
    """人类可读的评测摘要（Markdown）。"""
    rates = report.rates()
    lines = [
        "# 评测摘要",
        "",
        f"- backend: `{report.backend}`  model: `{report.model}`",
        f"- 样本数: {report.total}",
        f"- 平均延迟: {report.latency_mean_s:.3f}s",
        "",
        "## 总体指标",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| JSON 抽取率 | {rates['extraction_rate']:.2%} |",
        f"| schema 合法率 | {rates['schema_pass_rate']:.2%} |",
        f"| 不变量通过率 | {rates['invariant_pass_rate']:.2%} |",
        f"| 文本约束通过率 | {rates['text_constraint_rate']:.2%} |",
        "",
    ]

    if report.by_task:
        lines.extend(["## 分任务", "", "| task | n | 抽取率 | schema | 不变量 | 文本 |", "| --- | --- | --- | --- | --- | --- |"])
        for task, counts in sorted(report.by_task.items()):
            lines.append(
                f"| {task} | {counts['total']} | {counts['extraction_rate']:.2%} | "
                f"{counts['schema_pass_rate']:.2%} | {counts['invariant_pass_rate']:.2%} | "
                f"{counts['text_constraint_rate']:.2%} |"
            )
        lines.append("")

    for task, fields in sorted(report.field_stats.items()):
        lines.extend(
            [
                f"## 字段指标（{task}）",
                "",
                "类别/文本字段按匹配率计；数值字段按 MAE 与误差命中率计"
                "（`exact_match_rate` 是浮点完全相等的比例，仅供参考）。",
                "",
                "| 字段 | 口径 | 指标 | 缺失 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for name, stats in sorted(fields.items()):
            if stats.numeric:
                metric = (
                    f"MAE {stats.mae:.4f}；误差≤阈值 {stats.within_rate:.1%}；"
                    f"完全相等 {stats.exact_accuracy:.1%}"
                )
                kind = "数值"
            else:
                metric = f"匹配率 {stats.accuracy:.1%}"
                kind = "类别" if name in get_contract(task).categorical_fields else "文本"
            lines.append(f"| {name} | {kind} | {metric} | {stats.missing} |")
        lines.append("")

    if report.violation_counts:
        lines.extend(["## 不变量违规分布", "", "| 代码 | 次数 |", "| --- | --- |"])
        for code, count in sorted(report.violation_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {code} | {count} |")
        lines.append("")

    failing = [item for item in report.samples if item.parsed is None][:5]
    if failing:
        lines.extend(["## 抽取失败样例（前 5）", ""])
        for item in failing:
            snippet = item.raw_text[:200].replace("\n", " ")
            lines.append(f"- `{item.record_id}`: {item.extraction_error} | 原文：`{snippet}`")
        lines.append("")

    return "\n".join(lines)


def threshold_gate(
    report: EvalReport,
    *,
    min_schema_pass_rate: float = 0.98,
    min_extraction_rate: float = 0.99,
    min_invariant_pass_rate: float = 0.95,
) -> list[str]:
    """上线门禁：返回未达标项列表（空 = 通过）。"""
    rates = report.rates()
    failures: list[str] = []
    if rates["extraction_rate"] < min_extraction_rate:
        failures.append(
            f"JSON 抽取率 {rates['extraction_rate']:.2%} < {min_extraction_rate:.2%}"
        )
    if rates["schema_pass_rate"] < min_schema_pass_rate:
        failures.append(
            f"schema 合法率 {rates['schema_pass_rate']:.2%} < {min_schema_pass_rate:.2%}"
        )
    if rates["invariant_pass_rate"] < min_invariant_pass_rate:
        failures.append(
            f"不变量通过率 {rates['invariant_pass_rate']:.2%} < {min_invariant_pass_rate:.2%}"
        )
    return failures


def compare_reports(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    """对比两份报告的关键指标（用于 LoRA 前后 / BF16 vs Q4）。"""
    keys = ("extraction_rate", "schema_pass_rate", "invariant_pass_rate", "text_constraint_rate")
    lines = ["| 指标 | baseline | candidate | 变化 |", "| --- | --- | --- | --- |"]
    base_rates = baseline.get("rates", {})
    cand_rates = candidate.get("rates", {})
    for key in keys:
        left = float(base_rates.get(key, 0.0))
        right = float(cand_rates.get(key, 0.0))
        delta = right - left
        arrow = "→" if abs(delta) < 1e-9 else ("↑" if delta > 0 else "↓")
        lines.append(
            f"| {key} | {left:.2%} | {right:.2%} | {arrow} {delta:+.2%} |"
        )
    return "\n".join(lines)


def parse_json_lenient(text: str) -> Any:
    """便于调试：不加校验地抽取 JSON。"""
    return extract_json(text, prefer_object=False)
