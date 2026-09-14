"""Tests for the action attempt state machine and the asynchronous outbox."""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime import action as action_module
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import AttemptProjection, OutboxProjection, Projections
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    ActionAttempt,
    AttemptState,
    CandidateIntent,
    OutboxItem,
    OutboxKind,
    OutboxStatus,
    new_id,
)

from conftest import BASE_TIME, build_config


def make_attempt(**overrides) -> ActionAttempt:
    """Build an action attempt in the ``proposed`` state."""
    payload = {
        "attempt_id": "att_1",
        "candidate_id": "cnd_1",
        "state": AttemptState.PROPOSED.value,
        "intent": "询问面试结果",
        "goal": "表达关心",
        "based_on_version": 3,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
    }
    payload.update(overrides)
    return ActionAttempt(**payload)


def make_candidate(**overrides) -> CandidateIntent:
    """Build a candidate intent for attempt creation."""
    payload = {
        "candidate_id": "cnd_1",
        "type": "follow_up",
        "intent": "询问面试结果",
        "goal": "表达关心",
        "constraints": ["避免催促感"],
    }
    payload.update(overrides)
    return CandidateIntent(**payload)


# --------------------------------------------------------------------------------------
# transition table
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (AttemptState.PROPOSED.value, AttemptState.COMMITTED.value),
        (AttemptState.COMMITTED.value, AttemptState.RENDERING.value),
        (AttemptState.RENDERING.value, AttemptState.READY_TO_SEND.value),
        (AttemptState.READY_TO_SEND.value, AttemptState.SENT.value),
        (AttemptState.SENT.value, AttemptState.RESOLVED.value),
        (AttemptState.COMMITTED.value, AttemptState.ABORTED.value),
        (AttemptState.RENDERING.value, AttemptState.FAILED.value),
        (AttemptState.READY_TO_SEND.value, AttemptState.EXPIRED.value),
    ],
)
def test_legal_transitions(from_state: str, to_state: str) -> None:
    """Every documented transition is accepted."""
    assert action_module.can_transition(from_state, to_state)


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (AttemptState.PROPOSED.value, AttemptState.SENT.value),
        (AttemptState.PROPOSED.value, AttemptState.RENDERING.value),
        (AttemptState.COMMITTED.value, AttemptState.SENT.value),
        (AttemptState.RENDERING.value, AttemptState.SENT.value),
        (AttemptState.RESOLVED.value, AttemptState.SENT.value),
        (AttemptState.ABORTED.value, AttemptState.READY_TO_SEND.value),
        (AttemptState.SENT.value, AttemptState.ABORTED.value),
    ],
)
def test_illegal_transitions_are_rejected(from_state: str, to_state: str) -> None:
    """Skipping a state or reviving a terminal one is an error."""
    assert not action_module.can_transition(from_state, to_state)


def test_terminal_states_have_no_exits() -> None:
    """Terminal states are absorbing."""
    for state in action_module.TERMINAL_STATES:
        assert not action_module.TRANSITIONS[state]


# --------------------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------------------


def test_full_lifecycle_persists_and_logs_every_transition() -> None:
    """committed -> rendering -> ready_to_send -> sent -> resolved."""
    db = Database(":memory:")
    db.migrate()
    projection = AttemptProjection(db)
    try:
        attempt = make_attempt()
        with db.transaction() as conn:
            action_module.commit(projection, conn, attempt, now=BASE_TIME)
        assert attempt.state == AttemptState.COMMITTED.value
        assert attempt.committed_at is not None

        with db.transaction() as conn:
            action_module.mark_rendering(projection, conn, attempt, now=BASE_TIME)
            action_module.mark_ready(projection, conn, attempt, text="面试怎么样啦？", now=BASE_TIME)
            action_module.mark_sent(projection, conn, attempt, now=BASE_TIME)
            action_module.resolve(projection, conn, attempt, reason="user replied", now=BASE_TIME)

        stored = projection.get("att_1")
        assert stored.state == AttemptState.RESOLVED.value
        assert stored.rendered_text == "面试怎么样啦？"
        transitions = projection.transitions("att_1")
        assert [item["to_state"] for item in transitions] == [
            AttemptState.COMMITTED.value,
            AttemptState.RENDERING.value,
            AttemptState.READY_TO_SEND.value,
            AttemptState.SENT.value,
            AttemptState.RESOLVED.value,
        ]
        assert transitions[0]["from_state"] == AttemptState.PROPOSED.value
    finally:
        db.close()


