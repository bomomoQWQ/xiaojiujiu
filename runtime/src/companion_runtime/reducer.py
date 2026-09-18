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
from . import motivation as motivation_module
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
    MemoryCandidate,
    MemoryKind,
    OutboxItem,
    OutboxKind,
    OutboxStatus,
    Priority,
    ProtocolAction,
    RawEvent,
    ReconcileAction,
    RuntimeState,
    TaskKind,
    is_event_identifier,
    new_id,
)
from .user_model import BehaviourReaction
from .utility import clamp, delta_seconds, ensure_aware, isoformat, max_datetime, utcnow

LOGGER = logging.getLogger("companion_runtime.reducer")



#: Outbox statuses in which a row can still be worked on. A row in one of these
#: states is the live one for its attempt, and is the row a report belongs to.
_LIVE_OUTBOX_STATUSES: tuple[str, ...] = (
    OutboxStatus.PENDING.value,
    OutboxStatus.LEASED.value,
)

#: Attempt states a render result can still be applied to.
_RENDERABLE_ATTEMPT_STATES: tuple[str, ...] = (
    AttemptState.COMMITTED.value,
    AttemptState.RENDERING.value,
)

#: Attempt states that have left the render stage for good. ``sent`` belongs here
#: even though the state machine lets it reach ``resolved``: a delivered message
#: is never rendered again.
_ATTEMPT_STATES_BEYOND_RENDER: frozenset[str] = frozenset(
    {
        AttemptState.READY_TO_SEND.value,
        AttemptState.SENT.value,
        AttemptState.RESOLVED.value,
        AttemptState.ABORTED.value,
        AttemptState.EXPIRED.value,
        AttemptState.FAILED.value,
    }
)


