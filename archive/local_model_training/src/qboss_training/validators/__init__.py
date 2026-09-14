"""离线校验：JSON Schema + 跨字段不变量 + 文本约束。

用法::

    python -m qboss_training.cli validate --input data/raw/event_eval.jsonl

本模块**完全离线**，不需要网络也不需要大模型：
JSON Schema 校验用 ``jsonschema``，不变量用 :mod:`.invariants` 的纯函数。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..contracts import CONTRACTS, TASK_NAMES, TaskContract, get_contract
from ..errors import SchemaViolation
from ..utils.jsonx import dumps_canonical
from .invariants import (
    ERROR,
    WARNING,
    Violation,
    check_emotion_explain_invariants,
    check_event_eval_invariants,
    has_errors,
    worst_severity,
)

INVARIANT_CHECKS = {
    "event_eval": check_event_eval_invariants,
    "emotion_explain": check_emotion_explain_invariants,
}


@dataclass
class RecordReport:
    """单条样本的校验结果。"""

    index: int
    record_id: str
    task: str
    ok: bool
    schema_errors: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    input_errors: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str | None:
        return worst_severity(self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "id": self.record_id,
            "task": self.task,
            "ok": self.ok,
            "schema_errors": list(self.schema_errors),
            "input_errors": list(self.input_errors),
            "violations": [item.to_dict() for item in self.violations],
        }


@dataclass
class ValidationReport:
    """一个文件/数据集的校验汇总。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    records: list[RecordReport] = field(default_factory=list)
    by_task: dict[str, dict[str, int]] = field(default_factory=dict)
    violation_counts: dict[str, int] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def failed_reports(self) -> list[RecordReport]:
        return [item for item in self.records if not item.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 6),
            "by_task": self.by_task,
            "violation_counts": dict(
                sorted(self.violation_counts.items(), key=lambda kv: -kv[1])
            ),
            "failures": [item.to_dict() for item in self.failed_reports[:200]],
        }


def build_validator(contract: TaskContract) -> Draft202012Validator:
    """构造 JSON Schema 校验器（``format`` 不做断言，避免格式库依赖）。"""
    return Draft202012Validator(contract.output_schema())


def build_input_validator(contract: TaskContract) -> Draft202012Validator:
    return Draft202012Validator(contract.input_schema())


_VALIDATOR_CACHE: dict[str, tuple[Draft202012Validator, Draft202012Validator]] = {}


def _cached_validators(task: str) -> tuple[Draft202012Validator, Draft202012Validator]:
    if task not in _VALIDATOR_CACHE:
        contract = get_contract(task)
        _VALIDATOR_CACHE[task] = (
            build_validator(contract),
            build_input_validator(contract),
        )
    return _VALIDATOR_CACHE[task]


def format_schema_error(error: ValidationError) -> str:
    path = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{path}: {error.message}"


def validate_output(
    task: str,
    output: Any,
    *,
    model_input: Mapping[str, Any] | None = None,
    check_invariants: bool = True,
) -> list[Violation]:
    """校验单条输出。返回违规列表（空 = 通过）。

    严重度为 ``error`` 的项表示**不可用于训练**。
    """
    violations: list[Violation] = []
    output_validator, _ = _cached_validators(task)

    schema_errors = sorted(output_validator.iter_errors(output), key=lambda e: list(e.absolute_path))
    for error in schema_errors:
        violations.append(
            Violation(
                "SCHEMA",
                ERROR,
                format_schema_error(error),
                "/".join(str(part) for part in error.absolute_path) or None,
            )
        )

    if schema_errors:
        # schema 不通过时不再跑依赖字段类型的不变量
        return violations

    if check_invariants:
        checker = INVARIANT_CHECKS.get(task)
        if checker is not None:
            violations.extend(checker(output, model_input))
    return violations


def validate_input(task: str, model_input: Any) -> list[str]:
    """校验输入 payload，返回错误字符串列表。"""
    _, input_validator = _cached_validators(task)
    return [
        format_schema_error(error)
        for error in sorted(
            input_validator.iter_errors(model_input),
            key=lambda e: list(e.absolute_path),
        )
    ]


