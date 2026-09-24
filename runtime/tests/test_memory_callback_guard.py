"""同一条记忆不能被反复拿去当开场；日上限也不能读错量。

实测（`qq06`）：他的全部素材只有一句「有呀有呀」，她却发了 5 条主动，其中 4 条在说
这一句（+6h、+14h、+26h、+34h）—— 因为 `_memory_candidate` 每一轮都从同一条激活记忆再生，
而没有任何地方记得"这条已经说过了"：

* 候选上那句 ``constraints=["不要重复追问同一件事"]`` 只是**给模型看的一句话**，没有检查；
* ``repeat_cost`` 的窗口是 ``utility.repeat_window_seconds``（当时 1 小时），而重复跨了几小时
  到一天 → 那个乘子在 2.5 天的 8,949 行 utility 里恒为 0.000。

修法分两层：材料层面（``candidate.memory_callback_cooldown_hours``：这条记忆最近说过就不再
拿它当开场）与窗口层面（1h → 6h）。另外顺手修掉一处读错的量：``motivation.decide`` 的日上限
原本读的是 ``inputs.recent_contacts``（窗口内接触次数）而不是 ``state.contact_count_today``
（今天发了几条）—— 窗口一拉长它就会把"今天"算错，`authorize.py` 一直用的是后者。
"""

from __future__ import annotations

import random
from datetime import timedelta

from companion_runtime import candidate as candidate_module
from companion_runtime import motivation
from companion_runtime.runtime import Runtime
from companion_runtime.typing import CandidateIntent, RuntimeState
from companion_runtime.user_model import Prediction

from conftest import BASE_TIME, build_config


def _runtime(**overrides: object) -> Runtime:
    config = build_config(**overrides)
    config.conversation_id = "default"
    return Runtime(config=config, seed=1234, created_at=BASE_TIME)


def _activated_memory(runtime: Runtime) -> tuple[candidate_module.ActivatedMemory, object]:
    """Consolidate one durable preference and put it into the working set."""
    runtime.process_user_message(
        content="我喜欢喝手冲咖啡，不加糖。", timestamp=BASE_TIME
    )
    runtime.consolidate(now=BASE_TIME + timedelta(minutes=5))
    memories = runtime.projections.memory.list_memories(status=None)
    assert memories, "没有记忆，这条测试就没测到东西"
    memory = memories[0]
    activation = candidate_module.ActivatedMemory(memory_id=memory.memory_id, activation=0.9)
    with runtime.db.transaction() as conn:
        runtime.projections.memory.upsert_activation(conn, activation)
    return activation, memory


def _generate(runtime: Runtime, activation: object, memory: object, *, spoken: list[str]):
    return candidate_module.generate(
        state=runtime.state(),
        config=runtime.config,
        activated=[(activation, memory)],
        now=BASE_TIME + timedelta(minutes=10),
        recently_spoken_memories=spoken,
    )


def test_a_memory_used_as_an_opener_is_not_offered_again() -> None:
    runtime = _runtime()
    try:
        activation, memory = _activated_memory(runtime)
        source = f"{candidate_module.MEMORY_SOURCE_PREFIX}{memory.memory_id}"

        fresh = _generate(runtime, activation, memory, spoken=[])
        assert any(source in item.sources for item in fresh), [
            item.sources for item in fresh
        ]

        again = _generate(runtime, activation, memory, spoken=[memory.memory_id])
        assert not any(source in item.sources for item in again), [
            item.sources for item in again
        ]
    finally:
        runtime.close()


def test_the_guard_only_applies_inside_the_cooldown_window() -> None:
    runtime = _runtime()
    try:
        activation, memory = _activated_memory(runtime)
        source = f"{candidate_module.MEMORY_SOURCE_PREFIX}{memory.memory_id}"
        other = _generate(runtime, activation, memory, spoken=["mem_something_else"])
        assert any(source in item.sources for item in other)
    finally:
        runtime.close()


