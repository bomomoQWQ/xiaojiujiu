"""Canonical vocabulary for the Runtime: types, states and inter-module records.

Everything that other modules exchange lives here so that the state machines and
the protocol layer share one vocabulary. All identifiers are plain strings so the
records survive a round trip through JSON and SQLite without adapters.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------

ID_PREFIXES: Mapping[str, str] = {
    "event": "evt",
    "emotion": "emo",
    "boundary": "bnd",
    "unfinished": "unf",
    "memory_candidate": "mcd",
    "memory": "mem",
    "observation": "obs",
    "candidate": "cnd",
    "attempt": "att",
    "outbox": "obx",
    "task": "tsk",
    "interpretation": "itp",
}


def new_id(kind: str) -> str:
    """Create a short, sortable-ish identifier for a Runtime record.

    Args:
        kind: Logical record kind, a key of :data:`ID_PREFIXES`.

    Returns:
        An identifier such as ``evt_9f1c2ab34d5e``.
    """
    prefix = ID_PREFIXES.get(kind, kind[:3])
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def is_event_identifier(identifier: str) -> bool:
    """Return whether ``identifier`` names a raw event.

    Runtime records all share one identifier shape (``<prefix>_<hex>``) but are not
    interchangeable: a grounding identifier may name a memory, a memory candidate or
    a candidate intent as well as an event. Anywhere a *raw event* is required - the
    protocol's "is this evidence still there" check, for instance - the identifier
    has to be recognised as an event first, otherwise a perfectly grounded
    suggestion is discarded as if its evidence had vanished.

    Args:
        identifier: A generated identifier.

    Returns:
        ``True`` when the identifier carries the event prefix.
    """
    return isinstance(identifier, str) and identifier.split("_", 1)[0] == ID_PREFIXES["event"]


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class EventType(str, Enum):
    """Kinds of append-only raw events."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    BOUNDARY_DECLARED = "boundary_declared"
    BOUNDARY_REVOKED = "boundary_revoked"
    REAPPRAISAL = "reappraisal"
    EMOTION_EVENT_EVAL = "emotion_event_eval"
    INTERACTION_OBSERVATION = "interaction_observation"
    MEMORY_CONSOLIDATED = "memory_consolidated"
    CANDIDATE_PROPOSAL = "candidate_proposal"
    ACTION_ATTEMPT = "action_attempt"
    PROACTIVE_COMMITTED = "proactive_committed"
    PROACTIVE_SENT = "proactive_sent"
    PROACTIVE_ABORTED = "proactive_aborted"
    USER_MODEL_SUMMARY = "user_model_summary"
    TICK = "tick"
    SYSTEM = "system"


