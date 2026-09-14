"""The Runtime core: single writer, ``lazy_tick`` and the cognitive pipeline.

This module is the spine of the sidecar. It owns:

* :class:`Runtime` - the process-level object holding the database, projections and
  the write lock;
* :meth:`Runtime.lazy_tick` - the **only** time entry point. Every external entry
  (user message, endogenous wake-up, a background result coming back, a delivery
  acknowledgement) must run ``lazy_tick(now)`` inside the write lock before it
  touches anything else, otherwise time stops being continuous;
* :meth:`Runtime.process_user_message` - the P0 foreground path including the
  entry barrier that pauses endogenous dispatch;
* :meth:`Runtime.endogenous_round` - the P2 path that may end in a committed
  action attempt;
* :meth:`Runtime.observe_reply` - the feedback path that closes the loop.

Only this module writes Runtime state. Models and background jobs hand in
proposals.
"""

from __future__ import annotations

import ast
import logging
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Sequence

from . import action as action_module
from . import boundaries as boundary_module
from . import candidate as candidate_module
from . import emotion as emotion_module
from . import memory as memory_module
from . import motivation as motivation_module
from . import pool as pool_module
from . import protocol as protocol_module
from . import unfinished as unfinished_module
from .config import RuntimeConfig, StorageConfig
from .db import Database
from .eventlog import EventLog, EventQuery
from .projections import AttemptProjection, Projections, VersionConflict
from .reducer import Reducer
from .typing import (
    Actor,
    AttemptState,
    CandidateIntent,
    CandidateStatus,
    EmotionEvent,
    EventType,
    Memory,
    OutboxItem,
    OutboxKind,
    Priority,
    RawEvent,
    ReconcileAction,
    RuntimeState,
    TaskKind,
    UnfinishedStatus,
    new_id,
)
from .user_model import BehaviourReaction, Prediction, UserInteractionModel
from .utility import clamp, delta_seconds, ensure_aware, isoformat, local_now, utcnow

LOGGER = logging.getLogger("companion_runtime.runtime")

#: Types of user message that indicate the user is unavailable right now.
BUSY_MARKERS = ("工作很多", "很忙", "没时间", "忙", "busy", "开会", "加班")
#: Types of user message that indicate explicit permission for proactive contact.
PERMISSION_MARKERS = ("多主动", "随时找我", "可以找我", "欢迎找我", "you can message me")


@dataclass(slots=True)
class TickReport:
    """What a :meth:`Runtime.lazy_tick` call actually changed."""

    dt_seconds: float = 0.0
    decayed_emotions: list[str] = field(default_factory=list)
    new_matters_due: list[str] = field(default_factory=list)
    expired_matters: list[str] = field(default_factory=list)
    expired_attempts: list[str] = field(default_factory=list)
    released_leases: int = 0
    mood: dict[str, float] = field(default_factory=dict)
    drive: dict[str, float] = field(default_factory=dict)
    version: int = 0
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "dt_seconds": round(self.dt_seconds, 3),
            "decayed_emotions": list(self.decayed_emotions),
            "new_matters_due": list(self.new_matters_due),
            "expired_matters": list(self.expired_matters),
            "expired_attempts": list(self.expired_attempts),
            "released_leases": self.released_leases,
            "mood": {k: round(v, 6) for k, v in self.mood.items()},
            "drive": {k: round(v, 6) for k, v in self.drive.items()},
            "version": self.version,
            "changed": self.changed,
        }


@dataclass(slots=True)
class MessageOutcome:
    """Result of ingesting a user message."""

    event: RawEvent
    version: int = 0
    boundary_ids: list[str] = field(default_factory=list)
    unfinished_created: list[str] = field(default_factory=list)
    unfinished_resolved: list[str] = field(default_factory=list)
    emotion_event_ids: list[str] = field(default_factory=list)
    memory_candidate_id: str | None = None
    observation_id: str | None = None
    reply_blocked: bool = False
    proactive_paused_until: datetime | None = None
    #: Which appraiser produced the emotional reading: ``coarse_rule`` (an explicit
    #: event settled by the Level 1 rule table), ``deferred`` (recorded as
    #: unresolved for a later deep refresh) or ``rule`` (the legacy lexicon path
    #: used by callers that still ask for a per-turn reading).
    appraisal_source: str = "rule"
    #: Whether this event has a durable semantic reading yet (patch v0.2).
    semantic_status: str = "resolved"
    #: Cheap priority for the deep-refresh queue when ``semantic_status`` is
    #: ``unresolved``.
    potential_relevance: str = "low"
    narrative: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "event": self.event.to_dict(),
            "version": self.version,
            "boundary_ids": list(self.boundary_ids),
            "unfinished_created": list(self.unfinished_created),
            "unfinished_resolved": list(self.unfinished_resolved),
            "emotion_event_ids": list(self.emotion_event_ids),
            "memory_candidate_id": self.memory_candidate_id,
            "observation_id": self.observation_id,
            "reply_blocked": self.reply_blocked,
            "proactive_paused_until": isoformat(self.proactive_paused_until),
            "appraisal_source": self.appraisal_source,
            "semantic_status": self.semantic_status,
            "potential_relevance": self.potential_relevance,
            "narrative": self.narrative,
        }


def _parse_counts(text: str) -> dict[str, int]:
    """Parse the reducer's ``{'kind': count}`` note without trusting its format.

    Args:
        text: The mapping literal produced by the deep-refresh handler.

    Returns:
        A counts mapping; an unparseable note yields an empty mapping rather than
        an exception, because reporting is never worth failing a refresh over.
    """
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): int(value) for key, value in parsed.items() if isinstance(value, int)}


@dataclass(slots=True)
class DeepRefreshOutcome:
    """Result of one low-frequency deep cognition refresh (patch v0.2 §18-§21).

    The refresh is allowed to be skipped for many reasons, and every one of them
    is reported rather than hidden: an operator must be able to tell "nothing
    needed doing" apart from "the provider was down" apart from "a model answered
    but everything it said failed grounding".
    """

    ran: bool = False
    reason: str = "not_attempted"
    trigger: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    degraded: bool = True
    operations: int = 0
    applied: dict[str, int] = field(default_factory=dict)
    violations: list[dict[str, Any]] = field(default_factory=list)
    settled_events: int = 0
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "ran": self.ran,
            "reason": self.reason,
            "trigger": dict(self.trigger),
            "provider": self.provider,
            "degraded": self.degraded,
            "operations": self.operations,
            "applied": dict(self.applied),
            "violations": [dict(item) for item in self.violations],
            "settled_events": self.settled_events,
            "latency_ms": self.latency_ms,
        }


