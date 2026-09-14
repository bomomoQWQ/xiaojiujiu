"""教师提示词构造：把种子场景变成 DeepSeek 的生成请求。

两种角色：
  * **生成**（generate）：先造一条真实的输入事件（用户/角色对话），
    再给出符合契约的教师输出。
  * **复核**（verify，可选）：对已有输出做一次一致性复核并修正。

输出统一要求为"一个 JSON 对象"，字段与 :mod:`.contracts` 一致，
这样下游可以直接用同一套 schema 校验，不需要单独的解析路径。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from ..contracts import EMOTION_EXPLAIN, EVENT_EVAL, TaskContract, get_contract
from ..errors import ConfigError
from .seeds import SeedScenario

GENERATOR_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# 生成提示词
# --------------------------------------------------------------------------

_COMMON_RULES = """\
你必须只输出一个 JSON 对象（不要 markdown 代码块、不要任何解释文字），字段如下：
{schema_inline}

通用要求：
- 所有文本使用简体中文，自然、口语化，不要像说明书。
- 不要出现具体人名、真实机构名、真实地名；用"用户""角色"或泛称。
- 不要色情、暴力、违法内容；不要出现真实联系方式。
- 数值要有区分度，不要每条都给 0.5 或 0.8。
"""

_EVENT_EVAL_TASK = """\
你要为"角色 Runtime 的事件评价器"造训练数据。

第一步：造一条**当前事件**（以及可选的最多 3 轮上下文），要像真实聊天记录的一小段。
第二步：给出对该事件的评价 JSON。

事件骨架（必须遵守，但用你自己的语言写具体内容）：
- 事件类型：{event_label}
- 说明：{event_hint}
- 事件发言人：{speaker}
- 角色当前背景心境：valence={valence}（-1 极负面 ~ 1 极正面）, arousal={arousal}（0 平静 ~ 1 高度激活）
- 角色价值观：{values}
- 可引用的已知事实（0~{fact_count} 条，可不用完）：{facts}
- 上下文轮数：{context_turns}

评价 JSON 字段与含义：
- direction: "+" 正向 / "-" 负向 / "0" 中性 / "+-" 正负混合
- impact: 0~1 影响程度（幅度，与方向无关）
- activation: 0~1 激活/紧绷程度
- uncertainty: 0~1 事件含义与走向的不确定性
- relation_signal: strong_approach / slight_approach / neutral / slight_distance / strong_distance
- responsibility: self / other / situation / shared / unclear
- confidence: 0~1 对本次评价的自信程度
- evidence: 20~40 字，必须取自你写的事件文本片段

硬性要求：
1. **绝对不要**输出任何具体情绪名称或情绪强度（不要出现"嫉妒""愤怒"，不要出现"嫉妒=0.82"）。
   事件评价只回答"这件事是什么性质"，最终情绪由 Runtime 代码计算。
2. 同一事件在不同角色价值观下影响不同：请让 impact/relation_signal 与上面给的价值观画像一致
   （例如 relatedness 高 + 用户抽身 → 负向更明显；autonomy/boundary 高 → 负向更轻）。
3. direction=0 时 impact 必须很小（<=0.2）；direction="+-" 时 uncertainty 不应很低。
4. 不得引入上面事实/事件之外的新信息。

输出格式（只输出这一个对象，包含两部分）：
{{"input": {{"current_event": {{"speaker": "...", "text": "..."}}, "context_turns": [{{"speaker": "user|char", "text": "..."}}], "background_mood": {{"valence": {valence}, "arousal": {arousal}}}, "character_values": {{...}}, "known_facts": ["..."]}}, "output": {{"direction": "...", "impact": 0.0, "activation": 0.0, "uncertainty": 0.0, "relation_signal": "...", "responsibility": "...", "confidence": 0.0, "evidence": "..."}}}}