class Actor(str, Enum):
    """Who caused an event."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    RUNTIME = "runtime"
    BACKGROUND_MODEL = "background_model"


class EmotionDirection(str, Enum):
    """Sign of an emotional impact."""

    POSITIVE = "+"
    NEGATIVE = "-"
    NEUTRAL = "0"


class BoundaryType(str, Enum):
    """Why a boundary exists."""

    TEMPORAL = "temporal"
    TOPIC = "topic"
    PERMANENT = "permanent"
    CONDITIONAL = "conditional"


class UnfinishedStatus(str, Enum):
    """Lifecycle of an unfinished matter."""

    OPEN = "open"
    WAITING = "waiting"
    DUE = "due"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    MUTED = "muted"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class CandidateStatus(str, Enum):
    """Lifecycle of a candidate intent.

    ``committed`` is deliberately absent: committing creates an action attempt
    instead of mutating the candidate lifecycle.
    """

    NEW = "new"
    ACTIVE = "active"
    DORMANT = "dormant"
    RESOLVED = "resolved"
    RETIRED = "retired"
    EXPIRED = "expired"


class AttemptState(str, Enum):
    """Lifecycle of an action attempt."""

    PROPOSED = "proposed"
    COMMITTED = "committed"
    RENDERING = "rendering"
    READY_TO_SEND = "ready_to_send"
    SENT = "sent"
    RESOLVED = "resolved"
    ABORTED = "aborted"
    EXPIRED = "expired"
    FAILED = "failed"


class MemoryKind(str, Enum):
    """Long-term memory categories."""

    EPISODIC = "episodic"
    STABLE_KNOWLEDGE = "stable_knowledge"
    USER_PREFERENCE = "user_preference"
    RELATIONSHIP = "relationship"


class MemoryStatus(str, Enum):
    """Long-term memory retention state (forgetting is archival, not deletion)."""

    ACTIVE = "active"
    LOW_ACTIVATION = "low_activation"
    ARCHIVED = "archived"


class CandidateOp(str, Enum):
    """Pool mutation operations a strong API is allowed to propose."""

    ADD = "add"
    UPDATE = "update"
    RETIRE = "retire"
    REINTERPRET = "reinterpret"


class OutboxStatus(str, Enum):
    """Delivery queue states."""

    PENDING = "pending"
    LEASED = "leased"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxKind(str, Enum):
    """What an outbox row asks the host to do."""

    RENDER = "render"
    SEND = "send"


class TaskKind(str, Enum):
    """Background task categories, used to pick a protocol policy."""

    SHALLOW_TAG = "shallow_tag"
    EMOTION_EVAL = "emotion_eval"
    EMOTION_EXPLAIN = "emotion_explain"
    CANDIDATE_GEN = "candidate_gen"
    MEMORY_SUMMARY = "memory_summary"
    USER_MODEL_SUMMARY = "user_model_summary"
    PROACTIVE_DRAFT = "proactive_draft"
    #: Low-frequency deep cognition refresh (patch v0.2 sections 18-21). It carries
    #: a grounded bundle of suggestions rather than a single reading, and it is the
    #: only place where "I only understood this later" is expressed.
    DEEP_REFRESH = "deep_refresh"


class ProtocolAction(str, Enum):
    """Outcome of the protocol classifier."""

    APPLY = "apply"
    REBASE = "rebase"
    DISCARD = "discard"


class ReconcileAction(str, Enum):
    """Concurrency re-coordination outcome for an in-flight action attempt."""

    KEEP = "keep"
    MERGE = "merge"
    RERENDER = "rerender"
    RESOLVED = "resolved"
    ABORT = "abort"


class Priority(str, Enum):
    """Scheduler priority classes."""

    P0_FOREGROUND = "p0_foreground"
    P1_NEAR_REALTIME = "p1_near_realtime"
    P2_ENDOGENOUS = "p2_endogenous"
    P3_MAINTENANCE = "p3_maintenance"


# --------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RawEvent:
    """An immutable entry in the append-only history."""

    event_id: str
    event_type: str
    timestamp: datetime
    actor: str
    conversation_id: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_event_ids: list[str] = field(default_factory=list)
    runtime_version: int = 0
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering of the event."""
        return dataclasses.asdict(self) | {"timestamp": self.timestamp.isoformat()}


@dataclass(slots=True)
class ValueProfile:
    """Personality compiled into dynamics parameters (the 8 value axes)."""

    autonomy: float = 0.72
    boundary_respect: float = 0.88
    emotional_expression: float = 0.46
    relationship_maintenance: float = 0.79
    user_care: float = 0.85
    conflict_directness: float = 0.41
    stability_commitment: float = 0.81
    curiosity: float = 0.76

    def to_dict(self) -> dict[str, float]:
        """Return the axes as a plain mapping."""
        return dataclasses.asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ValueProfile:
        """Build a profile from a partial mapping, keeping defaults for gaps."""
        if not data:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: float(v) for k, v in data.items() if k in known})


@dataclass(slots=True)
class RuntimeState:
    """The current projection: everything the reducer mutates."""

    version: int = 0
    updated_at: datetime | None = None
    #: When this Runtime came into existence. Used as the anchor for "how long
    #: has nothing happened" before the first exchange ever occurs.
    epoch_at: datetime | None = None
    last_tick_at: datetime | None = None
    mood_valence: float = 0.0
    mood_arousal: float = 0.0
    mood_stability: float = 0.70
    approach_impulse: float = 0.05
    restraint: float = 0.50
    pressure: float = 0.0
    cooldown_until: datetime | None = None
    contact_count_today: int = 0
    last_contact_at: datetime | None = None
    last_user_message_at: datetime | None = None
    #: Last time anything was exchanged in either direction. Only real events
    #: write this; ticks never do, so it is a stable anchor for the absence term.
    last_exchange_at: datetime | None = None
    allow_proactive: bool = True
    foreground_pause_until: datetime | None = None
    values: ValueProfile = field(default_factory=ValueProfile)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot suitable for the HTTP API."""
        return {
            "version": self.version,
            "updated_at": _iso(self.updated_at),
            "epoch_at": _iso(self.epoch_at),
            "last_tick_at": _iso(self.last_tick_at),
            "mood": {
                "valence": round(self.mood_valence, 6),
                "arousal": round(self.mood_arousal, 6),
                "stability": round(self.mood_stability, 6),
            },
            "drive": {
                "approach_impulse": round(self.approach_impulse, 6),
                "restraint": round(self.restraint, 6),
                "pressure": round(self.pressure, 6),
            },
            "cooldown_until": _iso(self.cooldown_until),
            "contact_count_today": self.contact_count_today,
            "last_contact_at": _iso(self.last_contact_at),
            "last_user_message_at": _iso(self.last_user_message_at),
            "allow_proactive": self.allow_proactive,
            "foreground_pause_until": _iso(self.foreground_pause_until),
            "values": self.values.to_dict(),
            "meta": dict(self.meta),
        }


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601 or ``None``."""
    return value.isoformat() if value is not None else None


