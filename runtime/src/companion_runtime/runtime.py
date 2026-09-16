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
from typing import Any, Iterator, Mapping, Sequence

from . import action as action_module
from . import boundaries as boundary_module
from . import candidate as candidate_module
from . import emotion as emotion_module
from . import memory as memory_module
from . import motivation as motivation_module
from . import pool as pool_module
from . import protocol as protocol_module
from . import unfinished as unfinished_module
from . import user_model as user_model_module
from .config import RuntimeConfig, StorageConfig
from .db import Database, open_database
from .db_base import ConflictError
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
    OutboxStatus,
    Priority,
    RawEvent,
    ReconcileAction,
    RuntimeState,
    TaskKind,
    UnfinishedStatus,
    new_id,
)
from .user_model import BehaviourReaction, Prediction, UserInteractionModel
from .utility import (
    clamp,
    delta_seconds,
    ensure_aware,
    isoformat,
    local_now,
    max_datetime,
    parse_datetime,
    utcnow,
)

LOGGER = logging.getLogger("companion_runtime.runtime")

#: Types of user message that indicate the user is unavailable right now.
BUSY_MARKERS = ("工作很多", "很忙", "没时间", "忙", "busy", "开会", "加班")
#: Types of user message that indicate explicit permission for proactive contact.
PERMISSION_MARKERS = ("多主动", "随时找我", "可以找我", "欢迎找我", "you can message me")

#: Key under which the last attempted deep refresh is stored in ``RuntimeState.meta``.
#:
#: The pacing of a deep refresh is a *durable* fact, not a process-local one: a
#: sidecar that restarts would otherwise forget that it refreshed a minute ago and
#: spend again immediately. Storing it in the runtime row also keeps it in the same
#: transaction-protected place as every other piece of state.
LAST_DEEP_REFRESH_META_KEY = "last_deep_refresh_at"

#: Key under which the moment of the last endogenous *decision* is stored in
#: ``RuntimeState.meta`` (design §50).
#:
#: The action hazard is ``P(act) = 1 - exp(-λ(t)·Δt)``, and ``Δt`` is the time between
#: two *opportunities to act* - two decisions - not between two advances of the clock.
#: A round used to read that interval off ``last_tick_at``, which is written by every
#: tick, including the decision entries that only read (``/context``); a poll could
#: therefore consume the character's whole waiting window (measured: one ``GET
#: /schedule`` collapsed the interval from a three-day offline window to 2 ms and
#: P(act) from ~1 to ~0, and the round that was supposed to reach out stayed silent).
#: ``last_exchange_at`` exists for the absence term for exactly this reason ("ticks
#: never write it, so it is a stable anchor"); the hazard needs the same kind of anchor
#: and it is the round itself.
#:
#: Like the deep-refresh pacing, the anchor is a *durable* fact: a restart must not
#: forget how long the character has been waiting for a decision.
LAST_DECISION_META_KEY = "last_decision_at"

#: How many delivered attempts reply attribution scans when it has to find the one
#: belonging to a specific conversation. Delivered attempts accumulate forever (a
#: ``sent`` attempt is never expired), so the lookup is bounded; the newest few are
#: the only ones a reply can plausibly be answering.
ATTRIBUTION_SCAN_LIMIT = 20


@dataclass(slots=True)
class TickReport:
    """What a :meth:`Runtime.lazy_tick` call actually changed."""

    dt_seconds: float = 0.0
    decayed_emotions: list[str] = field(default_factory=list)
    new_matters_due: list[str] = field(default_factory=list)
    expired_matters: list[str] = field(default_factory=list)
    expired_attempts: list[str] = field(default_factory=list)
    #: Delivered messages that were never answered, recorded as weak evidence once
    #: they age past ``user_model.silence_after_hours`` (design §22.3). Without a
    #: producer here the ``no_reply_weight`` path never fires and the model only ever
    #: learns from replies, i.e. it can never learn that it is being ignored.
    absent_replies: list[str] = field(default_factory=list)
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
            "absent_replies": list(self.absent_replies),
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
    #: The sent attempt this reply was attributed to, when there was one. The
    #: attempt is resolved by the same call, so this is reported to the caller
    #: rather than left to be discovered afterwards.
    attributed_attempt_id: str | None = None
    #: Re-coordination decisions applied to attempts that had not been delivered
    #: yet (see :meth:`Reducer.reconcile_pending_attempts`), one entry per
    #: re-coordinated attempt. Empty when nothing was in flight.
    reconcile_decisions: list[dict[str, Any]] = field(default_factory=list)
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
    #: Set when the message was already in the raw history: the identifier was
    #: seen before, so this call appended nothing and ran no foreground pass.
    #: Every other field then describes the *existing* event, which is what makes
    #: a redelivered envelope (a retried HTTP call, a replayed adapter batch)
    #: converge instead of overwriting history or raising a uniqueness error.
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "event": self.event.to_dict(),
            "version": self.version,
            "duplicate": self.duplicate,
            "boundary_ids": list(self.boundary_ids),
            "unfinished_created": list(self.unfinished_created),
            "unfinished_resolved": list(self.unfinished_resolved),
            "emotion_event_ids": list(self.emotion_event_ids),
            "memory_candidate_id": self.memory_candidate_id,
            "observation_id": self.observation_id,
            "attributed_attempt_id": self.attributed_attempt_id,
            "reconcile_decisions": [dict(item) for item in self.reconcile_decisions],
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


#: The six collection fields of a deep-refresh suggestion set. Used to recognise a
#: bare mapping as an unwrapped suggestion set.
_SUGGESTION_FIELDS: tuple[str, ...] = (
    "reinterpretations",
    "psychological_interpretation",
    "candidate_intent_operations",
    "memory_suggestions",
    "unfinished_matter_suggestions",
    "user_model_evidence_suggestions",
)


def _coerce_suggestions(suggestions: Any, *, provider_name: str) -> Any:
    """Normalise whatever a provider returned into a suggestion object or ``None``.

    A provider is third-party code reached over a wire contract: it may hand back a
    :class:`~companion_runtime.providers.DeepRefreshSuggestions`, a plain mapping, a
    ``suggestions`` envelope, a list or a string. A mapping is parsed through
    :func:`~companion_runtime.providers.parse_deep_refresh`, so a malformed reply
    degrades exactly the way a malformed HTTP body does, and anything unrecognisable
    is reported as absent rather than being allowed to raise further down.

    Args:
        suggestions: Raw return value of ``provider.deep_refresh``.
        provider_name: Name of the calling provider, used for provenance.

    Returns:
        A suggestion object, or ``None`` when the value carries no suggestion set.
    """
    if suggestions is None:
        return None
    if isinstance(suggestions, Mapping):
        from .providers import parse_deep_refresh

        return parse_deep_refresh(dict(suggestions), provider=provider_name)
    if any(hasattr(suggestions, name) for name in _SUGGESTION_FIELDS):
        return suggestions
    LOGGER.warning(
        "Deep refresh provider %s returned %s, which carries no suggestions; ignoring",
        provider_name,
        type(suggestions).__name__,
    )
    return None


def _suggestion_field(suggestions: Any, name: str, default: Any = "") -> Any:
    """Read one field from a suggestion object or mapping, defensively."""
    if isinstance(suggestions, Mapping):
        return suggestions.get(name, default)
    return getattr(suggestions, name, default)


