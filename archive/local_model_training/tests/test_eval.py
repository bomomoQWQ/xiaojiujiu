"""评测测试：合法率、字段准确率/误差、不变量、文本约束。

评测脚本本身必须可信 —— 如果它把 MAE 算错或漏报违规，
后续所有"模型变好了"的结论都是假的。因此这里用一个可控的
假后端，逐项验证指标定义。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qboss_training.eval.evaluator import (
    EvalConfig,
    FieldStats,
    check_text_constraints,
    compare_fields,
    compare_reports,
    evaluate_records,
    evaluate_text,
    evaluate_with_backend,
    limit_records_by_task,
    load_eval_records,
    render_eval_summary,
    threshold_gate,
    write_eval_report,
)
from qboss_training.inference import (
    EchoBackend,
    GenerationRequest,
    GenerationResponse,
    build_gold_replay_backend,
    extract_output,
)


# --------------------------------------------------------------------------
# 假回复
# --------------------------------------------------------------------------

def respond_with(mapping: dict[str, Any]) -> Any:
    """构造 ``respond`` 回调：按输入 JSON 精确匹配返回预设回复。"""

    def _respond(record: dict[str, Any]) -> tuple[str, float, int]:
        key = json.dumps(record["input"], ensure_ascii=False, sort_keys=True)
        payload = mapping[key]
        if isinstance(payload, Exception):
            raise payload
        return payload, 0.01, 10

    return _respond


def keyed(record: dict[str, Any]) -> str:
    return json.dumps(record["input"], ensure_ascii=False, sort_keys=True)


def gold_text(record: dict[str, Any]) -> str:
    return json.dumps(record["output"], ensure_ascii=False)


# --------------------------------------------------------------------------
# 抽取
# --------------------------------------------------------------------------

class TestExtractOutput:
    def test_accepts_valid_object(self, sample_event_eval: dict) -> None:
        parsed, error = extract_output(gold_text(sample_event_eval), "event_eval")
        assert error is None
        assert parsed == sample_event_eval["output"]

    def test_unwraps_output_wrapper(self, sample_event_eval: dict) -> None:
        text = json.dumps({"output": sample_event_eval["output"]}, ensure_ascii=False)
        parsed, _ = extract_output(text, "event_eval")
        assert parsed == sample_event_eval["output"]

    def test_rejects_incomplete_object(self) -> None:
        parsed, error = extract_output('{"direction": "-"}', "event_eval")
        assert parsed is None
        assert error

    def test_reports_error_for_garbage(self) -> None:
        parsed, error = extract_output("完全不是 JSON", "event_eval")
        assert parsed is None
        assert "JSON" in (error or "")

    def test_handles_fenced_block(self, sample_event_eval: dict) -> None:
        text = f"```json\n{gold_text(sample_event_eval)}\n```"
        parsed, _ = extract_output(text, "event_eval")
        assert parsed == sample_event_eval["output"]


# --------------------------------------------------------------------------
# 单条评测
# --------------------------------------------------------------------------

class TestEvaluateText:
    def test_gold_reply_is_perfect(self, sample_event_eval: dict) -> None:
        result = evaluate_text(sample_event_eval, gold_text(sample_event_eval), EvalConfig())
        assert result.schema_ok
        assert result.invariant_ok
        assert result.text_ok
        assert result.parsed == sample_event_eval["output"]

    def test_unparseable_reply_marks_extraction_error(self, sample_event_eval: dict) -> None:
        result = evaluate_text(sample_event_eval, "抱歉我不能输出 JSON", EvalConfig())
        assert result.parsed is None
        assert result.extraction_error is not None
        assert not result.schema_ok

    def test_schema_violation_detected(self, sample_event_eval: dict) -> None:
        broken = dict(sample_event_eval["output"], impact=5.0)
        result = evaluate_text(sample_event_eval, json.dumps(broken), EvalConfig())
        assert not result.schema_ok
        assert any(item.code == "SCHEMA" for item in result.violations)

    def test_invariant_violation_detected(self, sample_event_eval: dict) -> None:
        broken = dict(sample_event_eval["output"], direction="+")  # 与 slight_distance 矛盾
        result = evaluate_text(sample_event_eval, json.dumps(broken), EvalConfig())
        assert result.schema_ok  # schema 仍合法
        assert not result.invariant_ok  # 但不变量不过

    def test_text_constraint_violation_detected(self, sample_event_eval: dict) -> None:
        broken = dict(sample_event_eval["output"], evidence="很" * 120)
        result = evaluate_text(sample_event_eval, json.dumps(broken), EvalConfig())
        assert not result.text_ok
        assert result.text_constraint_errors

    def test_emotion_numeric_violation_detected(self, sample_emotion_explain: dict) -> None:
        broken = dict(sample_emotion_explain["output"], experience="失落感有 0.8 那么重。")
        result = evaluate_text(sample_emotion_explain, json.dumps(broken), EvalConfig())
        assert not result.text_ok

    def test_dialogue_violation_detected(self, sample_emotion_explain: dict) -> None:
        broken = dict(sample_emotion_explain["output"], expression='会说"你回来吗"。')
        result = evaluate_text(sample_emotion_explain, json.dumps(broken), EvalConfig())
        assert not result.text_ok

    def test_unknown_task_is_reported(self) -> None:
        record = {"id": "x", "task": "nope", "input": {}, "output": {}}
        result = evaluate_text(record, "{}", EvalConfig())
        assert result.extraction_error is not None

    def test_latency_and_tokens_are_recorded(self, sample_event_eval: dict) -> None:
        result = evaluate_text(
            sample_event_eval, gold_text(sample_event_eval), EvalConfig(),
            latency_s=1.5, completion_tokens=42,
        )
        assert result.latency_s == 1.5
        assert result.completion_tokens == 42


class TestTextConstraints:
    def test_long_field_flagged(self, sample_emotion_explain: dict) -> None:
        output = dict(sample_emotion_explain["output"], focus="很" * 100)
        problems = check_text_constraints("emotion_explain", output, EvalConfig())
        assert any("focus" in item for item in problems)

    def test_multiline_flagged(self, sample_emotion_explain: dict) -> None:
        output = dict(sample_emotion_explain["output"], focus="第一句\n第二句")
        problems = check_text_constraints("emotion_explain", output, EvalConfig())
        assert any("换行" in item for item in problems)

    def test_too_short_flagged(self, sample_emotion_explain: dict) -> None:
        output = dict(sample_emotion_explain["output"], focus="短")
        problems = check_text_constraints("emotion_explain", output, EvalConfig())
        assert any("低于" in item for item in problems)

    def test_clean_output_has_no_problems(self, sample_emotion_explain: dict) -> None:
        assert check_text_constraints(
            "emotion_explain", sample_emotion_explain["output"], EvalConfig()
        ) == []

    def test_event_eval_evidence_length(self, sample_event_eval: dict) -> None:
        output = dict(sample_event_eval["output"], evidence="依" * 80)
        problems = check_text_constraints("event_eval", output, EvalConfig())
        assert any("evidence" in item for item in problems)


# --------------------------------------------------------------------------
# 字段比对
# --------------------------------------------------------------------------

class TestCompareFields:
    def test_categorical_exact_match_counted(self, sample_event_eval: dict) -> None:
        stats: dict[str, FieldStats] = {}
        compare_fields(
            "event_eval",
            sample_event_eval["output"],
            sample_event_eval["output"],
            EvalConfig(),
            stats,
        )
        assert stats["direction"].accuracy == 1.0
        assert stats["direction"].correct == 1

    def test_categorical_mismatch_not_counted(self, sample_event_eval: dict) -> None:
        stats: dict[str, FieldStats] = {}
        predicted = dict(sample_event_eval["output"], direction="+")
        compare_fields(
            "event_eval", predicted, sample_event_eval["output"], EvalConfig(), stats
        )
        assert stats["direction"].accuracy == 0.0

    def test_numeric_mae(self, sample_event_eval: dict) -> None:
        stats: dict[str, FieldStats] = {}
        predicted = dict(sample_event_eval["output"], impact=0.42)  # gold 0.62
        compare_fields(
            "event_eval", predicted, sample_event_eval["output"], EvalConfig(), stats
        )
        assert stats["impact"].mae == pytest.approx(0.2)
        assert stats["impact"].numeric is True

    def test_numeric_within_tolerance(self, sample_event_eval: dict) -> None:
        stats: dict[str, FieldStats] = {}
        predicted = dict(sample_event_eval["output"], impact=0.60)  # 误差 0.02
        compare_fields(
            "event_eval", predicted, sample_event_eval["output"],
            EvalConfig(tolerance=0.05), stats,
        )
        assert stats["impact"].within_rate == 1.0

    def test_numeric_outside_tolerance(self, sample_event_eval: dict) -> None:
        stats: dict[str, FieldStats] = {}
        predicted = dict(sample_event_eval["output"], impact=0.10)
        compare_fields(
            "event_eval", predicted, sample_event_eval["output"],
            EvalConfig(tolerance=0.05), stats,
        )
        assert stats["impact"].within_rate == 0.0

    def test_missing_numeric_gets_max_error(self, sample_event_eval: dict) -> None:
        """缺失不应被当作"跳过"从而美化 MAE。"""
        stats: dict[str, FieldStats] = {}
        predicted = dict(sample_event_eval["output"])
        predicted["impact"] = "not a number"
        compare_fields(
            "event_eval", predicted, sample_event_eval["output"], EvalConfig(), stats
        )
        assert stats["impact"].mae == 1.0

    def test_text_field_counts_nonempty(self, sample_emotion_explain: dict) -> None:
        stats: dict[str, FieldStats] = {}
        compare_fields(
            "emotion_explain",
            sample_emotion_explain["output"],
            sample_emotion_explain["output"],
            EvalConfig(),
            stats,
        )
        assert stats["experience"].accuracy == 1.0
        assert stats["experience"].numeric is False

    def test_numeric_dict_uses_mae_keys(self, sample_event_eval: dict) -> None:
        stats: dict[str, FieldStats] = {}
        compare_fields(
            "event_eval",
            sample_event_eval["output"],
            sample_event_eval["output"],
            EvalConfig(),
            stats,
        )
        payload = stats["impact"].to_dict()
        assert "mae" in payload
        assert "within_tolerance_rate" in payload
        assert "accuracy" not in payload


# --------------------------------------------------------------------------
# 批量评测
# --------------------------------------------------------------------------

class TestEvaluateRecords:
    def test_all_gold_gives_perfect_rates(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        rates = report.rates()
        assert rates["extraction_rate"] == 1.0
        assert rates["schema_pass_rate"] == 1.0
        assert rates["invariant_pass_rate"] == 1.0
        assert rates["text_constraint_rate"] == 1.0

    def test_broken_backend_lowers_rates(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): "抱歉，我不能输出" for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        assert report.rates()["extraction_rate"] == 0.0
        assert report.rates()["schema_pass_rate"] == 0.0

    def test_backend_exception_does_not_abort_run(self, small_fixture_records: list[dict]) -> None:
        def _respond(record: dict[str, Any]) -> tuple[str, float, int]:
            if record["id"].endswith("0000"):
                raise RuntimeError("模拟后端崩溃")
            return gold_text(record), 0.0, 0

        report = evaluate_records(small_fixture_records, EvalConfig(), respond=_respond)
        assert report.total == len(small_fixture_records)
        assert report.rates()["extraction_rate"] < 1.0

    def test_counts_are_per_task(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        assert set(report.by_task) == {"event_eval", "emotion_explain"}
        assert report.by_task["event_eval"]["total"] == 6

    def test_field_stats_present_per_task(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        assert "direction" in report.field_stats["event_eval"]
        assert "experience" in report.field_stats["emotion_explain"]

    def test_violation_counts_aggregated(self, small_fixture_records: list[dict]) -> None:
        def _respond(record: dict[str, Any]) -> tuple[str, float, int]:
            if record["task"] == "event_eval":
                broken = dict(record["output"], direction="+", relation_signal="strong_distance")
                return json.dumps(broken, ensure_ascii=False), 0.0, 0
            return gold_text(record), 0.0, 0

        report = evaluate_records(small_fixture_records, EvalConfig(), respond=_respond)
        assert "EV02_DIRECTION_SIGNAL" in report.violation_counts

    def test_latency_averaged(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        assert report.latency_mean_s > 0

    def test_empty_input(self) -> None:
        report = evaluate_records([], EvalConfig(), respond=lambda record: ("", 0.0, 0))
        assert report.total == 0
        assert report.rates()["schema_pass_rate"] == 0.0


# --------------------------------------------------------------------------
# 后端集成
# --------------------------------------------------------------------------

class TestEchoBackend:
    def test_gold_replay_is_perfect(self, small_fixture_records: list[dict]) -> None:
        backend = build_gold_replay_backend(small_fixture_records)
        report = evaluate_with_backend(small_fixture_records, backend, EvalConfig())
        assert report.rates()["schema_pass_rate"] == 1.0

    def test_fenced_wrapping_is_handled(self, small_fixture_records: list[dict]) -> None:
        backend = build_gold_replay_backend(small_fixture_records, wrap_in_fence=True)
        report = evaluate_with_backend(small_fixture_records, backend, EvalConfig())
        assert report.rates()["extraction_rate"] == 1.0

    def test_unknown_input_returns_empty_object(self, sample_event_eval: dict) -> None:
        backend = EchoBackend({})
        response = backend.generate(
            GenerationRequest(
                messages=[{"role": "user", "content": json.dumps({"a": 1})}]
            )
        )
        assert response.text == "{}"

    def test_records_calls(self, sample_event_eval: dict) -> None:
        backend = build_gold_replay_backend([sample_event_eval])
        backend.generate(
            GenerationRequest(
                messages=[
                    {"role": "user", "content": json.dumps(sample_event_eval["input"])}
                ]
            )
        )
        assert len(backend.calls) == 1

    def test_describe(self, small_fixture_records: list[dict]) -> None:
        backend = build_gold_replay_backend(small_fixture_records)
        assert backend.describe()["backend"] == "echo"


# --------------------------------------------------------------------------
# 报告与门禁
# --------------------------------------------------------------------------

class TestReports:
    def test_summary_contains_key_sections(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        summary = render_eval_summary(report)
        assert "schema 合法率" in summary
        assert "字段指标" in summary
        assert "event_eval" in summary

    def test_summary_lists_extraction_failures(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): "垃圾输出" for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        assert "抽取失败样例" in render_eval_summary(report)

    def test_to_dict_is_json_serializable(self, small_fixture_records: list[dict]) -> None:
        mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        json.dumps(report.to_dict(include_samples=True), ensure_ascii=False)

    def test_write_report_creates_files(
        self, tmp_path: Path, small_fixture_records: list[dict]
    ) -> None:
        mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        report = evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )
        paths = write_eval_report(report, tmp_path)
        assert paths["report"].exists()
        assert paths["predictions"].exists()
        payload = json.loads(paths["report"].read_text(encoding="utf-8"))
        assert payload["rates"]["schema_pass_rate"] == 1.0

    def test_limit_records_by_task_is_balanced(self, fixture_records: list[dict]) -> None:
        limited = limit_records_by_task(fixture_records, 10)
        tasks = {record["task"] for record in limited}
        assert len(tasks) == 2
        assert len(limited) == 10

    def test_limit_none_keeps_all(self, fixture_records: list[dict]) -> None:
        assert len(limit_records_by_task(fixture_records, None)) == len(fixture_records)

    def test_load_eval_records(self, tmp_path: Path, small_fixture_records: list[dict]) -> None:
        from qboss_training.utils.io import write_jsonl

        path = tmp_path / "a.jsonl"
        write_jsonl(path, small_fixture_records)
        assert len(load_eval_records([path])) == len(small_fixture_records)


class TestThresholdGate:
    def _report(self, small_fixture_records: list[dict], bad: bool) -> Any:
        if bad:
            mapping = {keyed(record): "垃圾" for record in small_fixture_records}
        else:
            mapping = {keyed(record): gold_text(record) for record in small_fixture_records}
        return evaluate_records(
            small_fixture_records, EvalConfig(), respond=respond_with(mapping)
        )

    def test_gate_passes_on_good_report(self, small_fixture_records: list[dict]) -> None:
        assert threshold_gate(self._report(small_fixture_records, bad=False)) == []

    def test_gate_fails_on_bad_report(self, small_fixture_records: list[dict]) -> None:
        failures = threshold_gate(self._report(small_fixture_records, bad=True))
        assert failures
        assert any("抽取率" in item for item in failures)

    def test_gate_reports_each_failing_metric(self) -> None:
        from qboss_training.eval.evaluator import EvalReport

        report = EvalReport(total=10, extracted=5, schema_ok=4, invariant_ok=3)
        failures = threshold_gate(report)
        assert len(failures) == 3


class TestCompareReports:
    def test_shows_deltas(self) -> None:
        baseline = {"rates": {"schema_pass_rate": 0.5, "invariant_pass_rate": 0.4}}
        candidate = {"rates": {"schema_pass_rate": 0.6, "invariant_pass_rate": 0.3}}
        rendered = compare_reports(baseline, candidate)
        assert "↑" in rendered
        assert "↓" in rendered

    def test_handles_missing_keys(self) -> None:
        rendered = compare_reports({}, {})
        assert "extraction_rate" in rendered