@dataclass(slots=True)
class EndogenousOutcome:
    """Result of one endogenous wake-up round."""

    decision: dict[str, Any] = field(default_factory=dict)
    attempt_id: str | None = None
    outbox_id: str | None = None
    next_wake_at: datetime | None = None
    refreshed_candidates: bool = False
    activated_memory_ids: list[str] = field(default_factory=list)
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "decision": dict(self.decision),
            "attempt_id": self.attempt_id,
            "outbox_id": self.outbox_id,
            "next_wake_at": isoformat(self.next_wake_at),
            "refreshed_candidates": self.refreshed_candidates,
            "activated_memory_ids": list(self.activated_memory_ids),
            "version": self.version,
        }


class Runtime:
    """The cognitive Runtime sidecar.

    Args:
        config: Resolved configuration.
        seed: Seed for the internal RNG; ``None`` means system entropy.
        database: Optional pre-built database handle (used by tests for
            ``:memory:`` databases).
    """

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        seed: int | None = None,
        database: Database | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """Wire up storage, projections and the cognitive components.

        Args:
            config: Resolved configuration; defaults to :class:`RuntimeConfig`.
            seed: Seed for the internal RNG; ``None`` means system entropy.
            database: Optional pre-built database handle (used by tests for
                ``:memory:`` databases).
            created_at: Creation epoch for a brand-new Runtime. Defaults to the
                current UTC time. Callers that drive a simulated or replayed
                timeline should pass their own reference time here so the absence
                term does not start from a wall-clock instant in the future.
        """
        self.config = config or RuntimeConfig()
        storage: StorageConfig = self.config.storage
        self._db = database or Database(
            storage.database_path, busy_timeout_ms=storage.busy_timeout_ms, wal=storage.wal
        )
        self._db.migrate()
        self.mirror_path = storage.raw_log_path if storage.mirror_raw_events else None
        self.events = EventLog(self._db, self.mirror_path)
        self.projections = Projections(self._db, self.config.runtime_id)
        # The runtime row is created here so that maintenance tooling always sees a
        # fully initialised database. The version stays at 0 until a real tick or
        # event occurs, and ``lazy_tick`` may still install its own epoch the first
        # time it is driven. The configured value profile is what compiles the
        # character's dynamics, so it seeds the row rather than a neutral default.
        self.projections.ensure_defaults(created_at, values=self.config.values)
        self.memory_store = memory_module.MemoryStore(self.projections.memory, self.config)
        self.user_model = UserInteractionModel(self.projections.user_model, self.config)
        self.rng = random.Random(seed)
        self._write_lock = threading.RLock()
        self._worker_id = f"runtime-{new_id('task').split('_')[-1]}"
        #: Optional semantic provider (architecture patch v0.2).
        #:
        #: This is *not* on the ingest path and never will be: the host main LLM
        #: performs the current turn, and the Runtime only needs a model when it
        #: wants to reinterpret old events or compress long-term state into
        #: language. ``build_provider`` defaults to ``DisabledProvider`` and never
        #: raises, so the Runtime is fully functional with no model at all.
        from .providers import build_provider

        self.semantic_provider = build_provider(self.config)
        self.reducer = Reducer(
            db=self._db,
            events=self.events,
            projections=self.projections,
            config=self.config,
        )

    # ------------------------------------------------------------------ plumbing

    @property
    def db(self) -> Database:
        """Return the underlying database handle."""
        return self._db

    @property
    def worker_id(self) -> str:
        """Return this process's lease owner identifier."""
        return self._worker_id

    def close(self) -> None:
        """Close the database handle."""
        self._db.close()

    @contextmanager
    def write_session(self) -> Iterator[None]:
        """Acquire the single-writer lock for a compound operation."""
        with self._write_lock:
            yield

    def state(self) -> RuntimeState:
        """Return the current runtime state (a fresh read)."""
        return self.projections.runtime.read()

    def reload_user_model(self) -> UserInteractionModel:
        """Re-instantiate the user model from its persisted parameters.

        The in-memory instance is a cache. Any code path that can write user-model
        parameters outside this object's control (the reducer's proposal handler,
        for example) must call this so the instance does not serve stale beliefs.

        Returns:
            The refreshed model, also stored on ``self.user_model``.
        """
        with self._write_lock:
            self.user_model = UserInteractionModel(self.projections.user_model, self.config)
            return self.user_model

    def version(self) -> int:
        """Return the current runtime version."""
        return self.state().version

    # ------------------------------------------------------------------ lazy tick

    def lazy_tick(self, now: datetime | None = None) -> TickReport:
        """Advance the whole Runtime to ``now`` in a single pass.

        This is the unified time entry point. It must be called (under the write
        lock) before any other mutation; it is idempotent for a zero/negative
        elapsed time and cheap for tiny elapsed times.

        Updated in one shot: background mood recovery, emotion event decay,
        approach impulse, restraint, pressure, cooldown, memory activation decay,
        unfinished-matter timing, boundary expiry and candidate deadlines.

        Args:
            now: Reference time, defaults to the current UTC time.

        Returns:
            A :class:`TickReport` describing what changed.
        """
        stamp = ensure_aware(now) or utcnow()
        report = TickReport()
        with self.write_session():
            state = self.projections.runtime.ensure(stamp)
            # The base clock is the creation epoch: a fresh Runtime measures
            # absence from when it was created, not from whenever the current
            # process started. The supplied clock is clamped to be no earlier than
            # the epoch, so driving a simulated timeline stays monotonic instead of
            # producing a negative elapsed time that silently clamps to zero.
            base = ensure_aware(state.epoch_at) or stamp
            if stamp < base:
                LOGGER.warning(
                    "lazy_tick called with a time earlier than the creation epoch; "
                    "using the epoch as the base clock"
                )
                stamp = base
            last = state.last_tick_at or base
            dt = delta_seconds(stamp, last)
            report.dt_seconds = dt
            report.changed = dt > 0.0

            with self._db.transaction() as conn:
                state = self._apply_time_passage(conn, state=state, now=stamp, dt_seconds=dt, report=report)
                report.released_leases = self.projections.outbox.reclaim_expired(conn, stamp)
                version = self.projections.runtime.write(state, conn, expect_version=state.version)
                report.version = version
        return report

    def _apply_time_passage(
        self,
        conn: Any,
        *,
        state: RuntimeState,
        now: datetime,
        dt_seconds: float,
        report: TickReport,
    ) -> RuntimeState:
        """Apply every time-driven update to ``state`` and the projections."""
        emotion_config = self.config.emotion

        # --- emotions
        active = self.projections.emotion.list_active()
        survivors = emotion_module.tick_emotions(
            active=active, state=state, config=emotion_config, dt_seconds=dt_seconds
        )
        survivor_ids = {event.emotion_event_id for event in survivors}
        decayed = [e.emotion_event_id for e in active if e.emotion_event_id not in survivor_ids]
        if decayed:
            self.projections.emotion.deactivate(conn, decayed)
            report.decayed_emotions = decayed

        emotion_module.mood_relax(state, emotion_config, dt_seconds)

        # --- unfinished matters
        matters = self.projections.unfinished.list_open()
        tick_result = unfinished_module.tick(
            self.projections.unfinished, conn, config=self.config, now=now
        )
        report.new_matters_due = tick_result["newly_due"]
        report.expired_matters = tick_result["expired"]

        # --- boundaries (expiry is implicit; nothing is deleted).
        # ``allow_proactive`` is derived state: because boundaries carry expiry
        # times, a tick must recompute the permission rather than let a stale flag
        # keep the character silent forever after the window closed.
        boundary_module.decay_and_persist(self.projections.boundaries, conn, now=now)

        # --- candidate deadlines
        self._expire_candidates(conn, now=now)

        # --- memory activation decay
        self.memory_store.decay_pool(conn, dt_seconds=dt_seconds)

        # --- drive dynamics
        refreshed = self.projections.unfinished.list_open()
        busy = self.user_model.busy_probability(
            hours_since_contact=delta_seconds(now, state.last_user_message_at) / 3600.0,
            replied_recently=delta_seconds(now, state.last_user_message_at) < 1800.0,
        )
        boundary_verdict = boundary_module.evaluate(
            self.projections.boundaries.active(now),
            now=now,
            state=state,
            is_proactive=True,
        )
        # ``allow_proactive`` is derived state, not something remembered: it is
        # recomputed from the live boundaries on every tick so that an expired
        # window cannot leave the character muted forever.
        state.allow_proactive = boundary_verdict.allow_proactive
        boundary_pressure = 1.0 if not boundary_verdict.allow_proactive else 0.0
        recent_contacts = self._recent_contact_count(now)
        drive_inputs = motivation_module.DriveInputs(
            emotion_tendency=self._emotion_tendency(state, survivors),
            unfinished=unfinished_module.priority_of(refreshed),
            memory_activation=self.memory_store.activation_strength(),
            hours_since_contact=self._hours_since_exchange(state, now),
            recent_contact_ratio=clamp(recent_contacts / max(1, self.config.utility.repeat_contact_tolerance)),
            boundary_pressure=boundary_pressure,
            user_busy=busy,
            uncertainty=0.35 if self.user_model.numeric_view()["effective_count"] < 3 else 0.15,
            mood_valence=state.mood_valence,
        )
        targets = motivation_module.target_drives(drive_inputs, state=state, config=self.config)
        motivation_module.step_drives(
            state=state, targets=targets, config=self.config, dt_seconds=dt_seconds
        )

        # --- daily counter rollover
        self._rollover_contact_day(state, now)

        state.last_tick_at = now
        state.updated_at = now
        report.mood = {
            "valence": state.mood_valence,
            "arousal": state.mood_arousal,
            "stability": state.mood_stability,
        }
        report.drive = {
            "approach_impulse": state.approach_impulse,
            "restraint": state.restraint,
            "pressure": state.pressure,
        }
        return state

    # -------------------------------------------------------- foreground entry

    def process_user_message(
        self,
        *,
        content: str,
        conversation_id: str | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        reason: BehaviourReaction | None = None,
        rng: random.Random | None = None,
    ) -> MessageOutcome:
        """Ingest a user message through the full P0/P1 path.

        Steps: ``lazy_tick`` -> append raw event -> entry barrier (pause
        endogenous dispatch) -> boundary rules -> emotion appraisal -> mood update
        -> unfinished matters -> memory candidate -> user-model observation ->
        version bump.

        Args:
            content: Verbatim user text.
            conversation_id: Conversation scope.
            event_id: Explicit event identifier (tests, replay).
            timestamp: Event time.
            metadata: Extra structured payload.
            reason: Reaction to the *previous* assistant message, when known.
            rng: Random source.

        Returns:
            A :class:`MessageOutcome`.
        """
        stamp = ensure_aware(timestamp) or utcnow()
        source = rng or self.rng
        self.lazy_tick(stamp)

        with self.write_session():
            state = self.projections.runtime.ensure()
            with self._db.transaction() as conn:
                event = self.events.append(
                    EventType.USER_MESSAGE,
                    actor=Actor.USER,
                    content=content,
                    conversation_id=conversation_id or self.config.conversation_id,
                    metadata=metadata,
                    timestamp=stamp,
                    runtime_version=state.version,
                    event_id=event_id,
                    connection=conn,
                )

                # Entry barrier: stop any new endogenous dispatch immediately.
                state.foreground_pause_until = stamp + timedelta(
                    seconds=self.config.scheduler.foreground_pause_seconds
                )

                outcome = MessageOutcome(event=event)
                outcome.proactive_paused_until = state.foreground_pause_until

                # --- boundary rules (before any semantic layer wakes up)
                declared = boundary_module.detect_boundaries(
                    event, state=state, config=self.config, now=stamp
                )
                for boundary in declared:
                    self.projections.boundaries.upsert(conn, boundary)
                    self.events.append(
                        EventType.BOUNDARY_DECLARED,
                        actor=Actor.RUNTIME,
                        content=boundary.note,
                        conversation_id=event.conversation_id,
                        metadata={"boundary": boundary.to_dict()},
                        source_event_ids=[event.event_id],
                        timestamp=stamp,
                        runtime_version=state.version,
                        connection=conn,
                    )
                    outcome.boundary_ids.append(boundary.boundary_id)
                active_boundaries = self.projections.boundaries.active(stamp)
                revoked = boundary_module.detect_revocation(event, active=active_boundaries)
                if revoked:
                    boundary_module.revoke(self.projections.boundaries, conn, revoked, now=stamp)

                # Two separate questions must not be conflated: whether I may
                # *reply* (always true unless explicitly denied) and whether I may
                # initiate contact later (what an explicit boundary removes).
                reply_verdict = boundary_module.evaluate(
                    self.projections.boundaries.active(stamp),
                    now=stamp,
                    state=state,
                    is_proactive=False,
                )
                proactive_verdict = boundary_module.evaluate(
                    self.projections.boundaries.active(stamp),
                    now=stamp,
                    state=state,
                    is_proactive=True,
                )
                outcome.reply_blocked = not reply_verdict.allow_reply

                # --- unfinished matters resolved by this message.
                # Resolution runs *before* the working situation is projected so
                # that the projection already reflects the settled state.
                live_matters = self.projections.unfinished.list_open()
                resolved_ids: list[str] = []
                for unfinished_id, why in unfinished_module.detect_resolution(event, live=live_matters):
                    if unfinished_module.resolve(
                        self.projections.unfinished, conn, unfinished_id, note=why
                    ):
                        outcome.unfinished_resolved.append(unfinished_id)
                        resolved_ids.append(unfinished_id)
                if resolved_ids:
                    self._retire_candidates_for(resolved_ids, conn, now=stamp)

                # --- coarse persistent settlement (architecture patch v0.2)
                #
                # The acting layer already understands this turn: the host main LLM
                # sees the user's words directly. What the Runtime owes the future
                # is not a second opinion about "how does this feel right now", but
                # a decision about "what does this leave behind".
                #
                # So the per-turn path is deliberately cheap and allowed to fail:
                # an explicit event is settled coarsely, and anything ambiguous is
                # recorded as ``unresolved`` and revisited later. Guessing here
                # would silently corrupt long-term state, while deferring costs only
                # the chance to settle early.
                busy = self.user_model.busy_probability(
                    hours_since_contact=delta_seconds(stamp, state.last_user_message_at) / 3600.0,
                    replied_recently=False,
                    context={"stated_busy": any(marker in content for marker in BUSY_MARKERS)},
                )
                from .emotion import apply_new_emotion_events
                from .semantic import (
                    SemanticStatus,
                    classify_event,
                    potential_relevance,
                    settlement_to_evaluation,
                )

                settlement = None
                if self.config.semantic.settle_on_ingest:
                    settlement = classify_event(
                        content, event_type=event.event_type, actor=event.actor
                    )

                evaluation = None
                created: list[Any] = []
                if settlement is not None:
                    evaluation = settlement_to_evaluation(settlement)
                    self.projections.semantics.record_settlement(
                        conn,
                        event_id=event.event_id,
                        direction=settlement.direction,
                        intensity_band=settlement.intensity,
                        confidence=settlement.confidence,
                        settlement_source=settlement.source,
                        evidence=settlement.evidence,
                        version=state.version,
                        now=stamp,
                    )
                    outcome.appraisal_source = "coarse_rule"
                    outcome.semantic_status = SemanticStatus.RESOLVED.value
                else:
                    relevance = potential_relevance(
                        content,
                        hours_since_contact=delta_seconds(stamp, state.last_user_message_at)
                        / 3600.0,
                        has_open_matters=bool(self.projections.unfinished.list_open()),
                    )
                    self.projections.semantics.record_unresolved(
                        conn,
                        event_id=event.event_id,
                        potential_relevance=relevance,
                        reason="no_explicit_anchor",
                        version=state.version,
                        now=stamp,
                    )
                    outcome.appraisal_source = "deferred"
                    outcome.semantic_status = SemanticStatus.UNRESOLVED.value
                    outcome.potential_relevance = relevance
                    # An unresolved event yields no emotional after-effect yet.
                    # The raw event is preserved, so a later deep refresh can
                    # reinterpret it - that is the "追夫火葬场" path in patch v0.2.

                if evaluation is not None:
                    existing_emotions = self.projections.emotion.list_active()
                    _, created = apply_new_emotion_events(
                        evaluations=[(event, evaluation)],
                        active=existing_emotions,
                        state=state,
                        config=self.config.emotion,
                    )
                    for emotion_event in created:
                        self.projections.emotion.upsert(conn, emotion_event)
                        outcome.emotion_event_ids.append(emotion_event.emotion_event_id)

                # --- working situation: facts and inferences stay separate
                self.projections.situation.upsert(
                    conn,
                    kind="fact",
                    content=f"用户说：{content.strip()[:120]}",
                    salience=0.6,
                    confidence=1.0,
                    source_kind="event",
                    source_id=event.event_id,
                    expires_at=stamp + timedelta(hours=24),
                )
                if evaluation is not None and evaluation.confidence >= 0.5 and evaluation.relation_signal != "neutral":
                    self.projections.situation.upsert(
                        conn,
                        kind="inference",
                        content=f"关系信号：{evaluation.relation_signal}",
                        salience=0.4,
                        confidence=evaluation.confidence * (1.0 - 0.5 * busy),
                        source_kind="event",
                        source_id=event.event_id,
                        expires_at=stamp + timedelta(hours=12),
                    )
                self.projections.situation.expire(conn, stamp)

                # --- unfinished matters created by this message
                proposals = unfinished_module.detect(
                    event, config=self.config, existing=self.projections.unfinished.list_open()
                )
                for proposal in proposals:
                    matter = unfinished_module.create(
                        self.projections.unfinished, conn, proposal, config=self.config, now=stamp
                    )
                    outcome.unfinished_created.append(matter.unfinished_id)
                    self.projections.situation.upsert(
                        conn,
                        kind="fact",
                        content=f"未尽之事：{matter.title}",
                        salience=0.7,
                        confidence=0.9,
                        source_kind="unfinished",
                        source_id=matter.unfinished_id,
                        expires_at=matter.expire_at,
                    )
                for unfinished_id in resolved_ids:
                    matter = self.projections.unfinished.get(unfinished_id)
                    title = matter.title if matter is not None else unfinished_id
                    self.projections.situation.upsert(
                        conn,
                        kind="inference",
                        content=f"未尽之事已了结：{title}",
                        salience=0.5,
                        confidence=0.9,
                        source_kind="unfinished",
                        source_id=unfinished_id,
                        expires_at=stamp + timedelta(hours=6),
                    )

                # Keep the working set bounded only after every contributor ran.
                self.projections.situation.prune(conn)

                # --- memory candidate
                # ``created`` only exists when the event was settled; an
                # unresolved event has no emotional salience yet by definition,
                # so it contributes none rather than guessing one.
                salience = max((e.intensity for e in created), default=0.0)
                proposal_memory = memory_module.propose_from_event(
                    event,
                    state=state,
                    unfinished=self.projections.unfinished.list_open(),
                    emotion_salience=salience,
                    config=self.config,
                )
                if proposal_memory is not None:
                    self.projections.memory.upsert_candidate(conn, proposal_memory)
                    outcome.memory_candidate_id = proposal_memory.candidate_id
                    outcome.narrative = proposal_memory.summary

                # --- user interaction observation for the previous proactive act
                if reason is not None or self._has_pending_observation():
                    reaction = reason or BehaviourReaction(
                        replied=True,
                        reply_delay_seconds=self._reply_delay_seconds(stamp),
                        reply_length=len(content),
                    )
                    reaction.busy_probability = busy
                    context = self._last_proactive_context()
                    observation = self.user_model.observe(
                        conn,
                        action=context.get("action", {"type": "reply", "proactive": False}),
                        context=context.get("context", {}),
                        reaction=reaction,
                        now=stamp,
                        observed_at=stamp,
                        source_event_ids=[event.event_id],
                        attempt_id=context.get("attempt_id"),
                        busy_probability=busy,
                    )
                    outcome.observation_id = observation.observation_id

                # --- invalidate candidates whose premises just died
                self._invalidate_candidates(conn, now=stamp, user_message=content)

                state.last_user_message_at = stamp
                state.last_exchange_at = stamp
                # ``allow_proactive`` itself is not written here: it is derived on
                # every tick from the live boundaries, so writing it would only
                # create a second source of truth.
                version = self.projections.runtime.write(state, conn, expect_version=state.version)
                outcome.version = version
                self.events.append(
                    EventType.SYSTEM,
                    actor=Actor.RUNTIME,
                    content="foreground_pause",
                    conversation_id=event.conversation_id,
                    metadata={"until": isoformat(state.foreground_pause_until)},
                    timestamp=stamp,
                    runtime_version=version,
                    connection=conn,
                )

        self.user_model = UserInteractionModel(self.projections.user_model, self.config)
        return outcome

    # --------------------------------------------------------- endogenous entry

    def endogenous_round(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        create_attempt: bool = True,
    ) -> EndogenousOutcome:
        """Run one endogenous wake-up round (P2): the proactive decision.

        Args:
            now: Reference time.
            force: Bypass the foreground pause and the scheduler gate.
            create_attempt: When a candidate wins, create the action attempt.

        Returns:
            An :class:`EndogenousOutcome`.
        """
        stamp = ensure_aware(now) or utcnow()
        # Capture the previous tick before advancing time: the hazard rate must be
        # integrated over the elapsed interval, not over an arbitrary one.
        elapsed_seconds = delta_seconds(stamp, self.state().last_tick_at)
        report = self.lazy_tick(stamp)
        outcome = EndogenousOutcome(version=report.version)

        with self.write_session():
            state = self.projections.runtime.ensure()
            if not force and state.foreground_pause_until is not None and stamp < state.foreground_pause_until:
                outcome.decision = {"acted": False, "reason": "foreground_pause"}
                outcome.next_wake_at = state.foreground_pause_until
                return outcome

            active = self.projections.emotion.list_active()
            pending = self.projections.candidates.list_active(limit=self.config.candidate.max_active)
            last_refresh = self._last_refresh_at()

            if candidate_module.should_refresh(
                existing=pending, last_refresh_at=last_refresh, now=stamp, config=self.config
            ):
                refreshed = self._refresh_candidates(now=stamp, state=state, active=active)
                outcome.refreshed_candidates = True
                pending = refreshed

            with self._db.transaction() as conn:
                # memory activation gives the candidates something to be about
                cue = memory_module.build_cue(
                    state=state,
                    recent_events=self.events.recent(4),
                    unfinished=self.projections.unfinished.list_open(),
                    active_emotions=active,
                    now=stamp,
                )
                hits = self.memory_store.retrieve(cue, limit=self.config.memory.activation_pool_size, rng=self.rng)
                touched = self.memory_store.activate(conn, hits, now=stamp)
                outcome.activated_memory_ids = [item.memory_id for item in touched]

                cue_text = " ".join(
                    [cue.query_text]
                    + [hit.memory.summary for hit in hits[:3]]
                    + [f"{a.memory_id}" for a in touched]
                )

                # ---- boundary gate (hard, before any utility comparison)
                verdict = boundary_module.evaluate(
                    self.projections.boundaries.active(stamp),
                    now=stamp,
                    state=state,
                    is_proactive=True,
                )

                predictions: dict[str, Prediction] = {}
                alignments: dict[str, float] = {}
                context = self._situation_context(stamp)
                for item in pending:
                    action_spec = self._action_spec(item)
                    predictions[item.candidate_id] = self.user_model.predict(
                        action=action_spec, context=context
                    )
                    alignments[item.candidate_id] = self._emotion_alignment(item, active)

                inputs = motivation_module.MotivationInputs(                    state=state,
                    candidates=pending,
                    predictions=predictions,
                    boundary_allow_proactive=verdict.allow_proactive,
                    boundary_ids=verdict.blocking_ids,
                    boundary_risk_baseline=1.0 if verdict.allow_proactive is False else 0.0,
                    active_emotions=active,
                    recent_contacts=self._recent_contact_count(stamp),
                    hours_since_contact=delta_seconds(stamp, state.last_contact_at) / 3600.0,
                    cooldown_active=motivation_module.cooldown_remaining(state, stamp) > 0.0,
                    now=stamp,
                    elapsed_seconds=elapsed_seconds or report.dt_seconds,
                    force_allow=force,
                )
                result = motivation_module.decide(
                    inputs,
                    config=self.config,
                    rng=self.rng,
                    situation_text=cue_text,
                    emotion_alignment=alignments,
                )
                outcome.decision = result.to_dict()
                outcome.next_wake_at = result.outcome.next_wake_at

                if result.outcome.acted and result.outcome.chosen_candidate_id:
                    chosen = next(
                        (
                            item
                            for item in pending
                            if item.candidate_id == result.outcome.chosen_candidate_id
                        ),
                        None,
                    )
                    if chosen is not None and create_attempt:
                        # ``lazy_tick`` already bumped the version, so the commit
                        # must build on a freshly read state, not on the snapshot
                        # taken before the tick.
                        commit_state = self.projections.runtime.read()
                        attempt_id, outbox_id = self._commit_attempt(
                            conn, chosen=chosen, state=commit_state, now=stamp
                        )
                        outcome.attempt_id = attempt_id
                        outcome.outbox_id = outbox_id
                        outcome.version = commit_state.version
                return outcome

    def _commit_attempt(
        self,
        conn: Any,
        *,
        chosen: CandidateIntent,
        state: RuntimeState,
        now: datetime,
    ) -> tuple[str, str]:
        """Create the action attempt, apply the post-contact transition, enqueue rendering."""
        attempt = action_module.create_proposal(
            candidate=chosen, based_on_version=state.version, now=now
        )
        action_module.commit(
            self.projections.attempts, conn, attempt, now=now, reason="motivational_game"
        )
        self.events.append(
            EventType.PROACTIVE_COMMITTED,
            actor=Actor.RUNTIME,
            content=chosen.intent,
            conversation_id=self.config.conversation_id,
            metadata={"attempt": attempt.to_dict(), "goal": chosen.goal},
            source_event_ids=[s for s in chosen.sources],
            timestamp=now,
            runtime_version=state.version,
            connection=conn,
        )
        # The decision itself already changes the dynamics: impulse and pressure
        # are released the moment the character commits.
        motivation_module.release_after_contact(state, config=self.config, now=now)
        state.contact_count_today += 1
        self._rollover_contact_day(state, now)

        item = OutboxItem(
            outbox_id=new_id("outbox"),
            kind=OutboxKind.RENDER.value,
            payload={
                "attempt_id": attempt.attempt_id,
                "candidate_id": chosen.candidate_id,
                "intent": chosen.intent,
                "goal": chosen.goal,
                "constraints": list(chosen.constraints),
                "based_on_version": attempt.based_on_version,
            },
            priority=10,
            available_at=now,
            created_at=now,
            max_attempts=self.config.outbox.max_attempts,
            conversation_id=self.config.conversation_id,
        )
        self.projections.outbox.enqueue(conn, item)
        attempt.outbox_id = item.outbox_id
        self.projections.attempts.upsert(conn, attempt)
        self.projections.candidates.set_status(
            conn, chosen.candidate_id, CandidateStatus.ACTIVE.value, reason="committed"
        )
        self.projections.runtime.write(state, conn, expect_version=state.version)
        return attempt.attempt_id, item.outbox_id

    # ------------------------------------------------------ deep cognition path

    def deep_refresh(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        trigger_context: dict[str, Any] | None = None,
    ) -> DeepRefreshOutcome:
        """Run one low-frequency deep cognition refresh (patch v0.2 §18-§21).

        This is the *only* place a semantic provider may influence state, and it
        is deliberately slow, optional and off the ingest path. The pipeline is:

        ``evaluate triggers`` -> ``build request`` -> ``provider`` ->
        ``grounding`` -> ``one proposal`` -> ``Reducer`` (APPLY / REBASE / DISCARD).

        Every stage can decline. Nothing here raises: a refresh is an attempt to
        understand old events better, and failing to improve is not an error.

        Args:
            now: Reference time; defaults to the wall clock.
            force: Skip the trigger check (diagnostics and tests).
            trigger_context: Extra trigger signals, e.g. ``matter_due``.

        Returns:
            A :class:`DeepRefreshOutcome` describing what happened and why.
        """
        stamp = ensure_aware(now) or utcnow()
        config = self.config.semantic
        outcome = DeepRefreshOutcome()

        if not config.deep_refresh_enabled:
            outcome.reason = "disabled"
            return outcome
        if not self.semantic_provider.available():
            outcome.reason = "provider_unavailable"
            outcome.provider = self.semantic_provider.name
            return outcome

        from .deep_refresh import build_request, evaluate_triggers, ground_suggestions

        signals = dict(trigger_context or {})
        unresolved = self.projections.semantics.list_unresolved(limit=200)
        trigger = evaluate_triggers(
            unresolved_count=len(unresolved),
            hours_since_last_refresh=signals.get("hours_since_last_refresh", 0.0),
            config=config,
            **{
                key: value
                for key, value in signals.items()
                if key != "hours_since_last_refresh"
            },
        )
        outcome.trigger = trigger.to_dict()
        if not trigger.should_refresh and not force:
            outcome.reason = trigger.reason
            return outcome

        request = build_request(runtime=self, now=stamp, limit=config.max_operations_per_refresh)
        started = time.monotonic()
        try:
            suggestions = self.semantic_provider.deep_refresh(request)
        except Exception:  # noqa: BLE001 - a provider fault is never fatal
            LOGGER.exception("Deep refresh provider raised; treating as unavailable")
            outcome.reason = "provider_error"
            return outcome
        outcome.provider = getattr(suggestions, "provider", "") or self.semantic_provider.name
        outcome.latency_ms = int((time.monotonic() - started) * 1000)

        if suggestions is None:
            outcome.reason = "no_suggestions"
            return outcome
        outcome.degraded = bool(getattr(suggestions, "degraded", True))
        if suggestions.is_empty():
            outcome.reason = "empty_suggestions"
            return outcome

        operations, violations = ground_suggestions(
            suggestions,
            resolvable=self._is_resolvable,
            max_candidate_operations=config.max_operations_per_refresh,
        )
        outcome.violations = list(violations)
        outcome.operations = len(operations)
        if not operations:
            outcome.reason = "all_suggestions_ungrounded"
            return outcome

        payload = {
            "operations": [operation.to_dict() for operation in operations],
        }
        interpretation = dict(getattr(suggestions, "psychological_interpretation", {}) or {})
        if interpretation:
            # The cached interpretation is carried inside the same proposal so it
            # passes the same protocol gate as every other suggested change.
            payload["operations"].append(
                {
                    "kind": "psychological_interpretation",
                    "payload": interpretation,
                    "sources": [],
                }
            )

        source_event_ids = [
            item["event_id"] for item in unresolved if item.get("event_id")
        ][: config.max_operations_per_refresh]
        with self.write_session():
            state = self.projections.runtime.ensure(stamp)
            proposal = protocol_module.Proposal(
                task_id=f"deep_refresh_{new_id('task').split('_')[-1]}",
                task_type=TaskKind.DEEP_REFRESH.value,
                based_on_version=state.version,
                source_event_ids=source_event_ids,
                payload=payload,
                created_at=stamp,
            )
            result = self.reducer.process_proposal(proposal)

        outcome.ran = True
        outcome.reason = "applied" if result.applied else result.action
        for note in result.notes:
            text = str(note)
            if text.startswith("deep_refresh_applied="):
                outcome.applied = _parse_counts(text.split("=", 1)[1])
            elif text.startswith("settled_events="):
                try:
                    outcome.settled_events = int(text.split("=", 1)[1])
                except ValueError:
                    outcome.settled_events = 0
        return outcome

    def _is_resolvable(self, identifier: str) -> bool:
        """Return whether a grounding identifier names something that exists.

        Grounding is what stops a model from inventing a memory about an event
        that never happened. Any identifier the Runtime cannot resolve makes the
        operation carrying it be discarded before it can touch state.

        Args:
            identifier: An event, memory, unfinished-matter, emotion or candidate id.

        Returns:
            ``True`` when the identifier resolves to a stored entity.
        """
        if not identifier or not isinstance(identifier, str):
            return False
        prefix = identifier.split("_", 1)[0]
        try:
            if prefix == "evt":
                return self.events.get(identifier) is not None
            if prefix == "mem":
                return self.projections.memory.get_memory(identifier) is not None
            if prefix == "cnd":
                return self.projections.candidates.get(identifier) is not None
        except Exception:  # noqa: BLE001 - a lookup failure is simply "not resolvable"
            return False
        # Unfinished matters and emotion events use generated ids that the
        # projections expose through list calls rather than point lookups.
        try:
            if any(item.unfinished_id == identifier for item in self.projections.unfinished.list_all()):
                return True
            return any(
                event.emotion_event_id == identifier
                for event in self.projections.emotion.list_active()
            )
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ feedback path

    def observe_reply(
        self,
        *,
        attempt_id: str,
        reaction: BehaviourReaction,
        now: datetime | None = None,
        event_ids: Sequence[str] = (),
        outcome: str = "replied",
    ) -> dict[str, Any]:
        """Record the user's reaction to a sent proactive message.

        This is where "committed" finally pays off: the attempt is resolved, the
        interaction observation is folded into the user model, and the associated
        candidate is closed.

        Args:
            attempt_id: The attempt that was sent.
            reaction: Purely observed reaction.
            now: Reference time.
            event_ids: User events that carry the reaction.
            outcome: Label recorded on the attempt transition.

        Returns:
            A mapping with ``observation_id``, ``attempt_state`` and ``weight``.

        Raises:
            KeyError: If the attempt does not exist.
        """
        stamp = ensure_aware(now) or utcnow()
        with self.write_session():
            attempt = self.projections.attempts.get(attempt_id)
            if attempt is None:
                raise KeyError(f"unknown attempt: {attempt_id}")
            state = self.projections.runtime.ensure()
            with self._db.transaction() as conn:
                busy = self.user_model.busy_probability(
                    hours_since_contact=delta_seconds(stamp, state.last_contact_at) / 3600.0,
                    replied_recently=reaction.replied,
                )
                candidate = (
                    self.projections.candidates.get(attempt.candidate_id)
                    if attempt.candidate_id
                    else None
                )
                action_spec = {
                    "type": candidate.type if candidate else "contact",
                    "proactive": True,
                    "question": bool(candidate and "?" in (candidate.intent or "")),
                }
                observation = self.user_model.observe(
                    conn,
                    action=action_spec,
                    context=self._situation_context(stamp),
                    reaction=reaction,
                    now=stamp,
                    observed_at=stamp,
                    source_event_ids=list(event_ids),
                    attempt_id=attempt_id,
                    busy_probability=busy,
                )
                if attempt.state != AttemptState.RESOLVED.value:
                    action_module.resolve(
                        self.projections.attempts, conn, attempt, reason=outcome, now=stamp
                    )
                if candidate is not None:
                    self.projections.candidates.set_status(
                        conn, candidate.candidate_id, CandidateStatus.RESOLVED.value, reason=outcome
                    )
                self.events.append(
                    EventType.INTERACTION_OBSERVATION,
                    actor=Actor.RUNTIME,
                    content=None,
                    conversation_id=self.config.conversation_id,
                    metadata={
                        "observation_id": observation.observation_id,
                        "weight": observation.weight,
                        "reaction": reaction.to_dict(),
                    },
                    source_event_ids=list(event_ids),
                    timestamp=stamp,
                    runtime_version=state.version,
                    connection=conn,
                )
                version = self.projections.runtime.write(state, conn, expect_version=state.version)
            self.user_model = UserInteractionModel(self.projections.user_model, self.config)
            return {
                "observation_id": observation.observation_id,
                "attempt_state": attempt.state,
                "weight": observation.weight,
                "version": version,
            }

    # ------------------------------------------------------------------ helpers

    def _rollover_contact_day(self, state: RuntimeState, now: datetime) -> None:
        """Reset the daily contact counter when the local day changes."""
        local = local_now(now)
        day_key = local.strftime("%Y-%m-%d")
        if state.meta.get("contact_day") != day_key:
            state.meta = dict(state.meta) | {"contact_day": day_key}
            state.contact_count_today = 0

    def _recent_contact_count(self, now: datetime, window_seconds: float | None = None) -> int:
        """Count proactive sends inside the repeat window."""
        window = window_seconds or self.config.utility.repeat_window_seconds
        since = now - timedelta(seconds=window)
        events = self.events.read(
            EventQuery(
                event_types=[EventType.PROACTIVE_SENT.value],
                since=since,
                limit=100,
            )
        )
        return len(events)

    def _hours_since_exchange(self, state: RuntimeState, now: datetime) -> float:
        """Return hours since the last exchange in either direction.

        The absence term must collapse as soon as *anything* happens - the user
        replying counts just as much as the character speaking, otherwise silence
        pressure would keep accumulating through an active conversation.

        The anchor is a dedicated ``last_exchange_at`` field written only by real
        events. It must **not** fall back to ``last_tick_at``: that field is
        written by every tick, so using it would collapse the elapsed time to the
        length of the most recent tick and the approach drive would never build.

        When nothing has ever been exchanged, the anchor is the oldest known
        timestamp for the Runtime (its creation epoch). Taking the earliest rather
        than the newest matters when the caller drives a simulated timeline that
        starts in the past: a "created just now" epoch would otherwise make the
        elapsed time negative and silently clamp to zero.
        """
        anchor = state.last_exchange_at
        if anchor is None:
            anchor = state.epoch_at
        if anchor is None:
            candidates = [value for value in (state.updated_at, now) if value is not None]
            anchor = min(candidates) if candidates else now
        return delta_seconds(now, anchor) / 3600.0

    def _last_refresh_at(self) -> datetime | None:
        """Return when candidates were last refreshed."""
        event = self.events.last_of_types([EventType.CANDIDATE_PROPOSAL.value])
        return event.timestamp if event is not None else None

    def _reply_delay_seconds(self, now: datetime) -> float | None:
        """Return the delay since the last assistant proactive message."""
        event = self.events.last_of_types([EventType.PROACTIVE_SENT.value])
        if event is None:
            return None
        return delta_seconds(now, event.timestamp)

    def _has_pending_observation(self) -> bool:
        """Return whether a sent proactive message is still awaiting an observation."""
        sent = self.projections.attempts.list_by_state([AttemptState.SENT.value], limit=1)
        return bool(sent)

    def _last_proactive_context(self) -> dict[str, Any]:
        """Return the action/context of the most recent sent attempt."""
        sent = self.projections.attempts.list_by_state([AttemptState.SENT.value], limit=1)
        if not sent:
            return {}
        attempt = sent[-1]
        candidate = (
            self.projections.candidates.get(attempt.candidate_id) if attempt.candidate_id else None
        )
        return {
            "attempt_id": attempt.attempt_id,
            "action": {
                "type": candidate.type if candidate else "contact",
                "proactive": True,
            },
            "context": {},
        }

    def _situation_context(self, now: datetime) -> dict[str, Any]:
        """Assemble the fast-variable context used by the user model."""
        state = self.projections.runtime.ensure()
        boundaries = self.projections.boundaries.list_all(include_revoked=True)
        explicit_permission = self._recent_user_permission(now)
        hours = delta_seconds(now, state.last_contact_at) / 3600.0
        return {
            "busy_probability": self.user_model.busy_probability(
                hours_since_contact=delta_seconds(now, state.last_user_message_at) / 3600.0,
                replied_recently=delta_seconds(now, state.last_user_message_at) < 1800.0,
            ),
            "recent_contact_count": self._recent_contact_count(now),
            "hours_since_contact": hours,
            "user_active_now": delta_seconds(now, state.last_user_message_at) < 300.0,
            "ever_boundary": bool(boundaries),
            "novelty": 0.6,
            "explicit_permission": explicit_permission,
            "now": isoformat(now),
            "local_hour": local_now(now).hour,
        }

    def _recent_user_permission(self, now: datetime) -> bool:
        """Return whether the user recently invited proactive contact."""
        events = self.events.read(
            EventQuery(
                event_types=[EventType.USER_MESSAGE.value],
                since=now - timedelta(days=14),
                limit=50,
                newest_first=True,
            )
        )
        for event in events:
            text = (event.content or "").lower()
            if any(marker.lower() in text for marker in PERMISSION_MARKERS):
                return True
        return False

    def _emotion_tendency(self, state: RuntimeState, active: Sequence[EmotionEvent]) -> float:
        """Return the emotional approach tendency ``E``.

        Negative moods create more approach drive in an attached character (the
        wish to repair), while positive moods create contact drive.
        """
        intensity = max((e.intensity for e in active), default=0.0)
        sign = 1.0 if state.mood_valence >= 0 else 1.0
        return clamp(sign * (abs(state.mood_valence) * 0.6 + intensity * 0.4))

    def _emotion_alignment(self, candidate: CandidateIntent, active: Sequence[EmotionEvent]) -> float:
        """Return how well a candidate matches the current emotional needs."""
        if not active:
            return 0.2
        top = max(active, key=lambda e: e.intensity)
        if top.direction == "-" and candidate.type in {"repair", "follow_up", "check_in"}:
            return clamp(0.4 + top.intensity)
        if top.direction == "+" and candidate.type in {"share", "curious_question", "contact"}:
            return clamp(0.4 + top.intensity)
        return clamp(0.2 + 0.3 * top.intensity)

    def _action_spec(self, candidate: CandidateIntent) -> dict[str, Any]:
        """Convert a candidate into the action description the user model expects."""
        return {
            "type": candidate.type,
            "proactive": candidate_module.is_candidate_proactive(candidate),
            "emotional_expression": candidate.type == "share",
            "question": candidate.type in {"follow_up", "curious_question", "check_in"},
            "topic_shift": candidate.type == "curious_question",
            "length": len(candidate.intent or ""),
        }

    def _expire_candidates(self, conn: Any, *, now: datetime) -> list[str]:
        """Expire candidates past their deadline."""
        expired: list[str] = []
        for candidate in self.projections.candidates.list_active(limit=100):
            if candidate.expires_at is not None and candidate.expires_at <= now:
                self.projections.candidates.set_status(
                    conn, candidate.candidate_id, CandidateStatus.EXPIRED.value, reason="ttl"
                )
                expired.append(candidate.candidate_id)
        return expired

    def _retire_candidates_for(
        self, unfinished_ids: Sequence[str], conn: Any, *, now: datetime
    ) -> list[str]:
        """Retire candidates whose grounding unfinished matter just settled."""
        markers = {f"{candidate_module.UNFINISHED_SOURCE_PREFIX}{uid}" for uid in unfinished_ids}
        retired: list[str] = []
        for candidate in self.projections.candidates.list_active(limit=100):
            if not markers & set(candidate.sources):
                continue
            self.projections.candidates.set_status(
                conn,
                candidate.candidate_id,
                CandidateStatus.RESOLVED.value,
                reason="grounding_matter_resolved",
            )
            retired.append(candidate.candidate_id)
        return retired

    def _invalidate_candidates(self, conn: Any, *, now: datetime, user_message: str) -> list[str]:
        """Retire candidates whose ``invalidate_when`` conditions just became true."""
        situation = " ".join(
            str(item.get("content") or "")
            for item in self.projections.situation.list_active(limit=20)
        )
        retired: list[str] = []
        for candidate in self.projections.candidates.list_active(limit=100):
            matched = candidate_module.invalidated_by_situation(
                candidate, situation_text=situation, user_message=user_message
            )
            if matched is None:
                continue
            self.projections.candidates.set_status(
                conn,
                candidate.candidate_id,
                CandidateStatus.RETIRED.value,
                reason=f"invalidated:{matched}",
            )
            retired.append(candidate.candidate_id)
        return retired

    def _refresh_candidates(
        self, *, now: datetime, state: RuntimeState, active: Sequence[EmotionEvent]
    ) -> list[CandidateIntent]:
        """Generate and apply candidate operations through the pool manager."""
        existing = self.projections.candidates.list_active(limit=self.config.candidate.max_active)
        activated = self.memory_store.activated_memories(limit=4)
        proposals = candidate_module.generate(
            state=state,
            config=self.config,
            unfinished=self.projections.unfinished.list_open(),
            activated=activated,
            existing=existing,
            now=now,
            emotion_intensity=max((e.intensity for e in active), default=0.0),
        )
        operations = candidate_module.plan_operations(
            proposals=proposals, existing=existing, config=self.config
        )
        self.apply_candidate_operations(operations, now=now)
        return self.projections.candidates.list_active(limit=self.config.candidate.max_active)

    def apply_candidate_operations(
        self,
        operations: Sequence[candidate_module.CandidateOperation],
        *,
        now: datetime | None = None,
        source: str = "runtime",
        emit_event: bool = True,
    ) -> candidate_module.PoolApplyResult:
        """Apply ADD/UPDATE/RETIRE/REINTERPRET operations to the pool.

        This is the only path by which a semantic model can influence the pool, and
        it runs with validation inside the single writer. Invalid operations are
        rejected individually rather than aborting the batch.

        Args:
            operations: Operations to apply.
            now: Reference time.
            source: Label recorded on the proposal event.
            emit_event: Append a ``candidate_proposal`` raw event.

        Returns:
            A :class:`~companion_runtime.candidate.PoolApplyResult`.
        """
        stamp = ensure_aware(now) or utcnow()
        with self.write_session():
            state = self.projections.runtime.ensure()
            with self._db.transaction() as conn:
                context = pool_module.PoolContext(projections=self.projections, config=self.config)
                result = pool_module.apply_operations(
                    context, conn, operations, now=stamp, state=state
                )
                if emit_event:
                    self.events.append(
                        EventType.CANDIDATE_PROPOSAL,
                        actor=Actor.RUNTIME,
                        content=None,
                        conversation_id=self.config.conversation_id,
                        metadata={"source": source, "result": result.to_dict()},
                        timestamp=stamp,
                        runtime_version=state.version,
                        connection=conn,
                    )
                self.projections.runtime.write(state, conn, expect_version=state.version)
        return result