def _is_empty_suggestions(suggestions: Any) -> bool:
    """Return whether a suggestion set carries nothing, tolerating its shape."""
    checker = getattr(suggestions, "is_empty", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - a broken checker means "assume empty"
            return True
    for name in _SUGGESTION_FIELDS:
        if _suggestion_field(suggestions, name, None):
            return False
    return True


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
    #: Outcome of the deep cognition refresh attempted during this round. Always
    #: present so an operator can tell "nothing needed doing" from "never tried".
    deep_refresh: dict[str, Any] = field(default_factory=dict)
    #: Outcome of the rule-based consolidation pass attempted during this round.
    #: Always present, for the same reason as ``deep_refresh``: long-term memory
    #: forming (or not forming) must be visible without guessing.
    consolidation: dict[str, Any] = field(default_factory=dict)

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
            "deep_refresh": dict(self.deep_refresh),
            "consolidation": dict(self.consolidation),
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
        self._db = database or open_database(storage)
        self._db.migrate()
        self.mirror_path = storage.raw_log_path if storage.mirror_raw_events else None
        self.events = EventLog(self._db, self.mirror_path)
        self.projections = Projections(self._db, self.config.runtime_id)
        # The runtime row is created here so that maintenance tooling always sees a
        # fully initialised database. The version stays at 0 until a real tick or
        # event occurs, and ``lazy_tick`` may still install its own epoch the first
        # time it is driven. The configured value profile is what compiles the
        # character's dynamics, so it seeds the row rather than a neutral default.
        initial_state = self.projections.ensure_defaults(created_at, values=self.config.values)
        self.memory_store = memory_module.MemoryStore(self.projections.memory, self.config)
        self.user_model = UserInteractionModel(self.projections.user_model, self.config)
        self.rng = random.Random(seed)
        self._write_lock = threading.RLock()
        #: Per-thread state for the two re-entrancy questions this class has to answer:
        #: ``ticking`` (a tick triggered from inside a tick must not run - see
        #: :meth:`lazy_tick`) and ``write_depth`` (a write entry called from inside an
        #: existing write session must not tick - see :meth:`tick_for_entry`).
        self._thread_state = threading.local()
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
        #: In-memory mirror of :data:`LAST_DEEP_REFRESH_META_KEY`. The persisted
        #: value in ``state.meta`` is authoritative; this cache only avoids a state
        #: read on the hot path.
        self._last_deep_refresh_at: datetime | None = None
        #: In-memory mirror of :data:`LAST_DECISION_META_KEY`. The persisted value in
        #: ``state.meta`` is authoritative and survives a restart; this cache carries
        #: the anchor written by this process's rounds.
        self._last_decision_at: datetime | None = None
        #: The clock as this process found it, captured *before* any entry can tick it.
        #: It is the interval start for the first round of a database that has no
        #: recorded decision yet: a brand-new database starts at its creation epoch and
        #: an existing one at the clock it was left at - i.e. exactly the interval the
        #: round would have integrated before the anchor existed, so an upgrade does not
        #: change the first round's behaviour. Reading ``last_tick_at`` live instead
        #: would let an entry tick between construction and that first round consume the
        #: whole waiting window.
        self._initial_clock_at = initial_state.last_tick_at or initial_state.epoch_at
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


    def close(self) -> None:
        """Close the database handle."""
        self._db.close()

    @contextmanager
    def write_session(self) -> Iterator[None]:
        """Acquire the single-writer lock for a compound operation.

        The nesting depth is tracked per thread so :meth:`tick_for_entry` can tell an
        *external* entry from an internal step of one that is already in progress.
        """
        with self._write_lock:
            self._thread_state.write_depth = getattr(self._thread_state, "write_depth", 0) + 1
            try:
                yield
            finally:
                self._thread_state.write_depth -= 1

    def tick_for_entry(self, now: datetime | None = None) -> TickReport:
        """Advance the clock for a *decision* entry that is about to judge something.

        This is the entry-level half of design §86.4, and the boundary it draws was
        measured rather than assumed:

        * **decision entries tick** - ``/authorize``, ``/context``, and the operator
          commands. The audit's complaint is exactly here: those read a drive value, a
          hazard rate and a decayed emotion, and without this they read values integrated
          only up to the last heartbeat, so a decision taken after a long silence was
          judged against the past.
        * **read-only entries do not** - ``GET /schedule`` and ``POST
          /user-model/predict`` inspect the state rather than judge it (the plan is
          computed from timestamp anchors, not from an integrated drive), and a query
          that mutates the world is its own defect: a caller polling either endpoint
          would change the character's behaviour. They used to tick; the hazard anchor
          (see :meth:`_record_decision`) makes removing that safe, because a tick that
          no longer has to carry the hazard interval can no longer consume it.
        * **write entries do not** - a claim, a render report, a delivery receipt or a
          proposal is a step *of* the outbox and attempt lifecycle, and integrating
          time in the middle of one races the very operation being reported. Wiring it
          there was implemented and then withdrawn: it turned two resilience invariants
          red ("a committed-but-undelivered attempt closes the gate for new rounds", and
          the autonomous round queuing render work) because the attempt was aged out
          from under the step that was reporting it.

        Two guards make the call safe where it *is* made. A caller already inside a write
        session, or already inside a database transaction, leaves the clock to the entry
        that owns it - not as an optimisation but because a tick opens its own
        transaction on the shared connection, and a thread that holds the connection
        while asking for the runtime's write lock deadlocks against a thread holding the
        write lock and asking for the connection. That inversion is what the first
        version of this wiring produced.

        Args:
            now: The entry's moment, or ``None`` when the caller has none.

        Returns:
            The tick report (empty when the clock was left to the surrounding entry).
        """
        if getattr(self._thread_state, "write_depth", 0) > 0:
            return TickReport()
        if self._db.in_transaction_nowait():
            return TickReport()
        # ``maintenance=False``: an entry integrates time, it does not sweep. See
        # :meth:`lazy_tick` for why the lifecycle sweeps belong to the heartbeat.
        return self.lazy_tick(now, maintenance=False)

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

    def lazy_tick(self, now: datetime | None = None, *, maintenance: bool = True) -> TickReport:
        """Advance the whole Runtime to ``now`` in a single pass.

        This is the unified time entry point. It must be called (under the write
        lock) before any other mutation; it is idempotent for a zero/negative
        elapsed time and cheap for tiny elapsed times.

        Updated in one shot: background mood recovery, emotion event decay,
        approach impulse, restraint, pressure, cooldown, memory activation decay,
        unfinished-matter timing, boundary expiry and candidate deadlines.

        **Re-entrancy.** A tick triggered from inside a tick is not a new external
        entry, and this guard is load-bearing rather than defensive: the reducer's
        write entries advance the clock before they decide anything (design §86.4), and
        the tick itself writes through those same entries
        (:meth:`_close_stalled_attempts` closes attempts, the absent-reply sweep records
        observations). Without the guard the two call each other until the stack runs
        out - which is exactly what happened the first time this was wired. A nested
        call returns an empty report and leaves the work to the tick already running,
        which is the honest answer: the clock is being advanced by the caller.

        The flag is per-thread because the Runtime serves several threads and only the
        inner ``write_session`` serialises them; a shared flag would let one thread's
        tick swallow another's.

        Args:
            now: Reference time, defaults to the current UTC time.
            maintenance: Whether this tick may also *sweep* - reclaim expired leases,
                close attempts that can no longer be delivered, record the silences of
                unanswered messages. The heartbeat (``endogenous_round``, ``POST
                /tick``) does; an entry tick does not, because an entry is often a step
                *of* the outbox lifecycle (a claim, a render report, a delivery receipt)
                and a sweep interleaved with it races the very operation being reported.
                Time is integrated either way: what an entry needs is that the drive,
                hazard and decay it is about to read are current, not that unrelated
                lifecycle work happens in the middle of its own.

        Returns:
            A :class:`TickReport` describing what changed.
        """
        if getattr(self._thread_state, "ticking", False):
            LOGGER.debug("lazy_tick re-entered from inside a tick; leaving it to the caller")
            return TickReport()
        self._thread_state.ticking = True
        try:
            return self._lazy_tick(now, maintenance=maintenance)
        finally:
            self._thread_state.ticking = False

    def _lazy_tick(self, now: datetime | None = None, *, maintenance: bool = True) -> TickReport:
        """Do the work of :meth:`lazy_tick`; call that, never this directly."""
        stamp = ensure_aware(now) or utcnow()
        report = TickReport()
        with self.write_session():
            state = self.projections.runtime.ensure(stamp)
            # The base clock is the creation epoch: a fresh Runtime measures
            # absence from when it was created, not from whenever the current
            # process started. The supplied clock is clamped to be no earlier than
            # the epoch, so driving a simulated timeline stays monotonic instead of
            # producing a negative elapsed time that silently clamps to zero.
            epoch = ensure_aware(state.epoch_at)
            if epoch is not None and stamp < epoch:
                LOGGER.warning(
                    "lazy_tick called with a time earlier than the creation epoch; "
                    "using the epoch as the base clock"
                )
                stamp = epoch
            last = state.last_tick_at or epoch or stamp
            # A *delayed* caller may hand in a timestamp older than the last tick
            # (a batch of events replayed out of order, a clock that stepped back).
            # Letting the tick move ``last_tick_at`` backwards would make the next
            # forward tick integrate the same interval twice - extra hazard
            # exposure the character never lived through. Time is therefore
            # monotone: an earlier timestamp can never undo a tick that happened.
            if stamp < last:
                LOGGER.warning(
                    "lazy_tick called with %s, earlier than the last tick %s; "
                    "keeping the clock monotone",
                    stamp.isoformat(),
                    last.isoformat(),
                )
                stamp = last
            dt = delta_seconds(stamp, last)
            report.dt_seconds = dt
            report.changed = dt > 0.0
            if dt <= 0.0:
                # Nothing has elapsed, so there is nothing to integrate: no decay, no
                # sweep, no write. This early return is what makes the clock advance
                # cheap enough to run on *every* write entry (design §86.4): an entry
                # that arrives in the same instant as the last tick costs one state read
                # instead of a full maintenance pass. The sweeps are time-based, so the
                # tick that already ran at this instant has just done them.
                return report

            with self._db.transaction() as conn:
                state = self._apply_time_passage(conn, state=state, now=stamp, dt_seconds=dt, report=report)
                if maintenance:
                    report.released_leases = self.projections.outbox.reclaim_expired(conn, stamp)
                    report.expired_attempts = self._close_stalled_attempts(conn, now=stamp)
                    report.absent_replies = self._record_absent_replies(conn, now=stamp)
                # Closing a stalled attempt bumps the version inside this same
                # transaction (a nested savepoint), so the tick commits on the
                # version actually stored rather than on the one it read before.
                state.version = max(state.version, self.projections.runtime.read().version)
                version = self.projections.runtime.write(state, conn, expect_version=state.version)
                report.version = version
        if report.absent_replies:
            # The user model was updated inside the tick's transaction; rebuild the
            # in-memory instance from the committed rows so callers do not read a
            # model that predates the evidence they can see in the database.
            self.reload_user_model()
        return report

    def _record_absent_replies(self, conn: Any, *, now: datetime) -> list[str]:
        """Record "the user did not answer" as weak evidence (design §22.3).

        Only the *positive* half of the feedback loop had a producer: a reply is
        attributed (and consumed) when the user speaks next, so the user model learned
        exclusively from answers. A message that was delivered and then ignored left no
        trace at all, and the ``no_reply_weight`` path in
        :func:`~companion_runtime.user_model.evidence_weight` was unreachable in
        production - the character could not learn that it was being ignored, whatever
        the user did.

        A delivered attempt older than ``user_model.silence_after_hours`` with no
        observation of its own is therefore closed as ``no_reply`` and recorded as
        ``BehaviourReaction(replied=False, reply_delay_seconds=<waited>)``. The evidence
        is deliberately weak (``no_reply_weight``, damped by ``1 - P(busy)``) and it is
        *not* a rejection: "6 hours without a reply" must not read as negative feedback.

        Args:
            conn: Open write transaction (this runs inside the tick).
            now: Reference time.

        Returns:
            Identifiers of the observations recorded by this pass.
        """
        horizon = self.config.user_model.silence_after_hours * 3600.0
        if horizon <= 0:
            return []
        recorded: list[str] = []
        for attempt in self.projections.attempts.list_by_state(
            [AttemptState.SENT.value], limit=ATTRIBUTION_SCAN_LIMIT, newest_first=True
        ):
            reference = attempt.updated_at or attempt.created_at
            if reference is None:
                continue
            waited = delta_seconds(now, reference)
            if waited < horizon:
                continue
            if self.projections.user_model.observation_for_attempt(attempt.attempt_id) is not None:
                continue
            state = self.projections.runtime.ensure()
            # The same busy belief the reply-attribution path uses, and for the same
            # reason: a user who *said* they are swamped should have their silence read
            # as "busy", not as disinterest. This path used to look only at how long
            # the silence lasted, so "今天工作很多" softened nothing while a reply
            # arriving after the same message was damped correctly.
            busy = self.user_model.busy_probability(
                hours_since_contact=delta_seconds(now, state.last_contact_at) / 3600.0,
                replied_recently=False,
                context={"stated_busy": self._recent_stated_busy(now)},
            )
            candidate = (
                self.projections.candidates.get(attempt.candidate_id)
                if attempt.candidate_id
                else None
            )
            reaction = BehaviourReaction(replied=False, reply_delay_seconds=waited)
            reaction.busy_probability = busy
            observation = self.user_model.observe(
                conn,
                action=self._action_spec(candidate),
                context=self._situation_context(now),
                reaction=reaction,
                now=now,
                observed_at=now,
                source_event_ids=[],
                attempt_id=attempt.attempt_id,
                busy_probability=busy,
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
                    "attempt_id": attempt.attempt_id,
                    "reason": "no_reply",
                },
                source_event_ids=[],
                timestamp=now,
                runtime_version=self.projections.runtime.read().version,
                connection=conn,
            )
            # The attempt is consumed exactly like a reply would consume it: the
            # attribution path uses the same "one user message closes the attempt"
            # rule, so consuming it here is what keeps a later message from being
            # folded into a conversation that has moved on.
            action_module.resolve(
                self.projections.attempts, conn, attempt, reason="no_reply", now=now
            )
            self.projections.outbox.cancel_for_attempt(
                conn, attempt.attempt_id, reason="attempt_resolved"
            )
            if candidate is not None:
                self.projections.candidates.set_status(
                    conn,
                    candidate.candidate_id,
                    CandidateStatus.RESOLVED.value,
                    reason="no_reply",
                )
            recorded.append(observation.observation_id)
            LOGGER.info(
                "recorded %s as unanswered after %.1f h (weight %.3f)",
                attempt.attempt_id,
                waited / 3600.0,
                observation.weight,
            )
        return recorded

    def _close_stalled_attempts(self, conn: Any, *, now: datetime) -> list[str]:
        """Close action attempts that can no longer be delivered.

        Two independent leaks are swept here, both of which used to leave an
        attempt in flight forever - blocking every later endogenous dispatch:

        1. **Failed delivery rows.** A row whose lease expired with its attempt
           budget exhausted, or that a worker failed terminally, can never be
           completed. The attempt it belongs to is failed and its sibling rows are
           cancelled.
        2. **Orphaned attempts.** An attempt that is still pre-send but has no
           pending or leased outbox row has nothing left that could ever deliver
           it (its rows were cancelled, or it was rendered outside the queue), so
           once the send window has elapsed it is expired.

        Args:
            conn: Open write transaction.
            now: Reference time.

        Returns:
            Identifiers of the attempts closed by this pass.
        """
        closed = self.reducer.close_settled_outbox_attempts(now=now)
        live_rows: set[str] = set()
        for item in self.projections.outbox.list_items(status=None, limit=500):
            if item.status not in {OutboxStatus.PENDING.value, OutboxStatus.LEASED.value}:
                continue
            attempt_id = str(item.payload.get("attempt_id") or "")
            if attempt_id:
                live_rows.add(attempt_id)
        window = timedelta(seconds=self.config.action.send_expiry_seconds)
        for attempt in self.projections.attempts.list_by_state(
            list(action_module.PRE_SEND_STATES)
        ):
            if attempt.attempt_id in live_rows or attempt.attempt_id in closed:
                continue
            reference = attempt.updated_at or attempt.created_at
            if reference is None or now - reference <= window:
                continue
            if attempt.state == AttemptState.PROPOSED.value:
                action_module.abort(
                    self.projections.attempts, conn, attempt, reason="proposal_orphaned", now=now
                )
            else:
                action_module.expire(
                    self.projections.attempts,
                    conn,
                    attempt,
                    reason="no_deliverable_outbox_row",
                    now=now,
                )
            closed.append(attempt.attempt_id)
        return closed

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

        # --- user model: belief confidence ages with *time*, not only with new
        # observations (design §28). Without this the model kept whatever certainty the
        # last observation gave it, so a fortnight of silence left it as sure about the
        # user as it was on the day they last spoke. The means are historical facts and
        # do not move; what relaxes is the precision on top of the prior, which shows
        # up as more uncertainty and a more conservative bound.
        self.user_model.tick_drift(dt_seconds, connection=conn)

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
                # Idempotency is decided *inside* the transaction that would do the
                # write: a caller-supplied identifier that already exists means the
                # message is a redelivery, and the only correct answer is the
                # outcome the first delivery produced (or, since the first
                # delivery's derived effects are already recorded, an outcome
                # referring to the same event). Running the foreground path twice
                # would double-count the message: a second boundary, a second
                # unfinished matter, a second user-model observation.
                if event_id:
                    existing = self.events.get(event_id)
                    if existing is not None:
                        LOGGER.info(
                            "user message %s already ingested; returning the recorded event",
                            event_id,
                        )
                        return MessageOutcome(
                            event=existing, version=state.version, duplicate=True
                        )
                try:
                    # The insert runs in a nested transaction - a SAVEPOINT - and that
                    # is what makes the recovery below work on both backends:
                    # PostgreSQL aborts the *whole* transaction on a constraint
                    # violation, so with a bare ``except`` the follow-up read would
                    # fail there, while SQLite tolerates it. Rolling back to the
                    # savepoint leaves a usable transaction either way.
                    with self._db.transaction() as insert_conn:
                        event = self.events.append(
                            EventType.USER_MESSAGE,
                            actor=Actor.USER,
                            content=content,
                            conversation_id=conversation_id or self.config.conversation_id,
                            metadata=metadata,
                            timestamp=stamp,
                            runtime_version=state.version,
                            event_id=event_id,
                            connection=insert_conn,
                        )
                except ConflictError:
                    # A concurrent writer appended this identifier between the
                    # check above and this insert (two processes on one database
                    # file). The invariant is the same, so the answer is too.
                    existing = self.events.get(event_id) if event_id else None
                    if existing is None:
                        raise
                    return MessageOutcome(
                        event=existing, version=state.version, duplicate=True
                    )

                # Entry barrier: stop any new endogenous dispatch immediately.
                # The pause is extended, never shortened: a delayed message must
                # not cut short a barrier that a newer message put in place.
                state.foreground_pause_until = max_datetime(
                    state.foreground_pause_until,
                    stamp + timedelta(seconds=self.config.scheduler.foreground_pause_seconds),
                )

                outcome = MessageOutcome(event=event)
                outcome.proactive_paused_until = state.foreground_pause_until

                # --- boundary rules (before any semantic layer wakes up)
                # A topic-scoped rule matches the *instruction*, never its object, so
                # the referent of "暂时不要跟我说这个" is resolved here - from the open
                # matter that was being discussed, else from the last thing the user
                # said - and travels with the boundary. ``None`` means it could not be
                # established, and then the boundary constrains nothing: guessing would
                # silence a subject the user never named.
                declared = boundary_module.detect_boundaries(
                    event,
                    state=state,
                    config=self.config,
                    now=stamp,
                    referent=boundary_module.referent_for(
                        previous_events=[
                            prior
                            for prior in reversed(self.events.recent(8))
                            if prior.event_type == EventType.USER_MESSAGE.value
                            and prior.event_id != event.event_id
                        ],
                        matters=self.projections.unfinished.list_open(),
                    ),
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
                # that the projection already reflects the settled state, and
                # before new obligations are detected so that a completion
                # statement ("到家了") cannot re-open the obligation it just met.
                live_matters = self.projections.unfinished.list_open()
                resolved_ids: list[str] = []
                resolution_pairs = unfinished_module.detect_resolution(
                    event, live=live_matters
                )
                for unfinished_id, why in resolution_pairs:
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
                    event,
                    config=self.config,
                    # ``existing`` answers "whose subject is already spoken for?", not
                    # "what is currently open?". A matter the user settled a moment
                    # ago still owns its subject: the sentence that reports a result
                    # and the sentence that forbids the topic both name the interview
                    # without promising anything new about it. Passing the *open* set
                    # was the defect - the matter had been resolved a few lines above,
                    # so the very message that closed the interview re-opened it, and
                    # ``resolved_topics`` cannot prevent that on its own because the
                    # ``result_reported`` rule declares no subjects at all.
                    existing=unfinished_module.subject_guards(
                        self.projections.unfinished.list_all(limit=200), now=stamp
                    ),
                    # A completion statement must not create the obligation it just
                    # discharged, so the subjects this event settled are excluded.
                    resolved_topics=unfinished_module.resolved_topics(
                        event, resolution_pairs
                    ),
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
                # so it contributes none rather than guessing one. The candidate is
                # stamped with the Runtime's own timeline instant, not the wall
                # clock: when it becomes due for consolidation is a question about
                # this timeline (a replayed or simulated one included).
                salience = max((e.intensity for e in created), default=0.0)
                proposal_memory = memory_module.propose_from_event(
                    event,
                    state=state,
                    unfinished=self.projections.unfinished.list_open(),
                    emotion_salience=salience,
                    config=self.config,
                    created_at=stamp,
                )
                if proposal_memory is not None:
                    self.projections.memory.upsert_candidate(conn, proposal_memory)
                    outcome.memory_candidate_id = proposal_memory.candidate_id
                    outcome.narrative = proposal_memory.summary

                # --- user interaction observation for the previous proactive act
                #
                # A normal reply *is* the feedback signal the user model learns
                # from, so it is attributed to the newest message that is still
                # awaiting one - exactly once, because the attempt is resolved as
                # part of the attribution and a resolved attempt can never be
                # attributed again.
                (
                    outcome.observation_id,
                    outcome.attributed_attempt_id,
                ) = self._attribute_user_reply(
                    conn,
                    event=event,
                    content=content,
                    reason=reason,
                    busy=busy,
                    now=stamp,
                    boundary_declared=bool(declared),
                )

                # --- invalidate candidates whose premises just died
                self._invalidate_candidates(conn, now=stamp, user_message=content)

                # The absence anchors are monotone: a delayed message carries an
                # older timestamp than the one already recorded, and following it
                # would make the Runtime believe contact happened later than it
                # did - inflating silence pressure and re-arming the entry barrier.
                state.last_user_message_at = max_datetime(state.last_user_message_at, stamp)
                state.last_exchange_at = max_datetime(state.last_exchange_at, stamp)
                state.foreground_pause_until = max_datetime(
                    state.foreground_pause_until,
                    stamp + timedelta(seconds=self.config.scheduler.foreground_pause_seconds),
                )
                # ``allow_proactive`` itself is not written here: it is derived on
                # every tick from the live boundaries, so writing it would only
                # create a second source of truth.
                version = self.projections.runtime.write(state, conn, expect_version=state.version)
                outcome.version = version
                outcome.proactive_paused_until = state.foreground_pause_until
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

            # --- re-coordination of intentions that have not left yet
            #
            # The user speaking first is exactly the situation the re-coordination
            # protocol exists for, so it runs here rather than only when a caller
            # remembers to call ``/reconcile``. An intention that is already
            # delivered is deliberately excluded: its message is in the world, and
            # only the reply above can close it.
            decisions = self.reducer.reconcile_pending_attempts(
                new_events=[event], now=stamp, states=list(action_module.PRE_SEND_STATES)
            )
            if decisions:
                outcome.reconcile_decisions = decisions
                outcome.version = self.projections.runtime.read().version

        self.user_model = UserInteractionModel(self.projections.user_model, self.config)
        return outcome

    # --------------------------------------------------------- endogenous entry

    # ------------------------------------------------------------------ hazard anchor

    def _record_decision(self, when: datetime, connection=None) -> int:
        """Persist the moment of the latest motivational verdict (design §50).

        The hazard is integrated over the interval between two decisions, so the
        moment of the last one is what the next round reads. It is recorded for a
        round that acts *and* for one that draws "not yet", because the interval that
        draw covered has been spent either way - that is what keeps the hazard
        frequency-independent (two short intervals keep the survival probability of
        one long interval). It is also recorded for a round that holds back on a
        foreground pause, which is the same verdict its tick used to express.

        Persisted (rather than kept process-local) for the same reason
        :meth:`_record_deep_refresh` is: a restart must not forget how long the
        character has been waiting, or it would act as if it had just decided.

        Args:
            when: The decision's moment.
            connection: Connection of an enclosing transaction; ``None`` opens one.

        Returns:
            The new runtime version.
        """
        if connection is None:
            with self._db.transaction() as own_connection:
                return self._record_decision(when, own_connection)
        state = self.projections.runtime.ensure(when)
        state.meta = dict(state.meta) | {LAST_DECISION_META_KEY: isoformat(when)}
        version = self.projections.runtime.write(state, connection, expect_version=state.version)
        self._last_decision_at = when
        return version

    def _record_observability(
        self,
        connection: Any,
        stamp: datetime,
        *,
        outcome: dict[str, Any],
        state: RuntimeState,
        trigger: str = "endogenous_round",
    ) -> None:
        """Persist one motivational verdict and one point of the state curve.

        Both are beta instrumentation: the verdict (including every candidate that
        lost, and the reason nothing was attempted) is the only way to tune the
        motivational game from real data instead of re-running a week, and the
        samples turn the single current-state row into a mood curve.

        Failure here must not take a round down -- observability is not the product
        -- so it is guarded, and the round's own transaction stays the writer.

        Args:
            connection: Connection of the enclosing round transaction.
            stamp: Reference time of the round.
            outcome: The decision as rendered by ``DecisionOutcome.to_dict()``.
            state: The runtime state read for this round.
            trigger: What caused the round, for grouping in the export.
        """
        if not self.config.observability.enabled:
            return
        if connection is None:
            # Called from a path that owns no transaction (the paused round): open one.
            with self._db.transaction() as own_connection:
                self._record_observability(
                    own_connection, stamp, outcome=outcome, state=state, trigger=trigger,
                )
            return
        # ``outcome`` is ``MotivationResult.to_dict()``: the verdict under
        # ``outcome`` plus every assessment. Index the verdict, keep the whole thing
        # as the payload -- the losers are the reason this table exists.
        inner = outcome.get("outcome") if isinstance(outcome.get("outcome"), dict) else outcome
        try:
            self.projections.observability.record_decision(
                connection,
                {
                    "decided_at": stamp,
                    "runtime_version": int(state.version or 0),
                    "conversation_id": self.config.conversation_id,
                    "trigger": trigger,
                    "acted": bool(inner.get("acted")),
                    "reason": inner.get("reason"),
                    "chosen_candidate_id": inner.get("chosen_candidate_id"),
                    "hazard": inner.get("hazard"),
                    "advantage": inner.get("advantage"),
                    "silence_utility": inner.get("silence_utility"),
                    "action_probability": inner.get("action_probability"),
                    "delta_t": inner.get("delta_t"),
                    "next_wake_at": inner.get("next_wake_at"),
                    "payload": outcome,
                },
            )
            self.projections.observability.record_state_sample(
                connection,
                {
                    "sampled_at": stamp,
                    "runtime_version": int(state.version or 0),
                    "reason": trigger,
                    "mood_valence": state.mood_valence,
                    "mood_arousal": state.mood_arousal,
                    "mood_stability": state.mood_stability,
                    "approach_impulse": state.approach_impulse,
                    "restraint": state.restraint,
                    "pressure": state.pressure,
                    "allow_proactive": state.allow_proactive,
                    "payload": {
                        "contact_count_today": state.contact_count_today,
                        "cooldown_until": isoformat(state.cooldown_until) if state.cooldown_until else None,
                        "foreground_pause_until": (
                            isoformat(state.foreground_pause_until)
                            if state.foreground_pause_until
                            else None
                        ),
                    },
                },
            )
        except Exception:
            LOGGER.warning("Recording the decision for observability failed; continuing", exc_info=True)

    def record_context_render(
        self,
        *,
        session: str,
        trigger: str,
        version: str,
        text: str,
        sections: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record what the host was handed on one context render (observability).

        The injected block is temporary by design and never persisted by the host, so
        without this record an optimization pass can see what the character answered
        but never what she had been told. Section sizes (and the full text, when
        ``observability.record_context_text`` is on) are what make a turn
        reproducible after the fact.

        Args:
            session: Session the block was rendered for.
            trigger: ``llm_request`` or ``message`` (the prefetch path).
            version: The state version the block was rendered from.
            text: The rendered block.
            sections: Section name -> body, as returned to the host.
            now: Reference time; defaults to the current UTC time.
        """
        if not self.config.observability.enabled:
            return
        stamp = ensure_aware(now) or utcnow()
        metadata: dict[str, Any] = {
            "trigger": trigger,
            "version": version,
            "chars": len(text),
            "sections": {str(name): len(str(body or "")) for name, body in (sections or {}).items()},
        }
        if self.config.observability.record_context_text:
            metadata["text"] = text
        try:
            with self._db.transaction() as conn:
                self.events.append(
                    EventType.SYSTEM,
                    actor=Actor.RUNTIME,
                    content="context_rendered",
                    conversation_id=session or self.config.conversation_id,
                    metadata=metadata,
                    timestamp=stamp,
                    runtime_version=self.projections.runtime.read().version,
                    connection=conn,
                )
        except Exception:
            LOGGER.warning("Recording a context render failed; continuing", exc_info=True)

    def _last_decision(self, state: RuntimeState | None = None) -> datetime | None:
        """Return when the last decision was taken, or ``None`` when there is none.

        The persisted value is the authority (it is written on the same path and also
        survives a restart) and the in-memory mirror is the fallback. A corrupt field
        yields ``None`` rather than raising, so the caller can fall back to
        :attr:`_initial_clock_at` instead of taking the round down.
        """
        current = state if state is not None else self.state()
        persisted = current.meta.get(LAST_DECISION_META_KEY)
        if isinstance(persisted, str):
            try:
                parsed = parse_datetime(persisted)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                return ensure_aware(parsed)
        return self._last_decision_at

    def endogenous_round(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        create_attempt: bool = True,
        deep_refresh: bool = True,
    ) -> EndogenousOutcome:
        """Run one endogenous wake-up round (P2): the proactive decision.

        This is the Runtime's periodic heartbeat, so it is also where the
        low-frequency deep cognition refresh belongs (patch v0.2 section 21):
        "later I understood" has to be able to happen without an operator asking.
        The refresh runs *before* the proactive decision so that a newly
        understood backlog can inform whether to speak, and it is never allowed to
        block the round - a refresh that declines is simply recorded.

        Memory consolidation runs *after* the decision, in its own write
        transaction and gated on
        :func:`~companion_runtime.memory.needs_consolidation`. It is the only writer
        of the ``memories`` table, so without this step the shipped default
        deployment (``semantic.provider = "disabled"``) would never form a single
        long-term memory; running it last also means the memories it forms can
        inform the *next* round without changing the decision this one just made.
        Like the refresh, a failing pass is logged and reported, never raised.

        Args:
            now: Reference time.
            force: Bypass the foreground pause and the scheduler gate.
            create_attempt: When a candidate wins, create the action attempt.
            deep_refresh: Whether this round may spend on a deep refresh.

        Returns:
            An :class:`EndogenousOutcome`.
        """
        stamp = ensure_aware(now) or utcnow()
        # The hazard rate is integrated over the interval between two *decisions* - the
        # character's opportunities to act - not between two advances of the clock.
        # ``last_tick_at`` is written by every tick, so a read-only decision entry
        # (``/context``, and the operator commands) would otherwise consume the window
        # the character has been waiting through; see :data:`LAST_DECISION_META_KEY`.
        entry_state = self.state()
        anchor = (
            self._last_decision(entry_state)
            or self._initial_clock_at
            or entry_state.last_tick_at
        )
        elapsed_seconds = delta_seconds(stamp, anchor)
        report = self.lazy_tick(stamp)
        outcome = EndogenousOutcome(version=report.version)

        if deep_refresh:
            try:
                outcome.deep_refresh = self.deep_refresh(now=stamp).to_dict()
            except Exception:  # noqa: BLE001 - understanding later is never urgent
                LOGGER.exception("Deep refresh during the endogenous round failed; continuing")
                outcome.deep_refresh = {"ran": False, "reason": "error"}

        with self.write_session():
            state = self.projections.runtime.ensure()
            if not force and state.foreground_pause_until is not None and stamp < state.foreground_pause_until:
                outcome.decision = {"acted": False, "reason": "foreground_pause"}
                outcome.next_wake_at = state.foreground_pause_until
                # A paused round still took a verdict (to hold back), so it starts the
                # next hazard interval - the same thing its tick used to do.
                outcome.version = self._record_decision(stamp)
                # The most frequent verdict of an active conversation is "she is
                # talking, so hold back". Recording it is what makes "why did she
                # never speak today?" answerable without re-reading the transcripts.
                self._record_observability(
                    None,
                    stamp,
                    outcome=outcome.decision,
                    state=state,
                    trigger="foreground_pause",
                )
                # A paused foreground silences speech, not maintenance: memory
                # formation is exactly the kind of work that must still happen
                # while the character is being quiet.
                outcome.consolidation = self._consolidate_if_due(now=stamp)
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
                    situation_terms=memory_module.situation_terms(self.projections),
                )
                hits = self.memory_store.retrieve(cue, limit=self.config.memory.activation_pool_size)
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
                # ---- per-candidate boundary gate (also before any utility comparison)
                # A topic boundary ("别再提面试", "别再一直追问我在干嘛") carries
                # ``allow_proactive=True`` - being told to drop a subject is not being
                # told to fall silent - so the gate above cannot express it. Such a
                # candidate must not be *weighed* at all: weighing it is what spends the
                # decision on something that can never be sent, and it is how a topic
                # the user ruled out still won the argument inside the utility model.
                pending, boundary_blocked = self._partition_by_boundaries(pending, now=stamp)

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
                if boundary_blocked:
                    # Reported rather than silently dropped: an operator asking "why
                    # did it not bring that up" needs to see that the user ruled the
                    # subject out, and which boundary did it.
                    outcome.decision["boundary_blocked"] = boundary_blocked
                outcome.next_wake_at = result.outcome.next_wake_at
                # The round is a hazard trial: the next interval starts here whether it
                # acted or drew "not yet". The anchor is written inside this transaction
                # (a savepoint), so a committed attempt builds on the version it stored.
                outcome.version = self._record_decision(stamp, conn)
                # Persist the verdict itself, acted or not: a week of "why didn't she
                # speak" cannot be answered from the decisions that did act.
                self._record_observability(conn, stamp, outcome=outcome.decision, state=state)

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

            # The decision is committed, so the maintenance pass cannot change it.
            # It gets its own transaction (the block above has closed) and its own
            # failure domain, exactly like the deep refresh.
            outcome.consolidation = self._consolidate_if_due(now=stamp)
            return outcome

    def _partition_by_boundaries(
        self, candidates: Sequence[CandidateIntent], *, now: datetime
    ) -> tuple[list[CandidateIntent], list[dict[str, Any]]]:
        """Split candidates into the ones a topic boundary allows and the ones it rules out.

        Only the *topic* scopes are handled here; a boundary that forbids proactive
        contact outright is already handled by
        :func:`~companion_runtime.boundaries.evaluate`, which the caller consults
        before this. A candidate that violates a topic boundary is removed from the set
        the motivational layer sees, so it cannot win a comparison at all.

        The candidate's subject is its target plus its intent text: those are what the
        generator derived from real state (the topic tags of a memory, the subject of an
        unfinished matter), so comparing them against the boundary's bound subject is
        comparing like with like.

        Args:
            candidates: The candidates that would otherwise be weighed.
            now: Reference time.

        Returns:
            ``(allowed, blocked)``; each blocked entry names the candidate, the boundary
            and a stable reason string, for the operator surface.
        """
        boundaries = self.projections.boundaries.active(now)
        allowed: list[CandidateIntent] = []
        blocked: list[dict[str, Any]] = []
        for candidate in candidates:
            violation = boundary_module.blocks_candidate(
                boundaries,
                now=now,
                subject=f"{candidate.target or ''} {candidate.intent or ''}",
                is_question=candidate.type in boundary_module.QUESTION_CANDIDATE_TYPES,
            )
            if violation is None:
                allowed.append(candidate)
                continue
            boundary_id, reason = violation
            blocked.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "type": candidate.type,
                    "intent": candidate.intent,
                    "boundary_id": boundary_id,
                    "reason": reason,
                }
            )
            LOGGER.info(
                "candidate %s (%s) is ruled out by boundary %s (%s)",
                candidate.candidate_id,
                candidate.type,
                boundary_id,
                reason,
            )
        return allowed, blocked

    def _event_ids_behind(self, source: str) -> list[str]:
        """Return the event ids one candidate source ultimately rests on.

        Not every source is an event id. A follow-up candidate cites
        ``unfinished:<id>`` and a curiosity candidate cites ``memory:<id>``: internal
        namespaces that point *at* the events the intention was actually built on.
        Feeding one of them to the event log as if it were an event id is not a
        crash - the lookup simply finds nothing - which is what made the routing
        defect silent: every follow-up fell through to "whoever spoke last".

        Totality matters more than precision here. A commit must never fail because
        routing could not be worked out, so an unknown or dangling identifier yields
        no ids and lets the caller fall back.

        Args:
            source: One entry of a candidate's ``sources``.

        Returns:
            The event ids to resolve a conversation with, possibly empty.
        """
        identifier = str(source or "").strip()
        if not identifier:
            return []
        try:
            if identifier.startswith(candidate_module.UNFINISHED_SOURCE_PREFIX):
                matter = self.projections.unfinished.get(
                    identifier[len(candidate_module.UNFINISHED_SOURCE_PREFIX) :]
                )
                return [
                    str(item)
                    for item in (matter.source_event_ids if matter is not None else [])
                    if item
                ]
            if identifier.startswith(candidate_module.MEMORY_SOURCE_PREFIX):
                memory = self.projections.memory.get_memory(
                    identifier[len(candidate_module.MEMORY_SOURCE_PREFIX) :]
                )
                return [
                    str(item)
                    for item in (memory.source_event_ids if memory is not None else [])
                    if item
                ]
        except Exception:  # noqa: BLE001 - routing must not fail the commit
            return []
        if ":" in identifier:
            # Another internal namespace (``emotion:``, ``situation:``). It names no
            # event, and assuming it does would send the intention into whichever
            # conversation happens to share the identifier.
            return []
        return [identifier]

    def _conversation_for(self, chosen: CandidateIntent, *, now: datetime) -> str:
        """Return the conversation a proactive action belongs to.

        A proactive message must be delivered to the conversation the character
        formed the intention *in*. Falling back to the configured default would
        mean a multi-session deployment sends every unprompted message to a
        session that may not exist - the plugin then cannot resolve the address
        and the delivery silently fails.

        The sources of an intention are *not all events*, which is the second half
        of the same defect: a follow-up candidate cites ``unfinished:<id>``, so
        treating every source as an event id made the event lookup come back empty
        for exactly the candidates whose conversation mattered most, and routing
        degraded to "the chat that spoke last". :meth:`_event_ids_behind` translates
        each source into the events behind it first.

        Resolution order, most precise first:

        1. the events the candidate's sources ultimately rest on, when they name real
           events (the evidence the intention was built on already carries the
           conversation);
        2. the conversation of the most recent user message, which is simply the
           conversation the character is currently living in - most candidates are
           generated from internal signals such as an approach-drive anchor and
           therefore have no event sources at all;
        3. the configured default, so a commit can never fail for this reason.

        Args:
            chosen: The candidate that won the motivational game.
            now: Reference time, unused today but kept for future weighting.

        Returns:
            A conversation identifier suitable for the host's addressing scheme.
        """
        identifiers: list[str] = []
        for source in chosen.sources or []:
            if source:
                identifiers.extend(self._event_ids_behind(source))
        if identifiers:
            try:
                events = self.events.get_many(identifiers)
            except Exception:  # noqa: BLE001 - a lookup failure must not fail the commit
                events = []
            with_conversation = [event for event in events if event.conversation_id]
            if with_conversation:
                newest = max(with_conversation, key=lambda event: event.timestamp)
                return str(newest.conversation_id)

        try:
            recent = self.events.read(
                EventQuery(event_types=[EventType.USER_MESSAGE.value], limit=1, newest_first=True)
            )
        except Exception:  # noqa: BLE001 - fall through to the configured default
            recent = []
        if recent and recent[0].conversation_id:
            return str(recent[0].conversation_id)
        return self.config.conversation_id

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
        # The action belongs to the conversation the intention was formed in, not
        # to whatever the process default happens to be.
        conversation_id = self._conversation_for(chosen, now=now)
        self.events.append(
            EventType.PROACTIVE_COMMITTED,
            actor=Actor.RUNTIME,
            content=chosen.intent,
            conversation_id=conversation_id,
            metadata={"attempt": attempt.to_dict(), "goal": chosen.goal},
            source_event_ids=[s for s in chosen.sources],
            timestamp=now,
            runtime_version=state.version,
            connection=conn,
        )
        # The decision itself already changes the dynamics: impulse and pressure
        # are released the moment the character commits.
        motivation_module.release_after_contact(state, config=self.config, now=now)
        # The daily contact budget is *not* charged here. ``committed`` is not
        # ``sent``: this intention may still be re-coordinated, aborted or lost
        # before it is delivered, and it is exactly this early increment that used
        # to make one delivered message count twice, because ``mark_delivered``
        # charges the same budget again when the message actually leaves. The
        # counter is therefore owned by the delivery path alone; the day key is
        # still refreshed so the row never serves a stale day.
        motivation_module.rollover_contact_day(state, now=now)

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
            conversation_id=conversation_id,
        )
        self.projections.outbox.enqueue(conn, item)
        attempt.outbox_id = item.outbox_id
        self.projections.attempts.upsert(conn, attempt)
        self.projections.candidates.set_status(
            conn, chosen.candidate_id, CandidateStatus.ACTIVE.value, reason="committed"
        )
        self.projections.runtime.write(state, conn, expect_version=state.version)
        return attempt.attempt_id, item.outbox_id

    # ------------------------------------------------------ memory maintenance

    def consolidate(
        self, *, now: datetime | None = None, limit: int = 20
    ) -> memory_module.ConsolidationResult:
        """Run one rule-based memory consolidation pass.

        This is the unattended writer of the ``memories`` table: it needs no model
        and no operator, which is what makes long-term memory exist in the shipped
        default deployment (``semantic.provider = "disabled"``). The pass promotes
        pending candidates with the summary the rule-based proposal already built,
        merges restatements, records conflicts and archives what has faded.

        It is deliberately not on the foreground path: consolidation is a P3
        maintenance job, so the endogenous round runs it when
        :func:`~companion_runtime.memory.needs_consolidation` says it is due, and
        ``companion-runtime consolidate`` runs one pass on demand.

        Args:
            now: Reference time; defaults to the wall clock.
            limit: Maximum number of candidates to promote in one pass.

        Returns:
            A :class:`~companion_runtime.memory.ConsolidationResult`.
        """
        stamp = ensure_aware(now) or utcnow()
        with self.write_session():
            with self._db.transaction() as conn:
                return memory_module.consolidate(
                    self.projections.memory,
                    conn,
                    config=self.config,
                    now=stamp,
                    limit=limit,
                )

    def _consolidate_if_due(self, *, now: datetime) -> dict[str, Any]:
        """Consolidate pending memory candidates when the interval has elapsed.

        The failure tolerance matches the deep-refresh path: forming a memory is
        never urgent enough to break the round it happened in, so a fault is logged
        and reported instead of propagating. The report is always present, so an
        operator can tell "nothing was due" from "the pass failed" from "it ran".

        Args:
            now: Reference time.

        Returns:
            A rendering of the pass: ``ran``, ``reason``, ``consolidated``,
            ``archived`` and ``skipped``.
        """
        try:
            if not memory_module.needs_consolidation(
                self.projections.memory, config=self.config, now=now
            ):
                return {"ran": False, "reason": "not_due"}
            result = self.consolidate(now=now)
        except Exception:  # noqa: BLE001 - a maintenance pass is never worth a failed round
            LOGGER.exception(
                "Memory consolidation during the endogenous round failed; continuing"
            )
            return {"ran": False, "reason": "error"}
        worked = bool(result.consolidated or result.archived)
        return {"ran": True, "reason": "applied" if worked else "nothing_to_do"} | result.to_dict()

    # ------------------------------------------------------ deep cognition path

    def deep_refresh(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        trigger_context: dict[str, Any] | None = None,
    ) -> DeepRefreshOutcome:
        """Run one low-frequency deep cognition refresh and record the attempt.

        The recording wrapper exists so that *every* attempt leaves a row, including
        the ones that decline or fail: the interesting question after a week is not
        "how many refreshes ran" but "why did the ones that ran accomplish nothing",
        and an attempt that returns early is exactly the case a log has to keep.

        Args:
            now: Reference time; defaults to the wall clock.
            force: Skip the trigger check (diagnostics and tests).
            trigger_context: Extra trigger signals, e.g. ``matter_due``.

        Returns:
            A :class:`DeepRefreshOutcome` describing what happened and why.
        """
        stamp = ensure_aware(now) or utcnow()
        outcome = self._deep_refresh(now=stamp, force=force, trigger_context=trigger_context)
        self._record_refresh_run(stamp, outcome)
        return outcome

    def _record_refresh_run(self, stamp: datetime, outcome: "DeepRefreshOutcome") -> None:
        """Persist one deep-refresh attempt for the beta's read-back.

        Observability is not the product: a failure here is logged and swallowed
        rather than allowed to take the refresh down with it.
        """
        if not self.config.observability.enabled:
            return
        try:
            payload = outcome.to_dict()
            trigger = payload.get("trigger") or {}
            with self._db.transaction() as conn:
                self.projections.observability.record_refresh_run(
                    conn,
                    {
                        "ran_at": stamp,
                        "runtime_version": int(self.projections.runtime.read().version),
                        "conversation_id": self.config.conversation_id,
                        "trigger": str(trigger.get("reason") or ""),
                        "ran": outcome.ran,
                        "reason": outcome.reason,
                        "provider": outcome.provider,
                        "degraded": outcome.degraded,
                        "operations": outcome.operations,
                        "settled_events": outcome.settled_events,
                        "latency_ms": outcome.latency_ms,
                        "payload": payload,
                    },
                )
        except Exception:
            LOGGER.warning("Recording the deep refresh run failed; continuing", exc_info=True)

    def _deep_refresh(
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

        # Signals the Runtime can answer for itself are computed here; only the
        # ones that genuinely require an outside judgement (a model flagging a
        # suspicious history, or the motivation layer mid-decision) come from the
        # caller. Anything a caller omits is treated as "not established", never
        # optimistically assumed.
        signals = self._refresh_signals(now=stamp)
        signals.update(trigger_context or {})
        unresolved = self.projections.semantics.list_unresolved(limit=200)
        trigger = evaluate_triggers(
            unresolved_count=len(unresolved),
            config=config,
            **signals,
        )
        outcome.trigger = trigger.to_dict()
        if not trigger.should_refresh and not force:
            outcome.reason = trigger.reason
            return outcome
        # Record the attempt time before spending, so a provider that times out
        # still counts against the interval rather than being retried every tick.
        # The write is durable: a restart must not forget that this just happened.
        self._record_deep_refresh(stamp)

        request = build_request(runtime=self, now=stamp, limit=config.max_operations_per_refresh)
        started = time.monotonic()
        try:
            suggestions = self.semantic_provider.deep_refresh(request)
        except Exception:  # noqa: BLE001 - a provider fault is never fatal
            LOGGER.exception("Deep refresh provider raised; treating as unavailable")
            outcome.reason = "provider_error"
            return outcome
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        # A provider that answers with a bare mapping is normalised through the same
        # parser the wire format uses, so a malformed reply degrades instead of
        # raising somewhere deeper in the pipeline.
        suggestions = _coerce_suggestions(
            suggestions, provider_name=self.semantic_provider.name
        )
        outcome.provider = _suggestion_field(suggestions, "provider") or (
            self.semantic_provider.name
        )

        if suggestions is None:
            outcome.reason = "no_suggestions"
            return outcome
        outcome.degraded = bool(_suggestion_field(suggestions, "degraded", True) or False)
        if _is_empty_suggestions(suggestions):
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
        interpretation = dict(
            _suggestion_field(suggestions, "psychological_interpretation", {}) or {}
        )
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

        # Provenance is *per operation*, not per refresh: the proposal claims only
        # the entities its surviving operations actually referenced. Passing the
        # whole backlog instead would claim that one reinterpretation rested on
        # every open event - which is untrue, and which the reducer would then have
        # to defend against when deciding what may be marked as understood.
        source_event_ids = list(
            dict.fromkeys(
                source for operation in operations for source in operation.sources if source
            )
        )
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

    def _record_deep_refresh(self, when: datetime) -> None:
        """Persist the time of the last attempted deep refresh.

        The attempt time is written before the provider is called and inside its own
        transaction, so a provider that times out still counts against the minimum
        interval. Persisting it (rather than keeping a process-local attribute)
        matters because the pacing rule is about the *cost already paid*: a restart
        that forgot the last refresh would spend again on its first heartbeat.
        """
        with self.write_session():
            with self._db.transaction() as conn:
                state = self.projections.runtime.ensure(when)
                state.meta = dict(state.meta) | {LAST_DEEP_REFRESH_META_KEY: isoformat(when)}
                self.projections.runtime.write(state, conn, expect_version=state.version)
        self._last_deep_refresh_at = when

    def _last_deep_refresh(self) -> datetime | None:
        """Return when a deep refresh was last attempted, or ``None``.

        The in-memory mirror is consulted first because it is written on the same
        path; the persisted value is the authority and wins whenever both exist,
        since it also survives a restart.
        """
        persisted = self.projections.runtime.read().meta.get(LAST_DEEP_REFRESH_META_KEY)
        if isinstance(persisted, str):
            try:
                parsed = parse_datetime(persisted)
            except (TypeError, ValueError):
                # A corrupt field must not stall the pacing rule permanently; the
                # in-memory mirror, or "never refreshed", is the honest fallback.
                parsed = None
            if parsed is not None:
                return ensure_aware(parsed)
        return self._last_deep_refresh_at

    def _refresh_signals(self, *, now: datetime) -> dict[str, Any]:
        """Return the deep-refresh trigger signals the Runtime can answer itself.

        Patch v0.2 section 21 lists several reasons to spend on a refresh. Six of
        them are observable from state the Runtime already holds, so requiring the
        caller to supply them would mean that in the default deployment - no
        operator, no model - the refresh would simply never fire. The remaining
        two (``history_suspect``, ``user_evidence_overturns``) are judgements the
        Runtime cannot make, and they default to ``False`` rather than being
        guessed at.

        Elapsed time is measured from the last *attempted* refresh. When no refresh
        has ever been attempted it is measured from the creation epoch, which is a
        real timestamp rather than a sentinel meaning "unknown": the early version
        of this method used the last tick as the anchor, so a heartbeat loop reset
        the clock every round and the pacing rule could never fire.

        Args:
            now: Reference time.

        Returns:
            Keyword arguments for :func:`companion_runtime.deep_refresh.evaluate_triggers`.
        """
        active_candidates = self.projections.candidates.list_active(limit=50)
        matters = self.projections.unfinished.list_open()
        due = [item for item in matters if item.status == UnfinishedStatus.DUE.value]
        last_refresh = self._last_deep_refresh()
        hours_since = self._hours_since_refresh(now=now, last_refresh=last_refresh)
        # Every trigger below is only meaningful when there is something for a
        # refresh to reason about. On a brand-new Runtime the pool is empty and
        # nothing is pending because nothing has happened yet, and firing then would
        # spend a request to rediscover exactly that.
        has_material = bool(matters) or self.projections.semantics.unresolved_count() > 0
        if not has_material:
            # Nothing has happened, so nothing needs understanding: the idle rule is
            # explicitly disarmed rather than merely unmatched. A fresh Runtime has
            # been "idle" since its creation epoch, which would otherwise satisfy the
            # idle threshold on its first heartbeat and spend a request to
            # rediscover that no events exist. The elapsed time is still reported,
            # because it is what the minimum-interval guard needs.
            return {
                "candidate_pool_size": None,
                "matter_due": False,
                "hours_since_last_refresh": hours_since,
                "has_previous_refresh": last_refresh is not None,
                "has_material": False,
            }

        return {
            "candidate_pool_size": len(active_candidates),
            "matter_due": bool(due),
            "hours_since_last_refresh": hours_since,
            "has_previous_refresh": last_refresh is not None,
            "has_material": True,
        }

    def _hours_since_refresh(self, *, now: datetime, last_refresh: datetime | None) -> float:
        """Return hours since the last attempted refresh, or since the epoch.

        Args:
            now: Reference time.
            last_refresh: Time of the last attempted refresh, or ``None``.

        Returns:
            A non-negative number of hours. When nothing has been refreshed yet the
            anchor is the creation epoch, so "has this Runtime been idle long
            enough to deserve a speculative refresh" is answerable on the very
            first round instead of being disabled by a zero sentinel.
        """
        baseline = last_refresh or self.state().epoch_at
        if baseline is None:
            return 0.0
        return max(0.0, delta_seconds(now, baseline) / 3600.0)

    def _is_resolvable(self, identifier: str) -> bool:
        """Return whether a grounding identifier names something that exists.

        Grounding is what stops a model from inventing a memory about an event
        that never happened. Any identifier the Runtime cannot resolve makes the
        operation carrying it be discarded before it can touch state.

        Args:
            identifier: An event, memory candidate, memory, unfinished-matter,
                emotion or candidate-intent id.

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
            if prefix == "mcd":
                # A memory *candidate* is a real entity the Runtime holds, and a
                # refresh is allowed to reason about it ("this pending candidate
                # matters because ..."). Treating its ids as unresolvable discarded
                # every such suggestion as if the model had invented it.
                return self.projections.memory.get_candidate(identifier) is not None
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
        # A delivery receipt is an entry: it changes state that the next decision
        # reads, so the clock moves first (design §86.4).
        self.lazy_tick(stamp)
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
                action_spec = self._action_spec(candidate)
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
                # Only a delivered attempt can be resolved. An attempt that was
                # aborted, expired or failed while the report was in flight is
                # already closed, and ``resolve`` would be an illegal transition -
                # the observation is still recorded, the history is left alone.
                if attempt.state == AttemptState.SENT.value:
                    action_module.resolve(
                        self.projections.attempts,
                        conn,
                        attempt,
                        reason=outcome,
                        now=stamp,
                    )
                    self.projections.outbox.cancel_for_attempt(
                        conn, attempt.attempt_id, reason="attempt_resolved"
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

    def _newest_sent_attempt(self, *, conversation_id: str | None = None) -> Any:
        """Return the most recent attempt whose message has been delivered.

        Ordering matters: ``list_by_state`` is oldest-first, so asking for one row
        in that order returns the *oldest* outstanding intention. Attribution must
        use the newest, otherwise a reply to the message the user just received
        trains the user model on a message from hours ago.

        ``conversation_id`` scopes the search to one chat, and attribution always
        passes the conversation the reply arrived in. Attribution *consumes* the
        attempt, so a global lookup lets a message in one chat resolve an intention
        the user never saw in another: the real answer can then never be attributed
        to it, and the user model learns from a reply to something that was never
        delivered there. An attempt whose row cannot be found is skipped for the
        same reason - its conversation is unknown, so it cannot be the message the
        user is answering.

        Args:
            conversation_id: Only consider attempts delivered in this conversation.

        Returns:
            The newest matching attempt, or ``None``.
        """
        sent = self.projections.attempts.list_by_state(
            [AttemptState.SENT.value], limit=ATTRIBUTION_SCAN_LIMIT, newest_first=True
        )
        if conversation_id is None:
            return sent[0] if sent else None
        for attempt in sent:
            if self._attempt_conversation(attempt) == conversation_id:
                return attempt
        return None

    def _attempt_conversation(self, attempt: Any) -> str | None:
        """Return the conversation an attempt's message was delivered into."""
        outbox_id = getattr(attempt, "outbox_id", None)
        if not outbox_id:
            return None
        row = self.projections.outbox.get(outbox_id)
        conversation = getattr(row, "conversation_id", None)
        return str(conversation) if conversation else None

    def _attribute_user_reply(
        self,
        conn: Any,
        *,
        event: RawEvent,
        content: str,
        reason: BehaviourReaction | None,
        busy: float,
        now: datetime,
        boundary_declared: bool = False,
    ) -> tuple[str | None, str | None]:
        """Attribute one user reply to the newest sent attempt, exactly once.

        A reply is the only real evidence the user model ever gets, so the two
        failure modes are both expensive: attributing it to nothing (the model
        never learns) and attributing it twice (one reply counts as two pieces of
        evidence, biasing every learned parameter). The attempt is therefore
        resolved as part of the attribution - a resolved attempt is no longer
        ``sent``, so the next message cannot be folded into it again - and an
        existing observation for the attempt is honoured as a hard stop.

        Args:
            conn: Write connection.
            event: The user message being ingested.
            content: Its verbatim text.
            reason: An explicitly supplied reaction, when the host knows one.
            busy: Belief that the user is busy, used as attribution damping.
            now: Reference time.
            boundary_declared: Whether this message declared a hard boundary. A
                boundary is a reply, but it is negative evidence, and the user
                model has a dedicated target for exactly that.

        Returns:
            ``(observation_id, attributed_attempt_id)``; either may be ``None``.
        """
        attempt = self._newest_sent_attempt(conversation_id=event.conversation_id)
        if attempt is None:
            if reason is None:
                return None, None
            # An explicit reaction with no outstanding proactive message is still
            # worth recording; it simply belongs to no attempt.
            reaction = reason
            reaction.busy_probability = busy
            observation = self.user_model.observe(
                conn,
                action=user_model_module.describe_action(type="reply", proactive=False),
                context=self._situation_context(now),
                reaction=reaction,
                now=now,
                observed_at=now,
                source_event_ids=[event.event_id],
                busy_probability=busy,
            )
            return observation.observation_id, None

        candidate = (
            self.projections.candidates.get(attempt.candidate_id) if attempt.candidate_id else None
        )
        observation_id: str | None = None
        if self.projections.user_model.observation_for_attempt(attempt.attempt_id) is None:
            reaction = reason or BehaviourReaction(
                replied=True,
                reply_delay_seconds=self._reply_delay_seconds(now),
                reply_length=len(content),
            )
            reaction.busy_probability = busy
            if boundary_declared:
                reaction.boundary_touched = True
            observation = self.user_model.observe(
                conn,
                action=self._action_spec(candidate),
                context=self._situation_context(now),
                reaction=reaction,
                now=now,
                observed_at=now,
                source_event_ids=[event.event_id],
                attempt_id=attempt.attempt_id,
                busy_probability=busy,
            )
            observation_id = observation.observation_id
            self.events.append(
                EventType.INTERACTION_OBSERVATION,
                actor=Actor.RUNTIME,
                content=None,
                conversation_id=event.conversation_id,
                metadata={
                    "observation_id": observation.observation_id,
                    "weight": observation.weight,
                    "reaction": reaction.to_dict(),
                    "attempt_id": attempt.attempt_id,
                },
                source_event_ids=[event.event_id],
                timestamp=now,
                runtime_version=self.projections.runtime.read().version,
                connection=conn,
            )

        # The attempt is closed here, and this is the only closure a delivered
        # message gets: it is what makes the attribution exactly-once and what
        # frees the dispatch gate for the next endogenous round.
        current = self.projections.attempts.get(attempt.attempt_id)
        if current is None or current.state != AttemptState.SENT.value:
            return observation_id, None
        action_module.resolve(
            self.projections.attempts, conn, current, reason="user_replied", now=now
        )
        self.projections.outbox.cancel_for_attempt(
            conn, current.attempt_id, reason="attempt_resolved"
        )
        if candidate is not None:
            self.projections.candidates.set_status(
                conn,
                candidate.candidate_id,
                CandidateStatus.RESOLVED.value,
                reason="user_replied",
            )
        return observation_id, current.attempt_id

    def _rollover_contact_day(self, state: RuntimeState, now: datetime) -> None:
        """Reset the daily contact counter when the local day changes."""
        motivation_module.rollover_contact_day(state, now=now)

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

    def _recent_stated_busy(self, now: datetime, *, window_hours: float = 24.0) -> bool:
        """Return whether the user recently *said* they are busy.

        The same marker test the entry path applies to the message that arrived
        (:data:`BUSY_MARKERS`), asked of the recent window: a silence that follows
        "今天工作很多" is evidence about their workload, not about the character.

        Args:
            now: Reference time.
            window_hours: How far back to look.

        Returns:
            ``True`` when one of the recent user messages states busy-ness.
        """
        since = now - timedelta(hours=window_hours)
        for event in self.events.read(
            EventQuery(
                event_types=[EventType.USER_MESSAGE.value],
                since=since,
                limit=20,
                newest_first=True,
            )
        ):
            text = (event.content or "").lower()
            if any(marker.lower() in text for marker in BUSY_MARKERS):
                return True
        return False

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
        """Return how well a candidate matches the current emotional needs (design §7).

        The match is decided by the candidate's **behaviour class**, not by its type
        spelling. The two hand-written sets this replaced (``{repair, follow_up, check_in}``
        for a negative mood, ``{share, curious_question, contact}`` for a positive one) had
        no relationship to :data:`~companion_runtime.user_model.TYPE_TO_BEHAVIOUR`, so the
        *same intention* scored 1.000 as ``repair`` and 0.440 as ``apology``. The number
        goes into the candidate's utility (design §45), so the spelling changed which
        candidate the character picked (``docs/BUSINESS_LOGIC_AUDIT.md`` §4).

        An unknown type gets the generic value rather than either match. That is the
        opposite of :func:`~companion_runtime.candidate.is_candidate_proactive`, and
        deliberately so: this is a matter of proportion, not a hard constraint, so "I do not
        recognise this shape" must not earn a bonus.
        """
        if not active:
            return 0.2
        top = max(active, key=lambda e: e.intensity)
        behaviour = user_model_module.TYPE_TO_BEHAVIOUR.get(str(candidate.type or ""))
        if top.direction == "-" and behaviour in user_model_module.MOOD_MATCH_NEGATIVE_CLASSES:
            return clamp(0.4 + top.intensity)
        if top.direction == "+" and behaviour in user_model_module.MOOD_MATCH_POSITIVE_CLASSES:
            return clamp(0.4 + top.intensity)
        return clamp(0.2 + 0.3 * top.intensity)

    def _action_spec(self, candidate: CandidateIntent | None) -> dict[str, Any]:
        """Convert a candidate into the canonical action record ``A`` (design §22.1).

        This is the *only* place the Runtime builds an ``A``, and every observation path
        calls it with the same candidate the prediction scored. Design §25 features a
        candidate as ``x = phi(A, C, Z)`` and §27 reuses that ``X_i`` in the posterior, so
        a second, thinner description on the observation side would teach the model about
        a feature vector it never scored. Before this was shared, three of the five
        action-derived features disagreed between the two paths (see
        :func:`~companion_runtime.user_model.describe_action`).

        ``proactive`` comes from :func:`candidate_module.is_candidate_proactive`, the same
        predicate the hard-boundary gate uses, so a behaviour blocked as proactive is also
        learned about as proactive. A missing candidate means "a message the character
        sent that no live candidate accounts for", which is an unprompted contact.
        """
        if candidate is None:
            return user_model_module.describe_action(type="contact", proactive=True)
        return user_model_module.describe_action(
            type=candidate.type,
            proactive=candidate_module.is_candidate_proactive(candidate),
        )

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
        """Retire candidates whose ``invalidate_when`` conditions just became true.

        Two questions are asked, in this order, because they are different: whether the
        candidate's condition is *visible in the current situation text* (the coarse,
        text-based test the pool manager also uses), and whether the **state it came
        from** has moved - the matter it was about resolved, the memory it cited
        archived or superseded, the question it asked already answered, the boundary it
        cited revoked, the emotion it rode on decayed. The second question needs the
        records, not the text, and without it a candidate citing a memory kept asking
        about something the character no longer believed.

        The record reads are hoisted out of the loop: they are the same for every
        candidate and a per-candidate query would turn one round into a hundred.
        """
        situation = " ".join(
            str(item.get("content") or "")
            for item in self.projections.situation.list_active(limit=20)
        )
        unfinished = self.projections.unfinished.list_all(limit=200)
        boundaries = self.projections.boundaries.list_all(include_revoked=True)
        emotions = self.projections.emotion.list_active()
        recent_events = self.events.read(EventQuery(limit=40, newest_first=True))
        retired: list[str] = []
        for candidate in self.projections.candidates.list_active(limit=100):
            matched = candidate_module.invalidated_by_situation(
                candidate, situation_text=situation, user_message=user_message
            )
            if matched is None:
                matched = candidate_module.invalidated_by_source_state(
                    candidate,
                    unfinished=unfinished,
                    # Only the memories this candidate actually cites: passing the whole
                    # table would make the answer depend on rows the candidate never
                    # mentioned.
                    memories=self.projections.memory.get_memories(
                        [
                            source.split(":", 1)[1]
                            for source in candidate.sources
                            if source.startswith("memory:")
                        ]
                    ),
                    boundaries=boundaries,
                    emotions=emotions,
                    recent_events=recent_events,
                    now=now,
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
            # Subjects an unfinished matter already owns - live ones and ones that
            # settled recently enough to still hold their subject. A memory about one
            # of them must not become a second candidate about it.
            spoken_for=[
                matter.title
                for matter in unfinished_module.subject_guards(
                    self.projections.unfinished.list_all(limit=200), now=now
                )
            ],
            # The shapes that need more than the pool and the matters: ``share`` reads
            # what the character holds *about* the user (an activated preference or
            # relational memory) and can be coloured by a live emotion, ``repair`` reads
            # the evidence that something landed badly (a negative interaction
            # observation, or a boundary the user had to declare), and ``reply`` reads a
            # question the user asked that nothing has answered. ``emotion:`` and
            # ``situation:`` ride along as extra sources on those. Before this call
            # carried them, all three shapes were reachable only in tests - the rule
            # producers existed and production never handed them the state they read.
            observations=self.projections.user_model.list_observations(limit=20),
            emotions=active,
            situations=self.projections.situation.list_active(limit=20),
            boundaries=self.projections.boundaries.active(now),
            recent_events=self.events.read(EventQuery(limit=40, newest_first=True)),
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
