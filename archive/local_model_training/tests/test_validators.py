"""校验器与不变量测试。

这是本工程最关键的测试：JSON Schema 只能保证"字段在不在"，
真正拦住坏数据的是跨字段不变量（架构文档 §8.1 / §11.3）。
"""

from __future__ import annotations

from typing import Any

import pytest

from qboss_training.validators import (
    assert_valid,
    build_validator,
    bundle_fingerprint,
    is_trainable,
    schema_bundle,
    validate_input,
    validate_output,
    validate_record,
    validate_records,
)
from qboss_training.validators.invariants import (
    ERROR,
    INFO,
    WARNING,
    check_emotion_explain_invariants,
    check_event_eval_invariants,
    has_dialogue,
    numeric_assertions,
    summarize_state,
    tone_score,
)
from qboss_training.errors import SchemaViolation


def codes(violations: list[Any]) -> set[str]:
    return {item.code for item in violations}


def severities(violations: list[Any]) -> set[str]:
    return {item.severity for item in violations}


# --------------------------------------------------------------------------
# event_eval
# --------------------------------------------------------------------------

class TestEventEvalSchema:
    def test_valid_output_passes(self, sample_event_eval: dict[str, Any]) -> None:
        violations = validate_output(
            "event_eval", sample_event_eval["output"], model_input=sample_event_eval["input"]
        )
        assert not [item for item in violations if item.severity == ERROR]

    def test_missing_field_is_schema_error(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"])
        del output["confidence"]
        violations = validate_output("event_eval", output)
        assert "SCHEMA" in codes(violations)

    def test_extra_field_is_rejected(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], extra_field=1)
        assert "SCHEMA" in codes(validate_output("event_eval", output))

    def test_out_of_range_number_is_rejected(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], impact=1.5)
        assert "SCHEMA" in codes(validate_output("event_eval", output))

    def test_negative_number_is_rejected(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], impact=-0.1)
        assert "SCHEMA" in codes(validate_output("event_eval", output))

    def test_bad_enum_is_rejected(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], direction="negative")
        assert "SCHEMA" in codes(validate_output("event_eval", output))

    def test_string_number_is_rejected(self, sample_event_eval: dict[str, Any]) -> None:
        """严格的类型检查是"字段准确率"可评测的前提。"""
        output = dict(sample_event_eval["output"], impact="0.62")
        assert "SCHEMA" in codes(validate_output("event_eval", output))

    def test_boolean_is_not_a_number(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], impact=True)
        assert "SCHEMA" in codes(validate_output("event_eval", output))

    def test_schema_error_suppresses_invariant_checks(
        self, sample_event_eval: dict[str, Any]
    ) -> None:
        """schema 不通过时不应再跑依赖类型的不变量（会误报）。"""
        violations = validate_output("event_eval", {"direction": "+"})
        assert codes(violations) == {"SCHEMA"}