def _terminate_undeliverable_attempt(
    projection: Any,
    connection: sqlite3.Connection,
    attempt: Any,
    *,
    reason: str,
    now: datetime,
) -> bool:
    """Move an attempt that can never be delivered to its terminal state.

    The transition depends on where the attempt stopped:

    * ``proposed`` may be aborted but not failed, so it is aborted;
    * ``committed`` may not fail directly either: it reaches ``failed`` through
      ``rendering``, which is also the honest history - the work was handed to a
      renderer and never came back;
    * ``rendering`` and ``ready_to_send`` fail directly.

    An attempt that already left the Runtime (``sent``) or is already terminal is
    left untouched: a failed *report* about a message that was delivered must
    never rewrite that history.

    Args:
        projection: Attempt storage.
        connection: Write connection.
        attempt: Attempt to close.
        reason: Reason recorded on the transition and in ``failure_reason``.
        now: Reference time.

    Returns:
        ``True`` when this call transitioned the attempt.
    """
    if action_module.is_terminal(attempt.state) or attempt.state == AttemptState.SENT.value:
        return False
    if attempt.state == AttemptState.PROPOSED.value:
        action_module.abort(projection, connection, attempt, reason=reason, now=now)
        return True
    if attempt.state == AttemptState.COMMITTED.value:
        action_module.mark_rendering(projection, connection, attempt, now=now)
    action_module.fail(projection, connection, attempt, reason=reason, now=now)
    return True


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
    """Outcome of the render outbox handler.

    The three flags exist because "the call returned" and "the render was
    applied" are different facts, and a caller that has just reported a render
    has to be able to tell them apart:

    * ``applied``    -- this call changed persistent state;
    * ``duplicate``  -- the effect was already recorded, so nothing was applied
      a second time (a replay of a render the Runtime already absorbed);
    * ``reason``     -- why nothing was applied, when ``applied`` is false.

    ``outbox_id`` is the **send** row the rendered text was queued into, and is
    ``None`` whenever no send row belongs to the outcome.
    """

    attempt_id: str
    state: str
    text: str | None = None
    outbox_id: str | None = None
    reconciled: str | None = None
    applied: bool = True
    duplicate: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "attempt_id": self.attempt_id,
            "state": self.state,
            "text": self.text,
            "outbox_id": self.outbox_id,
            "reconciled": self.reconciled,
            "applied": self.applied,
            "duplicate": self.duplicate,
            "reason": self.reason,
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

    def process_proposal(
        self, proposal: protocol_module.Proposal, *, now: datetime | None = None
    ) -> ProposalResult:
        """Classify and (when allowed) apply one asynchronous proposal.

        A ``task_id`` identifies one dispatched task, so a second submission of
        the same id is a redelivery (a retried HTTP call, a replayed queue item),
        not a second result. It is answered with the same shape as any other
        non-application -- ``action=discard``, ``applied=false`` -- and is
        recorded in history, because the check and the decision happen inside the
        same transaction as the write: two concurrent submissions of one id
        cannot both pass it.

        Args:
            proposal: The submitted result.
            now: The moment this entry is being processed; it is handed to the
                Runtime's clock first, so a proposal that lands after a long silence is
                classified against an up-to-date state rather than against the last
                tick (design §86.4).

        Returns:
            A :class:`ProposalResult`.
        """
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            # A proposal may be grounded in more than raw events: a deep refresh can
            # cite a memory candidate or a stored memory as the entity it reasoned
            # about, and provenance is recorded per operation. Only identifiers that
            # name an *event* can be missing as events - treating the others as
            # missing evidence discarded perfectly grounded work.
            event_ids = [eid for eid in proposal.source_event_ids if is_event_identifier(eid)]
            known = self._events.get_many(event_ids)
            missing = [eid for eid in event_ids if not any(e.event_id == eid for e in known)]
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

            if self._task_already_settled(proposal.task_id):
                duplicate = protocol_module.Classification(
                    action=ProtocolAction.DISCARD.value,
                    reason="duplicate_task_id",
                    sensitivity=classification.sensitivity,
                    version_gap=classification.version_gap,
                )
                result.action = duplicate.action
                result.reason = duplicate.reason
                result.applied = False
                result.version = state.version
                self._record_proposal(conn, proposal, duplicate, applied=False, state=state)
                return result

            if classification.action == ProtocolAction.DISCARD.value:
                self._p.tasks.settle(conn, proposal.task_id, f"discard:{classification.reason}", "discarded")
                result.version = state.version
                self._record_proposal(conn, proposal, classification, applied=False, state=state)
                return result

            payload = dict(proposal.payload)
            if classification.action == ProtocolAction.REBASE.value:
                payload, rebase_notes = self._rebase(proposal, payload, state=state)
                result.notes.extend(rebase_notes)

            self._p.tasks.register(
                conn,
                task_id=proposal.task_id,
                task_type=proposal.task_type,
                based_on_version=proposal.based_on_version,
                source_event_ids=proposal.source_event_ids,
                priority=Priority.P1_NEAR_REALTIME.value,
            )
            self._apply_payload(conn, proposal, payload, state=state, result=result)
            self._p.tasks.settle(
                conn, proposal.task_id, f"{classification.action}:{classification.reason}", "settled"
            )
            result.version = self._p.runtime.write(state, conn, expect_version=state.version)
            self._record_proposal(conn, proposal, classification, applied=True, state=state)
            return result

    def _task_already_settled(self, task_id: str) -> bool:
        """Return whether ``task_id`` was already applied or discarded.

        ``background_tasks`` records a snapshot at dispatch time and a status on
        settlement, so a row that is no longer ``in_flight`` means this task id
        has already been dealt with. A task id with no row at all was never
        dispatched through the Runtime, and is therefore not a redelivery of
        anything the Runtime knows about.
        """
        if not task_id:
            return False
        record = self._p.tasks.get(task_id)
        if record is None:
            return False
        return str(record.get("status") or "") not in {"in_flight", ""}

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
            # Measure staleness from the dispatch moment; fall back to "now" when
            # the caller did not supply one, which simply means no damping.
            reference = proposal.created_at or utcnow()
            hours = delta_seconds(utcnow(), reference) / 3600.0
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
            self._apply_explanation(conn, payload, state=state, now=proposal.created_at)
        elif task_type == TaskKind.SHALLOW_TAG.value:
            self._apply_shallow_tag(conn, proposal, payload, state=state)
        elif task_type == TaskKind.DEEP_REFRESH.value:
            self._apply_deep_refresh(conn, proposal, payload, state=state, result=result)
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

    def _apply_appraisal(
        self,
        conn: sqlite3.Connection,
        body: Mapping[str, Any],
        event_ids: Sequence[str],
        *,
        state: RuntimeState,
    ) -> None:
        """Fold a deep refresh's emotional reading of an event into emotion and mood.

        This is the second half of the deferral contract. The rule appraiser only runs
        for events the rule table already settled, so before this existed an event could
        be read by a refresh, settle, and still leave no trace in how she felt - which is
        why every instance sat at ``mood_valence`` 0.0 while thousands of events went
        through. ``appraise_event``'s docstring states the intent: a semantic provider may
        replace it, with an identical output contract. ``apply_new_emotion_events`` keeps
        its own floor (``emotion.min_event_impact``), so a small reading changes nothing.
        """
        from .emotion import EmotionEvaluation, apply_new_emotion_events

        sources = self._events.get_many(list(event_ids))
        if not sources:
            return
        impact = clamp(float(body.get("impact", 0.0)))
        confidence = clamp(float(body.get("confidence", 0.5)))
        evaluation = EmotionEvaluation(
            direction=str(body.get("direction") or "0"),
            impact=impact,
            activation=clamp(float(body.get("activation", impact * 0.8))),
            uncertainty=clamp(float(body.get("uncertainty", 1.0 - confidence))),
            relation_signal=str(body.get("relation_signal") or "neutral"),
            responsibility=str(body.get("responsibility") or "unclear"),
            confidence=confidence,
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
        now: datetime | None = None,
    ) -> None:
        """Cache a psychological explanation produced elsewhere.

        The proposal's own timestamp is used as the storage time rather than the
        wall clock: the freshness rule compares that stamp against the caller's
        ``now``, so writing wall-clock time while everything else runs on the
        proposal's clock would make a fresh entry look like it came from the future.
        """
        cache_key = str(payload.get("cache_key") or "")
        if not cache_key:
            raise ValueError("explanation payload requires cache_key")
        stored = {k: v for k, v in payload.items() if k != "cache_key"}
        self._p.emotion.store_explanation(
            conn,
            cache_key=cache_key,
            payload=stored,
            source="semantic_proposal",
            now=now,
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

    def _apply_deep_refresh(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        payload: Mapping[str, Any],
        *,
        state: RuntimeState,
        result: ProposalResult,
    ) -> None:
        """Apply one grounded deep-refresh bundle (patch v0.2 sections 19-20).

        The bundle is a *set of suggestions*, and this handler is the only place
        where they can become state. Three rules keep that safe:

        1. Every operation already passed grounding in
           :mod:`companion_runtime.deep_refresh`, so an operation naming an
           entity that does not exist never reaches here.
        2. Nothing is applied that the Runtime cannot attribute to a real source
           event, so an invented memory cannot enter the archive.
        3. The handler never raises for a partially valid bundle: one bad
           operation is recorded in ``result.notes`` and skipped, because a
           refresh that understood most of the backlog is still valuable.

        Args:
            conn: Open write transaction.
            proposal: The proposal carrying the bundle.
            payload: Grounded operation list plus the optional interpretation.
            state: Current runtime state.
            result: Result object used to record what was applied and skipped.
        """
        operations = payload.get("operations") or []
        applied: dict[str, int] = {}
        skipped: list[str] = []
        #: Only events an applied operation actually referred to may be closed.
        #: Passing the whole backlog as the proposal's sources would let one
        #: reinterpretation silently mark unrelated events as understood, which is
        #: precisely the failure the unresolved state exists to prevent.
        touched: set[str] = set()

        for operation in operations:
            if not isinstance(operation, Mapping):
                skipped.append("malformed_operation")
                continue
            kind = str(operation.get("kind") or "")
            body = operation.get("payload")
            if not isinstance(body, Mapping):
                skipped.append(f"{kind}:missing_payload")
                continue
            sources = [str(item) for item in (operation.get("sources") or [])]
            try:
                if kind == "reinterpretation":
                    self._apply_reinterpretation(conn, proposal, body, sources, state=state)
                elif kind == "psychological_interpretation":
                    self._apply_interpretation_cache(
                        conn, body, state=state, now=proposal.created_at
                    )
                elif kind == "candidate_intent":
                    self._apply_candidate_operations(
                        conn,
                        [candidate_module.CandidateOperation.from_mapping(dict(body))],
                        state=state,
                    )
                elif kind == "memory":
                    self._apply_memory_suggestion(conn, body, sources)
                elif kind == "unfinished_matter":
                    created = self._apply_unfinished_suggestion(conn, body, sources)
                    if not created:
                        # A restated obligation is not a failure -- it is the refresh
                        # re-reading something already understood -- so it is recorded
                        # as a skip rather than counted as an applied operation. The
                        # source events still count as understood: they were.
                        skipped.append(f"{kind}:already_spoken_for")
                        touched.update(sources)
                        continue
                elif kind == "user_model_evidence":
                    self._apply_user_model_evidence(conn, body, sources)
                elif kind == "event_appraisal":
                    self._apply_appraisal(conn, body, sources, state=state)
                else:
                    skipped.append(f"unknown_kind:{kind}")
                    continue
            except Exception as exc:  # noqa: BLE001 - one bad op must not void the bundle
                LOGGER.warning("Deep refresh operation %s failed: %s", kind, exc)
                skipped.append(f"{kind}:{type(exc).__name__}")
                continue
            applied[kind] = applied.get(kind, 0) + 1
            touched.update(sources)

        # An event stops being unresolved only once something was actually said
        # about it; a skipped operation, or an operation about a different event,
        # must not close it.
        refreshed: list[str] = []
        for event_id in sorted(touched):
            if not self._p.semantics.get(event_id):
                continue
            if self._p.semantics.settle_from_deep_refresh(
                conn,
                event_id=event_id,
                deep_refresh_id=proposal.task_id,
                version=state.version,
            ):
                refreshed.append(event_id)

        result.notes.append(f"deep_refresh_applied={applied}")
        if refreshed:
            result.notes.append(f"settled_events={len(refreshed)}")
        for note in skipped:
            result.notes.append(f"skipped:{note}")

    def _apply_reinterpretation(
        self,
        conn: sqlite3.Connection,
        proposal: protocol_module.Proposal,
        body: Mapping[str, Any],
        sources: Sequence[str],
        *,
        state: RuntimeState,
    ) -> None:
        """Append a new interpretation version and a reappraisal event.

        History is never rewritten: the previous version stays, and the new one
        explicitly supersedes it. That is what makes "I only understood this
        later" auditable rather than a silent edit of the past.

        ``state`` is used only for the ``runtime_version`` stamped on the event appended to
        the log (design §67's ``reappraisal_event``) - the projection row and the log entry
        are two views of one fact and must carry the same version.
        """
        target_event = sources[0] if sources else (
            proposal.source_event_ids[0] if proposal.source_event_ids else None
        )
        if target_event is None:
            raise ValueError("reinterpretation requires a target event")
        content = str(body.get("content") or body.get("summary") or "").strip()
        if not content:
            raise ValueError("reinterpretation requires content")
        previous = self._p.interpretations.latest("event", target_event)
        # The proposal's sources already include the target when the model grounded its
        # reading on the very event it rereads, so the naive ``[target, *sources]``
        # duplicated it. Provenance is a set of events, and a repeated identifier reads as
        # two pieces of evidence for one fact.
        provenance = list(dict.fromkeys([target_event, *proposal.source_event_ids]))
        record = self._p.interpretations.add_version(
            conn,
            target_kind="event",
            target_id=target_event,
            content=content,
            confidence=clamp(float(body.get("confidence", 0.6))),
            source_version=proposal.based_on_version,
            source_event_ids=provenance,
            supersedes_id=str(previous["interpretation_id"]) if previous else None,
        )
        reappraisal_id = self._p.interpretations.add_reappraisal(
            conn,
            source_event_ids=provenance,
            new_interpretation=str(record["content"]),
            previous_interpretation=str(previous["content"]) if previous else None,
            delta_summary=str(body.get("realized_text") or content),
        )
        # Design §67 asks for a ``reappraisal_event``, not only a projection row: the event
        # log is the Runtime's history of record, and "I understood this later" is exactly
        # the kind of thing it exists to keep. Until this append existed,
        # ``EventType.REAPPRAISAL`` was a declared-but-never-emitted member and the two
        # records of the same fact disagreed: the projection knew, the log did not.
        self._events.append(
            EventType.REAPPRAISAL,
            actor=Actor.RUNTIME,
            content=str(record["content"]),
            conversation_id=self._config.conversation_id,
            metadata={
                "reappraisal_id": reappraisal_id,
                "interpretation_id": record["interpretation_id"],
                "target_event_id": target_event,
                "previous_interpretation": str(previous["content"]) if previous else None,
                "supersedes_id": str(previous["interpretation_id"]) if previous else None,
            },
            source_event_ids=provenance,
            timestamp=proposal.created_at,
            runtime_version=state.version,
            connection=conn,
        )

    def _apply_interpretation_cache(
        self,
        conn: sqlite3.Connection,
        body: Mapping[str, Any],
        *,
        state: RuntimeState,
        now: datetime | None = None,
    ) -> None:
        """Store a deep psychological interpretation in the explanation cache.

        Stored at the proposal's timestamp (see :meth:`_apply_explanation`), so the
        entry's age is measured on the same clock the refresh itself ran on.
        """
        fields = ("experience", "focus", "conflict", "impulse", "inhibition", "expression")
        stored = {name: str(body.get(name) or "") for name in fields}
        if not any(stored.values()):
            raise ValueError("psychological_interpretation is empty")
        cache_key = str(body.get("cache_key") or "").strip()
        if not cache_key:
            from .emotion import EmotionExplainer

            cache_key = EmotionExplainer.cache_key(state, self._p.emotion.list_active())
        stored["source"] = "deep_refresh"
        self._p.emotion.store_explanation(
            conn, cache_key=cache_key, payload=stored, source="deep_refresh", now=now
        )

    def _apply_memory_suggestion(
        self,
        conn: sqlite3.Connection,
        body: Mapping[str, Any],
        sources: Sequence[str],
    ) -> None:
        """Record a suggested long-term memory as a *candidate*.

        A deep refresh may propose a memory, but it may not create one directly:
        the candidate still has to pass consolidation, which applies the same
        value and transience criteria as every other memory. Otherwise a single
        plausible-sounding refresh could fill the archive with things that were
        never worth keeping.
        """
        summary = str(body.get("summary") or "").strip()
        if not summary:
            raise ValueError("memory suggestion requires summary")
        if not sources:
            raise ValueError("memory suggestion requires at least one source")
        self._p.memory.upsert_candidate(
            conn,
            MemoryCandidate(
                candidate_id=str(body.get("candidate_id") or new_id("memory")),
                summary=summary,
                kind=str(body.get("kind") or body.get("type") or MemoryKind.EPISODIC.value),
                source_event_ids=list(sources),
                value=clamp(float(body.get("importance", 0.6))),
                confidence=clamp(float(body.get("confidence", 0.6))),
                topics=[str(item) for item in (body.get("topics") or [])],
                created_at=utcnow(),
            ),
        )

    def _apply_unfinished_suggestion(
        self,
        conn: sqlite3.Connection,
        body: Mapping[str, Any],
        sources: Sequence[str],
    ) -> bool:
        """Create an unfinished matter proposed by a deep refresh.

        Returns:
            ``True`` when a matter was created, ``False`` when the proposal restated
            one that already exists.

        The guard is not optional here the way it is on the ingest path. A refresh
        re-reads the same unresolved events on every run, so an event it has already
        understood gets interpreted again and the model re-derives the same
        obligation with fresh wording. Measured: ten open matters from two events,
        five each, over twelve hours, none resolved. Unbounded growth in the open set
        is not cosmetic - every matter is injected into the context block and feeds
        the candidate grounding.
        """
        title = str(body.get("title") or "").strip()
        if not title:
            raise ValueError("unfinished suggestion requires a title")
        existing = unfinished_module.subject_guards(
            self._p.unfinished.list_all(limit=200), now=utcnow()
        )
        if unfinished_module.already_spoken_for(title, sources, existing):
            return False
        unfinished_module.create(
            self._p.unfinished,
            conn,
            unfinished_module.UnfinishedProposal(
                title=title,
                source_event_ids=list(sources),
                priority=clamp(float(body.get("priority", 0.5))),
                resolution_conditions=[
                    str(item) for item in (body.get("resolution_conditions") or [])
                ],
                topics=[str(item) for item in (body.get("topics") or [])],
            ),
            config=self._config,
            now=utcnow(),
        )
        return True

    def _apply_user_model_evidence(
        self,
        conn: sqlite3.Connection,
        body: Mapping[str, Any],
        sources: Sequence[str],
    ) -> None:
        """Store semantic user-model evidence as an interpretation version.

        This is deliberately **not** folded into the numeric user model. That model
        is trained from *observed interactions* - what the user actually did after
        the character acted - and a language model's inference about the user is
        not such an observation. Mixing the two would let a plausible narrative
        silently overwrite measured behaviour.

        So the suggestion is preserved as an auditable interpretation, where later
        real interactions can corroborate or contradict it.
        """
        statement = str(body.get("statement") or body.get("summary") or "").strip()
        if not statement:
            raise ValueError("user model evidence requires a statement")
        if not sources:
            raise ValueError("user model evidence requires at least one source")
        self._p.interpretations.add_version(
            conn,
            target_kind="user_model_evidence",
            target_id=",".join(sources),
            content=statement,
            confidence=clamp(float(body.get("confidence", 0.5))),
            source_version=0,
            source_event_ids=list(sources),
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
        self,
        *,
        new_events: Sequence[RawEvent],
        now: datetime | None = None,
        states: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Re-coordinate every in-flight attempt after new user events.

        Args:
            new_events: User events that just arrived.
            now: Reference time.
            states: Attempt states to re-coordinate; defaults to every in-flight
                state. Ingest passes
                :data:`~companion_runtime.action.PRE_SEND_STATES`, because a
                message that has already been delivered is not re-coordinated -
                only the user's reply can close it.

        Returns:
            One decision per re-coordinated attempt.
        """
        if not new_events:
            return []
        decisions: list[dict[str, Any]] = []
        for attempt in self._p.attempts.list_by_state(states or action_module.IN_FLIGHT_STATES):
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

    def ack_outbox(
        self, outbox_id: str, *, now: datetime | None = None, owner: str | None = None
    ) -> bool:
        """Acknowledge a leased outbox row as delivered.

        Args:
            outbox_id: Row to acknowledge.
            now: Acknowledgement time.
            owner: When given, the row's current lease owner must match. The API
                passes whatever its caller supplied so a worker cannot silently
                complete work that another worker is still responsible for;
                omitting it keeps the owner-agnostic behaviour for callers that
                never learned the owner (the delivery worker, tests).

        Returns:
            ``True`` when a leased row was transitioned.
        """
        with self._db.transaction() as conn:
            return self._p.outbox.ack(conn, outbox_id, now, expect_owner=owner)

    def nack_outbox(
        self,
        outbox_id: str,
        *,
        error: str,
        terminal: bool = False,
        now: datetime | None = None,
        retry_delay_seconds: float | None = None,
        owner: str | None = None,
    ) -> bool:
        """Return a leased outbox row to the queue, or fail it terminally.

        The row becomes claimable **immediately** by default, so a caller that
        nacks and retries at once sees its retry instead of an unexplained empty
        claim. Pacing is the caller's concern (the host side owns a retry queue);
        set ``retry_delay_seconds`` -- or ``config.outbox.retry_backoff_seconds`` --
        only when the Runtime itself should throttle retries.

        Args:
            outbox_id: Row to release.
            error: Reason recorded on the row.
            terminal: Fail the row outright instead of requeueing it.
            now: Reference time; defaults to the current UTC time.
            retry_delay_seconds: Override for how long the row stays unavailable.
            owner: When given, the row's current lease owner must match, so a
                caller cannot release work leased to somebody else.

        Returns:
            ``True`` when a leased row was transitioned.
        """
        stamp = ensure_aware(now) or utcnow()
        delay = (
            self._config.outbox.retry_backoff_seconds
            if retry_delay_seconds is None
            else max(0.0, retry_delay_seconds)
        )
        with self._db.transaction() as conn:
            retry_at = stamp + timedelta(seconds=delay)
            updated = self._p.outbox.nack(
                conn,
                outbox_id,
                error=error,
                retry_at=retry_at,
                terminal=terminal,
                expect_owner=owner,
            )
        if terminal:
            # A row that can never be completed must not leave its attempt in
            # flight: the workspace would stay "busy" forever, the scheduler
            # would refuse to dispatch, and the intention would never be closed.
            self.close_settled_outbox_attempts(now=stamp)
        return updated

    def requeue_outbox(
        self,
        outbox_id: str,
        *,
        error: str,
        now: datetime | None = None,
        retry_delay_seconds: float | None = None,
        owner: str | None = None,
        claimed_attempts: int | None = None,
        refund_attempt: bool = False,
    ) -> bool:
        """Return a claim that could not be used to the queue, without a verdict.

        This is the "the Runtime could not even be asked" path, not the "we tried
        and failed" path: an adapter that leased an action and then could not get
        an authorization verdict sends nothing and owes the Runtime no verdict, so
        the Runtime returns the row to the queue instead of writing a verdict the
        Runtime never gave. The attempt is deliberately not touched -- it is still
        exactly as deliverable as it was, and only the delivery gate failed.

        Unlike :meth:`nack_outbox` there is no exhaustion rule here: a row whose
        ``max_attempts`` is spent is still returned to the queue, because an outage
        is not a delivery attempt. The claim counter is left alone by default,
        which keeps a ``lease_id`` unique per claim -- see
        :meth:`OutboxProjection.requeue` for why that matters. The trade-off is
        that after a long outage a *real* delivery failure sees the budget as spent
        and is terminal on its first report, which is the conservative failure.

        Args:
            outbox_id: Leased row to return to the queue.
            error: Reason recorded on the row.
            now: Reference time; defaults to the current UTC time.
            retry_delay_seconds: Override for how long the row stays unavailable;
                defaults to ``config.outbox.retry_backoff_seconds``, which is 0 --
                immediate reclaimability, the same pacing ``nack_outbox`` uses.
            owner: When given, the row's current lease owner must match.
            claimed_attempts: When given, the row's claim counter must match, which
                is how a report about an already-released or re-claimed claim is
                recognised and ignored.
            refund_attempt: Give the claim's attempt back; off by default, because
                it makes lease ids repeat across claims.

        Returns:
            ``True`` when the row was returned to the queue.
        """
        stamp = ensure_aware(now) or utcnow()
        delay = (
            self._config.outbox.retry_backoff_seconds
            if retry_delay_seconds is None
            else max(0.0, retry_delay_seconds)
        )
        with self._db.transaction() as conn:
            return self._p.outbox.requeue(
                conn,
                outbox_id,
                error=error,
                retry_at=stamp + timedelta(seconds=delay),
                expect_owner=owner,
                expect_attempts=claimed_attempts,
                refund_attempt=refund_attempt,
            )

    def close_settled_outbox_attempts(
        self, *, now: datetime | None = None, limit: int = 200
    ) -> list[str]:
        """Close attempts whose outbox rows have terminally failed.

        An outbox row reaches ``failed`` when its attempt budget is exhausted
        (``reclaim_expired``) or when a worker reports an unretryable fault. In
        both cases the referenced work can never be completed, so leaving the
        attempt in ``committed``/``rendering``/``ready_to_send`` is a leak with
        consequences: the attempt counts as in flight forever, and the scheduler
        gate stops dispatching new rounds.

        What this does **not** do is retire the candidate the attempt came from. An
        earlier version of this docstring listed "the candidate stays active" among the
        consequences of the leak, which read as if terminating the attempt also settled
        the candidate; it does not, and it is measured (`docs/REDELIVERY.md` §5.2). That
        is deliberate rather than forgotten: retiring the candidate would drop an
        intention whose delivery never happened, while leaving it active lets the
        character say the same thing again when the message *was* delivered. Both
        directions are defensible, so the choice is left open and documented instead of
        being decided by accident here.

        Every sibling row of the attempt is cancelled as well, so a render row
        that failed cannot be followed by a send row that tries to deliver the
        same dead intention.

        Args:
            now: Reference time.
            limit: Maximum number of failed rows to inspect in one sweep.

        Returns:
            Identifiers of the attempts that were closed by this call.
        """
        stamp = ensure_aware(now) or utcnow()
        closed: list[str] = []
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            failed_rows = self._p.outbox.list_items(
                status=OutboxStatus.FAILED.value, limit=limit
            )
            for row in failed_rows:
                attempt_id = str(row.payload.get("attempt_id") or "")
                if not attempt_id or attempt_id in closed:
                    continue
                attempt = self._p.attempts.get(attempt_id)
                if attempt is None:
                    continue
                error = row.last_error or "outbox_failed"
                if not _terminate_undeliverable_attempt(
                    self._p.attempts,
                    conn,
                    attempt,
                    reason=f"outbox_failed:{error}",
                    now=stamp,
                ):
                    continue
                cancelled = self._p.outbox.cancel_for_attempt(
                    conn, attempt.attempt_id, reason=f"attempt_{attempt.state}"
                )
                self._events.append(
                    EventType.PROACTIVE_ABORTED,
                    actor=Actor.RUNTIME,
                    content=error,
                    conversation_id=row.conversation_id or self._config.conversation_id,
                    metadata={
                        "attempt_id": attempt.attempt_id,
                        "stage": row.kind,
                        "outbox_id": row.outbox_id,
                        "reason": attempt.failure_reason,
                        "cancelled_outbox_rows": cancelled,
                    },
                    timestamp=stamp,
                    runtime_version=state.version,
                    connection=conn,
                )
                closed.append(attempt.attempt_id)
            if closed:
                self._p.runtime.write(state, conn, expect_version=state.version)
        return closed

    def complete_render(
        self,
        *,
        outbox_id: str,
        text: str,
        now: datetime | None = None,
    ) -> RenderResult:
        """Handle a render result: attach text and move the attempt to ``ready_to_send``.

        Idempotency is decided and applied **inside one transaction**, from the
        recorded effect rather than from a caller-supplied report id. A second
        delivery of the same render therefore cannot attach the text twice,
        cannot queue a second send row, and cannot fail an attempt that is
        already past rendering -- which is what the previous implementation did
        when a replayed report arrived after a successful send.

        Outcomes that apply nothing are reported rather than raised, because each
        of them is an ordinary condition in a distributed deployment:

        * the row already carries the render's effect (``delivered``), or it was
          cancelled/failed, so the render is void -> ``duplicate=True``;
        * the attempt is already ``ready_to_send`` or has left the render stage
          (``sent``, ``resolved``, ``aborted``, ``expired``, ``failed``) -> the
          render is a replay, and a live row is settled out of the queue so it
          stops being re-leased;
        * the attempt is still only ``proposed`` -- nothing was ever committed,
          so there is no message to render -> ``duplicate=False``, rejected, and
          the moot row is cancelled.

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

            replay = self._render_replay(conn, item=item, attempt=attempt, now=stamp)
            if replay is not None:
                return replay

            result = self._apply_rendered_text(
                conn, state=state, item=item, attempt=attempt, text=text, stamp=stamp
            )
            if result.applied:
                self._p.runtime.write(state, conn, expect_version=state.version)
            return result

    def complete_render_for_attempt(
        self,
        *,
        attempt_id: str,
        text: str,
        now: datetime | None = None,
    ) -> RenderResult:
        """Attach rendered text for an attempt that has no render outbox row.

        Some callers commit an attempt themselves and never queue a render row,
        so there is nothing to claim and nothing to acknowledge. The state
        machine work is identical to :meth:`complete_render`, including the
        enqueueing of the send row: a render that produced text the Runtime
        cannot deliver would otherwise be accepted and silently dropped.

        Args:
            attempt_id: Attempt the text belongs to.
            text: Rendered message text.
            now: Reference time.

        Returns:
            A :class:`RenderResult`.

        Raises:
            KeyError: If the attempt is unknown.
        """
        stamp = ensure_aware(now) or utcnow()
        with self._db.transaction() as conn:
            state = self._p.runtime.ensure()
            attempt = self._p.attempts.get(attempt_id)
            if attempt is None:
                raise KeyError(f"unknown attempt: {attempt_id!r}")
            live_rows = self._p.outbox.find_for_attempt(
                attempt_id, kind=OutboxKind.RENDER.value, statuses=_LIVE_OUTBOX_STATUSES
            )
            if live_rows:
                # A render row appeared since the caller looked: going through it
                # keeps the outbox and the attempt consistent instead of leaving a
                # leased row that can only produce a duplicate report later.
                replay = self._render_replay(conn, item=live_rows[0], attempt=attempt, now=stamp)
                if replay is not None:
                    return replay
                result = self._apply_rendered_text(
                    conn,
                    state=state,
                    item=live_rows[0],
                    attempt=attempt,
                    text=text,
                    stamp=stamp,
                )
                if result.applied:
                    self._p.runtime.write(state, conn, expect_version=state.version)
                return result

            blocked = self._render_state_outcome(attempt)
            if blocked is not None:
                return blocked

            result = self._apply_rendered_text(
                conn, state=state, item=None, attempt=attempt, text=text, stamp=stamp
            )
            if result.applied:
                self._p.runtime.write(state, conn, expect_version=state.version)
            return result

    def _render_state_outcome(self, attempt: Any) -> RenderResult | None:
        """Return why an attempt cannot take a render, or ``None`` when it can.

        Only ``committed`` and ``rendering`` are renderable. Every other state
        has an outcome of its own, and the difference between them matters to the
        caller: an attempt that already carries rendered text (or has left the
        render stage) is a **replay** -- ``duplicate=True`` -- while an attempt
        that was never committed has no earlier outcome at all and is a
        **rejection**.

        Nothing is transitioned here: a report that arrives for an attempt in the
        wrong stage must not move it, least of all a report about a message that
        was already delivered.
        """
        def outcome(*, duplicate: bool, reason: str) -> RenderResult:
            """Build the report for an attempt that cannot take a render."""
            return RenderResult(
                attempt_id=attempt.attempt_id,
                state=attempt.state,
                text=attempt.rendered_text,
                outbox_id=attempt.outbox_id,
                reconciled=attempt.reconcile_action,
                applied=False,
                duplicate=duplicate,
                reason=reason,
            )

        if attempt.state == AttemptState.READY_TO_SEND.value and attempt.rendered_text:
            return outcome(duplicate=True, reason="attempt_already_rendered")
        if attempt.state in _ATTEMPT_STATES_BEYOND_RENDER:
            return outcome(duplicate=True, reason=f"attempt_beyond_render:{attempt.state}")
        if attempt.state not in _RENDERABLE_ATTEMPT_STATES:
            return outcome(duplicate=False, reason=f"render_not_applicable:{attempt.state}")
        return None

    def _render_replay(
        self,
        conn: sqlite3.Connection,
        *,
        item: OutboxItem,
        attempt: Any,
        now: datetime,
    ) -> RenderResult | None:
        """Return the outcome already carried by a render row, or ``None``.

        Called with an open write transaction, so the row and the attempt it is
        compared against cannot change between the check and the write.

        A live row whose effect is already recorded is settled here -- recorded as
        delivered when the render it asked for is already attached, cancelled when
        the attempt it belongs to can no longer use it. Without that, the only
        outcome available to a correct implementation ("apply nothing") would also
        mean "lease this row forever", and every worker would keep picking up dead
        work.

        Returns:
            The outcome to report, or ``None`` when the render **should** be
            applied. ``duplicate`` distinguishes "this was already done" from
            "this cannot be done": the first is a convergence, the second is a
            rejection, and a caller retrying the first must not be told to retry.
        """
        settled: str | None = None
        #: True when the row describes work that can never be completed, so the
        #: live row is cancelled rather than acknowledged.
        void = False
        if item.status == OutboxStatus.DELIVERED.value:
            settled = "render_already_completed"
        elif item.status == OutboxStatus.CANCELLED.value:
            settled = "render_row_cancelled"
            void = True
        elif item.status == OutboxStatus.FAILED.value:
            settled = "render_row_failed"
            void = True
        else:
            blocked = self._render_state_outcome(attempt)
            if blocked is None:
                return None
            # A row whose work is already done (``attempt_already_rendered``,
            # ``attempt_beyond_render``) is settled as delivered so it leaves the
            # queue; a row whose work can never be done is cancelled.
            settled = blocked.reason
            if not blocked.duplicate:
                self._p.outbox.cancel(conn, item.outbox_id, reason=settled or "render_void")
            else:
                self._p.outbox.settle(
                    conn, item.outbox_id, status=OutboxStatus.DELIVERED.value, now=now
                )
            return blocked
        if item.status in _LIVE_OUTBOX_STATUSES:
            if void:
                self._p.outbox.cancel(conn, item.outbox_id, reason=settled)
            else:
                self._p.outbox.settle(
                    conn, item.outbox_id, status=OutboxStatus.DELIVERED.value, now=now
                )
        return RenderResult(
            attempt_id=attempt.attempt_id,
            state=attempt.state,
            text=attempt.rendered_text,
            outbox_id=attempt.outbox_id,
            reconciled=attempt.reconcile_action,
            applied=False,
            duplicate=True,
            reason=settled,
        )

    def _apply_rendered_text(
        self,
        conn: sqlite3.Connection,
        *,
        state: RuntimeState,
        item: OutboxItem | None,
        attempt: Any,
        text: str,
        stamp: datetime,
    ) -> RenderResult:
        """Move an attempt to ``ready_to_send`` and queue its send row.

        Called with an open write transaction, and only for an attempt the state
        machine can still advance. ``item`` is the render row to acknowledge, or
        ``None`` on the direct path where no render row exists.

        Args:
            conn: Write connection of the enclosing transaction.
            state: Current runtime state; the version an abort record carries.
            item: Render row to acknowledge, when one exists.
            attempt: Attempt the text belongs to.
            text: Rendered message text.
            stamp: Reference time.

        Returns:
            A :class:`RenderResult`; ``applied`` is true in both the success and
            the recorded-failure branch, because both change persistent state.
        """
        try:
            if attempt.state == AttemptState.COMMITTED.value:
                action_module.mark_rendering(self._p.attempts, conn, attempt, now=stamp)
            action_module.mark_ready(self._p.attempts, conn, attempt, text=text, now=stamp)
        except (ValueError, action_module.IllegalTransition) as exc:
            # The text cannot be used (empty, or a state the machine forbids).
            # The reported row is failed *first*, so the error stays on it and
            # closing the attempt's other rows cannot swallow this failure: a send
            # row for a message that could not be rendered must never be delivered.
            if item is not None:
                self._p.outbox.settle(
                    conn,
                    item.outbox_id,
                    status=OutboxStatus.FAILED.value,
                    error=str(exc),
                    now=stamp,
                )
            # The attempt is closed through the reducer's one terminal-synchronization
            # helper, exactly as ``fail_render`` closes it, so a render that produced
            # unusable text and a render that reported a failure leave the same
            # history -- including the ``proactive_aborted`` record -- while an
            # attempt that already left the Runtime is never rewritten.
            if _terminate_undeliverable_attempt(
                self._p.attempts, conn, attempt, reason=str(exc), now=stamp
            ):
                self._p.outbox.cancel_for_attempt(
                    conn, attempt.attempt_id, reason="render_failed"
                )
                self._events.append(
                    EventType.PROACTIVE_ABORTED,
                    actor=Actor.RUNTIME,
                    content=str(exc),
                    conversation_id=self._config.conversation_id,
                    metadata={"attempt_id": attempt.attempt_id, "stage": "render"},
                    timestamp=stamp,
                    runtime_version=state.version,
                    connection=conn,
                )
            return RenderResult(
                attempt_id=attempt.attempt_id,
                state=attempt.state,
                outbox_id=None,
                applied=True,
                reason=str(exc),
            )

        if item is not None:
            self._p.outbox.settle(
                conn, item.outbox_id, status=OutboxStatus.DELIVERED.value, now=stamp
            )
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
            conversation_id=item.conversation_id if item is not None else None,
        )
        self._p.outbox.enqueue(conn, send_item)
        attempt.outbox_id = send_item.outbox_id
        self._p.attempts.upsert(conn, attempt)
        return RenderResult(
            attempt_id=attempt.attempt_id,
            state=attempt.state,
            text=attempt.rendered_text,
            outbox_id=send_item.outbox_id,
            reconciled=attempt.reconcile_action,
            applied=True,
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
            # The reported row is failed *first*: cancelling the attempt's other
            # rows must not swallow the failure this report is about.
            self._p.outbox.nack(conn, outbox_id, error=error, terminal=True)
            if attempt is not None and not action_module.is_terminal(attempt.state):
                # The attempt was handed to the renderer and the renderer failed,
                # so it passed through ``rendering`` on the way to ``failed``.
                if attempt.state == AttemptState.COMMITTED.value:
                    action_module.mark_rendering(self._p.attempts, conn, attempt, now=stamp)
                if _terminate_undeliverable_attempt(
                    self._p.attempts, conn, attempt, reason=error, now=stamp
                ):
                    self._p.outbox.cancel_for_attempt(
                        conn, attempt.attempt_id, reason="render_failed"
                    )
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
                # The reported row is failed first, then the rest of the
                # intention is cancelled: the failure this report describes has
                # to survive the cleanup.
                self._p.outbox.nack(conn, outbox_id, error=error or "delivery_failed", terminal=True)
                if attempt is not None and _terminate_undeliverable_attempt(
                    self._p.attempts,
                    conn,
                    attempt,
                    reason=error or "delivery_failed",
                    now=stamp,
                ):
                    # Nothing else about this intention may be delivered either.
                    self._p.outbox.cancel_for_attempt(
                        conn, attempt.attempt_id, reason="delivery_failed"
                    )
                self._p.runtime.write(state, conn, expect_version=state.version)
                return {"delivered": False, "attempt_id": attempt_id, "error": error}

            if attempt is not None and attempt.state in {
                AttemptState.SENT.value,
                AttemptState.RESOLVED.value,
            }:
                # A repeated delivery report for the same intention. The
                # acknowledgement is idempotent, and so is everything derived from
                # it: the contact must not be counted a second time and the raw
                # history must not gain a second ``proactive_sent`` event.
                self._p.outbox.ack(conn, outbox_id, stamp)
                version = self._p.runtime.write(state, conn, expect_version=state.version)
                return {
                    "delivered": True,
                    "duplicate": True,
                    "attempt_id": attempt.attempt_id,
                    "state": attempt.state,
                    "version": version,
                }

            self._p.outbox.ack(conn, outbox_id, stamp)
            if attempt is None:
                self._p.runtime.write(state, conn, expect_version=state.version)
                return {"delivered": True, "attempt_id": None}

            if action_module.is_terminal(attempt.state):
                # The Runtime gave up on this intention (its row failed, expired or
                # was cancelled) while the report was still in flight. The message
                # may well have reached the user, but it can no longer be recorded
                # as a fresh send: a terminal attempt has no exit, and reviving one
                # would rewrite history. Report the truth and leave the record.
                LOGGER.warning(
                    "late delivery report for attempt %s in terminal state %s; not re-opened",
                    attempt.attempt_id,
                    attempt.state,
                )
                version = self._p.runtime.write(state, conn, expect_version=state.version)
                return {
                    "delivered": True,
                    "late_report": True,
                    "attempt_id": attempt.attempt_id,
                    "state": attempt.state,
                    "version": version,
                }

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
            # The daily budget is charged exactly here, once per delivered
            # message. Rolling over first keeps the first delivery of a new local
            # day from being added to yesterday's total.
            motivation_module.rollover_contact_day(state, now=stamp)
            state.last_contact_at = max_datetime(state.last_contact_at, stamp)
            state.last_exchange_at = max_datetime(state.last_exchange_at, stamp)
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
        now: datetime | None = None,
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