def test_illegal_transition_raises_and_leaves_state_untouched() -> None:
    """A rejected transition does not corrupt the stored state."""
    db = Database(":memory:")
    db.migrate()
    projection = AttemptProjection(db)
    try:
        attempt = make_attempt()
        with db.transaction() as conn:
            projection.upsert(conn, attempt)
            with pytest.raises(action_module.IllegalTransition):
                action_module.mark_sent(projection, conn, attempt, now=BASE_TIME)
        assert projection.get("att_1").state == AttemptState.PROPOSED.value
    finally:
        db.close()


def test_empty_rendered_text_is_rejected_and_fails_the_attempt() -> None:
    """An empty message can never be sent."""
    db = Database(":memory:")
    db.migrate()
    projection = AttemptProjection(db)
    try:
        attempt = make_attempt()
        with db.transaction() as conn:
            action_module.commit(projection, conn, attempt, now=BASE_TIME)
            action_module.mark_rendering(projection, conn, attempt, now=BASE_TIME)
            with pytest.raises(ValueError):
                action_module.mark_ready(projection, conn, attempt, text="   ", now=BASE_TIME)
            action_module.fail(projection, conn, attempt, reason="empty_text", now=BASE_TIME)
        stored = projection.get("att_1")
        assert stored.state == AttemptState.FAILED.value
        assert stored.failure_reason == "empty_text"
    finally:
        db.close()


def test_abort_preserves_history() -> None:
    """An abandoned intention stays in the record."""
    db = Database(":memory:")
    db.migrate()
    projection = AttemptProjection(db)
    try:
        attempt = make_attempt()
        with db.transaction() as conn:
            action_module.commit(projection, conn, attempt, now=BASE_TIME)
            action_module.abort(
                projection, conn, attempt, reason="user spoke first", now=BASE_TIME
            )
        stored = projection.get("att_1")
        assert stored.state == AttemptState.ABORTED.value
        assert stored.committed_at is not None
        assert stored.failure_reason == "user spoke first"
    finally:
        db.close()


def test_committed_is_not_sent() -> None:
    """Invariant 9, expressed directly."""
    attempt = make_attempt()
    assert attempt.state == AttemptState.PROPOSED.value
    assert attempt.rendered_text is None
    assert not action_module.is_terminal(AttemptState.COMMITTED.value)
    assert AttemptState.COMMITTED.value != AttemptState.SENT.value


def test_expire_stale_attempts() -> None:
    """Attempts that wait too long in flight are expired."""
    db = Database(":memory:")
    db.migrate()
    projection = AttemptProjection(db)
    config = build_config()
    config.action.send_expiry_seconds = 60.0
    try:
        attempt = make_attempt(created_at=BASE_TIME, updated_at=BASE_TIME)
        with db.transaction() as conn:
            action_module.commit(projection, conn, attempt, now=BASE_TIME)
            action_module.mark_rendering(projection, conn, attempt, now=BASE_TIME)
            action_module.mark_ready(projection, conn, attempt, text="hi", now=BASE_TIME)
        with db.transaction() as conn:
            fresh = action_module.expire_stale(
                projection, conn, config=config, now=BASE_TIME + timedelta(seconds=30)
            )
        assert fresh == []
        with db.transaction() as conn:
            expired = action_module.expire_stale(
                projection, conn, config=config, now=BASE_TIME + timedelta(seconds=120)
            )
        assert expired == ["att_1"]
        assert projection.get("att_1").state == AttemptState.EXPIRED.value
    finally:
        db.close()


def test_stale_proposal_is_aborted_not_expired() -> None:
    """A proposal that was never committed is aborted."""
    db = Database(":memory:")
    db.migrate()
    projection = AttemptProjection(db)
    config = build_config()
    config.action.send_expiry_seconds = 10.0
    try:
        attempt = make_attempt()
        with db.transaction() as conn:
            projection.upsert(conn, attempt)
        with db.transaction() as conn:
            action_module.expire_stale(
                projection, conn, config=config, now=BASE_TIME + timedelta(seconds=60)
            )
        assert projection.get("att_1").state == AttemptState.ABORTED.value
    finally:
        db.close()