其中 background_mood 与 character_values 必须与上面给定值完全一致。
"""

_EMOTION_EXPLAIN_TASK = """\
你要为"角色 Runtime 的情绪解释器"造训练数据。

第一步：造一小段**对话片段**（角色说一句、用户回一句），要像真实聊天。
第二步：构造该时刻**已经存在的结构化心理状态**（这是 Runtime 算好的，不是你要评价的）。
第三步：把这个状态翻译成第一人称心理语言。

事件骨架（必须遵守）：
- 事件类型：{event_label}
- 说明：{event_hint}
- 背景心境：valence={valence}, arousal={arousal}
- 主导情绪：target={target}, direction={direction}, intensity={intensity}
- 是否存在内心冲突：{conflict_present}（{conflict_hint}）
- approach_drive（靠近冲动）= {approach}，restraint（节制）= {restraint}
- 另外 {extra_emotions} 条次要情绪（若无则为空数组）

心理语言 JSON 字段（每个字段一句话，20~40 字）：
- experience: 第一人称感受
- focus: 此刻最在意什么
- conflict: 内心冲突结构
- impulse: 冲动/倾向
- inhibition: 节制/抑制
- expression: 表达的总体风格取向

硬性要求：
1. 强度必须与 intensity/valence/arousal 相符：**不得放大轻微情绪**，也**不得压低或抬高**底层情绪。
   例如 intensity=0.32（轻微）就不要写"非常难受""几乎崩溃"。
2. 不得创造输入中不存在的事件；只能围绕上面这段对话和心理状态写。
3. 不得决定最终行为，不得生成台词，不得出现引号、括号动作描写（如"（笑）"）。
4. **不得出现任何数字**，不要自己发明情绪强度值。
5. 没有冲突就不要虚构冲突（conflict_present=false 时，conflict 字段要说明当前不构成冲突）。
6. impulse 的方向必须与 approach_drive 一致；inhibition 必须与 restraint 一致。

输出格式（只输出这一个对象，包含两部分）：
{{"input": {{"event": {{"char": "...", "user": "..."}}, "background_mood": {{"valence": {valence}, "arousal": {arousal}}}, "active_emotions": [{{"target": "{target}", "cause": "...", "direction": "{direction}", "intensity": {intensity}, "semantic_label": null, "action_tendency": "..."}}], "approach_drive": {approach}, "restraint": {restraint}, "conflict_present": {conflict_present}}}, "output": {{"experience": "...", "focus": "...", "conflict": "...", "impulse": "...", "inhibition": "...", "expression": "..."}}}}

注意：
- active_emotions 里 direction/intensity 必须与上面给定值完全一致。
- semantic_label 允许为 null（系统可能只知道"中等负向影响"而尚不知具体情绪名）。
- conflict_present 必须与上面给定值完全一致。
"""

_VERIFY_TASK = """\
下面是一条"事件评价/情绪解释"训练样本。请检查它是否满足硬约束，若违反就修正。

契约（输出字段）：
{schema_inline}

必须满足：
1. 输出只含契约字段，类型与取值域正确。
2. 事件评价不得输出具体情绪名称或情绪强度值。
3. 情绪解释的强度必须与输入一致：不得放大轻微情绪，不得压低强烈情绪。
4. 情绪解释不得出现数字、引号、台词、括号动作描写。
5. 情绪解释不得决定行为；不得创造输入中不存在的事件。
6. 输出的情感方向必须与输入的情绪方向一致。

待检查样本：
{sample_json}