@dataclass(slots=True)
class EmotionEvent:
    """A local emotional impact attached to a source event."""

    emotion_event_id: str
    source_event_id: str
    direction: str
    intensity: float
    activation: float
    target: str = "user"
    semantic_label: str | None = None
    created_at: datetime | None = None
    decay_rate: float = 0.08

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "emotion_event_id": self.emotion_event_id,
            "source_event_id": self.source_event_id,
            "direction": self.direction,
            "intensity": round(self.intensity, 6),
            "activation": round(self.activation, 6),
            "target": self.target,
            "semantic_label": self.semantic_label,
            "created_at": _iso(self.created_at),
            "decay_rate": self.decay_rate,
            "signed_intensity": round(self.signed_intensity, 6),
        }

    @property
    def signed_intensity(self) -> float:
        """Return intensity oriented by direction (``-`` is negative)."""
        if self.direction == EmotionDirection.NEGATIVE.value:
            return -self.intensity
        if self.direction == EmotionDirection.NEUTRAL.value:
            return 0.0
        return self.intensity


@dataclass(slots=True)
class EmotionEvaluation:
    """Structured result of the event appraisal step (no final emotion values)."""

    direction: str = EmotionDirection.NEUTRAL.value
    impact: float = 0.0
    activation: float = 0.0
    uncertainty: float = 0.5
    relation_signal: str = "neutral"
    responsibility: str = "unclear"
    confidence: float = 0.5
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return dataclasses.asdict(self)


@dataclass(slots=True)
class Boundary:
    """An explicit user boundary; a hard constraint that outranks motivation."""

    boundary_id: str
    type: str
    scope: str = "all_topics"
    allow_reply: bool = True
    allow_proactive: bool = False
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    revocable_by: str = "explicit_user_revoke"
    source_event_id: str | None = None
    revoked_at: datetime | None = None
    note: str | None = None
    #: What a *topic*-scoped boundary is about, bound when it is declared.
    #:
    #: The language rules are deictic - "暂时不要跟我说这个" names nothing - so a scope
    #: like ``topic_avoid`` is a category until it is tied to a referent. Without this
    #: field there was nothing to compare a candidate against, which is why topic
    #: boundaries could only ever be enforced at delivery time. ``None`` means the
    #: referent could not be established; callers must treat that as "do not guess".
    subject: str | None = None

    def is_active(self, now: datetime) -> bool:
        """Return whether the boundary currently constrains proactive contact.

        Args:
            now: Reference timestamp.

        Returns:
            ``True`` when the boundary is in force at ``now``.
        """
        if self.revoked_at is not None:
            return False
        if self.starts_at is not None and now < self.starts_at:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "boundary_id": self.boundary_id,
            "type": self.type,
            "scope": self.scope,
            "allow_reply": self.allow_reply,
            "allow_proactive": self.allow_proactive,
            "starts_at": _iso(self.starts_at),
            "expires_at": _iso(self.expires_at),
            "revocable_by": self.revocable_by,
            "source_event_id": self.source_event_id,
            "revoked_at": _iso(self.revoked_at),
            "note": self.note,
            "subject": self.subject,
        }


