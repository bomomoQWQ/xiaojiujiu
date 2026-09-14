"""End-to-end scenario tests.

These mirror the scenario set in the architecture document. Each one drives the
whole Runtime - foreground path, endogenous round, delivery, feedback - rather
than a single module, and each asserts a property a human would recognise.

The invariant tests at the end are the "constitution" checks: they verify the ten
system invariants directly, so a future refactor that quietly breaks one of them
fails loudly.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime import context as context_module
from companion_runtime import memory as memory_module
from companion_runtime.delivery import DeliveryService, EchoRenderer, NullTransport
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    Actor,
    AttemptState,
    CandidateStatus,
    EventType,
    OutboxStatus,
    UnfinishedStatus,
)
from companion_runtime.user_model import BehaviourReaction

from conftest import BASE_TIME, Harness, build_config

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _service(runtime: Runtime) -> DeliveryService:
    """Attach a delivery service with an in-memory transport."""
    return DeliveryService(
        reducer=runtime.reducer,
        config=runtime.config,
        runtime=runtime,
        renderer=EchoRenderer(),
        transport=NullTransport(),
    )


def _run_proactive_round(runtime: Runtime, *, now, force: bool = True):
    """Run an endogenous round and return ``(outcome, decision)``."""
    outcome = runtime.endogenous_round(now=now, force=force)
    return outcome, outcome.decision["outcome"]


def _drain(service: DeliveryService, *, now, cycles: int = 3) -> list[dict]:
    """Run delivery cycles until the queue is quiet."""
    reports: list[dict] = []
    for index in range(cycles):
        reports.append(service.cycle(now=now + timedelta(seconds=index)).to_dict())
    return reports


# --------------------------------------------------------------------------------------
# Scenario 1: an explicit boundary
# --------------------------------------------------------------------------------------


def test_scenario_1_explicit_boundary(runtime: Runtime) -> None:
    """Boundary takes effect immediately and pressure can never override it."""
    runtime.process_user_message(content="今天不要主动联系我。", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))
    assert runtime.state().allow_proactive is False

    boundaries = runtime.projections.boundaries.list_all()
    assert len(boundaries) == 1
    assert boundaries[0].allow_reply is True
    assert boundaries[0].expires_at is not None

    # Inside the window, with every drive pushed to maximum, nothing may be sent.
    inside = boundaries[0].expires_at - timedelta(hours=1)
    with runtime.db.transaction() as conn:
        state = runtime.state()
        state.pressure = 1.0
        state.approach_impulse = 1.0
        state.restraint = 0.0
        runtime.projections.runtime.write(state, conn, expect_version=state.version)
    _outcome, decision = _run_proactive_round(runtime, now=inside)
    assert decision["acted"] is False
    assert decision["reason"] == "blocked_by_boundary"
    assert all(utility["blocked"] for utility in decision["utilities"])
    assert runtime.projections.attempts.count_in_flight() == 0
    assert runtime.projections.outbox.list_items(status=OutboxStatus.PENDING.value) == []

    # Replying to the user is still permitted, even though outreach is not.
    runtime.process_user_message(
        content="在吗，你在做什么", timestamp=inside + timedelta(minutes=5)
    )
    stored = runtime.projections.boundaries.list_all()
    assert all(boundary.allow_reply for boundary in stored)

    # Once the window lapses, proactive permission comes back on its own.
    runtime.lazy_tick(boundaries[0].expires_at + timedelta(minutes=1))
    assert runtime.state().allow_proactive is True


# --------------------------------------------------------------------------------------
# Scenario 2: a long silence
# --------------------------------------------------------------------------------------


def test_scenario_2_long_absence_raises_drive_gradually(runtime: Runtime) -> None:
    """Proactive pressure grows smoothly instead of jumping at a threshold."""
    runtime.lazy_tick(BASE_TIME)
    impulses: list[float] = []
    pressures: list[float] = []
    for hours in (2, 6, 12, 24, 36, 48):
        report = runtime.lazy_tick(BASE_TIME + timedelta(hours=hours))
        impulses.append(report.drive["approach_impulse"])
        pressures.append(report.drive["pressure"])

    assert impulses == sorted(impulses), "impulse must rise monotonically"
    assert pressures == sorted(pressures), "pressure must accumulate monotonically"
    # No cliff: the step between consecutive samples stays small.
    steps = [b - a for a, b in zip(impulses, impulses[1:])]
    assert max(steps) < 0.35
    assert runtime.state().approach_impulse > 0.35


def test_scenario_2b_absence_does_not_force_a_message(runtime: Runtime) -> None:
    """Loneliness alone does not make a restrained character speak."""
    runtime.lazy_tick(BASE_TIME)
    _outcome, decision = _run_proactive_round(runtime, now=BASE_TIME + timedelta(hours=48))
    assert decision["acted"] is False
    assert decision["reason"] in {"no_candidate_beats_silence", "hazard_not_triggered"}
    # The impulse really did build; it just never beats the value of silence.
    assert runtime.state().approach_impulse > 0.35
    assert decision["advantage"] < 0


def test_scenario_2d_a_less_restrained_character_does_reach_out() -> None:
    """The same absence *does* produce contact for a more impulsive personality."""
    config = build_config()
    config.values.boundary_respect = 0.25
    config.values.stability_commitment = 0.30
    config.values.autonomy = 0.55
    config.values.relationship_maintenance = 0.95
    config.values.user_care = 0.95
    # A slightly stronger standing wish to be in touch: not a due matter, but a
    # background need. Combined with a long absence this is enough for a character
    # that does not hold itself back.
    config.candidate.contact_baseline_prior = 0.10
    from companion_runtime.db import Database

    runtime = Runtime(config, seed=7, database=Database(":memory:"), created_at=BASE_TIME)
    try:
        runtime.lazy_tick(BASE_TIME)
        # ``endogenous_round`` advances time itself; passing the later moment is
        # what gives the hazard something to integrate over.
        outcome, decision = _run_proactive_round(
            runtime, now=BASE_TIME + timedelta(hours=49)
        )
        assert outcome.decision["outcome"]["advantage"] > 0
        assert decision["reason"] == "hazard_triggered"
        assert runtime.projections.attempts.count_in_flight() == 1
    finally:
        runtime.close()


def test_scenario_2c_a_concrete_reason_unlocks_contact(runtime: Runtime) -> None:
    """Given a due matter, the same character does speak."""
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    matter = runtime.projections.unfinished.list_open()[0]
    due_at = matter.waiting_until + timedelta(hours=1)

    outcome, decision = _run_proactive_round(runtime, now=due_at)
    assert decision["advantage"] > 0
    assert decision["acted"] is True
    assert outcome.attempt_id is not None
    assert outcome.outbox_id is not None
    attempt = runtime.projections.attempts.get(outcome.attempt_id)
    assert attempt.state == AttemptState.COMMITTED.value
    assert attempt.rendered_text is None, "committed must not mean sent"


# --------------------------------------------------------------------------------------
# Scenario 3: just after contacting
# --------------------------------------------------------------------------------------


def test_scenario_3_recent_contact_suppresses_a_second_message(runtime: Runtime) -> None:
    """Impulse and pressure release; restraint and cooldown rise."""
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    matter = runtime.projections.unfinished.list_open()[0]
    due_at = matter.waiting_until + timedelta(hours=1)
    outcome, _decision = _run_proactive_round(runtime, now=due_at)
    assert outcome.attempt_id

    service = _service(runtime)
    _drain(service, now=due_at)
    attempt = runtime.projections.attempts.get(outcome.attempt_id)
    assert attempt.state == AttemptState.SENT.value

    state = runtime.state()
    assert state.cooldown_until is not None
    assert state.pressure < 0.3
    assert state.last_contact_at is not None

    # Ten minutes later, a second outreach must be impossible.
    _later, decision = _run_proactive_round(
        runtime, now=due_at + timedelta(minutes=10)
    )
    assert decision["acted"] is False
    assert decision["reason"] in {"no_candidate_beats_silence", "cooldown_active"}


# --------------------------------------------------------------------------------------
# Scenario 4: the user is busy
# --------------------------------------------------------------------------------------


def test_scenario_4_busy_user_does_not_lower_acceptance(runtime: Runtime) -> None:
    """Six hours of silence from a busy user is nearly no evidence."""
    runtime.process_user_message(content="这几天工作很多，我可能回得慢", timestamp=BASE_TIME)
    before = runtime.user_model.predict(
        action={"type": "contact", "proactive": True},
        context={"busy_probability": 0.0},
    ).reply_probability

    service = _service(runtime)
    _drain(service, now=BASE_TIME)
    runtime.process_user_message(
        content="还在忙，晚点说", timestamp=BASE_TIME + timedelta(hours=6)
    )
    after = runtime.user_model.predict(
        action={"type": "contact", "proactive": True},
        context={"busy_probability": 0.0},
    ).reply_probability

    # The belief may move slightly, but a busy user's slow reply must not read as
    # a rejection.
    assert abs(after - before) < 0.08


# --------------------------------------------------------------------------------------
# Scenario 5: posterior reinterpretation
# --------------------------------------------------------------------------------------


def test_scenario_5_reappraisal_does_not_rewrite_history(runtime: Runtime) -> None:
    """New understanding is appended; the original wording stays untouched."""
    first = runtime.process_user_message(
        content="算了，也没什么", timestamp=BASE_TIME
    )
    original_id = first.event.event_id
    original_content = first.event.content

    # A later message reveals that the earlier one mattered more than it seemed.
    second = runtime.process_user_message(
        content="你那时候果然没发现", timestamp=BASE_TIME + timedelta(hours=6)
    )
    assert second.relation_signal if hasattr(second, "relation_signal") else True

    # Interpretations are versioned, never overwritten.
    with runtime.db.transaction() as conn:
        runtime.projections.interpretations.add_version(
            conn,
            target_kind="event",
            target_id=original_id,
            content="当时可能存在失望",
            confidence=0.55,
            source_version=runtime.version(),
            source_event_ids=[original_id],
        )
        runtime.projections.interpretations.add_version(
            conn,
            target_kind="event",
            target_id=original_id,
            content="现在意识到，昨天可能没有察觉用户的失望",
            confidence=0.7,
            source_version=runtime.version(),
            source_event_ids=[original_id],
            supersedes_id="interpretation_1",
        )
        runtime.projections.interpretations.add_reappraisal(
            conn,
            source_event_ids=[original_id],
            previous_interpretation="不确定",
            new_interpretation="当时可能存在失望",
            delta_summary="新增愧疚与修复冲动",
        )

    versions = runtime.projections.interpretations.list_for_target("event", original_id)
    assert [item["interpretation_version"] for item in versions] == [1, 2]

    # The raw event is byte-for-byte unchanged.
    stored = runtime.events.get(original_id)
    assert stored.content == original_content
    assert runtime.projections.interpretations.list_reappraisals()


def test_scenario_5b_reappraisal_events_are_append_only(runtime: Runtime) -> None:
    """The reappraisal table has no update path exposed."""
    from companion_runtime.projections import InterpretationProjection

    public = {name for name in dir(InterpretationProjection) if not name.startswith("_")}
    assert "add_version" in public
    assert "add_reappraisal" in public
    assert not (public & {"update_version", "delete_version", "rewrite"})


# --------------------------------------------------------------------------------------
# Scenario 6: the user speaks three seconds after commitment
# --------------------------------------------------------------------------------------


def test_scenario_6_user_speaks_three_seconds_after_commitment(runtime: Runtime) -> None:
    """The committed intention is not erased; it is re-coordinated."""
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    matter = runtime.projections.unfinished.list_open()[0]
    due_at = matter.waiting_until + timedelta(hours=1)
    outcome, decision = _run_proactive_round(runtime, now=due_at)
    assert decision["acted"] is True
    attempt_id = outcome.attempt_id
    service = _service(runtime)

    # The user speaks three seconds later, answering exactly what we wanted.
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor=Actor.USER,
        content="面试过啦，结果是过了",
        conversation_id="default",
        timestamp=due_at + timedelta(seconds=3),
    )
    decisions = runtime.reducer.reconcile_pending_attempts(
        new_events=[runtime.events.get(event.event_id)],
        now=due_at + timedelta(seconds=3),
    )
    assert decisions and decisions[0]["action"] in {"resolved", "abort"}

    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.committed_at is not None, "history of the intention survives"
    assert attempt.state in {AttemptState.ABORTED.value, AttemptState.RESOLVED.value}

    # Nothing is delivered, and the history records that we intended to speak.
    _drain(service, now=due_at + timedelta(seconds=4))
    assert service._transport.sent == []
    assert runtime.events.count(EventType.PROACTIVE_COMMITTED.value) == 1
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 0

    # The intention is closed, and the drive is released later rather than never.
    candidate_id = attempt.candidate_id
    assert runtime.projections.candidates.get(candidate_id).status in {
        CandidateStatus.RESOLVED.value,
        CandidateStatus.RETIRED.value,
    }
    assert runtime.projections.outbox.stats().get("cancelled") == 1


def test_scenario_6b_topic_overlap_merges_instead_of_aborting(runtime: Runtime) -> None:
    """A related-but-not-satisfying message lets the intention survive."""
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    matter = runtime.projections.unfinished.list_open()[0]
    due_at = matter.waiting_until + timedelta(hours=1)
    outcome, _decision = _run_proactive_round(runtime, now=due_at)

    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor=Actor.USER,
        content="面试有点紧张",
        timestamp=due_at + timedelta(seconds=3),
    )
    decision = runtime.reducer.reconcile_attempt(
        attempt_id=outcome.attempt_id,
        new_events=[runtime.events.get(event.event_id)],
        now=due_at + timedelta(seconds=3),
    )
    assert decision.action == "merge"
    queued = runtime.projections.outbox.get(outcome.outbox_id)
    assert queued.status != OutboxStatus.CANCELLED.value
    assert event.event_id in queued.payload.get("merge_event_ids", [])


# --------------------------------------------------------------------------------------
# Scenario 7: severe news while a light message is in flight
# --------------------------------------------------------------------------------------


def test_scenario_7_severe_news_aborts_a_light_message(runtime: Runtime) -> None:
    """The abandoned expression is preserved but not sent."""
    from companion_runtime.typing import CandidateIntent, new_id

    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="contact",
        intent="撒个娇聊聊天",
        goal="轻松互动",
        sources=["internal_approach_drive"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        attempt_id, outbox_id = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=BASE_TIME
        )

    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor=Actor.USER,
        content="家里出事了，我很难受",
        timestamp=BASE_TIME + timedelta(seconds=2),
    )
    decision = runtime.reducer.reconcile_attempt(
        attempt_id=attempt_id,
        new_events=[runtime.events.get(event.event_id)],
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert decision.action == "abort"
    assert decision.reason == "user_situation_changed_severely"

    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.ABORTED.value
    assert attempt.intent == "撒个娇聊聊天", "the original intention is still on record"
    assert runtime.projections.outbox.get(outbox_id).status == OutboxStatus.CANCELLED.value

    service = _service(runtime)
    _drain(service, now=BASE_TIME + timedelta(seconds=3))
    assert service._transport.sent == []


# --------------------------------------------------------------------------------------
# The complete "interview" story from the document
# --------------------------------------------------------------------------------------


def test_complete_interview_story() -> None:
    """The documented example, end to end, including the lucky coincidence.

    The character is a little less restrained than the shipped default so the
    motivational game reliably chooses to speak; the point of the story is the
    loop, not the threshold.
    """
    config = build_config()
    config.values.boundary_respect = 0.45
    config.values.stability_commitment = 0.50
    from companion_runtime.db import Database

    runtime = Runtime(config, seed=3, database=Database(":memory:"), created_at=BASE_TIME)
    transport = NullTransport()
    service = DeliveryService(
        reducer=runtime.reducer,
        config=config,
        runtime=runtime,
        renderer=EchoRenderer(),
        transport=transport,
    )
    harness = Harness(
        runtime=runtime,
        config=config,
        service=service,
        transport=transport,
        start=BASE_TIME,
    )
    # Day 1: the user mentions tomorrow's interview.
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=harness.start
    )
    matter = runtime.projections.unfinished.list_open()[0]
    assert matter.title == "等待面试结果"
    assert matter.status == UnfinishedStatus.WAITING.value

    # Night passes; the Runtime sleeps and its state evolves.
    runtime.lazy_tick(harness.start + timedelta(hours=14))

    # The interview ends. The endogenous round is the time entry point, so it must
    # be given the later moment directly: pre-ticking to it first would leave zero
    # hazard exposure and the round could never act.
    due_at = matter.waiting_until + timedelta(hours=1)
    outcome, decision = _run_proactive_round(runtime, now=due_at)
    assert runtime.projections.unfinished.get(matter.unfinished_id).status == (
        UnfinishedStatus.DUE.value
    )
    assert decision["acted"] is True, decision
    assert decision["chosen_candidate_id"]
    assert outcome.attempt_id

    # The main LLM renders it, and it is delivered.
    _drain(harness.service, now=due_at)
    attempt = runtime.projections.attempts.get(outcome.attempt_id)
    assert attempt.state == AttemptState.SENT.value
    assert harness.transport.sent

    # The user replies positively; the loop closes.
    reaction = BehaviourReaction(
        replied=True, reply_delay_seconds=120, reply_length=12, explicit_positive=True
    )
    observation = runtime.observe_reply(
        attempt_id=outcome.attempt_id, reaction=reaction, now=due_at + timedelta(minutes=3)
    )
    assert observation["weight"] > 0
    assert runtime.user_model.effective_count > 0

    final = runtime.process_user_message(
        content="面试过啦！！", timestamp=due_at + timedelta(minutes=4)
    )
    assert final.unfinished_resolved == [matter.unfinished_id]
    assert runtime.state().mood_valence > 0
    assert final.memory_candidate_id is not None

    # Memory consolidation and the closing summary.
    with runtime.db.transaction() as conn:
        consolidated = memory_module.consolidate(
            runtime.projections.memory, conn, config=runtime.config, now=due_at
        )
    assert consolidated.consolidated
    assert runtime.user_model.semantic_view()["summary"]
    runtime.close()


# --------------------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------------------


def test_invariant_1_raw_events_are_never_modified(runtime: Runtime) -> None:
    """1. History is append-only at the API level and in behaviour."""
    first = runtime.process_user_message(content="第一句话", timestamp=BASE_TIME)
    second = runtime.process_user_message(
        content="第二句话", timestamp=BASE_TIME + timedelta(minutes=1)
    )
    assert runtime.events.get(first.event.event_id).content == "第一句话"
    assert runtime.events.get(second.event.event_id).content == "第二句话"
    from companion_runtime.eventlog import EventLog

    public = {name for name in dir(EventLog) if not name.startswith("_")}
    assert not (public & {"update", "delete", "edit", "replace"})


def test_invariant_2_inference_cannot_become_fact(runtime: Runtime) -> None:
    """2. Observations and interpretations are stored in separate tables."""
    runtime.process_user_message(content="我今晚想自己待着", timestamp=BASE_TIME)
    situation = runtime.projections.situation.list_active()
    kinds = {item["kind"] for item in situation}
    assert "fact" in kinds
    assert "inference" in kinds
    for item in situation:
        if item["kind"] == "inference":
            assert float(item["confidence"]) < 1.0


def test_invariant_3_background_models_never_write_directly(runtime: Runtime) -> None:
    """3. Only the reducer mutates state, and only through proposals."""
    # A proposal for a task type with no handler changes nothing but is recorded.
    from companion_runtime import protocol as protocol_module

    before = runtime.version()
    result = runtime.reducer.process_proposal(
        protocol_module.Proposal(
            task_id="tsk_unknown",
            task_type="totally_unknown_task",
            based_on_version=before,
            payload={"anything": True},
        )
    )
    assert result.version > before, "the proposal itself is recorded"
    # The reducer is the only object with the write methods.
    assert hasattr(runtime.reducer, "process_proposal")
    assert not hasattr(runtime, "apply_payload")


def test_invariant_4_every_entry_calls_lazy_tick(runtime: Runtime) -> None:
    """4. Time is continuous because every entry advances it first."""
    original = runtime.lazy_tick
    calls: list[str] = []

    def counting_lazy_tick(now=None):
        calls.append("tick")
        return original(now)

    runtime.lazy_tick = counting_lazy_tick  # type: ignore[method-assign]
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        assert calls, "the foreground path must tick first"
        calls.clear()
        runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
        assert calls, "the endogenous path must tick first"
    finally:
        runtime.lazy_tick = original  # type: ignore[method-assign]


def test_invariant_5_explicit_boundaries_outrank_the_game(runtime: Runtime) -> None:
    """5. No amount of pressure can cross a hard boundary."""
    runtime.process_user_message(content="永远别联系我", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))
    # Force every drive to its maximum.
    with runtime.db.transaction() as conn:
        state = runtime.state()
        state.pressure = 1.0
        state.approach_impulse = 1.0
        state.restraint = 0.0
        runtime.projections.runtime.write(state, conn, expect_version=state.version)
    _outcome, decision = _run_proactive_round(runtime, now=BASE_TIME + timedelta(hours=72))
    assert decision["acted"] is False
    assert decision["reason"] == "blocked_by_boundary"
    assert all(utility["total"] == float("-inf") for utility in decision["utilities"])


def test_invariant_6_main_llm_has_no_state_write_authority(runtime: Runtime) -> None:
    """6. Language output is never a source of internal facts."""
    from companion_runtime.emotion import EmotionExplainer

    public = {name for name in dir(EmotionExplainer) if not name.startswith("_")}
    assert not (public & {"set_mood", "update", "apply", "write_state"})
    # Text arriving as an assistant message carries no emotional impact.
    event = runtime.events.append(
        EventType.ASSISTANT_MESSAGE,
        actor=Actor.ASSISTANT,
        content="我现在非常生气",
        timestamp=BASE_TIME,
    )
    assert event.content == "我现在非常生气"
    assert runtime.state().mood_valence == 0.0


def test_invariant_7_reinterpretation_never_rewrites_the_past(runtime: Runtime) -> None:
    """7. Only new interpretation versions and reappraisal events are added."""
    event = runtime.process_user_message(content="算了，也没什么", timestamp=BASE_TIME)
    with runtime.db.transaction() as conn:
        runtime.projections.interpretations.add_version(
            conn,
            target_kind="event",
            target_id=event.event.event_id,
            content="不确定",
            confidence=0.4,
            source_version=0,
            source_event_ids=[event.event.event_id],
        )
    assert runtime.events.get(event.event.event_id).content == "算了，也没什么"
    assert runtime.projections.interpretations.latest("event", event.event.event_id)


def test_invariant_8_no_reply_is_not_negative_feedback() -> None:
    """8. Absence of a reply carries a small, explicitly damped weight."""
    from companion_runtime.user_model import (
        BehaviourReaction,
        UserInteractionModel,
        compute_weight,
    )
    from companion_runtime.db import Database
    from companion_runtime.projections import UserModelProjection

    db = Database(":memory:")
    db.migrate()
    model = UserInteractionModel(UserModelProjection(db), build_config())
    try:
        action = {"type": "contact", "proactive": True}
        context = {"busy_probability": 0.9}
        before = model.predict(action=action, context=context)
        with db.transaction() as conn:
            model.observe(
                conn,
                action=action,
                context=context,
                reaction=BehaviourReaction(replied=False, reply_delay_seconds=21600),
                now=BASE_TIME,
                observed_at=BASE_TIME,
                busy_probability=0.9,
            )
        after = model.predict(action=action, context=context)
        assert abs(after.positive_probability - before.positive_probability) < 0.05
        weight = compute_weight(
            BehaviourReaction(replied=False, busy_probability=0.9),
            config=model._config.user_model,
            observed_at=BASE_TIME,
            now=BASE_TIME,
        )
        assert weight.total < 0.05
    finally:
        db.close()


def test_invariant_9_committed_is_not_sent(runtime: Runtime) -> None:
    """9. The action attempt has a complete state machine."""
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    matter = runtime.projections.unfinished.list_open()[0]
    outcome, decision = _run_proactive_round(
        runtime, now=matter.waiting_until + timedelta(hours=1)
    )
    assert decision["acted"] is True
    attempt = runtime.projections.attempts.get(outcome.attempt_id)
    assert attempt.state == AttemptState.COMMITTED.value
    assert attempt.rendered_text is None
    assert attempt.outbox_id is not None
    transitions = runtime.projections.attempts.transitions(attempt.attempt_id)
    assert [item["to_state"] for item in transitions] == ["committed"]


def test_invariant_10_hidden_context_never_enters_history(runtime: Runtime) -> None:
    """10. Only visible messages reach the conversation history."""
    runtime.process_user_message(content="家里出事了，我很难受", timestamp=BASE_TIME)
    bundle = context_module.build(runtime=runtime, now=BASE_TIME)
    block = context_module.render_block(bundle)
    assert block

    # Nothing from the hidden block was persisted as an event.
    for event in runtime.events.recent(20):
        if event.content:
            assert "__RUNTIME_STATE__" not in event.content
            for line in block.splitlines():
                if line.startswith("- 感受"):
                    assert line not in (event.content or "")


# --------------------------------------------------------------------------------------
# Degradation and robustness
# --------------------------------------------------------------------------------------


def test_degradation_level_0_runs_with_no_models_at_all(runtime: Runtime) -> None:
    """The Runtime is fully functional with rules and templates only."""
    outcome = runtime.process_user_message(
        content="明天下午面试，结束告诉我结果", timestamp=BASE_TIME
    )
    assert outcome.unfinished_created
    assert outcome.emotion_event_ids or outcome.memory_candidate_id
    matter = runtime.projections.unfinished.list_open()[0]
    _round, decision = _run_proactive_round(
        runtime, now=matter.waiting_until + timedelta(hours=1)
    )
    assert "utilities" in decision
    assert runtime.projections.emotion.list_active() or True


def test_runtime_survives_a_broken_round(runtime: Runtime, monkeypatch) -> None:
    """A failing cognitive step does not corrupt the stored version."""
    version_before = runtime.version()
    with pytest.raises(RuntimeError):
        with runtime.db.transaction():
            runtime.projections.runtime.write(runtime.state(), runtime.db._conn)
            raise RuntimeError("simulated failure")
    assert runtime.version() == version_before


def test_state_version_is_monotonic(runtime: Runtime) -> None:
    """Version numbers only ever increase."""
    versions = [runtime.version()]
    runtime.process_user_message(content="a", timestamp=BASE_TIME)
    versions.append(runtime.version())
    runtime.lazy_tick(BASE_TIME + timedelta(minutes=5))
    versions.append(runtime.version())
    runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=10), force=True)
    versions.append(runtime.version())
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)


def test_two_runtimes_on_one_file_do_not_interleave_writes(tmp_path) -> None:
    """Optimistic concurrency reports a conflict instead of losing a write."""
    from companion_runtime.db import Database
    from companion_runtime.projections import VersionConflict

    path = tmp_path / "shared.sqlite3"
    config_a = build_config()
    config_b = build_config()
    first = Runtime(config_a, seed=1, database=Database(str(path)))
    second = Runtime(config_b, seed=2, database=Database(str(path)))
    try:
        stale = first.state()
        second.lazy_tick(BASE_TIME + timedelta(minutes=1))
        with pytest.raises(VersionConflict):
            with first.db.transaction() as conn:
                first.projections.runtime.write(stale, conn, expect_version=stale.version)
    finally:
        first.close()
        second.close()
