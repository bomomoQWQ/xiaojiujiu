"""时间的锚：可查证，而不是禁止；而且锚只给**主模型**。

设计上时间只有两个来源——控制时钟的外接框架，和主动消息自己的演出提示词。语义模型
（深层整理器）**一个字的时间都不碰**：它的输入、提示词、产出都和以前完全一样。时间的事
全在主模型这一侧做完：

* **引用用户原话的地方都带他说的时刻**（``context.said_at_resolver``）——
  "用户在 11-14 19:36 说：我明天要去面试"，于是"明天"= 11-15，隔多久都算得清、查得到；
* **草稿带它自己动笔的时刻**（``api_v1.RENDER_DRAFT_PREFIX``，代码给，不是模型写的）；
* 演出提示词同时给出「现在是」和「写于」，草稿含相对时刻时另外**点名**是哪个词要换算
  （``api_v1.RENDER_DRIFT_HINT``）；
* 只有"什么时候说"这类动力学时刻一个都不给模型：那是代码算的。

锚必须由代码给，而且必须是**那句话**的时刻——所以候选另有一个 ``wording_at``：刷新每轮都
会合并数字进活着的候选，行被碰过很多次，措辞却没动。取错一个，五小时前那句"中午"看起来
就跟刚写的一样，要查的偏差正好被抹掉。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from companion_runtime import candidate as candidate_module
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME


# --------------------------------------------------------------------------------------
# 词表：认出"锚点相关"的时段，别把事实当时刻
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("约他明天中午去那家面馆", "明天"),
        ("跟他说早呀，问他那边是不是刚天亮", "早呀"),
        ("他上周去了青岛", "上周"),
        ("下午三点见", "下午"),
        ("问他 15:30 那场面试", "15:30"),
    ],
)
def test_anchor_dependent_time_is_recognised(text: str, expected: str) -> None:
    assert candidate_module.time_expression_in(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # 事实，不是时刻：日历日期说的是"他是谁"，抹掉或标记都是篡改事实。
        "他家在山东，生日是三月三号",
        # 这些不是钟点。
        "有一点点想他",
        "差一点就说了",
        "谈一谈他最近有点累这件事",
    ],
)
def test_a_fact_is_not_flagged_as_a_moment(text: str) -> None:
    assert candidate_module.time_expression_in(text) == ""


# --------------------------------------------------------------------------------------
# 锚：引用用户原话的地方都带他说的时刻
# --------------------------------------------------------------------------------------


def _runtime(**overrides: object) -> Runtime:
    from conftest import build_config

    return Runtime(config=build_config(**overrides), seed=4321, created_at=BASE_TIME)


def test_a_quoted_fact_names_the_moment_the_user_said_it() -> None:
    """块里的引用写成"用户在 …… 说：…"——没有它"明天"隔一天就没法读，也没法查。"""
    from companion_runtime import context as context_module

    runtime = _runtime()
    try:
        runtime.process_user_message(
            content="我明天下午三点面试，结束了告诉你。", timestamp=BASE_TIME
        )
        block = context_module.render_block(
            context_module.build(runtime=runtime, now=BASE_TIME + timedelta(hours=5))
        )
        local = BASE_TIME.astimezone()
        expected = f"用户在 {local.strftime('%Y-%m-%d %H:%M')}"
        assert expected in block, block
        assert "我明天下午三点面试" in block
        # Same readable form the render states its clock in, or the two cannot be compared.
        assert any(day in block for day in ("周一", "周二", "周三", "周四", "周五", "周六", "周日"))
    finally:
        runtime.close()


def test_a_memory_is_anchored_at_the_utterance_not_at_consolidation() -> None:
    """记忆的锚是"他那句话是什么时候说的"，不是"角色什么时候整理出来的"。"""
    from companion_runtime import context as context_module

    runtime = _runtime()
    try:
        runtime.process_user_message(
            content="我喜欢喝手冲咖啡，不加糖。", timestamp=BASE_TIME
        )
        # 整理发生在五小时之后：锚仍应指向 BASE_TIME。
        runtime.consolidate(now=BASE_TIME + timedelta(hours=5))
        bundle = context_module.build(runtime=runtime, now=BASE_TIME + timedelta(hours=6))
        assert bundle.memories, "没有记忆可选，这条测试就没测到东西"
        local = BASE_TIME.astimezone()
        for memory in bundle.memories:
            assert memory["said_at"].startswith(local.strftime("%Y-%m-%d %H:%M")), memory
        block = context_module.render_block(bundle)
        assert "（他在 " in block, block
    finally:
        runtime.close()


def test_an_unresolvable_source_degrades_to_no_attribution() -> None:
    """锚解不出来就不写锚——提示词永远不该是这一轮失败的原因。"""
    from companion_runtime import context as context_module

    runtime = _runtime()
    resolve = context_module.said_at_resolver(runtime)
    assert resolve("") == ""
    assert resolve("evt_does_not_exist") == ""


def test_the_block_never_states_a_clock() -> None:
    """锚是"他说这句话的时刻"；"现在几点"不在这块里——那只有演出那一段说。"""
    from companion_runtime import context as context_module

    runtime = _runtime()
    try:
        runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
        block = context_module.render_block(
            context_module.build(runtime=runtime, now=BASE_TIME + timedelta(hours=9))
        )
        assert "当前本地时间" not in block
        assert "- 现在是：" not in block
        # 认知性的时长留着：它不是钟。
        assert "距离上次用户消息" in block
    finally:
        runtime.close()


def test_the_semantic_model_is_not_touched_by_any_of_this() -> None:
    """语义模型那边的输入一个字段都没变——时间的事全在主模型这一侧做完。

    这条是防回归的：上一版把锚塞进了深层刷新的请求里，被否掉了。请求里不该出现
    ``said_at`` 这类为提示词准备的字段，它的形状由 ``providers`` 的契约说了算。
    """
    from companion_runtime import deep_refresh as dr

    runtime = _runtime()
    try:
        runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
        payload = dr.build_request(runtime=runtime, now=BASE_TIME).to_dict()
        assert "said_at" not in json.dumps(payload, ensure_ascii=False)
        # 它原来拿到的原始时间戳照旧（那是它自己的契约，不是我们加的）。
        assert payload["key_quotes"][0]["timestamp"]
    finally:
        runtime.close()


# --------------------------------------------------------------------------------------
# 草稿的锚：是"这几个字什么时候写的"，不是"这行什么时候被碰过"
# --------------------------------------------------------------------------------------


def test_a_refresh_does_not_move_the_drafts_anchor() -> None:
    """刷新每一轮都会合并进活着的候选并改写它的数字——措辞没动，锚就不能动。

    否则渲染看到的是"刚写的"，于是五小时前那句"中午"看起来毫无偏差——正好把要查的东西
    查没了。所以候选另有一个 ``wording_at``：只有真的改写措辞（intent/goal/constraints）
    才会移动。这也是新加那个字段的全部理由。
    """
    from companion_runtime import candidate as candidate_module

    runtime = _runtime()
    try:
        runtime.process_user_message(content="我明天下午三点面试，结束了告诉你。", timestamp=BASE_TIME)
        runtime.apply_candidate_operations(
            [
                candidate_module.CandidateOperation(
                    op="add",
                    candidate={
                        "type": "share",
                        "intent": "问他明天中午还去不去那家店",
                        "sources": ["internal_approach_drive"],
                    },
                )
            ],
            now=BASE_TIME,
            source="test",
        )
        stated = next(
            item for item in runtime.projections.candidates.list_active()
            if "那家店" in item.intent
        )
        assert stated.wording_at == BASE_TIME
        assert stated.created_at == BASE_TIME

        # A refresh-shaped merge: numbers and sources, never the words.
        runtime.apply_candidate_operations(
            [
                candidate_module.CandidateOperation(
                    op="update",
                    candidate_id=stated.candidate_id,
                    patch={"confidence": 0.9, "internal_need": 0.9, "sources": ["internal_approach_drive"]},
                )
            ],
            now=BASE_TIME + timedelta(hours=5),
            source="test",
        )
        after = runtime.projections.candidates.get(stated.candidate_id)
        assert after.wording_at == BASE_TIME, "合并数字把措辞的锚也挪了"
        assert after.updated_at > BASE_TIME, "行被碰过，updated_at 该动"

        # Rewriting the words *is* a rewrite, and the anchor moves with it.
        runtime.apply_candidate_operations(
            [
                candidate_module.CandidateOperation(
                    op="update",
                    candidate_id=stated.candidate_id,
                    patch={"intent": "问问他面试怎么样"},
                )
            ],
            now=BASE_TIME + timedelta(hours=6),
            source="test",
        )
        rewritten = runtime.projections.candidates.get(stated.candidate_id)
        assert rewritten.wording_at == BASE_TIME + timedelta(hours=6)
    finally:
        runtime.close()


def test_the_render_states_the_wording_moment_not_the_row_moment() -> None:
    """同一个语义，落到渲染提示词上：``- 写于：`` 读的是措辞的时刻。"""
    from datetime import datetime, timezone

    from companion_runtime.api_v1 import _draft_written_at

    runtime = _runtime()
    try:
        runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
        from companion_runtime import candidate as candidate_module

        runtime.apply_candidate_operations(
            [
                candidate_module.CandidateOperation(
                    op="add",
                    candidate={
                        "type": "share",
                        "intent": "问他那家店还去不去",
                        "sources": ["internal_approach_drive"],
                    },
                )
            ],
            now=BASE_TIME,
            source="test",
        )
        stated = runtime.projections.candidates.list_active()[0]
        with runtime.db.transaction() as conn:
            state = runtime.projections.runtime.ensure()
            attempt_id, _outbox = runtime._commit_attempt(
                conn, chosen=stated, state=state, now=BASE_TIME + timedelta(hours=5)
            )
        written = _draft_written_at(runtime, {"attempt_id": attempt_id})
        assert written == BASE_TIME, written.astimezone(timezone.utc)
    finally:
        runtime.close()
