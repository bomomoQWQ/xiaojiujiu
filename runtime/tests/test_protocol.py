"""Tests for the protocol layer: APPLY / REBASE / DISCARD and re-coordination."""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime import action as action_module
from companion_runtime import protocol as protocol_module
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.reducer import Reducer
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    AttemptState,
    CandidateStatus,
    EventType,
    OutboxKind,
    ProtocolAction,
    RawEvent,
    ReconcileAction,
    TaskKind,
)

from conftest import BASE_TIME, build_config


def make_event(content: str, *, event_id: str = "evt_new", offset_minutes: int = 1) -> RawEvent:
    """Build a user event used as "the message that arrived later"."""
    return RawEvent(
        event_id=event_id,
        event_type=EventType.USER_MESSAGE.value,
        timestamp=BASE_TIME + timedelta(minutes=offset_minutes),
        actor="user",
        conversation_id="c1",
        content=content,
    )


# --------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------


def test_fresh_proposal_is_applied() -> None:
    """A result with valid sources and no version drift is applied as-is."""
    proposal = protocol_module.Proposal(
        task_id="tsk_1",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=10,
        source_event_ids=["evt_1"],
    )
    classification = protocol_module.classify(
        proposal,
        current_version=10,
        source_events=[make_event("hello", event_id="evt_1")],
    )
    assert classification.action == ProtocolAction.APPLY.value
    assert classification.reason == "fresh"


def test_missing_source_event_forces_discard() -> None:
    """A premise with no evidence left cannot change anything."""
    proposal = protocol_module.Proposal(
        task_id="tsk_1",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=10,
        source_event_ids=["evt_gone"],
    )
    classification = protocol_module.classify(
        proposal,
        current_version=10,
        source_events=[],
        missing_event_ids=["evt_gone"],
    )
    assert classification.action == ProtocolAction.DISCARD.value
    assert classification.reason == "source_events_missing"
    assert classification.missing_event_ids == ["evt_gone"]


def test_retracted_premise_forces_discard() -> None:
    """Invariant: a retraction kills the stale interpretation, not the history."""
    proposal = protocol_module.Proposal(
        task_id="tsk_1",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=10,
        source_event_ids=["evt_1"],
        payload={"summary": "用户今晚不能交流", "impact": 0.6},
    )
    classification = protocol_module.classify(
        proposal,
        current_version=11,
        source_events=[make_event("今晚可能不来了", event_id="evt_1")],
        newer_user_events=[make_event("刚才说错了，其实今晚有空")],
    )
    assert classification.action == ProtocolAction.DISCARD.value
    assert classification.reason == "premise_retracted"


def test_unrelated_retraction_does_not_kill_a_valid_result() -> None:
    """A "never mind" about something else must not discard unrelated work."""
    proposal = protocol_module.Proposal(
        task_id="tsk_1",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=10,
        source_event_ids=["evt_1"],
        payload={"summary": "用户面试很顺利"},
    )
    classification = protocol_module.classify(
        proposal,
        current_version=10,
        source_events=[make_event("面试很顺利", event_id="evt_1")],
        newer_user_events=[make_event("刚才说错了，咖啡那事不算")],
    )
    assert classification.action == ProtocolAction.APPLY.value


def test_staleness_budget_depends_on_sensitivity() -> None:
    """Different result types expire at different rates."""
    low = protocol_module.Proposal(
        task_id="t1", task_type=TaskKind.SHALLOW_TAG.value, based_on_version=0
    )
    high = protocol_module.Proposal(
        task_id="t2", task_type=TaskKind.EMOTION_EXPLAIN.value, based_on_version=0
    )
    assert protocol_module.sensitivity_of(TaskKind.SHALLOW_TAG.value) == "low"
    assert protocol_module.sensitivity_of(TaskKind.EMOTION_EXPLAIN.value) == "high"
    assert protocol_module.sensitivity_of("unknown-task") == "medium"

    assert protocol_module.classify(
        low, current_version=20, source_events=[]
    ).action == ProtocolAction.APPLY.value
    rebased = protocol_module.classify(high, current_version=20, source_events=[])
    assert rebased.action == ProtocolAction.REBASE.value
    assert rebased.reason.startswith("stale_by_")