class TestEventEvalInvariants:
    def test_direction_signal_contradiction_is_error(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], direction="+")
        violations = validate_output("event_eval", output)
        assert "EV02_DIRECTION_SIGNAL" in codes(violations)
        assert ERROR in severities(violations)

    def test_neutral_direction_with_relation_signal_is_error(
        self, sample_event_eval: dict[str, Any]
    ) -> None:
        output = dict(sample_event_eval["output"], direction="0", relation_signal="strong_approach")
        assert "EV02_DIRECTION_SIGNAL" in codes(validate_output("event_eval", output))

    def test_neutral_direction_with_high_impact_warns(
        self, sample_event_eval: dict[str, Any]
    ) -> None:
        output = dict(sample_event_eval["output"], direction="0", impact=0.8)
        violations = validate_output("event_eval", output)
        assert "EV01_DIRECTION_IMPACT" in codes(violations)

    def test_directional_with_zero_impact_warns(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], impact=0.01)
        assert "EV01_DIRECTION_IMPACT" in codes(validate_output("event_eval", output))

    def test_mixed_direction_with_low_uncertainty_warns(
        self, sample_event_eval: dict[str, Any]
    ) -> None:
        output = dict(sample_event_eval["output"], direction="+-", uncertainty=0.02)
        assert "EV04_MIXED_LOW_UNCERTAINTY" in codes(validate_output("event_eval", output))

    def test_emotion_value_leak_is_error(self, sample_event_eval: dict[str, Any]) -> None:
        """§8.1：2B 不负责最终情绪值。

        注意：直接加字段会被 schema 拦住，所以这里绕过 schema 单独测不变量函数，
        确保即使 schema 放宽，不变量仍然能抓到。
        """
        output = dict(sample_event_eval["output"], jealousy=0.82)
        violations = check_event_eval_invariants(output)
        assert "EV05_EMOTION_VALUE_LEAK" in codes(violations)

    def test_ungrounded_evidence_warns(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(sample_event_eval["output"], evidence="用户昨天说要去国外定居三年")
        violations = validate_output(
            "event_eval", output, model_input=sample_event_eval["input"]
        )
        assert "EV06_EVIDENCE_UNGROUNDED" in codes(violations)
        assert WARNING in severities(violations)

    def test_grounded_evidence_passes(self, sample_event_eval: dict[str, Any]) -> None:
        """evidence 是输入片段时应通过。"""
        output = dict(sample_event_eval["output"], evidence="可能没时间")
        violations = validate_output(
            "event_eval", output, model_input=sample_event_eval["input"]
        )
        assert "EV06_EVIDENCE_UNGROUNDED" not in codes(violations)

    def test_overconfident_high_impact_warns(self, sample_event_eval: dict[str, Any]) -> None:
        output = dict(
            sample_event_eval["output"],
            impact=0.9,
            confidence=0.99,
            uncertainty=0.01,
        )
        assert "EV03_OVERCONFIDENT_HIGH_IMPACT" in codes(validate_output("event_eval", output))


# --------------------------------------------------------------------------
# emotion_explain
# --------------------------------------------------------------------------

class TestEmotionExplainSchema:
    def test_valid_output_passes(self, sample_emotion_explain: dict[str, Any]) -> None:
        violations = validate_output(
            "emotion_explain",
            sample_emotion_explain["output"],
            model_input=sample_emotion_explain["input"],
        )
        assert not [item for item in violations if item.severity == ERROR]

    def test_empty_field_is_rejected(self, sample_emotion_explain: dict[str, Any]) -> None:
        output = dict(sample_emotion_explain["output"], experience="")
        assert "SCHEMA" in codes(validate_output("emotion_explain", output))

    def test_too_long_field_is_rejected(self, sample_emotion_explain: dict[str, Any]) -> None:
        output = dict(sample_emotion_explain["output"], experience="很" * 200)
        assert "SCHEMA" in codes(validate_output("emotion_explain", output))

    def test_missing_field_is_rejected(self, sample_emotion_explain: dict[str, Any]) -> None:
        output = dict(sample_emotion_explain["output"])
        del output["inhibition"]
        assert "SCHEMA" in codes(validate_output("emotion_explain", output))


class TestEmotionExplainInvariants:
    def test_direction_flip_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        """负向输入却给出明显正向感受 —— 核心红线。"""
        output = dict(
            sample_emotion_explain["output"],
            experience="非常开心，觉得特别温暖。",
            focus="觉得很满足。",
            conflict="完全没有冲突，很放松。",
            impulse="想靠近一点，想多聊。",
            inhibition="克制，不给压力。",
            expression="表达上很轻快",
        )
        violations = validate_output(
            "emotion_explain", output, model_input=sample_emotion_explain["input"]
        )
        assert "EX02_DIRECTION_FLIP" in codes(violations)
        assert ERROR in severities(violations)

    def test_amplifying_light_emotion_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        """§11.3：不得放大轻微情绪。"""
        model_input = dict(sample_emotion_explain["input"])
        model_input["active_emotions"] = [
            {
                "target": "user",
                "cause": "小事",
                "direction": "-",
                "intensity": 0.25,
            }
        ]
        output = dict(
            sample_emotion_explain["output"],
            experience="非常难受，几乎崩溃，心里特别堵。",
        )
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX01_AMPLIFY_LIGHT_EMOTION" in codes(violations)

    def test_dampening_strong_emotion_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        """§11.3：不得自行降低底层情绪。"""
        model_input = dict(sample_emotion_explain["input"])
        model_input["active_emotions"] = [
            {"target": "user", "cause": "重击", "direction": "-", "intensity": 0.9}
        ]
        output = dict(
            sample_emotion_explain["output"],
            experience="还算平静，没什么特别的。",
            conflict="没有冲突，很平顺。",
        )
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX01B_DAMPEN_STRONG_EMOTION" in codes(violations)

    def test_numeric_assertion_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        """解释器不得自己发明情绪量化值。"""
        output = dict(
            sample_emotion_explain["output"],
            experience="失落感大概有 0.8 那么强。",
        )
        violations = validate_output(
            "emotion_explain", output, model_input=sample_emotion_explain["input"]
        )
        assert "EX03_UNSUPPORTED_NUMERIC" in codes(violations)

    def test_dialogue_in_output_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        """§11.3：不得生成台词。"""
        output = dict(
            sample_emotion_explain["output"],
            expression='会想说"你还会回来吗"。',
        )
        violations = validate_output(
            "emotion_explain", output, model_input=sample_emotion_explain["input"]
        )
        assert "EX06_DIALOGUE_GENERATED" in codes(violations)

    def test_stage_direction_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        output = dict(
            sample_emotion_explain["output"],
            expression="会轻轻笑一下（笑），显得舍不得。",
        )
        violations = validate_output(
            "emotion_explain", output, model_input=sample_emotion_explain["input"]
        )
        assert "EX06_DIALOGUE_GENERATED" in codes(violations)

    def test_action_decision_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        """§11.3：不得决定最终行为。"""
        output = dict(
            sample_emotion_explain["output"],
            impulse="我决定马上就去找他问清楚。",
        )
        violations = validate_output(
            "emotion_explain", output, model_input=sample_emotion_explain["input"]
        )
        assert "EX07_DECIDES_ACTION" in codes(violations)

    def test_fabricated_conflict_warns(self, sample_emotion_explain: dict[str, Any]) -> None:
        model_input = dict(sample_emotion_explain["input"], conflict_present=False)
        output = dict(
            sample_emotion_explain["output"],
            conflict="一方面想靠近，另一方面又想退开，很矛盾。",
        )
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX08_FABRICATED_CONFLICT" in codes(violations)

    def test_conflict_absent_statement_is_accepted(self, sample_emotion_explain: dict[str, Any]) -> None:
        """明确说明"没有冲突"是合法的，不应被误判为虚构冲突。"""
        model_input = dict(sample_emotion_explain["input"], conflict_present=False)
        output = dict(
            sample_emotion_explain["output"],
            conflict="当前没有明显冲突，状态比较平顺。",
        )
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX08_FABRICATED_CONFLICT" not in codes(violations)

    def test_impulse_mismatch_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        """approach_drive 高却完全没有靠近类表述。"""
        model_input = dict(sample_emotion_explain["input"], approach_drive=0.9)
        output = dict(
            sample_emotion_explain["output"],
            impulse="先不打算追问，想留一点空间。",
        )
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX04_IMPULSE_APPROACH_MISMATCH" in codes(violations)

    def test_low_approach_with_active_impulse_is_error(
        self, sample_emotion_explain: dict[str, Any]
    ) -> None:
        model_input = dict(sample_emotion_explain["input"], approach_drive=0.1)
        output = dict(
            sample_emotion_explain["output"],
            impulse="想靠近一点，想追问清楚。",
        )
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX04_IMPULSE_APPROACH_MISMATCH" in codes(violations)

    def test_approach_with_restraint_is_not_a_mismatch(
        self, sample_emotion_explain: dict[str, Any]
    ) -> None:
        """"想靠近但忍住"是合法的冲突结构，不应被误判。"""
        model_input = dict(sample_emotion_explain["input"], approach_drive=0.8)
        output = dict(
            sample_emotion_explain["output"],
            impulse="想靠近一点，但忍住了没追问。",
        )
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX04_IMPULSE_APPROACH_MISMATCH" not in codes(violations)

    def test_restraint_missing_is_error(self, sample_emotion_explain: dict[str, Any]) -> None:
        model_input = dict(sample_emotion_explain["input"], restraint=0.85)
        output = dict(
            sample_emotion_explain["output"],
            inhibition="没什么需要压着的，可以自然表达。",
            conflict="没有冲突，很平顺。",
            restraint_evidence="",
        )
        output.pop("restraint_evidence")
        violations = validate_output("emotion_explain", output, model_input=model_input)
        assert "EX05_INHIBITION_RESTRAINT_MISMATCH" in codes(violations)

    def test_no_input_means_only_schema(self, sample_emotion_explain: dict[str, Any]) -> None:
        """没有输入时无法判断一致性，只做 schema 校验。"""
        violations = validate_output("emotion_explain", sample_emotion_explain["output"])
        assert not [item for item in violations if item.severity == ERROR]


# --------------------------------------------------------------------------
# 文本工具
# --------------------------------------------------------------------------

class TestToneScore:
    def test_negative_text_scores_negative(self) -> None:
        assert tone_score("有些失落，很难受。") < 0

    def test_positive_text_scores_positive(self) -> None:
        assert tone_score("挺高兴的，心里温暖。") > 0

    def test_neutral_text_scores_zero(self) -> None:
        assert tone_score("今天天气还行，没什么特别。") == 0

    def test_negated_positive_word_is_not_positive(self) -> None:
        """"不安心"里的"安心"不应被算成正向。"""
        assert tone_score("不安心，心里特别堵。") < 0

    def test_negated_negative_word_is_not_negative(self) -> None:
        assert tone_score("不难过，挺平静的。") >= 0


class TestNumericAssertions:
    def test_finds_plain_numbers(self) -> None:
        assert numeric_assertions("强度大概 0.8 左右") == [0.8]

    def test_ignores_provided_values(self) -> None:
        assert numeric_assertions("强度 0.56", ignore_values=[0.56]) == []

    def test_ignores_negative_numbers(self) -> None:
        """``valence=-0.15`` 这种输入里的负数不是断言。"""
        assert numeric_assertions("valence -0.15") == []

    def test_no_numbers(self) -> None:
        assert numeric_assertions("有些失落，也有一点不确定。") == []


class TestHasDialogue:
    def test_detects_quotes(self) -> None:
        assert has_dialogue('会想说"你还会回来吗"')

    def test_detects_stage_direction(self) -> None:
        assert has_dialogue("轻轻笑一下（笑）")

    def test_plain_text_is_clean(self) -> None:
        assert not has_dialogue("表达上会稍微显得舍不得，但整体仍然克制。")


class TestSummarizeState:
    def test_extracts_direction_and_intensity(self, sample_emotion_explain: dict[str, Any]) -> None:
        state = summarize_state(sample_emotion_explain["input"])
        assert state["direction"] == "-"
        assert state["intensity"] == pytest.approx(0.56)
        assert state["approach_drive"] == pytest.approx(0.61)
        assert state["restraint"] == pytest.approx(0.72)

    def test_mixed_directions_imply_conflict(self) -> None:
        state = summarize_state(
            {
                "active_emotions": [
                    {"direction": "+", "intensity": 0.5},
                    {"direction": "-", "intensity": 0.5},
                ]
            }
        )
        assert state["has_conflict"] is True

    def test_explicit_hint_wins(self) -> None:
        state = summarize_state(
            {
                "active_emotions": [{"direction": "-", "intensity": 0.5}],
                "conflict_present": False,
            }
        )
        assert state["has_conflict"] is False

    def test_empty_input_is_safe(self) -> None:
        state = summarize_state({})
        assert state["direction"] == "0"
        assert state["intensity"] is None


# --------------------------------------------------------------------------
# 批量校验与记录级校验
# --------------------------------------------------------------------------

class TestValidateRecords:
    def test_fixture_records_all_pass(self, fixture_records: list[dict[str, Any]]) -> None:
        report = validate_records(fixture_records)
        assert report.failed == 0, report.to_dict()["failures"][:3]
        assert report.pass_rate == 1.0

    def test_counts_by_task(self, fixture_records: list[dict[str, Any]]) -> None:
        report = validate_records(fixture_records)
        assert set(report.by_task) == {"event_eval", "emotion_explain"}
        assert report.by_task["event_eval"]["total"] == 30

    def test_unknown_task_fails(self) -> None:
        report = validate_records([{"id": "x", "task": "nope", "input": {}, "output": {}}])
        assert report.failed == 1

    def test_missing_output_fails(self) -> None:
        report = validate_records([{"id": "x", "task": "event_eval", "input": {}}])
        assert report.failed == 1

    def test_invalid_input_schema_fails(self) -> None:
        record = {
            "id": "x",
            "task": "event_eval",
            "input": {"current_event": {"speaker": "nobody", "text": "hi"}},
            "output": {
                "direction": "-",
                "impact": 0.5,
                "activation": 0.3,
                "uncertainty": 0.6,
                "relation_signal": "neutral",
                "responsibility": "other",
                "confidence": 0.7,
                "evidence": "hi",
            },
        }
        report = validate_records([record])
        assert report.failed == 1
        assert report.records[0].input_errors

    def test_missing_input_fails_by_default(self) -> None:
        record = {
            "id": "x",
            "task": "event_eval",
            "output": {
                "direction": "-",
                "impact": 0.5,
                "activation": 0.3,
                "uncertainty": 0.6,
                "relation_signal": "neutral",
                "responsibility": "other",
                "confidence": 0.7,
                "evidence": "无输入",
            },
        }
        assert validate_records([record]).failed == 1

    def test_input_schema_can_be_skipped(self) -> None:
        record = {
            "id": "x",
            "task": "event_eval",
            "input": {"bogus": 1},
            "output": {
                "direction": "-",
                "impact": 0.5,
                "activation": 0.3,
                "uncertainty": 0.6,
                "relation_signal": "neutral",
                "responsibility": "other",
                "confidence": 0.7,
                "evidence": "无输入",
            },
        }
        assert validate_records([record]).failed == 1
        assert validate_records([record], require_input_schema=False).passed == 1

    def test_is_trainable_helper(self, sample_event_eval: dict[str, Any]) -> None:
        assert is_trainable(sample_event_eval)
        broken = dict(sample_event_eval, output={"direction": "?"})
        assert not is_trainable(broken)

    def test_assert_valid_raises_with_details(self, sample_event_eval: dict[str, Any]) -> None:
        broken = dict(sample_event_eval, output={"direction": "?"})
        with pytest.raises(SchemaViolation) as excinfo:
            assert_valid(broken)
        assert "校验失败" in str(excinfo.value)

    def test_assert_valid_passes_silently(self, sample_event_eval: dict[str, Any]) -> None:
        assert_valid(sample_event_eval)

    def test_validate_input_reports_paths(self) -> None:
        errors = validate_input("event_eval", {"current_event": {"speaker": "user"}})
        assert errors
        assert any("text" in item for item in errors)


class TestSchemaBundle:
    def test_bundle_has_both_tasks(self) -> None:
        bundle = schema_bundle()
        assert set(bundle) == {"event_eval", "emotion_explain"}
        for entry in bundle.values():
            assert "output_schema" in entry
            assert "input_schema" in entry
            assert "system_prompt" in entry

    def test_fingerprint_is_stable_and_short(self) -> None:
        first = bundle_fingerprint()
        second = bundle_fingerprint()
        assert first == second
        assert len(first) == 16

    def test_build_validator_works(self) -> None:
        from qboss_training.contracts import get_contract

        validator = build_validator(get_contract("event_eval"))
        assert validator.is_valid(
            {
                "direction": "0",
                "impact": 0.1,
                "activation": 0.2,
                "uncertainty": 0.3,
                "relation_signal": "neutral",
                "responsibility": "unclear",
                "confidence": 0.5,
                "evidence": "无实质依据",
            }
        )

    def test_build_validator_rejects_short_evidence(self) -> None:
        from qboss_training.contracts import get_contract

        validator = build_validator(get_contract("event_eval"))
        assert not validator.is_valid(
            {
                "direction": "0",
                "impact": 0.1,
                "activation": 0.2,
                "uncertainty": 0.3,
                "relation_signal": "neutral",
                "responsibility": "unclear",
                "confidence": 0.5,
                "evidence": "无",
            }
        )
