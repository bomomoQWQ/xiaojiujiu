"""任务契约：把 JSON Schema、系统提示词、不变量绑定到同一个任务名上。

设计原则（来自架构文档 §8.1 / §11.3）：
  * 2B 只输出"事件性质"或"心理语言"，绝不输出最终情绪值；
  * 2B 不决定行为、不写台词、不修改 Runtime 状态；
  * 所有输出必须是可严格校验的 JSON。

本模块是纯数据 + 纯函数，不依赖任何第三方网络或 ML 库，
因此离线测试可以直接 import。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

EVENT_EVAL = "event_eval"
EMOTION_EXPLAIN = "emotion_explain"
TASK_NAMES: tuple[str, ...] = (EVENT_EVAL, EMOTION_EXPLAIN)


# --------------------------------------------------------------------------
# 系统提示词
# --------------------------------------------------------------------------
# 刻意简短、单一定义来源：小模型对长指令的遵循度差，且上下文要短（§74.2）。

EVENT_EVAL_SYSTEM = (
    "你是角色 Runtime 的事件评价器，只做一件事：判断『这件事对角色是什么性质』。\n"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要额外文字。\n"
    "严格使用以下字段，不得增删字段：\n"
    '{"direction":"+|-|0|+-","impact":0~1,"activation":0~1,"uncertainty":0~1,'
    '"relation_signal":"strong_approach|slight_approach|neutral|slight_distance|strong_distance",'
    '"responsibility":"self|other|situation|shared|unclear","confidence":0~1,"evidence":"输入的简短依据"}\n'
    "约束：\n"
    "1. 不要输出具体情绪名称或情绪强度（不要出现 嫉妒/愤怒=0.82 这类内容）。\n"
    "2. 只依据输入，不得引入输入中没有的事实。\n"
    "3. impact 接近 0 时 direction 应为 \"0\"；正负都有时用 \"+-\"。\n"
    "4. 信息不足时提高 uncertainty、降低 confidence，不要编造。\n"
    "5. evidence 必须是输入中出现过的文字片段，20~40 字。"
)

EMOTION_EXPLAIN_SYSTEM = (
    "你是角色 Runtime 的情绪解释器：把已经存在的结构化心理状态翻译成第一人称心理语言。\n"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要额外文字。\n"
    "严格使用以下字段，不得增删字段：\n"
    '{"experience":"感受","focus":"在意什么","conflict":"内心冲突",'
    '"impulse":"冲动倾向","inhibition":"节制","expression":"表达风格"}\n'
    "硬约束：\n"
    "1. 不得创造输入中不存在的事件。\n"
    "2. 不得放大轻微情绪，也不得降低或提高底层情绪；强度必须与输入一致。\n"
    "3. 不得决定最终行为，不得生成台词，不得修改任何状态。\n"
    "4. 每个字段一句话，20~40 字，不要出现数字。\n"
    "5. 没有冲突就不要虚构冲突。"
)


@dataclass(frozen=True)
class TaskContract:
    """单个任务的完整契约。"""

    name: str
    output_schema_file: str
    input_schema_file: str
    system_prompt: str
    #: 数值字段（用于评测 MAE / 误差统计）
    numeric_fields: tuple[str, ...]
    #: 类别字段（用于评测准确率）
    categorical_fields: tuple[str, ...]
    #: 文本字段（用于评测文本约束）
    text_fields: tuple[str, ...]

    def output_schema(self) -> dict[str, Any]:
        return _load_json(SCHEMA_DIR / self.output_schema_file)

    def input_schema(self) -> dict[str, Any]:
        return _load_json(SCHEMA_DIR / self.input_schema_file)

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(self.output_schema().get("required", ()))

    @property
    def allowed_fields(self) -> frozenset[str]:
        return frozenset(self.output_schema().get("properties", {}))

    @property
    def enums(self) -> dict[str, list[str]]:
        props = self.output_schema().get("properties", {})
        return {
            key: list(spec["enum"])
            for key, spec in props.items()
            if isinstance(spec, dict) and "enum" in spec
        }

    def numeric_bounds(self, name: str) -> tuple[float, float]:
        spec = self.output_schema()["properties"][name]
        return float(spec.get("minimum", 0.0)), float(spec.get("maximum", 1.0))


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


CONTRACTS: dict[str, TaskContract] = {
    EVENT_EVAL: TaskContract(
        name=EVENT_EVAL,
        output_schema_file="event_eval.schema.json",
        input_schema_file="event_eval.input.schema.json",
        system_prompt=EVENT_EVAL_SYSTEM,
        numeric_fields=("impact", "activation", "uncertainty", "confidence"),
        categorical_fields=("direction", "relation_signal", "responsibility"),
        text_fields=("evidence",),
    ),
    EMOTION_EXPLAIN: TaskContract(
        name=EMOTION_EXPLAIN,
        output_schema_file="emotion_explain.schema.json",
        input_schema_file="emotion_explain.input.schema.json",
        system_prompt=EMOTION_EXPLAIN_SYSTEM,
        numeric_fields=(),
        categorical_fields=(),
        text_fields=("experience", "focus", "conflict", "impulse", "inhibition", "expression"),
    ),
}


def get_contract(task: str) -> TaskContract:
    try:
        return CONTRACTS[task]
    except KeyError as exc:  # pragma: no cover - 明确报错路径
        raise KeyError(
            f"未知任务 {task!r}，可用任务：{', '.join(TASK_NAMES)}"
        ) from exc


def all_contracts() -> Iterable[TaskContract]:
    return (CONTRACTS[name] for name in TASK_NAMES)
