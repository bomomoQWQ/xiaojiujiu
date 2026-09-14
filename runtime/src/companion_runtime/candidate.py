"""Candidate intent generation and pool management.

The candidate generator answers "given all this, what might I want to do now?".
It is *not* retrieval (that answers "what do I remember") and it is *not* the
motivational layer (that answers "will I actually do it").

The strong semantic API never writes to the pool directly. It submits
``ADD`` / ``UPDATE`` / ``RETIRE`` / ``REINTERPRET`` operations, and the pool
manager applies them through the reducer.

One candidate is permanent and structural: the pure desire to reach out with no
specific subject, whose internal value rises with impulse and pressure and falls
with restraint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .config import RuntimeConfig
from .typing import (
    ActivatedMemory,
    CandidateIntent,
    CandidateOp,
    CandidateStatus,
    Memory,
    RuntimeState,
    UnfinishedMatter,
    UnfinishedStatus,
    new_id,
)
from .utility import clamp, sigmoid, utcnow

LOGGER = logging.getLogger("companion_runtime.candidate")

#: Identifier reserved for the permanent "just want to reach out" candidate.
CONTACT_CANDIDATE_TYPES: tuple[str, ...] = ("contact", "check_in")

CONTACT_SOURCE = "internal_approach_drive"
UNFINISHED_SOURCE_PREFIX = "unfinished:"
MEMORY_SOURCE_PREFIX = "memory:"
EMOTION_SOURCE_PREFIX = "emotion:"
SITUATION_SOURCE_PREFIX = "situation:"


@dataclass(slots=True)
class CandidateOperation:
    """A proposed mutation of the candidate pool."""

    op: str
    candidate_id: str | None = None
    candidate: dict[str, Any] | None = None
    patch: dict[str, Any] | None = None
    reason: str | None = None
    interpretation: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "op": self.op,
            "candidate_id": self.candidate_id,
            "candidate": self.candidate,
            "patch": self.patch,
            "reason": self.reason,
            "interpretation": self.interpretation,
            "sources": list(self.sources),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CandidateOperation:
        """Build an operation from a raw mapping (e.g. model output).

        Raises:
            ValueError: If the operation kind is unknown.
        """
        raw_op = str(data.get("op") or "").lower()
        if raw_op not in {op.value for op in CandidateOp}:
            raise ValueError(f"unknown candidate operation: {raw_op!r}")
        return cls(
            op=raw_op,
            candidate_id=data.get("candidate_id"),
            candidate=data.get("candidate"),
            patch=data.get("patch"),
            reason=data.get("reason"),
            interpretation=data.get("interpretation"),
            sources=list(data.get("sources") or []),
        )


# --------------------------------------------------------------------------------------
# Rule-based generation (works with zero models)
# --------------------------------------------------------------------------------------


def contact_candidate_value(state: RuntimeState, config: RuntimeConfig) -> float:
    """Return the internal value of the pure "just want to reach out" candidate.

    ``V_contact = sigmoid(aI + bP - cR)`` with a neutral prior, so a calm,
    well-restrained character assigns it almost no value while pent-up impulse and
    pressure make it climb naturally.

    Args:
        state: Runtime state.
        config: Runtime configuration.

    Returns:
        A value in ``[0, 1]``.
    """
    settings = config.candidate
    logit_value = (
        settings.contact_baseline_prior
        + settings.contact_bias_impulse * state.approach_impulse
        + settings.contact_bias_pressure * state.pressure
        - settings.contact_bias_restraint * state.restraint
    )
    return clamp(sigmoid(logit_value))


def _unfinished_candidate(
    matter: UnfinishedMatter, *, now: datetime, config: RuntimeConfig
) -> CandidateIntent:
    """Build the follow-up candidate for one unfinished matter."""
    due = matter.status == UnfinishedStatus.DUE.value
    confidence = clamp(0.35 + 0.5 * matter.priority + (0.15 if due else 0.0))
    return CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=f"询问{matter.title}",
        goal="了解后续进展并表达关心",
        target=matter.title,
        sources=[f"{UNFINISHED_SOURCE_PREFIX}{matter.unfinished_id}"],
        constraints=["避免造成催促感", "不要连续追问"],
        preconditions=[],
        invalidate_when=["已经得知后续结果"],
        confidence=confidence,
        status=CandidateStatus.NEW.value,
        internal_need=clamp(0.45 + 0.4 * matter.priority),
        unfinished_relevance=clamp(matter.priority),
        emotion_relevance=0.0,
        created_at=now,
        updated_at=now,
        expires_at=matter.expire_at or now + timedelta(seconds=config.candidate.default_ttl_seconds),
        proposed_by="rule",
    )


def _memory_candidate(
    activation: ActivatedMemory, memory: Memory, *, now: datetime, config: RuntimeConfig
) -> CandidateIntent:
    """Build a curiosity candidate from an activated memory."""
    topics = ", ".join(memory.topics[:3])
    return CandidateIntent(
        candidate_id=new_id("candidate"),
        type="curious_question",
        intent=f"聊起之前记过的事：{memory.summary[:40]}",
        goal="延续共同经历，保持联系的连续性",
        target=topics or memory.kind,
        sources=[f"{MEMORY_SOURCE_PREFIX}{memory.memory_id}"],
        constraints=["不要重复追问同一件事"],
        preconditions=[],
        invalidate_when=["用户表示不想聊这个话题"],
        confidence=clamp(0.3 + 0.5 * memory.importance),
        status=CandidateStatus.NEW.value,
        internal_need=clamp(0.3 + 0.5 * activation.activation),
        unfinished_relevance=0.0,
        emotion_relevance=clamp(activation.activation),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=config.candidate.default_ttl_seconds),
        proposed_by="rule",
    )


def generate(
    *,
    state: RuntimeState,
    config: RuntimeConfig,
    unfinished: Sequence[UnfinishedMatter] = (),
    activated: Sequence[tuple[ActivatedMemory, Memory]] = (),
    existing: Sequence[CandidateIntent] = (),
    now: datetime | None = None,
    emotion_intensity: float = 0.0,
) -> list[CandidateIntent]:
    """Generate candidate intents with rules only (degradation Level 0).

    Args:
        state: Runtime state.
        config: Runtime configuration.
        unfinished: Live unfinished matters.
        activated: Activation pool joined with memory content.
        existing: Currently live candidates, used to avoid duplicates.
        now: Reference time.
        emotion_intensity: Peak active emotion intensity.

    Returns:
        Newly generated candidates (not yet filtered by the pool manager).
    """
    stamp = now or utcnow()
    produced: list[CandidateIntent] = []
    existing_types = {candidate.type for candidate in existing}
    existing_targets = {candidate.target for candidate in existing}

    for matter in unfinished:
        if matter.status not in {
            UnfinishedStatus.OPEN.value,
            UnfinishedStatus.WAITING.value,
            UnfinishedStatus.DUE.value,
        }:
            continue
        candidate = _unfinished_candidate(matter, now=stamp, config=config)
        if candidate.target in existing_targets:
            continue
        produced.append(candidate)
        existing_targets.add(candidate.target)

    for activation, memory in activated[:4]:
        candidate = _memory_candidate(activation, memory, now=stamp, config=config)
        if candidate.target in existing_targets:
            continue
        if candidate.confidence < config.candidate.confidence_floor:
            continue
        produced.append(candidate)
        existing_targets.add(candidate.target)

    # The permanent "I just want to be in contact" candidate is always available;
    # only its internal value moves.
    if "contact" not in existing_types:
        contact_value = contact_candidate_value(state, config)
        produced.append(
            CandidateIntent(
                candidate_id=new_id("candidate"),
                type="contact",
                intent="没有具体事项，只是想和用户建立联系",
                goal="维持关系的连续性",
                target="relationship",
                sources=[CONTACT_SOURCE],
                constraints=["保持短、轻，容易忽略", "不假定关系状态"],
                preconditions=[],
                invalidate_when=[],
                confidence=clamp(0.35 + 0.5 * contact_value),
                status=CandidateStatus.NEW.value,
                internal_need=clamp(0.25 + 0.7 * contact_value),
                unfinished_relevance=0.0,
                emotion_relevance=clamp(emotion_intensity),
                created_at=stamp,
                updated_at=stamp,
                expires_at=stamp + timedelta(seconds=config.candidate.default_ttl_seconds),
                proposed_by="rule",
            )
        )

    return produced


# --------------------------------------------------------------------------------------
# Pool manager
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class PoolChange:
    """One applied pool mutation, for logging and API responses."""

    op: str
    candidate_id: str | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {"op": self.op, "candidate_id": self.candidate_id, "detail": self.detail}


def _candidate_from_mapping(data: Mapping[str, Any], *, now: datetime, config: RuntimeConfig) -> CandidateIntent:
    """Build a candidate from a model-provided mapping.

    Unknown fields are ignored; missing fields fall back to conservative
    defaults. ``sources`` is mandatory in spirit: a candidate that came from
    nowhere is marked as such by the caller.
    """
    return CandidateIntent(
        candidate_id=str(data.get("candidate_id") or new_id("candidate")),
        type=str(data.get("type") or "contact"),
        intent=str(data.get("intent") or "").strip() or "保持联系",
        goal=str(data.get("goal") or ""),
        target=str(data.get("target") or ""),
        sources=list(data.get("sources") or []),
        constraints=list(data.get("constraints") or []),
        preconditions=list(data.get("preconditions") or []),
        invalidate_when=list(data.get("invalidate_when") or []),
        confidence=clamp(float(data.get("confidence", 0.5))),
        status=CandidateStatus.NEW.value,
        internal_need=clamp(float(data.get("internal_need", 0.5))),
        unfinished_relevance=clamp(float(data.get("unfinished_relevance", 0.0))),
        emotion_relevance=clamp(float(data.get("emotion_relevance", 0.0))),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=config.candidate.default_ttl_seconds),
        proposed_by=str(data.get("proposed_by") or "semantic_api"),
    )


def plan_operations(
    *,
    proposals: Sequence[CandidateIntent],
    existing: Sequence[CandidateIntent],
    config: RuntimeConfig,
) -> list[CandidateOperation]:
    """Turn freshly generated candidates into ADD operations.

    Candidates that duplicate a live one (same type and target) become UPDATE
    operations instead, so the pool does not fill with near-identical thoughts.

    Args:
        proposals: Newly generated candidates.
        existing: Live candidates.
        config: Runtime configuration.

    Returns:
        A list of :class:`CandidateOperation`.
    """
    live = {(candidate.type, candidate.target): candidate for candidate in existing}
    operations: list[CandidateOperation] = []
    additions = 0
    for proposal in proposals:
        key = (proposal.type, proposal.target)
        current = live.get(key)
        if current is not None:
            operations.append(
                CandidateOperation(
                    op=CandidateOp.UPDATE.value,
                    candidate_id=current.candidate_id,
                    patch={
                        "confidence": max(current.confidence, proposal.confidence),
                        "internal_need": max(current.internal_need, proposal.internal_need),
                        "unfinished_relevance": max(
                            current.unfinished_relevance, proposal.unfinished_relevance
                        ),
                        "emotion_relevance": max(current.emotion_relevance, proposal.emotion_relevance),
                        "sources": sorted(set(current.sources) | set(proposal.sources)),
                    },
                    reason="refresh from generator",
                )
            )
            continue
        if additions >= config.candidate.max_active:
            break
        operations.append(
            CandidateOperation(op=CandidateOp.ADD.value, candidate=proposal.to_dict())
        )
        additions += 1
    return operations


def validate_candidate(candidate: CandidateIntent) -> str | None:
    """Return a rejection reason for a structurally invalid candidate, else None.

    A candidate without sources would be a thought that came from nowhere, which
    the architecture forbids.
    """
    if not candidate.intent.strip():
        return "empty_intent"
    if not candidate.sources:
        return "missing_sources"
    if candidate.type not in {
        "contact",
        "check_in",
        "follow_up",
        "curious_question",
        "share",
        "repair",
        "reply",
    }:
        return f"unknown_type:{candidate.type}"
    return None


@dataclass(slots=True)
class PoolApplyResult:
    """Outcome of applying a batch of pool operations."""

    changes: list[PoolChange] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "changes": [change.to_dict() for change in self.changes],
            "rejected": list(self.rejected),
        }


def is_candidate_proactive(candidate: CandidateIntent) -> bool:
    """Return whether a candidate represents an unprompted contact.

    Proactive candidates are the ones a hard boundary can block outright; a reply
    to a user message is governed by ``allow_reply`` instead.
    """
    return candidate.type in {"contact", "check_in", "follow_up", "curious_question", "share"}


def should_refresh(    *,
    existing: Sequence[CandidateIntent],
    last_refresh_at: datetime | None,
    now: datetime,
    config: RuntimeConfig,
) -> bool:
    """Return whether the pool should be refreshed now.

    Refreshing is expensive-ish (it may call the strong API), so it happens only
    when the pool is empty/dormant or enough time has passed.

    Args:
        existing: Live candidates.
        last_refresh_at: Time of the last refresh.
        now: Reference time.
        config: Runtime configuration.

    Returns:
        ``True`` when a refresh is due.
    """
    usable = [
        candidate
        for candidate in existing
        if candidate.status in {CandidateStatus.NEW.value, CandidateStatus.ACTIVE.value}
    ]
    if not usable:
        if last_refresh_at is None:
            return True
        return (now - last_refresh_at).total_seconds() >= config.candidate.empty_pool_refresh_seconds
    if last_refresh_at is None:
        return True
    return (now - last_refresh_at).total_seconds() >= config.candidate.refresh_min_seconds


def invalidated_by_situation(
    candidate: CandidateIntent, *, situation_text: str, user_message: str
) -> str | None:
    """Return the matching ``invalidate_when`` condition, if any.

    Args:
        candidate: Candidate to test.
        situation_text: Current working-situation text.
        user_message: Latest user message.

    Returns:
        The invalidation condition that matched, or ``None``.
    """
    haystack = f"{situation_text} {user_message}".lower()
    for condition in candidate.invalidate_when:
        needle = condition.lower()
        # Conditions are natural-language; a coarse keyword overlap is enough to
        # catch the common cases ("已经得知后续结果" after "面试过啦").
        keywords = [token for token in _condition_keywords(needle) if token]
        if keywords and all(keyword in haystack for keyword in keywords):
            return condition
    return None


def _condition_keywords(condition: str) -> list[str]:
    """Extract a few decisive keywords from a natural-language invalidation rule."""
    table = {
        "已经得知后续结果": ["面试", "结果"],
        "已经得知面试结果": ["面试", "结果"],
        "用户表示不想聊这个话题": ["不想聊", "别聊"],
        "用户表示不想被打扰": ["别打扰", "不要打扰"],
    }
    for key, keywords in table.items():
        if key in condition:
            return keywords
    return []


def describe_pool(candidates: Sequence[CandidateIntent]) -> list[dict[str, Any]]:
    """Return a compact rendering of a candidate pool for prompts and APIs."""
    return [
        {
            "candidate_id": candidate.candidate_id,
            "type": candidate.type,
            "intent": candidate.intent,
            "goal": candidate.goal,
            "status": candidate.status,
            "confidence": round(candidate.confidence, 3),
        }
        for candidate in candidates
    ]


def expires_in_seconds(candidate: CandidateIntent, now: datetime) -> float | None:
    """Return seconds until a candidate expires, or ``None`` when unbounded."""
    if candidate.expires_at is None:
        return None
    return (candidate.expires_at - now).total_seconds()