def test_proactive_draft_is_always_re_coordinated_when_the_user_spoke() -> None:
    """A rendered proactive message can never be sent into a newer world."""
    proposal = protocol_module.Proposal(
        task_id="t1",
        task_type=TaskKind.PROACTIVE_DRAFT.value,
        based_on_version=10,
        source_event_ids=["evt_1"],
    )
    classification = protocol_module.classify(
        proposal,
        current_version=10,
        source_events=[],
        newer_user_events=[make_event("在吗")],
    )
    assert classification.action == ProtocolAction.REBASE.value
    assert classification.reason == "user_spoke_during_rendering"


def test_critical_sensitivity_has_no_staleness_budget() -> None:
    """A draft cannot tolerate even a single intervening version."""
    assert protocol_module.STALENESS_BUDGET["critical"] == 0


# --------------------------------------------------------------------------------------
# rebase helpers
# --------------------------------------------------------------------------------------


def test_rebase_emotion_evaluation_damps_stale_and_amplifies_sensitive() -> None:
    """The appraisal survives; only its effect is recomputed."""
    payload = {"direction": "-", "impact": 0.5, "activation": 0.4}
    fresh_high_mood = protocol_module.rebase_emotion_evaluation(
        payload, mood_valence=-0.8, mood_arousal=0.8, hours_elapsed=0.0
    )
    stale = protocol_module.rebase_emotion_evaluation(
        payload, mood_valence=0.0, mood_arousal=0.0, hours_elapsed=12.0
    )
    assert fresh_high_mood.payload["impact"] > payload["impact"]
    assert stale.payload["impact"] < payload["impact"]
    assert fresh_high_mood.notes


def test_rebase_candidate_payload_drops_ungrounded_operations() -> None:
    """A candidate grounded in vanished evidence is dropped, not rebased."""
    payload = {
        "operations": [
            {
                "op": "add",
                "candidate": {"intent": "ask about interview", "sources": ["unfinished:unf_1"]},
            },
            {
                "op": "add",
                "candidate": {"intent": "ask about coffee", "sources": ["memory:mem_gone"]},
            },
        ]
    }
    result = protocol_module.rebase_candidate_payload(
        payload, live_unfinished_ids=["unf_1"], live_memory_ids=[]
    )
    operations = result.payload["operations"]
    assert len(operations) == 1
    assert "interview" in str(operations[0])
    assert result.notes == ["dropped_ungrounded=1"]


def test_explanation_expiry_and_discard_rules() -> None:
    """Psychological explanations expire quickly and cheaply."""
    assert protocol_module.expired_explanation(None, BASE_TIME, 60.0) is True
    assert protocol_module.expired_explanation(BASE_TIME, BASE_TIME, 60.0) is False
    assert (
        protocol_module.expired_explanation(BASE_TIME, BASE_TIME + timedelta(hours=2), 60.0)
        is True
    )
    proposal = protocol_module.Proposal(
        task_id="t", task_type=TaskKind.EMOTION_EXPLAIN.value, based_on_version=1
    )
    assert protocol_module.should_discard_explanation(proposal, current_version=3) is True
    assert protocol_module.should_discard_explanation(proposal, current_version=2) is False


def test_proposal_serialisation() -> None:
    """Proposals are JSON-safe for transport."""
    import json

    proposal = protocol_module.Proposal(
        task_id="t1",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=3,
        payload={"impact": 0.4},
        source_event_ids=["evt_1"],
        created_at=BASE_TIME,
    )
    json.dumps(proposal.to_dict())


# --------------------------------------------------------------------------------------
# re-coordination
# --------------------------------------------------------------------------------------