def test_reconcile_outcomes_terminate_or_preserve() -> None:
    """KEEP/MERGE/RERENDER preserve; RESOLVED/ABORT terminate with history."""
    db = Database(":memory:")
    db.migrate()
    projection = AttemptProjection(db)
    try:
        for action, expected in [
            ("keep", AttemptState.COMMITTED.value),
            ("merge", AttemptState.COMMITTED.value),
            ("rerender", AttemptState.COMMITTED.value),
            ("resolved", AttemptState.ABORTED.value),
            ("abort", AttemptState.ABORTED.value),
        ]:
            attempt = make_attempt(attempt_id=f"att_{action}")
            with db.transaction() as conn:
                action_module.commit(projection, conn, attempt, now=BASE_TIME)
                action_module.apply_reconcile_outcome(
                    projection,
                    conn,
                    attempt,
                    action=action,
                    new_event_ids=["evt_new"],
                    reason=f"reconcile:{action}",
                    now=BASE_TIME,
                )
            stored = projection.get(f"att_{action}")
            assert stored.state == expected, action
            assert stored.reconcile_action == action
            assert stored.superseded_by_event_ids == ["evt_new"]
            assert stored.committed_at is not None
    finally:
        db.close()


def test_summarise_and_event_type_mapping() -> None:
    """Helpers expose the attempt in a prompt-friendly shape."""
    attempt = make_attempt()
    summary = action_module.summarise(attempt)
    assert summary["attempt_id"] == "att_1"
    assert summary["state"] == AttemptState.PROPOSED.value
    assert (
        action_module.event_type_for_state(AttemptState.COMMITTED.value) == "proactive_committed"
    )
    assert action_module.event_type_for_state(AttemptState.SENT.value) == "proactive_sent"
    assert action_module.describe_reconcile("abort")


def test_create_proposal_carries_the_based_on_version() -> None:
    """Every attempt records the state version it was decided on."""
    attempt = action_module.create_proposal(
        candidate=make_candidate(), based_on_version=17, now=BASE_TIME
    )
    assert attempt.based_on_version == 17
    assert attempt.state == AttemptState.PROPOSED.value
    assert attempt.candidate_id == "cnd_1"


# --------------------------------------------------------------------------------------
# outbox: enqueue / claim / lease / ack
# --------------------------------------------------------------------------------------


def _outbox(db: Database) -> OutboxProjection:
    """Build an outbox projection over an in-memory database."""
    return OutboxProjection(db)


def test_enqueue_claim_ack_round_trip() -> None:
    """The happy path of the delivery queue."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        item = OutboxItem(
            outbox_id="obx_1",
            kind=OutboxKind.RENDER.value,
            payload={"attempt_id": "att_1"},
            created_at=BASE_TIME,
            available_at=BASE_TIME,
        )
        with db.transaction() as conn:
            outbox.enqueue(conn, item)
        with db.transaction() as conn:
            claimed = outbox.claim(conn, owner="w1", now=BASE_TIME, lease_seconds=60.0, limit=5)
        assert len(claimed) == 1
        assert claimed[0].status == OutboxStatus.LEASED.value
        assert claimed[0].lease_owner == "w1"
        assert claimed[0].attempts == 1
        assert claimed[0].lease_expires_at == BASE_TIME + timedelta(seconds=60)

        # A second claim while the lease is live gets nothing.
        with db.transaction() as conn:
            again = outbox.claim(conn, owner="w2", now=BASE_TIME, lease_seconds=60.0, limit=5)
        assert again == []

        with db.transaction() as conn:
            assert outbox.ack(conn, "obx_1", BASE_TIME) is True
        assert outbox.get("obx_1").status == OutboxStatus.DELIVERED.value
        # Acking twice is a no-op, not a silent state corruption.
        with db.transaction() as conn:
            assert outbox.ack(conn, "obx_1", BASE_TIME) is False
    finally:
        db.close()


def test_claim_is_exclusive_between_workers() -> None:
    """Two workers never receive the same row."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        with db.transaction() as conn:
            for index in range(6):
                outbox.enqueue(
                    conn,
                    OutboxItem(
                        outbox_id=f"obx_{index}",
                        kind=OutboxKind.SEND.value,
                        payload={"attempt_id": f"att_{index}"},
                        created_at=BASE_TIME,
                        available_at=BASE_TIME,
                    ),
                )
        claimed: dict[str, str] = {}
        for owner in ("w1", "w2", "w3"):
            with db.transaction() as conn:
                items = outbox.claim(conn, owner=owner, now=BASE_TIME, lease_seconds=60.0, limit=2)
            assert len(items) == 2
            for item in items:
                assert item.outbox_id not in claimed
                claimed[item.outbox_id] = owner
        assert len(claimed) == 6
    finally:
        db.close()