请只输出修正后的 output JSON 对象（不要 input，不要解释）。如果原样本已经合规，原样输出它的 output。
"""


def inline_schema(contract: TaskContract) -> str:
    """把输出 schema 压成一行紧凑描述，给模型当字段清单。"""
    schema = contract.output_schema()
    parts: list[str] = []
    for name, spec in schema.get("properties", {}).items():
        if "enum" in spec:
            type_desc = "|".join(str(item) for item in spec["enum"])
        elif spec.get("type") == "number":
            type_desc = f"{spec.get('minimum', 0)}~{spec.get('maximum', 1)}"
        elif spec.get("type") == "string":
            bounds = []
            if "minLength" in spec:
                bounds.append(f"min {spec['minLength']}")
            if "maxLength" in spec:
                bounds.append(f"max {spec['maxLength']}")
            type_desc = "string" + (f"({', '.join(bounds)})" if bounds else "")
        else:
            type_desc = str(spec.get("type", "any"))
        parts.append(f"{name}: {type_desc}")
    return "{" + ", ".join(parts) + "}"


def build_generation_messages(
    task: str,
    scenario: SeedScenario,
    *,
    contract: TaskContract | None = None,
) -> list[dict[str, str]]:
    """构造一次生成请求的消息列表。"""
    contract = contract or get_contract(task)
    if task == EVENT_EVAL:
        body = _render_event_eval(scenario, contract)
    elif task == EMOTION_EXPLAIN:
        body = _render_emotion_explain(scenario, contract)
    else:  # pragma: no cover - get_contract 已经拦住了
        raise ConfigError(f"不支持的生成任务：{task!r}")

    return [
        {"role": "system", "content": contract.system_prompt},
        {"role": "user", "content": body},
    ]


def _render_event_eval(scenario: SeedScenario, contract: TaskContract) -> str:
    payload = scenario.payload
    mood = payload.get("background_mood", {"valence": 0.0, "arousal": 0.3})
    values = payload.get("character_values", {})
    facts = payload.get("known_facts", [])
    facts_text = "；".join(facts) if facts else "（无）"
    return _EVENT_EVAL_TASK.format(
        event_label=scenario.event_label,
        event_hint=scenario.event_hint,
        speaker=payload.get("speaker", "user"),
        valence=mood.get("valence", 0.0),
        arousal=mood.get("arousal", 0.3),
        values=json.dumps(values, ensure_ascii=False),
        fact_count=len(facts),
        facts=facts_text,
        context_turns=payload.get("context_turns", 0),
        schema_inline=inline_schema(contract),
    )


def _render_emotion_explain(scenario: SeedScenario, contract: TaskContract) -> str:
    payload = scenario.payload
    mood = payload.get("background_mood", {"valence": 0.0, "arousal": 0.3})
    primary = payload.get("primary", {})
    conflict_present = payload.get("conflict_present", True)
    return _EMOTION_EXPLAIN_TASK.format(
        event_label=scenario.event_label,
        event_hint=scenario.event_hint,
        valence=mood.get("valence", 0.0),
        arousal=mood.get("arousal", 0.3),
        target=primary.get("target", "user"),
        direction=primary.get("direction", "-"),
        intensity=primary.get("intensity", 0.5),
        conflict_present="true" if conflict_present else "false",
        conflict_hint=primary.get("action_tendency_hint", ""),
        approach=payload.get("approach_drive", 0.5),
        restraint=payload.get("restraint", 0.5),
        extra_emotions=payload.get("extra_emotions", 0),
        schema_inline=inline_schema(contract),
    )


def build_verify_messages(
    task: str,
    model_input: Mapping[str, Any],
    output: Mapping[str, Any],
    *,
    violations: Sequence[str] = (),
    contract: TaskContract | None = None,
) -> list[dict[str, str]]:
    """构造一次复核请求。``violations`` 会作为额外提示。"""
    contract = contract or get_contract(task)
    sample = {"task": task, "input": model_input, "output": output}
    body = _VERIFY_TASK.format(
        schema_inline=inline_schema(contract),
        sample_json=json.dumps(sample, ensure_ascii=False, indent=1),
    )
    if violations:
        body += "\n已知问题（必须修正）：\n- " + "\n- ".join(violations)
    return [
        {"role": "system", "content": contract.system_prompt},
        {"role": "user", "content": body},
    ]