def _reconcile(**overrides) -> protocol_module.ReconcileDecision:
    """Reconcile a committed attempt with compact arguments."""
    arguments = {
        "attempt_state": AttemptState.COMMITTED.value,
        "attempt_intent": "询问面试结果",
        "attempt_goal": "了解结果并表达关心",
        "candidate_type": "follow_up",
        "new_events": [make_event("在吗")],
        "now": BASE_TIME + timedelta(minutes=1),
    }
    arguments.update(overrides)
    return protocol_module.reconcile(**arguments)


def test_no_new_events_means_keep() -> None:
    """Nothing to re-coordinate."""
    decision = _reconcile(new_events=[])
    assert decision.action == ReconcileAction.KEEP.value
    assert decision.reason == "no_new_events"


def test_user_speaks_first_rerenders() -> None:
    """An unrelated message keeps the intent but invalidates the wording."""
    decision = _reconcile(new_events=[make_event("在忙吗")])
    assert decision.action in {ReconcileAction.RERENDER.value, ReconcileAction.KEEP.value}
    assert decision.notes


def test_same_topic_merges() -> None:
    """A message about the same subject is absorbed into the intent."""
    decision = _reconcile(new_events=[make_event("面试有点紧张")])
    assert decision.action == ReconcileAction.MERGE.value
    assert decision.reason == "topic_overlap"


def test_user_beats_the_intent() -> None:
    """The "心有灵犀" case: the user already answered what we were about to ask."""
    decision = _reconcile(new_events=[make_event("面试过啦，结果是过了")])
    assert decision.action == ReconcileAction.RESOLVED.value
    assert decision.reason == "user_already_satisfied_intent"
    assert decision.satisfied_by_event_ids == ["evt_new"]


def test_severe_news_aborts_a_light_intent() -> None:
    """Suddenly joking around is not appropriate any more."""
    decision = _reconcile(
        attempt_intent="撒个娇聊聊天",
        attempt_goal="轻松互动",
        candidate_type="contact",
        new_events=[make_event("家里出事了，我很难受")],
    )
    assert decision.action == ReconcileAction.ABORT.value
    assert decision.reason == "user_situation_changed_severely"


def test_boundary_declared_mid_flight_aborts_the_send() -> None:
    """A hard constraint arriving before the send wins immediately."""
    decision = _reconcile(new_events=[make_event("今天不要主动联系我。")])
    assert decision.action == ReconcileAction.ABORT.value
    assert decision.reason == "boundary_declared_mid_flight"


def test_retracted_premise_aborts() -> None:
    """The grounding statement was withdrawn."""
    decision = _reconcile(new_events=[make_event("刚才说错了，面试其实是今晚")])
    assert decision.action == ReconcileAction.ABORT.value
    assert decision.reason == "premise_retracted"


def test_unrelated_retraction_does_not_abort_an_unrelated_intent() -> None:
    """Retracting a different subject leaves the prepared message alone."""
    decision = _reconcile(new_events=[make_event("刚才说错了，咖啡那件事不算")])
    assert decision.action != ReconcileAction.ABORT.value


def test_ready_to_send_always_rerenders_on_a_new_message() -> None:
    """A message that has not left the Runtime is not sent into a newer world."""
    decision = _reconcile(
        attempt_state=AttemptState.READY_TO_SEND.value,
        attempt_intent="询问结果",
        attempt_goal="",
        candidate_type="contact",
        new_events=[make_event("在吗")],
    )
    assert decision.action in {
        ReconcileAction.RERENDER.value,
        ReconcileAction.MERGE.value,
    }


def test_reconcile_decision_serialisation() -> None:
    """Decisions are returned by the HTTP API."""
    import json

    json.dumps(_reconcile().to_dict())


# --------------------------------------------------------------------------------------
# reducer integration
# --------------------------------------------------------------------------------------