def test_the_runtime_remembers_which_memory_a_sent_message_used() -> None:
    """候选把来源记成 ``memory:<id>``，所以一次 sent 就够查出"这条已经说过"。"""
    runtime = _runtime()
    try:
        candidate = CandidateIntent(
            candidate_id="cnd_memory_callback",
            type="curious_question",
            intent="聊起之前记过的事：有呀有呀",
            goal="延续共同经历",
            target="episodic",
            sources=[f"{candidate_module.MEMORY_SOURCE_PREFIX}mem_used"],
            status="new",
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        with runtime.db.transaction() as conn:
            runtime.projections.candidates.upsert(conn, candidate)
            outbox_id = runtime.projections.outbox.enqueue(
                conn,
                _send_outbox(runtime, "在忙吗？"),
            )
            _record_attempt(runtime, conn, outbox_id, candidate.candidate_id)
        assert runtime._recently_spoken_memory_ids(now=BASE_TIME + timedelta(hours=1)) == {
            "mem_used"
        }
        # 窗口之外的那次不算：48 小时以后它又可以被拿来说了。
        assert (
            runtime._recently_spoken_memory_ids(now=BASE_TIME + timedelta(hours=72)) == set()
        )
    finally:
        runtime.close()


def _send_outbox(runtime: Runtime, text: str):
    from companion_runtime.typing import OutboxItem, new_id

    return OutboxItem(
        outbox_id=new_id("outbox"),
        kind="send",
        payload={"text": text},
        status="delivered",
        conversation_id="default",
    )


def _record_attempt(runtime: Runtime, conn, outbox_id: str, candidate_id: str) -> None:
    from companion_runtime.typing import ActionAttempt, new_id

    runtime.projections.attempts.upsert(
        conn,
        ActionAttempt(
            attempt_id=new_id("attempt"),
            candidate_id=candidate_id,
            state="sent",
            intent="问候一下",
            outbox_id=outbox_id,
            # 时间钉在测试时间线上：默认会取真实 now，那样"48 小时以后"的断言就永远
            # 落在过去，窗口边界根本没被测到。
            created_at=BASE_TIME,
            committed_at=BASE_TIME,
        ),
    )


def _contact_candidate() -> CandidateIntent:
    return CandidateIntent(
        candidate_id="cnd_contact",
        type="contact",
        intent="没有具体事项，只是想和用户建立联系",
        goal="维持关系的连续性",
        target="relationship",
        status="new",
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def _assess(*, contact_count_today: int, recent_contacts: int):
    config = build_config()
    state = RuntimeState()
    state.contact_count_today = contact_count_today
    item = _contact_candidate()
    result = motivation.decide(
        motivation.MotivationInputs(
            state=state,
            candidates=[item],
            predictions={item.candidate_id: Prediction()},
            boundary_allow_proactive=True,
            boundary_risk_baseline=0.0,
            recent_contacts=recent_contacts,
            hours_since_contact=30.0,
            cooldown_active=False,
            now=BASE_TIME,
            elapsed_seconds=3600.0,
        ),
        config=config,
        rng=random.Random(1),
    )
    return result.assessments[0].breakdown


def test_the_daily_budget_counts_the_day_not_the_burst_window() -> None:
    """日上限读的是"今天发了几条"，不是"最近这个窗口里接触了几次"。"""
    limit = build_config().drive.max_contacts_per_day

    spent = _assess(contact_count_today=limit, recent_contacts=0)
    assert spent.blocked and spent.block_reason == "daily_contact_budget_exhausted"

    # 反过来：窗口里接触很多、但今天一条没发 —— 不能判成"今日额度用完"（修前会 ✗）。
    burst = _assess(contact_count_today=0, recent_contacts=limit)
    assert burst.block_reason != "daily_contact_budget_exhausted"


def test_the_repeat_window_covers_a_day_of_repeats_not_an_hour() -> None:
    """1 小时的窗口量不到"隔几小时又说一遍"，所以它必须是小时级。"""
    assert build_config().utility.repeat_window_seconds >= 3600.0 * 6
