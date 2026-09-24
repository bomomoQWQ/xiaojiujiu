"""Schema 与任务契约测试。"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from qboss_training.contracts import (
    CONTRACTS,
    EMOTION_EXPLAIN,
    EVENT_EVAL,
    SCHEMA_DIR,
    TASK_NAMES,
    all_contracts,
    get_contract,
)


class TestSchemaFiles:
    def test_all_schema_files_exist(self) -> None:
        for name in (
            "event_eval.schema.json",
            "event_eval.input.schema.json",
            "emotion_explain.schema.json",
            "emotion_explain.input.schema.json",
        ):
            assert (SCHEMA_DIR / name).is_file(), f"缺少 schema 文件 {name}"

    @pytest.mark.parametrize("task", TASK_NAMES)
    def test_output_schema_is_valid_jsonschema(self, task: str) -> None:
        schema = get_contract(task).output_schema()
        # 构造校验器即代表 schema 本身合法
        Draft202012Validator.check_schema(schema)

    @pytest.mark.parametrize("task", TASK_NAMES)
    def test_input_schema_is_valid_jsonschema(self, task: str) -> None:
        schema = get_contract(task).input_schema()
        Draft202012Validator.check_schema(schema)

    @pytest.mark.parametrize("task", TASK_NAMES)
    def test_schemas_are_strict(self, task: str) -> None:
        """两个 output schema 都必须是 additionalProperties=false。

        严格 schema 是"字段准确率"可评测的前提：模型不能偷偷多加字段。
        """
        for schema in (get_contract(task).output_schema(), get_contract(task).input_schema()):
            assert schema.get("additionalProperties") is False

    @pytest.mark.parametrize("task", TASK_NAMES)
    def test_required_fields_match_properties(self, task: str) -> None:
        schema = get_contract(task).output_schema()
        required = set(schema["required"])
        properties = set(schema["properties"])
        assert required <= properties, "required 里有未声明的字段"
        assert required, "required 不应为空"

    def test_event_eval_required_fields(self) -> None:
        contract = get_contract(EVENT_EVAL)
        assert contract.required_fields == (
            "direction",
            "impact",
            "activation",
            "uncertainty",
            "relation_signal",
            "responsibility",
            "confidence",
            "evidence",
        )

    def test_emotion_explain_required_fields(self) -> None:
        contract = get_contract(EMOTION_EXPLAIN)
        assert set(contract.required_fields) == {
            "experience",
            "focus",
            "conflict",
            "impulse",
            "inhibition",
            "expression",
        }

    def test_event_eval_enums_match_design_doc(self) -> None:
        """方向与关系信号的取值必须与架构文档 §8 一致。"""
        enums = get_contract(EVENT_EVAL).enums
        assert enums["direction"] == ["+", "-", "0", "+-"]
        assert enums["relation_signal"] == [
            "strong_approach",
            "slight_approach",
            "neutral",
            "slight_distance",
            "strong_distance",
        ]
        assert enums["responsibility"] == [
            "self",
            "other",
            "situation",
            "shared",
            "unclear",
        ]

    def test_numeric_bounds_are_unit_interval(self) -> None:
        contract = get_contract(EVENT_EVAL)
        for field in contract.numeric_fields:
            assert contract.numeric_bounds(field) == (0.0, 1.0)


class TestContracts:
    def test_get_contract_rejects_unknown_task(self) -> None:
        with pytest.raises(KeyError, match="未知任务"):
            get_contract("not_a_task")

    def test_all_contracts_covers_every_task(self) -> None:
        assert {item.name for item in all_contracts()} == set(TASK_NAMES)

    @pytest.mark.parametrize("task", TASK_NAMES)
    def test_system_prompt_is_substantive(self, task: str) -> None:
        prompt = get_contract(task).system_prompt
        assert len(prompt) > 80
        assert "JSON" in prompt
        # 必须明确禁止多余文字，否则小模型会输出解释
        assert "不要" in prompt

    def test_event_eval_prompt_forbids_emotion_values(self) -> None:
        """§8.1：事件评价器不得输出最终情绪值。"""
        prompt = get_contract(EVENT_EVAL).system_prompt
        assert "情绪" in prompt
        assert "0.82" in prompt or "强度" in prompt

    def test_emotion_prompt_states_hard_constraints(self) -> None:
        """§11.3：不得放大/压低、不得生成台词、不得决定行为。"""
        prompt = get_contract(EMOTION_EXPLAIN).system_prompt
        for keyword in ("放大", "台词", "行为"):
            assert keyword in prompt

    @pytest.mark.parametrize("task", TASK_NAMES)
    def test_field_classification_is_complete(self, task: str) -> None:
        """每个 required 字段都应被归入 numeric / categorical / text 之一。"""
        contract = get_contract(task)
        classified = (
            set(contract.numeric_fields)
            | set(contract.categorical_fields)
            | set(contract.text_fields)
        )
        assert set(contract.required_fields) <= classified

    def test_validation_schema_files_are_loadable_json(self) -> None:
        for path in SCHEMA_DIR.glob("*.json"):
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            assert "$schema" in payload
            assert payload.get("type") == "object"