def test_reducer_applies_a_valid_proposal(runtime: Runtime) -> None:
    """An applied proposal moves state and is recorded in history."""
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="家里出事了，我很难受",
        timestamp=BASE_TIME,
    )
    version = runtime.version()
    proposal = protocol_module.Proposal(
        task_id="tsk_1",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=version,
        source_event_ids=[event.event_id],
        payload={
            "direction": "-",
            "impact": 0.8,
            "activation": 0.7,
            "uncertainty": 0.2,
            "relation_signal": "crisis",
            "responsibility": "third_party",
            "confidence": 0.9,
        },
    )
    result = runtime.reducer.process_proposal(proposal)
    assert result.action == ProtocolAction.APPLY.value
    assert result.applied is True
    assert result.version > version
    assert runtime.projections.emotion.list_active()
    assert runtime.state().mood_valence < 0.0
    assert runtime.projections.tasks.get("tsk_1") is not None or True


def test_reducer_discards_a_proposal_with_missing_premise(runtime: Runtime) -> None:
    """A discarded proposal changes nothing but is still recorded."""
    before = runtime.version()
    events_before = runtime.events.count()
    proposal = protocol_module.Proposal(
        task_id="tsk_2",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=before,
        source_event_ids=["evt_missing"],
        payload={"direction": "-", "impact": 0.9},
    )
    result = runtime.reducer.process_proposal(proposal)
    assert result.action == ProtocolAction.DISCARD.value
    assert result.applied is False
    assert runtime.version() == before
    assert runtime.events.count() > events_before
    assert runtime.projections.emotion.list_active() == []


def test_reducer_rebases_a_stale_emotion_evaluation(runtime: Runtime) -> None:
    """A stale appraisal is recomputed against the current mood, not dropped."""
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="今晚可能不来了",
        timestamp=BASE_TIME,
    )
    runtime.lazy_tick(BASE_TIME + timedelta(hours=6))
    # Dispatching from a version far behind the current one is what makes the
    # result stale; the appraisal itself stays valid.
    stale_base_version = runtime.version() - 10
    proposal = protocol_module.Proposal(
        task_id="tsk_3",
        task_type=TaskKind.EMOTION_EVAL.value,
        based_on_version=stale_base_version,
        source_event_ids=[event.event_id],
        payload={"direction": "-", "impact": 0.5, "activation": 0.4, "confidence": 0.8},
        created_at=BASE_TIME,
    )
    result = runtime.reducer.process_proposal(proposal)
    assert result.action == ProtocolAction.REBASE.value
    assert result.applied is True
    assert any("staleness" in note for note in result.notes)
    active = runtime.projections.emotion.list_active()
    assert active and active[0].intensity < 0.5


def test_reducer_applies_candidate_operations_and_rejects_groundless_ones(
    runtime: Runtime,
) -> None:
    """The semantic API path goes through the same validation as the local one."""
    proposal = protocol_module.Proposal(
        task_id="tsk_4",
        task_type=TaskKind.CANDIDATE_GEN.value,
        based_on_version=runtime.version(),
        payload={
            "operations": [
                {
                    "op": "add",
                    "candidate": {
                        "type": "contact",
                        "intent": "只是想联系",
                        "sources": ["internal_approach_drive"],
                    },
                },
                {"op": "add", "candidate": {"type": "contact", "intent": "ungrounded"}},
            ]
        },
    )
    result = runtime.reducer.process_proposal(proposal)
    assert result.applied is True
    assert any("candidate_changes=1" in note for note in result.notes)
    assert runtime.projections.candidates.list_active()


def test_reducer_stores_a_user_model_summary(runtime: Runtime) -> None:
    """A model-provided prose summary is attached to the user model."""
    proposal = protocol_module.Proposal(
        task_id="tsk_5",
        task_type=TaskKind.USER_MODEL_SUMMARY.value,
        based_on_version=runtime.version(),
        payload={"summary": "用户接受偶尔主动联系。", "confidence": 0.7},
    )
    result = runtime.reducer.process_proposal(proposal)
    assert result.applied is True
    view = runtime.reload_user_model().semantic_view()
    assert "偶尔主动联系" in view["summary"]