@dataclass(slots=True)
class UnfinishedMatter:
    """Something that needs future attention but is not yet settled."""

    unfinished_id: str
    title: str
    source_event_ids: list[str] = field(default_factory=list)
    status: str = UnfinishedStatus.OPEN.value
    waiting_until: datetime | None = None
    priority: float = 0.5
    mute_until: datetime | None = None
    expire_at: datetime | None = None
    resolution_conditions: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    resolution_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "unfinished_id": self.unfinished_id,
            "title": self.title,
            "source_event_ids": list(self.source_event_ids),
            "status": self.status,
            "waiting_until": _iso(self.waiting_until),
            "priority": round(self.priority, 6),
            "mute_until": _iso(self.mute_until),
            "expire_at": _iso(self.expire_at),
            "resolution_conditions": list(self.resolution_conditions),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "resolution_note": self.resolution_note,
        }


@dataclass(slots=True)
class MemoryCandidate:
    """A provisional memory awaiting background consolidation."""

    candidate_id: str
    summary: str
    kind: str = MemoryKind.EPISODIC.value
    source_event_ids: list[str] = field(default_factory=list)
    value: float = 0.0
    status: str = "pending"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    consolidated_memory_id: str | None = None
    topics: list[str] = field(default_factory=list)
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "candidate_id": self.candidate_id,
            "summary": self.summary,
            "kind": self.kind,
            "source_event_ids": list(self.source_event_ids),
            "value": round(self.value, 6),
            "status": self.status,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "consolidated_memory_id": self.consolidated_memory_id,
            "topics": list(self.topics),
            "confidence": round(self.confidence, 6),
        }


@dataclass(slots=True)
class Memory:
    """A consolidated long-term memory with dual representation."""

    memory_id: str
    kind: str
    summary: str
    structured: dict[str, Any] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.5
    status: str = MemoryStatus.ACTIVE.value
    source_event_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "summary": self.summary,
            "structured": dict(self.structured),
            "topics": list(self.topics),
            "importance": round(self.importance, 6),
            "confidence": round(self.confidence, 6),
            "status": self.status,
            "source_event_ids": list(self.source_event_ids),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "archived_at": _iso(self.archived_at),
        }


@dataclass(slots=True)
class ActivatedMemory:
    """The current activation of a memory inside the working set."""

    memory_id: str
    activation: float
    last_recalled_at: datetime | None = None
    recall_count: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "memory_id": self.memory_id,
            "activation": round(self.activation, 6),
            "last_recalled_at": _iso(self.last_recalled_at),
            "recall_count": self.recall_count,
            "reason": self.reason,
        }


@dataclass(slots=True)
class InteractionObservation:
    """One complete interaction observation ``D_i = (A_i, C_i, Y_i)``."""

    observation_id: str
    action: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    outcome: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    attempt_id: str | None = None
    source_event_ids: list[str] = field(default_factory=list)
    attribution_confidence: float = 0.5
    source_weight: float = 0.5
    semantic_confidence: float = 0.5
    weight: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        payload = dataclasses.asdict(self)
        payload["created_at"] = _iso(self.created_at)
        return payload


@dataclass(slots=True)
class CandidateIntent:
    """A candidate action the runtime *might* want to take."""

    candidate_id: str
    type: str
    intent: str
    goal: str = ""
    target: str = ""
    sources: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    invalidate_when: list[str] = field(default_factory=list)
    confidence: float = 0.5
    status: str = CandidateStatus.NEW.value
    internal_need: float = 0.5
    unfinished_relevance: float = 0.0
    emotion_relevance: float = 0.0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    retired_reason: str | None = None
    proposed_by: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "candidate_id": self.candidate_id,
            "type": self.type,
            "intent": self.intent,
            "goal": self.goal,
            "target": self.target,
            "sources": list(self.sources),
            "constraints": list(self.constraints),
            "preconditions": list(self.preconditions),
            "invalidate_when": list(self.invalidate_when),
            "confidence": round(self.confidence, 6),
            "status": self.status,
            "internal_need": round(self.internal_need, 6),
            "unfinished_relevance": round(self.unfinished_relevance, 6),
            "emotion_relevance": round(self.emotion_relevance, 6),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "expires_at": _iso(self.expires_at),
            "retired_reason": self.retired_reason,
            "proposed_by": self.proposed_by,
        }


@dataclass(slots=True)
class UtilityBreakdown:
    """Decomposition of one candidate's utility, kept for auditability."""

    candidate_id: str
    internal: float = 0.0
    user: float = 0.0
    relation: float = 0.0
    boundary_cost: float = 0.0
    interrupt_cost: float = 0.0
    repeat_cost: float = 0.0
    risk_cost: float = 0.0
    total: float = 0.0
    blocked: bool = False
    block_reason: str | None = None
    reply_probability: float = 0.0
    positive_probability: float = 0.0
    continue_probability: float = 0.0
    boundary_risk: float = 0.0
    uncertainty: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            key: (round(value, 6) if isinstance(value, float) else value)
            for key, value in dataclasses.asdict(self).items()
        }


