"""Action attempt state machine.

A candidate intent is only a thought. Once the motivational layer decides to act,
an :class:`ActionAttempt` is created and it owns the whole lifecycle::

    proposed -> committed -> rendering -> ready_to_send -> sent -> resolved

with the exceptional terminal states ``aborted``, ``expired`` and ``failed``.

``committed`` is emphatically **not** ``sent``: it means "at this moment the
character has genuinely decided to reach out", while the message does not yet
exist. Everything that can go wrong between those two points (a new user
message, a rendering failure, a boundary that arrives late) is handled here.

Transitions are validated and written to an append-only ``attempt_events`` log so
that the history of an intention survives even when the intention is abandoned.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .config import RuntimeConfig
from .projections import AttemptProjection
from .typing import ActionAttempt, AttemptState, CandidateIntent, EventType, ReconcileAction, new_id
from .utility import utcnow

LOGGER = logging.getLogger("companion_runtime.action")

#: Allowed transitions of the action attempt state machine.
TRANSITIONS: dict[str, frozenset[str]] = {
    AttemptState.PROPOSED.value: frozenset(
        {AttemptState.COMMITTED.value, AttemptState.ABORTED.value}
    ),
    AttemptState.COMMITTED.value: frozenset(
        {
            AttemptState.RENDERING.value,
            AttemptState.ABORTED.value,
            AttemptState.EXPIRED.value,
            AttemptState.RESOLVED.value,
        }
    ),
    AttemptState.RENDERING.value: frozenset(
        {
            AttemptState.READY_TO_SEND.value,
            AttemptState.FAILED.value,
            AttemptState.ABORTED.value,
            AttemptState.EXPIRED.value,
        }
    ),
    AttemptState.READY_TO_SEND.value: frozenset(
        {
            AttemptState.SENT.value,
            AttemptState.ABORTED.value,
            AttemptState.EXPIRED.value,
            AttemptState.FAILED.value,
        }
    ),
    AttemptState.SENT.value: frozenset({AttemptState.RESOLVED.value}),
    AttemptState.RESOLVED.value: frozenset(),
    AttemptState.ABORTED.value: frozenset(),
    AttemptState.EXPIRED.value: frozenset(),
    AttemptState.FAILED.value: frozenset(),
}

#: States from which no further transition is possible.
TERMINAL_STATES: frozenset[str] = frozenset(
    {
        AttemptState.RESOLVED.value,
        AttemptState.ABORTED.value,
        AttemptState.EXPIRED.value,
        AttemptState.FAILED.value,
    }
)

#: States in which the attempt is still occupying attention.
IN_FLIGHT_STATES: tuple[str, ...] = (
    AttemptState.PROPOSED.value,
    AttemptState.COMMITTED.value,
    AttemptState.RENDERING.value,
    AttemptState.READY_TO_SEND.value,
    AttemptState.SENT.value,
)


class IllegalTransition(RuntimeError):
    """Raised when a state transition is not permitted by the state machine."""

    def __init__(self, from_state: str, to_state: str) -> None:
        """Record the rejected transition."""
        super().__init__(f"illegal action attempt transition: {from_state} -> {to_state}")
        self.from_state = from_state
        self.to_state = to_state


def can_transition(from_state: str, to_state: str) -> bool:
    """Return whether ``from_state -> to_state`` is allowed."""
    return to_state in TRANSITIONS.get(from_state, frozenset())


def create_proposal(
    *,
    candidate: CandidateIntent,
    based_on_version: int,
    now: datetime | None = None,
) -> ActionAttempt:
    """Build an attempt in the ``proposed`` state.

    Args:
        candidate: The candidate being escalated.
        based_on_version: Runtime version the decision was made on.
        now: Creation time.

    Returns:
        A new :class:`ActionAttempt` (not yet persisted).
    """
    stamp = now or utcnow()
    return ActionAttempt(
        attempt_id=new_id("attempt"),
        candidate_id=candidate.candidate_id,
        state=AttemptState.PROPOSED.value,
        intent=candidate.intent,
        goal=candidate.goal,
        based_on_version=int(based_on_version),
        created_at=stamp,
        updated_at=stamp,
    )


def commit(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    now: datetime | None = None,
    reason: str = "motivational_game",
) -> ActionAttempt:
    """Persist an attempt and move it to ``committed``.

    Args:
        projection: Attempt storage.
        connection: Write connection.
        attempt: Attempt to commit (mutated in place).
        now: Commit time.
        reason: Reason recorded in the transition log.

    Returns:
        The committed attempt.
    """
    stamp = now or utcnow()
    attempt.created_at = attempt.created_at or stamp
    attempt.updated_at = stamp
    projection.upsert(connection, attempt)
    transition(
        projection,
        connection,
        attempt,
        AttemptState.COMMITTED.value,
        reason=reason,
        now=stamp,
    )
    attempt.committed_at = stamp
    projection.upsert(connection, attempt)
    return attempt


def transition(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    to_state: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
    runtime_version: int = 0,
    force: bool = False,
) -> ActionAttempt:
    """Move an attempt to ``to_state``, validating and logging the transition.

    Args:
        projection: Attempt storage.
        connection: Write connection.
        attempt: Attempt to mutate.
        to_state: Target state.
        reason: Human-readable reason for the log.
        now: Transition time.
        runtime_version: Runtime version at transition time.
        force: Skip validation (used only for repairs and tests).

    Returns:
        The updated attempt.

    Raises:
        IllegalTransition: If the transition is not allowed and ``force`` is False.
    """
    from_state = attempt.state
    if from_state == to_state:
        return attempt
    if not force and not can_transition(from_state, to_state):
        raise IllegalTransition(from_state, to_state)
    stamp = now or utcnow()
    projection.record_transition(
        connection,
        attempt_id=attempt.attempt_id,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        runtime_version=runtime_version,
    )
    attempt.state = to_state
    # ``updated_at`` tracks the *logical* clock supplied by the caller, not the
    # wall clock: staleness decisions must be reproducible under a simulated time
    # axis, which is exactly how the long-absence scenarios are tested.
    attempt.updated_at = stamp
    if to_state in {AttemptState.ABORTED.value, AttemptState.FAILED.value, AttemptState.EXPIRED.value}:
        attempt.failure_reason = reason
    projection.upsert(connection, attempt)
    return attempt


def mark_rendering(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    now: datetime | None = None,
) -> ActionAttempt:
    """Move a committed attempt into ``rendering`` and return it."""
    return transition(
        projection, connection, attempt, AttemptState.RENDERING.value, reason="render_requested", now=now
    )


def mark_ready(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    text: str,
    now: datetime | None = None,
) -> ActionAttempt:
    """Attach rendered text and move the attempt to ``ready_to_send``.

    Raises:
        ValueError: If ``text`` is empty, since an empty message can never be sent.
    """
    if not text or not text.strip():
        raise ValueError("rendered text must not be empty")
    attempt.rendered_text = text.strip()
    return transition(
        projection,
        connection,
        attempt,
        AttemptState.READY_TO_SEND.value,
        reason="render_completed",
        now=now,
    )


def mark_sent(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    now: datetime | None = None,
    source_event_id: str | None = None,
) -> ActionAttempt:
    """Move a ready attempt to ``sent``.

    Raises:
        IllegalTransition: If the attempt was not ready to send.
    """
    stamp = now or utcnow()
    if attempt.state != AttemptState.READY_TO_SEND.value:
        raise IllegalTransition(attempt.state, AttemptState.SENT.value)
    return transition(
        projection,
        connection,
        attempt,
        AttemptState.SENT.value,
        reason=f"sent:{source_event_id}" if source_event_id else "sent",
        now=stamp,
    )


def resolve(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    reason: str = "resolved",
    now: datetime | None = None,
) -> ActionAttempt:
    """Move a sent attempt to ``resolved``."""
    return transition(
        projection, connection, attempt, AttemptState.RESOLVED.value, reason=reason, now=now
    )


def abort(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    reason: str,
    reconcile_action: str | None = None,
    now: datetime | None = None,
) -> ActionAttempt:
    """Abort an in-flight attempt, preserving it as history."""
    if reconcile_action:
        attempt.reconcile_action = reconcile_action
    return transition(
        projection,
        connection,
        attempt,
        AttemptState.ABORTED.value,
        reason=reason,
        now=now,
    )


def fail(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    reason: str,
    now: datetime | None = None,
) -> ActionAttempt:
    """Mark an attempt as failed (rendering or delivery error)."""
    return transition(
        projection, connection, attempt, AttemptState.FAILED.value, reason=reason, now=now
    )


def expire(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    reason: str = "expired",
    now: datetime | None = None,
) -> ActionAttempt:
    """Expire an attempt that waited too long to be delivered."""
    return transition(
        projection, connection, attempt, AttemptState.EXPIRED.value, reason=reason, now=now
    )


def is_terminal(state: str) -> bool:
    """Return whether ``state`` is terminal."""
    return state in TERMINAL_STATES


def expire_stale(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    *,
    config: RuntimeConfig,
    now: datetime,
) -> list[str]:
    """Expire attempts that have been waiting beyond their deadlines.

    Args:
        projection: Attempt storage.
        connection: Write connection.
        config: Runtime configuration.
        now: Reference time.

    Returns:
        Identifiers of expired attempts.
    """
    expired: list[str] = []
    limit = timedelta(seconds=config.action.send_expiry_seconds)
    for attempt in projection.list_by_state(IN_FLIGHT_STATES):
        reference = attempt.updated_at or attempt.created_at
        if reference is None:
            continue
        if now - reference <= limit:
            continue
        if attempt.state == AttemptState.PROPOSED.value:
            abort(projection, connection, attempt, reason="proposal_stale", now=now)
        else:
            expire(projection, connection, attempt, reason="send_window_elapsed", now=now)
        expired.append(attempt.attempt_id)
    return expired


def summarise(attempt: ActionAttempt) -> dict[str, Any]:
    """Return a compact rendering of an attempt for prompts and APIs."""
    return {
        "attempt_id": attempt.attempt_id,
        "state": attempt.state,
        "intent": attempt.intent,
        "goal": attempt.goal,
        "committed_at": attempt.committed_at.isoformat() if attempt.committed_at else None,
        "reconcile_action": attempt.reconcile_action,
    }


def reconcile_candidates(candidate_ids: Sequence[str]) -> list[str]:
    """Return the distinct candidate identifiers referenced by attempts."""
    return list(dict.fromkeys(candidate_ids))


def describe_reconcile(action: str) -> str:
    """Return a human-readable explanation of a re-coordination outcome."""
    return {
        ReconcileAction.KEEP.value: "新消息不影响原意图，继续发送",
        ReconcileAction.MERGE.value: "新消息与原意图合流，一起回应",
        ReconcileAction.RERENDER.value: "原意图仍成立，但措辞需要重写",
        ReconcileAction.RESOLVED.value: "用户已经抢先满足了原意图",
        ReconcileAction.ABORT.value: "新情况使原意图不再合适，放弃发送",
    }.get(action, action)


def event_type_for_state(state: str) -> str:
    """Return the raw event type that records a given attempt state."""
    return {
        AttemptState.COMMITTED.value: EventType.PROACTIVE_COMMITTED.value,
        AttemptState.SENT.value: EventType.PROACTIVE_SENT.value,
        AttemptState.ABORTED.value: EventType.PROACTIVE_ABORTED.value,
        AttemptState.FAILED.value: EventType.PROACTIVE_ABORTED.value,
        AttemptState.EXPIRED.value: EventType.PROACTIVE_ABORTED.value,
    }.get(state, EventType.ACTION_ATTEMPT.value)


def apply_reconcile_outcome(
    projection: AttemptProjection,
    connection: sqlite3.Connection,
    attempt: ActionAttempt,
    *,
    action: str,
    new_event_ids: Sequence[str] = (),
    reason: str = "",
    now: datetime | None = None,
) -> ActionAttempt:
    """Apply a re-coordination outcome to an in-flight attempt.

    ``KEEP`` / ``MERGE`` / ``RERENDER`` leave the attempt alive; ``RESOLVED`` and
    ``ABORT`` terminate it, but the ``committed`` history is never erased.

    Args:
        projection: Attempt storage.
        connection: Write connection.
        attempt: In-flight attempt.
        action: One of :class:`~companion_runtime.typing.ReconcileAction`.
        new_event_ids: User events that triggered the re-coordination.
        reason: Reason recorded on the transition.
        now: Reference time.

    Returns:
        The updated attempt.
    """
    attempt.superseded_by_event_ids = sorted(
        set(attempt.superseded_by_event_ids) | set(new_event_ids)
    )
    attempt.reconcile_action = action
    if action == ReconcileAction.ABORT.value:
        return abort(
            projection,
            connection,
            attempt,
            reason=reason or "reconcile:abort",
            reconcile_action=action,
            now=now,
        )
    if action == ReconcileAction.RESOLVED.value:
        if attempt.state == AttemptState.SENT.value:
            return resolve(
                projection,
                connection,
                attempt,
                reason=reason or "reconcile:resolved_by_user",
                now=now,
            )
        return abort(
            projection,
            connection,
            attempt,
            reason=reason or "reconcile:resolved_by_user",
            reconcile_action=action,
            now=now,
        )
    projection.upsert(connection, attempt)
    return attempt


def render_payload(attempt: ActionAttempt, candidate: CandidateIntent | None) -> Mapping[str, Any]:
    """Return the payload the host framework receives for rendering."""
    return {
        "attempt_id": attempt.attempt_id,
        "intent": attempt.intent,
        "goal": attempt.goal,
        "constraints": list(candidate.constraints) if candidate else [],
        "candidate_type": candidate.type if candidate else "contact",
        "based_on_version": attempt.based_on_version,
    }