def test_claim_respects_priority_and_availability() -> None:
    """Higher priority (lower number) wins, and rows not yet due are skipped."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        with db.transaction() as conn:
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_late",
                    kind=OutboxKind.SEND.value,
                    payload={},
                    priority=1,
                    created_at=BASE_TIME,
                    available_at=BASE_TIME + timedelta(hours=1),
                ),
            )
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_low",
                    kind=OutboxKind.SEND.value,
                    payload={},
                    priority=50,
                    created_at=BASE_TIME,
                    available_at=BASE_TIME,
                ),
            )
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_high",
                    kind=OutboxKind.SEND.value,
                    payload={},
                    priority=1,
                    created_at=BASE_TIME,
                    available_at=BASE_TIME,
                ),
            )
        with db.transaction() as conn:
            items = outbox.claim(conn, owner="w1", now=BASE_TIME, lease_seconds=60.0, limit=5)
        # ``obx_late`` is not claimable yet, so the two ready rows come back in
        # priority order.
        assert [item.outbox_id for item in items] == ["obx_high", "obx_low"]

        with db.transaction() as conn:
            later = outbox.claim(
                conn,
                owner="w1",
                now=BASE_TIME + timedelta(hours=2),
                lease_seconds=60.0,
                limit=5,
            )
        assert [item.outbox_id for item in later] == ["obx_late"]
    finally:
        db.close()


def test_claim_can_be_restricted_by_kind() -> None:
    """A render worker does not steal send rows."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        with db.transaction() as conn:
            outbox.enqueue(
                conn,
                OutboxItem(outbox_id="obx_r", kind=OutboxKind.RENDER.value, payload={}, created_at=BASE_TIME),
            )
            outbox.enqueue(
                conn,
                OutboxItem(outbox_id="obx_s", kind=OutboxKind.SEND.value, payload={}, created_at=BASE_TIME),
            )
        with db.transaction() as conn:
            items = outbox.claim(
                conn,
                owner="renderer",
                now=BASE_TIME,
                lease_seconds=60.0,
                limit=5,
                kinds=[OutboxKind.RENDER.value],
            )
        assert [item.outbox_id for item in items] == ["obx_r"]
    finally:
        db.close()


def test_nack_requeues_then_fails_after_max_attempts() -> None:
    """A failing row is retried, then failed for good."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        with db.transaction() as conn:
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_1",
                    kind=OutboxKind.SEND.value,
                    payload={},
                    created_at=BASE_TIME,
                    max_attempts=2,
                ),
            )
        with db.transaction() as conn:
            outbox.claim(conn, owner="w1", now=BASE_TIME, lease_seconds=10.0, limit=1)
        with db.transaction() as conn:
            assert outbox.nack(
                conn, "obx_1", error="transport down", retry_at=BASE_TIME + timedelta(seconds=5)
            )
        requeued = outbox.get("obx_1")
        assert requeued.status == OutboxStatus.PENDING.value
        assert requeued.attempts == 1
        assert requeued.last_error == "transport down"

        # Not claimable before the retry time.
        with db.transaction() as conn:
            assert outbox.claim(conn, owner="w1", now=BASE_TIME, lease_seconds=10.0, limit=1) == []
        with db.transaction() as conn:
            outbox.claim(
                conn, owner="w1", now=BASE_TIME + timedelta(seconds=10), lease_seconds=10.0, limit=1
            )
        with db.transaction() as conn:
            outbox.nack(conn, "obx_1", error="still down", terminal=False)
        assert outbox.get("obx_1").status == OutboxStatus.FAILED.value
        assert outbox.get("obx_1").attempts == 2
    finally:
        db.close()


def test_lease_expiry_reclaims_then_fails_exhausted_rows() -> None:
    """A dead worker's lease is recovered; a hopeless row is failed."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        with db.transaction() as conn:
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_recover",
                    kind=OutboxKind.SEND.value,
                    payload={},
                    created_at=BASE_TIME,
                    max_attempts=5,
                ),
            )
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_hopeless",
                    kind=OutboxKind.SEND.value,
                    payload={},
                    created_at=BASE_TIME,
                    max_attempts=1,
                ),
            )
        with db.transaction() as conn:
            outbox.claim(conn, owner="dead", now=BASE_TIME, lease_seconds=10.0, limit=5)
        with db.transaction() as conn:
            reclaimed = outbox.reclaim_expired(conn, BASE_TIME + timedelta(seconds=30))
        assert reclaimed == 2
        assert outbox.get("obx_recover").status == OutboxStatus.PENDING.value
        assert outbox.get("obx_hopeless").status == OutboxStatus.FAILED.value
    finally:
        db.close()