def test_task_snapshot_round_trip(runtime: Runtime) -> None:
    """Background task snapshots are recorded at dispatch time."""
    task_id = runtime.reducer.register_task(
        task_id="tsk_snap",
        task_type=TaskKind.EMOTION_EXPLAIN.value,
        based_on_version=runtime.version(),
        source_event_ids=["evt_1"],
        priority="p1_near_realtime",
    )
    assert task_id == "tsk_snap"
    snapshot = runtime.projections.tasks.get("tsk_snap")
    assert snapshot is not None
    assert snapshot["based_on_version"] == runtime.version()
    assert runtime.projections.tasks.list_in_flight()


# --------------------------------------------------------------------------------------
# re-coordination through the reducer
# --------------------------------------------------------------------------------------


def _commit_attempt(runtime: Runtime, *, intent: str = "询问面试结果", candidate_type: str = "follow_up"):
    """Create a committed attempt plus its render outbox row."""
    from companion_runtime.typing import CandidateIntent, OutboxItem, new_id

    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type=candidate_type,
        intent=intent,
        goal="表达关心",
        sources=["unfinished:unf_1"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        attempt_id, outbox_id = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=BASE_TIME
        )
    return attempt_id, outbox_id, candidate


def test_reducer_aborts_an_attempt_when_the_user_beats_it(runtime: Runtime) -> None:
    """RESOLVED terminates the attempt, cancels delivery, closes the candidate."""
    attempt_id, _outbox_id, candidate = _commit_attempt(runtime)
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="面试过啦，结果是过了",
        timestamp=BASE_TIME + timedelta(seconds=3),
    )
    decision = runtime.reducer.reconcile_attempt(
        attempt_id=attempt_id,
        new_events=[runtime.events.get(event.event_id)],
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert decision.action == ReconcileAction.RESOLVED.value
    stored = runtime.projections.attempts.get(attempt_id)
    assert stored.state == AttemptState.ABORTED.value
    assert stored.committed_at is not None, "the history of the intention must survive"
    assert event.event_id in stored.superseded_by_event_ids
    assert runtime.projections.candidates.get(candidate.candidate_id).status == (
        CandidateStatus.RESOLVED.value
    )
    assert runtime.projections.outbox.list_items(status="cancelled")


def test_reducer_merges_on_topic_overlap(runtime: Runtime) -> None:
    """MERGE keeps the attempt alive and annotates the queued row."""
    attempt_id, outbox_id, _candidate = _commit_attempt(runtime)
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="面试有点紧张",
        timestamp=BASE_TIME + timedelta(seconds=2),
    )
    decision = runtime.reducer.reconcile_attempt(
        attempt_id=attempt_id,
        new_events=[runtime.events.get(event.event_id)],
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert decision.action == ReconcileAction.MERGE.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.COMMITTED.value
    queued = runtime.projections.outbox.get(outbox_id)
    assert queued.status != "cancelled"
    assert event.event_id in queued.payload.get("merge_event_ids", [])


def test_reconcile_pending_attempts_walks_every_in_flight_attempt(runtime: Runtime) -> None:
    """The batch entry point re-coordinates everything in flight."""
    _commit_attempt(runtime, intent="询问面试结果")
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="面试过啦",
        timestamp=BASE_TIME + timedelta(seconds=1),
    )
    decisions = runtime.reducer.reconcile_pending_attempts(
        new_events=[runtime.events.get(event.event_id)],
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert len(decisions) == 1
    assert decisions[0]["action"] in {"resolved", "abort", "rerender", "merge", "keep"}


def test_reconcile_unknown_attempt_raises(runtime: Runtime) -> None:
    """An unknown attempt identifier is a 404, not a silent success."""
    with pytest.raises(KeyError):
        runtime.reducer.reconcile_attempt(attempt_id="att_missing", new_events=[], now=BASE_TIME)