def validate_record(
    record: Mapping[str, Any],
    *,
    index: int = 0,
    check_invariants: bool = True,
    require_input_schema: bool = True,
) -> RecordReport:
    """校验一条训练样本记录。

    记录形态（与生成器输出一致）::

        {"id": "...", "task": "event_eval", "input": {...}, "output": {...}, "meta": {...}}
    """
    record_id = str(record.get("id") or f"idx_{index}")
    task = record.get("task")
    report = RecordReport(index=index, record_id=record_id, task=str(task), ok=True)

    if task not in TASK_NAMES:
        report.schema_errors.append(
            f"<root>: task 必须是 {'/'.join(TASK_NAMES)} 之一，收到 {task!r}"
        )
        report.ok = False
        return report

    if "output" not in record:
        report.schema_errors.append("<root>: 缺少 output 字段")
        report.ok = False
        return report

    model_input = record.get("input")
    if require_input_schema:
        if model_input is None:
            report.input_errors.append("<root>: 缺少 input 字段")
        else:
            report.input_errors.extend(validate_input(task, model_input))

    violations = validate_output(
        task,
        record["output"],
        model_input=model_input if isinstance(model_input, Mapping) else None,
        check_invariants=check_invariants,
    )
    report.violations = violations
    report.ok = not report.schema_errors and not report.input_errors and not has_errors(violations)
    return report


def validate_records(
    records: Iterable[Mapping[str, Any]],
    *,
    check_invariants: bool = True,
    require_input_schema: bool = True,
) -> ValidationReport:
    """批量校验并汇总。"""
    report = ValidationReport()
    for index, record in enumerate(records):
        item = validate_record(
            record,
            index=index,
            check_invariants=check_invariants,
            require_input_schema=require_input_schema,
        )
        report.total += 1
        report.records.append(item)
        if item.ok:
            report.passed += 1
        else:
            report.failed += 1

        bucket = report.by_task.setdefault(
            item.task, {"total": 0, "passed": 0, "failed": 0}
        )
        bucket["total"] += 1
        bucket["passed" if item.ok else "failed"] += 1

        for violation in item.violations:
            report.violation_counts[violation.code] = (
                report.violation_counts.get(violation.code, 0) + 1
            )
        for _ in item.schema_errors:
            report.violation_counts["SCHEMA"] = report.violation_counts.get("SCHEMA", 0) + 1
        for _ in item.input_errors:
            report.violation_counts["INPUT_SCHEMA"] = (
                report.violation_counts.get("INPUT_SCHEMA", 0) + 1
            )
    return report


def is_trainable(record: Mapping[str, Any]) -> bool:
    """一条记录是否可进入训练集（严格：任何 error 都不行）。"""
    return validate_record(record).ok


def assert_valid(record: Mapping[str, Any]) -> None:
    """不合格即抛 :class:`SchemaViolation`（供脚本做快速失败）。"""
    report = validate_record(record)
    if not report.ok:
        details = [f"schema: {err}" for err in report.schema_errors]
        details += [f"input: {err}" for err in report.input_errors]
        details += [
            f"{item.code}: {item.message}"
            for item in report.violations
            if item.severity == ERROR
        ]
        raise SchemaViolation(
            f"记录 {report.record_id}（task={report.task}）校验失败：\n  - "
            + "\n  - ".join(details)
        )


def schema_bundle() -> dict[str, Any]:
    """把全部契约 schema 打进一个 bundle，便于随 adapter 一起发布。"""
    bundle: dict[str, Any] = {}
    for name, contract in CONTRACTS.items():
        bundle[name] = {
            "output_schema": contract.output_schema(),
            "input_schema": contract.input_schema(),
            "system_prompt": contract.system_prompt,
        }
    return bundle


def bundle_fingerprint() -> str:
    """schema 指纹：数据集与 adapter 版本对齐用。"""
    import hashlib

    payload = dumps_canonical(schema_bundle()).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
