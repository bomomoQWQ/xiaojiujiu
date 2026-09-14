"""The single-writer reducer.

Everything that changes Runtime state goes through :class:`Reducer`. Background
models do not hold the pen: they submit a :class:`~companion_runtime.protocol.Proposal`,
the reducer classifies it as APPLY / REBASE / DISCARD, and only then writes.

The reducer also owns:

* the transition of user-facing delivery through ``action_attempt`` states
  (``committed -> rendering -> ready_to_send -> sent -> resolved``);
* the re-coordination of in-flight attempts when the user speaks first;
* outbox claim/lease/ack bookkeeping.

Nesting transactions is safe: :class:`~companion_runtime.db.Database` implements
savepoints, so a reducer call can wrap several component calls atomically.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from . import action as action_module
from . import candidate as candidate_module
from . import memory as memory_module
from . import pool as pool_module
from . import protocol as protocol_module
from . import unfinished as unfinished_module
from .config import RuntimeConfig
from .db import Database
from .eventlog import EventLog, EventQuery
from .projections import Projections
from .typing import (
    Actor,
    AttemptState,
    CandidateStatus,
    EventType,
    OutboxItem,
    OutboxKind,
    OutboxStatus,
    ProtocolAction,
    RawEvent,
    ReconcileAction,
    RuntimeState,
    TaskKind,
    new_id,
)
from .user_model import BehaviourReaction
from .utility import clamp, delta_seconds, ensure_aware, isoformat, utcnow

LOGGER = logging.getLogger("companion_runtime.reducer")


@dataclass(slots=True)
class ProposalResult:
    """What the reducer did with a proposal."""

    task_id: str
    task_type: str
    action: str
    reason: str
    applied: bool = False
    notes: list[str] = field(default_factory=list)
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "action": self.action,
            "reason": self.reason,
            "applied": self.applied,
            "notes": list(self.notes),
            "version": self.version,
        }


@dataclass(slots=True)
class RenderResult:
    """Outcome of the render outbox handler."""

    attempt_id: str
    state: str
    text: str | None = None
    outbox_id: str | None = None
    reconciled: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "attempt_id": self.attempt_id,
            "state": self.state,
            "text": self.text,
            "outbox_id": self.outbox_id,
            "reconciled": self.reconciled,
        }


class Reducer:
    """The only writer of Runtime state."""

    def __init__(
        self,
        *,
        db: Database,
        events: EventLog,
        projections: Projections,
        config: RuntimeConfig,
    ) -> None:
        """Bind the reducer to storage and configuration."""
        self._db = db
        self._events = events
        self._p = projections
        self._config = config

    @property
    def projections(self) -> Projections:
        """Return the projection bundle (read access for services and APIs)."""
        return self._p

    @property
    def events(self) -> EventLog:
        """Return the append-only event log."""
        return self._events

    @property
    def config(self) -> RuntimeConfig:
        """Return the runtime configuration."""
        return self._config

    # ------------------------------------------------------------------ proposals

    def process_proposal(self, proposal: protocol_module.Proposal) -> ProposalResult:
        """Classify and (when allowed) apply one asynchronous proposal.

        Args:
            proposal: The submitted result.

        Returns:
            A :class:`ProposalResult`.
        """
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            known = self._events.get_many(proposal.source_event_ids)
            missing = [eid for eid in proposal.source_event_ids if not any(e.event_id == eid for e in known)]
            newer = self._newer_user_events(proposal, state)
            classification = protocol_module.classify(
                proposal,
                current_version=state.version,
                source_events=known,
                missing_event_ids=missing,
                newer_user_events=newer,
                config=self._config,
            )
            result = ProposalResult(
                task_id=proposal.task_id,
                task_type=proposal.task_type,
                action=classification.action,
                reason=classification.reason,
                notes=list(classification.rebase_notes),
            )

            if classification.action == ProtocolAction.DISCARD.value:
                self._p.tasks.settle(conn, proposal.task_id, f"discard:{classification.reason}", "discarded")
                result.version = state.version
                self._record_proposal(conn, proposal, classification, applied=False, state=state)
                return result

            payload = dict(proposal.payload)
            if classification.action == ProtocolAction.REBASE.value:
                payload, rebase_notes = self._rebase(proposal, payload, state=state)
                result.notes.extend(rebase_notes)

            self._apply_payload(conn, proposal, payload, state=state, result=result)
            self._p.tasks.settle(
                conn, proposal.task_id, f"{classification.action}:{classification.reason}", "settled"
            )
            result.version = self._p.runtime.write(state, conn, expect_version=state.version)
            self._record_proposal(conn, proposal, classification, applied=True, state=state)
            return result

    def _record_proposal(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        classification: protocol_module.Classification,
        *,
        applied: bool,
        state: RuntimeState,
    ) -> None:
        """Append the protocol decision to the raw history."""
        self._events.append(
            EventType.SYSTEM,
            actor=Actor.BACKGROUND_MODEL,
            content=f"proposal:{proposal.task_type}",
            conversation_id=self._config.conversation_id,
            metadata={
                "proposal": proposal.to_dict(),
                "classification": classification.to_dict(),
                "applied": applied,
            },
            source_event_ids=proposal.source_event_ids,
            timestamp=utcnow(),
            runtime_version=state.version,
            connection=conn,
        )

    def _newer_user_events(
        self, proposal: protocol_module.Proposal, state: RuntimeState
    ) -> list[RawEvent]:
        """Return user messages that arrived after the proposal was dispatched."""
        reference = proposal.created_at
        if reference is None:
            return []
        return self._events.read(
            EventQuery(
                conversation_id=self._config.conversation_id,
                event_types=[EventType.USER_MESSAGE.value],
                since=reference,
                limit=10,
            )
        )

    def _rebase(
        self, proposal: protocol_module.Proposal, payload: dict[str, Any], *, state: RuntimeState
    ) -> tuple[dict[str, Any], list[str]]:
        """Recompute a proposal's effects against the current state.

        Returns:
            ``(payload, notes)``.
        """
        notes: list[str] = []
        if proposal.task_type == TaskKind.EMOTION_EVAL.value:
            hours = delta_seconds(utcnow(), proposal.created_at) / 3600.0
            rebased = protocol_module.rebase_emotion_evaluation(
                payload,
                mood_valence=state.mood_valence,
                mood_arousal=state.mood_arousal,
                hours_elapsed=hours,
            )
            return rebased.payload, rebased.notes

        if proposal.task_type == TaskKind.CANDIDATE_GEN.value:
            live_unfinished = [m.unfinished_id for m in self._p.unfinished.list_open()]
            live_memories = [m.memory_id for m in self._p.memory.list_memories(limit=500)]
            rebased = protocol_module.rebase_candidate_payload(
                payload, live_unfinished_ids=live_unfinished, live_memory_ids=live_memories
            )
            return rebased.payload, rebased.notes

        if proposal.task_type == TaskKind.EMOTION_EXPLAIN.value:
            notes.append("explanation always recomputed for the current state")
            return {}, notes

        if proposal.task_type == TaskKind.PROACTIVE_DRAFT.value:
            notes.append("draft must be re-coordinated before send")
            return payload, notes

        notes.append("no rebase rule; payload kept as-is")
        return payload, notes

    def _apply_payload(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        payload: Mapping[str, Any],
        *,
        state: RuntimeState,
        result: ProposalResult,
    ) -> None:
        """Dispatch the payload to its task-specific handler."""
        task_type = proposal.task_type
        if task_type == TaskKind.EMOTION_EVAL.value:
            self._apply_emotion_evaluation(conn, proposal, payload, state=state)
        elif task_type == TaskKind.CANDIDATE_GEN.value:
            operations = [
                candidate_module.CandidateOperation.from_mapping(item)
                for item in (payload.get("operations") or [])
            ]
            pool_result = self._apply_candidate_operations(conn, operations, state=state)
            result.notes.append(f"candidate_changes={len(pool_result.changes)}")
            result.notes.extend(f"rejected:{item}" for item in pool_result.rejected)
        elif task_type == TaskKind.MEMORY_SUMMARY.value:
            self._apply_memory_summary(conn, proposal, payload, state=state)
        elif task_type == TaskKind.USER_MODEL_SUMMARY.value:
            self._apply_user_model_summary(conn, proposal, payload, state=state)
        elif task_type == TaskKind.EMOTION_EXPLAIN.value:
            self._apply_explanation(conn, payload, state=state)
        elif task_type == TaskKind.SHALLOW_TAG.value:
            self._apply_shallow_tag(conn, proposal, payload, state=state)
        else:
            LOGGER.debug("No handler for task type %s; proposal recorded only", task_type)
        result.applied = True

    def _apply_emotion_evaluation(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        payload: Mapping[str, Any],
        *,
        state: RuntimeState,
    ) -> None:
        """Fold an event appraisal into emotion events and mood."""
        from .emotion import EmotionEvaluation, apply_new_emotion_events

        sources = self._events.get_many(proposal.source_event_ids)
        if not sources:
            return
        evaluation = EmotionEvaluation(
            direction=str(payload.get("direction") or "0"),
            impact=clamp(float(payload.get("impact", 0.0))),
            activation=clamp(float(payload.get("activation", 0.0))),
            uncertainty=clamp(float(payload.get("uncertainty", 0.5))),
            relation_signal=str(payload.get("relation_signal") or "neutral"),
            responsibility=str(payload.get("responsibility") or "unclear"),
            confidence=clamp(float(payload.get("confidence", 0.5))),
            source="semantic",
        )
        active = self._p.emotion.list_active()
        _, created = apply_new_emotion_events(
            evaluations=[(sources[0], evaluation)],
            active=active,
            state=state,
            config=self._config.emotion,
        )
        for emotion_event in created:
            self._p.emotion.upsert(conn, emotion_event)

    def _apply_candidate_operations(
        self,
        conn: sqlite3.Connection,
        operations: Sequence[candidate_module.CandidateOperation],
        *,
        state: RuntimeState,
    ) -> candidate_module.PoolApplyResult:
        """Apply candidate pool operations through the pool manager."""
        context = pool_module.PoolContext(projections=self._p, config=self._config)
        return pool_module.apply_operations(
            context, conn, operations, now=utcnow(), state=state
        )

    def _apply_memory_summary(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        payload: Mapping[str, Any],
        *,
        state: RuntimeState,
    ) -> None:
        """Consolidate memory candidates with an optional model-provided summary."""
        summaries = {str(k): str(v) for k, v in (payload.get("summaries") or {}).items()}

        def summarizer(candidate: memory_module.MemoryCandidate) -> str:
            return summaries.get(candidate.candidate_id, candidate.summary)

        memory_module.consolidate(
            self._p.memory,
            conn,
            config=self._config,
            summarizer=summarizer if summaries else None,
        )

    def _apply_user_model_summary(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        payload: Mapping[str, Any],
        *,
        state: RuntimeState,
    ) -> None:
        """Store a natural-language summary of the user model."""
        summary = {
            "summary": str(payload.get("summary") or ""),
            "confidence": clamp(float(payload.get("confidence", 0.5))),
            "generated_at": isoformat(utcnow()),
            "based_on_version": proposal.based_on_version,
        }
        if not summary["summary"]:
            raise ValueError("user model summary payload is empty")
        self._p.user_model.set_summary(conn, summary)

    def _apply_explanation(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
        *,
        state: RuntimeState,
    ) -> None:
        """Cache a psychological explanation produced elsewhere."""
        cache_key = str(payload.get("cache_key") or "")
        if not cache_key:
            raise ValueError("explanation payload requires cache_key")
        stored = {k: v for k, v in payload.items() if k != "cache_key"}
        self._p.emotion.store_explanation(
            conn, cache_key=cache_key, payload=stored, source="semantic_proposal"
        )

    def _apply_shallow_tag(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        payload: Mapping[str, Any],
        *,
        state: RuntimeState,
    ) -> None:
        """Attach a shallow semantic tag as an interpretation version."""
        if not proposal.source_event_ids:
            return
        self._p.interpretations.add_version(
            conn,
            target_kind="event",
            target_id=proposal.source_event_ids[0],
            content=str(payload.get("tag") or payload.get("summary") or ""),
            confidence=clamp(float(payload.get("confidence", 0.5))),
            source_version=proposal.based_on_version,
            source_event_ids=proposal.source_event_ids,
        )

    # ------------------------------------------------------- re-coordination

    def reconcile_attempt(
        self,
        *,
        attempt_id: str,
        new_events: Sequence[RawEvent],
        now: datetime | None = None,
    ) -> protocol_module.ReconcileDecision:
        """Re-coordinate one in-flight attempt after the user speaks first.

        Args:
            attempt_id: Attempt to re-coordinate.
            new_events: User events that arrived during the in-flight window.
            now: Reference time.

        Returns:
            The applied :class:`~companion_runtime.protocol.ReconcileDecision`.

        Raises:
            KeyError: If the attempt does not exist.
        """
        stamp = ensure_aware(now) or utcnow()
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            attempt = self._p.attempts.get(attempt_id)
            if attempt is None:
                raise KeyError(f"unknown attempt: {attempt_id}")
            candidate = (
                self._p.candidates.get(attempt.candidate_id) if attempt.candidate_id else None
            )
            decision = protocol_module.reconcile(
                attempt_state=attempt.state,
                attempt_intent=attempt.intent,
                attempt_goal=attempt.goal,
                candidate_type=candidate.type if candidate else "contact",
                candidate_invalidate_when=candidate.invalidate_when if candidate else (),
                new_events=new_events,
                now=stamp,
            )
            event_ids = [event.event_id for event in new_events]
            if decision.action == ReconcileAction.KEEP.value:
                attempt.superseded_by_event_ids = sorted(
                    set(attempt.superseded_by_event_ids) | set(event_ids)
                )
                self._p.attempts.upsert(conn, attempt)
            else:
                action_module.apply_reconcile_outcome(
                    self._p.attempts,
                    conn,
                    attempt,
                    action=decision.action,
                    new_event_ids=event_ids,
                    reason=f"reconcile:{decision.reason}",
                    now=stamp,
                )
            if decision.action in {ReconcileAction.ABORT.value, ReconcileAction.RESOLVED.value}:
                if attempt.outbox_id:
                    self._p.outbox.cancel(
                        conn, attempt.outbox_id, reason=f"reconcile:{decision.reason}"
                    )
                self._p.outbox.cancel_for_attempt(
                    conn, attempt.attempt_id, reason=f"reconcile:{decision.reason}"
                )
                if candidate is not None:
                    target_status = (
                        CandidateStatus.RESOLVED.value
                        if decision.action == ReconcileAction.RESOLVED.value
                        else CandidateStatus.RETIRED.value
                    )
                    self._p.candidates.set_status(
                        conn, candidate.candidate_id, target_status, reason=decision.reason
                    )
            elif decision.action == ReconcileAction.MERGE.value:
                if attempt.outbox_id:
                    item = self._p.outbox.get(attempt.outbox_id)
                    if item is not None and item.status == OutboxStatus.PENDING.value:
                        item.payload = dict(item.payload) | {
                            "merge_event_ids": event_ids,
                            "merge_note": "user spoke first; merge the reply into this intent",
                        }
                        self._p.outbox.enqueue(conn, item)
            elif decision.action == ReconcileAction.RERENDER.value:
                attempt.superseded_by_event_ids = sorted(
                    set(attempt.superseded_by_event_ids) | set(event_ids)
                )
                self._p.attempts.upsert(conn, attempt)

            self._events.append(
                EventType.SYSTEM,
                actor=Actor.RUNTIME,
                content=f"reconcile:{decision.action}",
                conversation_id=self._config.conversation_id,
                metadata={"decision": decision.to_dict(), "attempt_id": attempt_id},
                source_event_ids=event_ids,
                timestamp=stamp,
                runtime_version=state.version,
                connection=conn,
            )
            self._p.runtime.write(state, conn, expect_version=state.version)
            return decision

    def reconcile_pending_attempts(
        self, *, new_events: Sequence[RawEvent], now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Re-coordinate every in-flight attempt after new user events.

        Args:
            new_events: User events that just arrived.
            now: Reference time.

        Returns:
            One decision per re-coordinated attempt.
        """
        if not new_events:
            return []
        decisions: list[dict[str, Any]] = []
        for attempt in self._p.attempts.list_by_state(action_module.IN_FLIGHT_STATES):
            decision = self.reconcile_attempt(
                attempt_id=attempt.attempt_id, new_events=new_events, now=now
            )
            decisions.append({"attempt_id": attempt.attempt_id} | decision.to_dict())
        return decisions

    # ------------------------------------------------------------------ outbox

    def claim_outbox(
        self,
        *,
        owner: str,
        now: datetime | None = None,
        limit: int = 1,
        kinds: Sequence[str] | None = None,
    ) -> list[OutboxItem]:
        """Lease ready outbox rows for ``owner``.

        Args:
            owner: Lease owner (worker identifier).
            now: Reference time.
            limit: Maximum number of rows.
            kinds: Optional restriction to specific kinds.

        Returns:
            The leased items.
        """
        stamp = ensure_aware(now) or utcnow()
        with self._db.transaction() as conn:
            return self._p.outbox.claim(
                conn,
                owner=owner,
                now=stamp,
                lease_seconds=self._config.outbox.lease_seconds,
                limit=limit,
                kinds=kinds,
            )

    def ack_outbox(self, outbox_id: str, *, now: datetime | None = None) -> bool:
        """Acknowledge a leased outbox row as delivered."""
        with self._db.transaction() as conn:
            return self._p.outbox.ack(conn, outbox_id, now)

    def nack_outbox(self, outbox_id: str, *, error: str, terminal: bool = False) -> bool:
        """Return a leased outbox row to the queue or fail it terminally."""
        stamp = utcnow()
        with self._db.transaction() as conn:
            retry_at = stamp + timedelta(seconds=self._config.outbox.retry_backoff_seconds)
            return self._p.outbox.nack(
                conn, outbox_id, error=error, retry_at=retry_at, terminal=terminal
            )

    def complete_render(
        self,
        *,
        outbox_id: str,
        text: str,
        now: datetime | None = None,
    ) -> RenderResult:
        """Handle a render result: attach text and move the attempt to ``ready_to_send``.

        Args:
            outbox_id: The render outbox row being acknowledged.
            text: Rendered message text produced by the host main LLM.
            now: Reference time.

        Returns:
            A :class:`RenderResult`.

        Raises:
            KeyError: If the outbox row or its attempt is unknown.
        """
        stamp = ensure_aware(now) or utcnow()
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            item = self._p.outbox.get(outbox_id)
            if item is None:
                raise KeyError(f"unknown outbox row: {outbox_id}")
            attempt_id = str(item.payload.get("attempt_id") or "")
            attempt = self._p.attempts.get(attempt_id) if attempt_id else None
            if attempt is None:
                raise KeyError(f"unknown attempt for outbox row {outbox_id}: {attempt_id!r}")
            try:
                if attempt.state == AttemptState.COMMITTED.value:
                    action_module.mark_rendering(self._p.attempts, conn, attempt, now=stamp)
                action_module.mark_ready(self._p.attempts, conn, attempt, text=text, now=stamp)
            except (ValueError, action_module.IllegalTransition) as exc:
                action_module.fail(self._p.attempts, conn, attempt, reason=str(exc), now=stamp)
                self._p.outbox.nack(conn, outbox_id, error=str(exc), terminal=True)
                self._p.runtime.write(state, conn, expect_version=state.version)
                return RenderResult(attempt_id=attempt.attempt_id, state=attempt.state, outbox_id=outbox_id)

            self._p.outbox.ack(conn, outbox_id, stamp)
            send_item = OutboxItem(
                outbox_id=new_id("outbox"),
                kind=OutboxKind.SEND.value,
                payload={
                    "attempt_id": attempt.attempt_id,
                    "text": attempt.rendered_text,
                    "intent": attempt.intent,
                    "goal": attempt.goal,
                },
                priority=10,
                available_at=stamp,
                created_at=stamp,
                max_attempts=self._config.outbox.max_attempts,
                conversation_id=item.conversation_id,
            )
            self._p.outbox.enqueue(conn, send_item)
            attempt.outbox_id = send_item.outbox_id
            self._p.attempts.upsert(conn, attempt)
            self._p.runtime.write(state, conn, expect_version=state.version)
            return RenderResult(
                attempt_id=attempt.attempt_id,
                state=attempt.state,
                text=attempt.rendered_text,
                outbox_id=send_item.outbox_id,
                reconciled=attempt.reconcile_action,
            )

    def fail_render(self, *, outbox_id: str, error: str, now: datetime | None = None) -> bool:
        """Handle a failed render: fail the attempt and the outbox row."""
        stamp = ensure_aware(now) or utcnow()
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            item = self._p.outbox.get(outbox_id)
            if item is None:
                return False
            attempt_id = str(item.payload.get("attempt_id") or "")
            attempt = self._p.attempts.get(attempt_id) if attempt_id else None
            if attempt is not None and attempt.state not in action_module.TERMINAL_STATES:
                action_module.fail(self._p.attempts, conn, attempt, reason=error, now=stamp)
                self._events.append(
                    EventType.PROACTIVE_ABORTED,
                    actor=Actor.RUNTIME,
                    content=error,
                    conversation_id=self._config.conversation_id,
                    metadata={"attempt_id": attempt.attempt_id, "stage": "render"},
                    timestamp=stamp,
                    runtime_version=state.version,
                    connection=conn,
                )
            self._p.outbox.nack(conn, outbox_id, error=error, terminal=True)
            self._p.runtime.write(state, conn, expect_version=state.version)
            return True

    def mark_delivered(
        self,
        *,
        outbox_id: str,
        now: datetime | None = None,
        reaction: BehaviourReaction | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Handle a delivery result: transition the attempt, release pressure, observe.

        On success the attempt moves to ``sent``, the raw history gets a
        ``proactive_sent`` event, and the post-contact dynamics are applied. The
        user's reaction is recorded later through
        :meth:`companion_runtime.runtime.Runtime.observe_reply`.

        Args:
            outbox_id: The send outbox row being acknowledged.
            now: Reference time.
            reaction: Immediate reaction, when already known.
            success: Whether delivery succeeded.
            error: Error text when ``success`` is false.

        Returns:
            A mapping describing the outcome.

        Raises:
            KeyError: If the outbox row is unknown.
        """
        stamp = ensure_aware(now) or utcnow()
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            item = self._p.outbox.get(outbox_id)
            if item is None:
                raise KeyError(f"unknown outbox row: {outbox_id}")
            attempt_id = str(item.payload.get("attempt_id") or "")
            attempt = self._p.attempts.get(attempt_id) if attempt_id else None
            if not success:
                if attempt is not None:
                    action_module.fail(
                        self._p.attempts, conn, attempt, reason=error or "delivery_failed", now=stamp
                    )
                self._p.outbox.nack(conn, outbox_id, error=error or "delivery_failed", terminal=True)
                self._p.runtime.write(state, conn, expect_version=state.version)
                return {"delivered": False, "attempt_id": attempt_id, "error": error}

            self._p.outbox.ack(conn, outbox_id, stamp)
            if attempt is None:
                self._p.runtime.write(state, conn, expect_version=state.version)
                return {"delivered": True, "attempt_id": None}

            action_module.mark_sent(self._p.attempts, conn, attempt, now=stamp)
            self._events.append(
                EventType.PROACTIVE_SENT,
                actor=Actor.ASSISTANT,
                content=attempt.rendered_text,
                conversation_id=item.conversation_id,
                metadata={
                    "attempt_id": attempt.attempt_id,
                    "intent": attempt.intent,
                    "goal": attempt.goal,
                    "based_on_version": attempt.based_on_version,
                },
                timestamp=stamp,
                runtime_version=state.version,
                connection=conn,
            )
            self._events.append(
                EventType.ASSISTANT_MESSAGE,
                actor=Actor.ASSISTANT,
                content=attempt.rendered_text,
                conversation_id=item.conversation_id,
                metadata={"proactive": True, "attempt_id": attempt.attempt_id},
                timestamp=stamp,
                runtime_version=state.version,
                connection=conn,
            )
            state.last_contact_at = stamp
            state.contact_count_today += 1
            self._p.situation.upsert(
                conn,
                kind="fact",
                content=f"我主动联系了用户：{attempt.intent}",
                salience=0.5,
                confidence=1.0,
                source_kind="attempt",
                source_id=attempt.attempt_id,
                expires_at=stamp + timedelta(hours=12),
            )
            version = self._p.runtime.write(state, conn, expect_version=state.version)

        # The user's reaction is intentionally *not* recorded here: it arrives
        # later and is handled by Runtime.observe_reply, so that a missing
        # observation can never roll back a successful send.
        return {
            "delivered": True,
            "attempt_id": attempt.attempt_id,
            "state": attempt.state,
            "version": version,
        }

    # ------------------------------------------------------------------ helpers

    def register_task(
        self,
        *,
        task_id: str,
        task_type: str,
        based_on_version: int,
        source_event_ids: Sequence[str],
        priority: str,
    ) -> str:
        """Record a background-task snapshot at dispatch time."""
        with self._db.transaction() as conn:
            return self._p.tasks.register(
                conn,
                task_id=task_id,
                task_type=task_type,
                based_on_version=based_on_version,
                source_event_ids=source_event_ids,
                priority=priority,
            )