@dataclass(slots=True)
class DecisionOutcome:
    """The full result of one motivational decision round."""

    acted: bool
    reason: str
    utilities: list[UtilityBreakdown] = field(default_factory=list)
    silence_utility: float = 0.0
    advantage: float = 0.0
    hazard: float = 0.0
    delta_t: float = 0.0
    action_probability: float = 0.0
    chosen_candidate_id: str | None = None
    selected_probability: float = 0.0
    attempt_id: str | None = None
    next_wake_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "acted": self.acted,
            "reason": self.reason,
            "utilities": [u.to_dict() for u in self.utilities],
            "silence_utility": round(self.silence_utility, 6),
            "advantage": round(self.advantage, 6),
            "hazard": round(self.hazard, 6),
            "delta_t": round(self.delta_t, 3),
            "action_probability": round(self.action_probability, 6),
            "chosen_candidate_id": self.chosen_candidate_id,
            "selected_probability": round(self.selected_probability, 6),
            "attempt_id": self.attempt_id,
            "next_wake_at": _iso(self.next_wake_at),
        }


@dataclass(slots=True)
class ActionAttempt:
    """A concrete intention to act, with a full state machine."""

    attempt_id: str
    candidate_id: str | None
    state: str
    intent: str
    goal: str = ""
    based_on_version: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    committed_at: datetime | None = None
    rendered_text: str | None = None
    failure_reason: str | None = None
    reconcile_action: str | None = None
    superseded_by_event_ids: list[str] = field(default_factory=list)
    outbox_id: str | None = None

    @property
    def state_enum(self) -> AttemptState:
        """Return :attr:`state` as an :class:`AttemptState` member.

        ``state`` is deliberately stored as a plain string so it round-trips
        through SQLite and ``to_dict()`` without any enum adapter. Callers that
        want the typed member -- and ``.value`` on it -- use this accessor rather
        than depending on the storage representation.

        Raises:
            ValueError: If the stored value is not a known attempt state, which
                would mean the database was written by an incompatible version.
        """
        return AttemptState(self.state)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "attempt_id": self.attempt_id,
            "candidate_id": self.candidate_id,
            "state": self.state,
            # The same value under an explicitly typed name, for clients that would
            # rather not assume the storage representation is a bare string.
            "state_value": self.state_enum.value,
            "intent": self.intent,
            "goal": self.goal,
            "based_on_version": self.based_on_version,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "committed_at": _iso(self.committed_at),
            "rendered_text": self.rendered_text,
            "failure_reason": self.failure_reason,
            "reconcile_action": self.reconcile_action,
            "superseded_by_event_ids": list(self.superseded_by_event_ids),
            "outbox_id": self.outbox_id,
        }


@dataclass(slots=True)
class OutboxItem:
    """A delivery-queue row handed to the host framework asynchronously."""

    outbox_id: str
    kind: str
    payload: dict[str, Any]
    status: str = OutboxStatus.PENDING.value
    priority: int = 100
    available_at: datetime | None = None
    created_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 3
    acked_at: datetime | None = None
    last_error: str | None = None
    conversation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "outbox_id": self.outbox_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "status": self.status,
            "priority": self.priority,
            "available_at": _iso(self.available_at),
            "created_at": _iso(self.created_at),
            "lease_owner": self.lease_owner,
            "lease_expires_at": _iso(self.lease_expires_at),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "acked_at": _iso(self.acked_at),
            "last_error": self.last_error,
            "conversation_id": self.conversation_id,
        }


@dataclass(slots=True)
class AuthorizeResult:
    """Permission verdict for a prospective or rendered action."""

    allowed: bool
    reason: str
    constraints: list[str] = field(default_factory=list)
    blocking_boundary_ids: list[str] = field(default_factory=list)
    allow_reply: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return dataclasses.asdict(self)


@dataclass(slots=True)
class BackgroundTaskRecord:
    """A snapshot of an in-flight background task used for protocol checks."""

    task_id: str
    task_type: str
    based_on_version: int
    source_event_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    priority: str = Priority.P1_NEAR_REALTIME.value

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        payload = dataclasses.asdict(self)
        payload["created_at"] = _iso(self.created_at)
        return payload