def test_cancel_by_attempt_cancels_pending_rows() -> None:
    """Reconciliation cancels the delivery of an abandoned intention."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        with db.transaction() as conn:
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_1",
                    kind=OutboxKind.SEND.value,
                    payload={"attempt_id": "att_1", "text": "hi"},
                    created_at=BASE_TIME,
                ),
            )
            outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id="obx_2",
                    kind=OutboxKind.SEND.value,
                    payload={"attempt_id": "att_2", "text": "hi"},
                    created_at=BASE_TIME,
                ),
            )
        with db.transaction() as conn:
            cancelled = outbox.cancel_for_attempt(conn, "att_1", reason="resolved")
        assert cancelled == 1
        assert outbox.get("obx_1").status == OutboxStatus.CANCELLED.value
        assert outbox.get("obx_2").status == OutboxStatus.PENDING.value
        with db.transaction() as conn:
            assert outbox.cancel(conn, "obx_2", reason="manual") is True
        assert outbox.get("obx_2").status == OutboxStatus.CANCELLED.value
    finally:
        db.close()


def test_outbox_stats() -> None:
    """Per-status counts are exposed for health checks."""
    db = Database(":memory:")
    db.migrate()
    outbox = _outbox(db)
    try:
        with db.transaction() as conn:
            outbox.enqueue(
                conn,
                OutboxItem(outbox_id="obx_1", kind=OutboxKind.SEND.value, payload={}, created_at=BASE_TIME),
            )
            outbox.enqueue(
                conn,
                OutboxItem(outbox_id="obx_2", kind=OutboxKind.SEND.value, payload={}, created_at=BASE_TIME),
            )
        with db.transaction() as conn:
            outbox.claim(conn, owner="w1", now=BASE_TIME, lease_seconds=60.0, limit=1)
        stats = outbox.stats()
        assert stats["pending"] == 1
        assert stats["leased"] == 1
    finally:
        db.close()


def test_outbox_item_serialisation() -> None:
    """Outbox rows serialise for the HTTP API."""
    import json

    item = OutboxItem(
        outbox_id="obx_1",
        kind=OutboxKind.RENDER.value,
        payload={"attempt_id": "att_1"},
        created_at=BASE_TIME,
        available_at=BASE_TIME,
    )
    json.dumps(item.to_dict())


def test_runtime_claim_and_ack_through_the_reducer(runtime: Runtime) -> None:
    """The reducer is the only writer, and it exposes the queue operations."""
    from companion_runtime.typing import OutboxItem, OutboxKind

    with runtime.db.transaction() as conn:
        runtime.projections.outbox.enqueue(
            conn,
            OutboxItem(
                outbox_id=new_id("outbox"),
                kind=OutboxKind.RENDER.value,
                payload={"attempt_id": "att_x"},
                created_at=BASE_TIME,
            ),
        )
    claimed = runtime.reducer.claim_outbox(owner="tester", now=BASE_TIME, limit=1)
    assert len(claimed) == 1
    assert runtime.reducer.ack_outbox(claimed[0].outbox_id, now=BASE_TIME) is True
    assert runtime.projections.outbox.stats().get("delivered") == 1
