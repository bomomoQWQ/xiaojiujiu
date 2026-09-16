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

Rule path (design §37-§43, patch v0.2 §12) - what :func:`generate` produces with
no model at all, and from which stored state:

``follow_up``
    one live ``UnfinishedMatter`` -> ``unfinished:<id>``.
``curious_question``
    one activated memory that is not a preference or a relationship memory ->
    ``memory:<id>``.
``share``
    one activated preference/relationship memory - something the character holds
    *about the user* that it can offer - -> ``memory:<id>``, plus
    ``emotion:<id>`` when an active emotion is strong enough to colour it.
``repair``
    one concrete negative reaction to the character's own outreach: an
    interaction observation with ``explicit_negative``/``boundary_touched``, or a
    live topic/permanent boundary the user declared -> the raw event ids behind
    that evidence, plus ``situation:<id>`` and ``emotion:<id>`` when those
    records exist.
``reply``
    one recent user message that asks something and has no later answer from the
    character -> the question's own event id, plus ``situation:<id>`` for the
    working-situation row that recorded it.
``contact``
    always, from ``internal_approach_drive``.

What it deliberately does not do: it never invents content. Every intent is
built from a stored summary, a matter title or the user's own words, and every
candidate carries at least one source the routing layer can resolve
(:meth:`~companion_runtime.runtime.Runtime._event_ids_behind` understands
``unfinished:``, ``memory:`` and raw event ids). ``emotion:`` and ``situation:``
are carried *in addition*, never alone: that method has no branch for them
today, so a candidate resting only on one would be routed to whichever chat
spoke last.

Pacing: each of the three new shapes contributes at most one candidate per round
(:data:`MAX_SHARE_PER_ROUND`, :data:`MAX_REPAIR_PER_ROUND`,
:data:`MAX_REPLY_PER_ROUND`), on top of the existing per-matter and
per-memory caps. The daily contact budget, the post-contact cooldown and the
pool's per-``target`` deduplication all apply downstream and are unchanged here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .config import RuntimeConfig
from .memory import DEDUPE_MIN_SHARED_TOKENS, QUESTION_MARKERS, is_superseded
from .typing import (
    ActivatedMemory,
    Boundary,
    CandidateIntent,
    CandidateOp,
    CandidateStatus,
    EmotionDirection,
    EmotionEvent,
    EventType,
    Memory,
    MemoryKind,
    MemoryStatus,
    RawEvent,
    RuntimeState,
    UnfinishedMatter,
    UnfinishedStatus,
    new_id,
)
from .utility import (
    clamp,
    parse_datetime,
    sigmoid,
    summarize_text,
    tokenize,
    topic_tokens,
    utcnow,
)

LOGGER = logging.getLogger("companion_runtime.candidate")

#: Identifier reserved for the permanent "just want to reach out" candidate.
CONTACT_CANDIDATE_TYPES: tuple[str, ...] = ("contact", "check_in")

CONTACT_SOURCE = "internal_approach_drive"
UNFINISHED_SOURCE_PREFIX = "unfinished:"
MEMORY_SOURCE_PREFIX = "memory:"
EMOTION_SOURCE_PREFIX = "emotion:"
SITUATION_SOURCE_PREFIX = "situation:"

# --------------------------------------------------------------------------------------
# Rule-path tuning for the shapes that had no producer
#
# These are module constants rather than ``CandidateConfig`` fields because the
# configuration module belongs to the config owner; they are reported so they can be
# promoted to real knobs with these defaults. Bounding matters more than tuning here:
# the point of the caps is that a rich state (four activated memories, several
# negative observations) still yields a handful of candidates, not a flood.
# --------------------------------------------------------------------------------------

#: Memory kinds that describe something the character *holds about the user* and can
#: therefore offer ("I remembered you ...") rather than ask about. Episodic and
#: stable-knowledge memories stay on the ``curious_question`` path: the difference is
#: between "I have something of my own to bring" and "I want to know more".
SHARE_MEMORY_KINDS: frozenset[str] = frozenset(
    {MemoryKind.USER_PREFERENCE.value, MemoryKind.RELATIONSHIP.value}
)

#: Intensity at or above which an active emotion colours a ``share`` candidate and is
#: cited as ``emotion:<id>``. 0.35 is roughly "clearly present": below it the emotion
#: is background mood, not something the character would bring up.
SHARE_EMOTION_MIN_INTENSITY = 0.35

#: Intensity at or above which an active *negative* emotion accompanies a ``repair``
#: candidate. Lower than the share bar because the repair impulse is the whole point of
#: the negative side (patch v0.2 §12: 懊恼 / 愧疚 / 修复冲动).
REPAIR_EMOTION_MIN_INTENSITY = 0.30

#: How long a negative reaction keeps driving a repair candidate. Three days is the
#: same window the obligation detector uses to keep a settled subject reserved: after
#: it, raising the incident again is dredging, not repairing.
REPAIR_EVIDENCE_MAX_AGE_SECONDS = 3 * 24 * 3600.0

#: How long a user question may stay unanswered before the character stops treating it
#: as a queued answer. Two days: long enough to survive a night, short enough that a
#: reply candidate cannot outlive the conversation it belongs to.
REPLY_QUESTION_MAX_AGE_SECONDS = 48 * 3600.0

#: Per-round caps for the shapes that had no producer. Each is one, deliberately: a
#: share is a deliberate act rather than a broadcast, repair concerns one incident, and
#: only the newest unanswered question is worth answering.
MAX_SHARE_PER_ROUND = 1
MAX_REPAIR_PER_ROUND = 1
MAX_REPLY_PER_ROUND = 1

#: Boundary types that count as negative evidence about the character's *behaviour*.
#: A temporal boundary ("我现在很忙，晚点聊") is about the user's availability, not
#: about something the character did, so it must not produce an apology.
REPAIR_BOUNDARY_TYPES: frozenset[str] = frozenset({"topic", "permanent", "conditional"})

#: What a boundary scope means when it becomes an apology, in the operator's language.
_BOUNDARY_SCOPE_LABELS: Mapping[str, str] = {
    "topic_avoid": "提到了用户不想聊的话题",
    "repeated_interrogation": "反复追问了用户",
    "all_topics": "主动联系得太频繁",
}

#: What the character was doing when the user reacted badly, by candidate type.
_ACTION_LABELS: Mapping[str, str] = {
    "contact": "冒昧搭话",
    "check_in": "问候",
    "follow_up": "追问近况",
    "curious_question": "打听",
    "share": "自顾自地分享",
    "reply": "回复",
    "repair": "道歉",
}

# --------------------------------------------------------------------------------------
# Source-derived invalidation conditions (design §42)
#
# A candidate generated from real state has to be invalidated by *that* state, not by a
# coincidence in the user's latest sentence. The strings below are therefore derived
# from the record the candidate came from and are written in the operator's language;
# they have a fixed ``"<prefix><key>"`` shape so the pool manager can evaluate them
# against the projection directly (see :func:`invalidated_by_source_state`) while a
# human reading the pool sees a sentence.
# --------------------------------------------------------------------------------------

#: The matter was settled by the user - also the exact wording the ingest path writes
#: into the working situation on resolution, which is what lets the text-based
#: :func:`invalidated_by_situation` catch it without any new wiring.
MATTER_RESOLVED_PREFIX = "未尽之事已了结："
#: The matter lapsed without being settled (cancelled / expired / invalidated / gone).
MATTER_RELEASED_PREFIX = "未尽之事已失效："
#: The memory left the retrievable pool by being archived.
MEMORY_ARCHIVED_PREFIX = "来源记忆已归档："
#: The memory was contradicted by a newer statement and is no longer retrieved.
MEMORY_SUPERSEDED_PREFIX = "来源记忆已被取代："
#: The user has since been answered in that conversation.
QUESTION_ANSWERED_PREFIX = "该问题已被回答："
#: The user withdrew or outlived the boundary the repair was about.
BOUNDARY_RELEASED_PREFIX = "用户已撤回这条界限："
#: The emotional after-effect that carried the repair impulse has decayed away, so the
#: character is no longer upset about it (patch v0.2 §12's 修复冲动).
EMOTION_FADED_PREFIX = "这件事的情绪已经过去："

#: Every derived prefix, in one place, for parsing and for tests.
DERIVED_INVALIDATION_PREFIXES: tuple[str, ...] = (
    MATTER_RESOLVED_PREFIX,
    MATTER_RELEASED_PREFIX,
    MEMORY_ARCHIVED_PREFIX,
    MEMORY_SUPERSEDED_PREFIX,
    QUESTION_ANSWERED_PREFIX,
    BOUNDARY_RELEASED_PREFIX,
    EMOTION_FADED_PREFIX,
)

#: Filler words that carry no discriminating power inside an invalidation condition or
#: a precondition. Mirrors ``motivation._condition_tokens``'s stopword set; the two are
#: kept identical in content because a condition and a precondition are written by the
#: same generator and read by two coarse matchers.
_CONDITION_STOPWORDS: frozenset[str] = frozenset(
    {"需要", "必须", "如果", "已经", "当前", "条件", "用户", "the", "a", "is", "if", "must"}
)

#: How many distinct content tokens of an unknown (model-written) condition must appear
#: in the text before it counts as satisfied. Two is the same bar
#: :data:`~companion_runtime.memory.DEDUPE_MIN_SHARED_TOKENS` uses for "these are about
#: the same thing": one shared bigram is a coincidence, two is a subject.
GENERIC_CONDITION_MIN_TOKENS = 2


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


def matter_invalidation(matter: UnfinishedMatter) -> list[str]:
    """Return the invalidation conditions implied by one unfinished matter.

    The matter is the premise of the follow-up, so the follow-up dies with it: when the
    user finally reports the result, and when the matter lapses unsettled. Both strings
    name the matter's title, so an operator reading the pool sees which obligation the
    condition is about rather than an opaque identifier.

    Args:
        matter: The matter the candidate is grounded in.

    Returns:
        Human-readable conditions; see :func:`invalidated_by_source_state`.
    """
    return [
        f"{MATTER_RESOLVED_PREFIX}{matter.title}",
        f"{MATTER_RELEASED_PREFIX}{matter.title}",
    ]


def memory_invalidation(memory: Memory) -> list[str]:
    """Return the invalidation conditions implied by one memory.

    A candidate that offers a remembered fact is only worth acting on while that fact
    is still what the character believes: an archived memory has left the retrievable
    pool, and a superseded one was contradicted by a newer statement.

    Args:
        memory: The memory the candidate is grounded in.

    Returns:
        Human-readable conditions; see :func:`invalidated_by_source_state`.
    """
    return [
        f"{MEMORY_ARCHIVED_PREFIX}{memory.memory_id}",
        f"{MEMORY_SUPERSEDED_PREFIX}{memory.memory_id}",
    ]


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
        # The legacy condition stays first: it is what an operator (and an older
        # database) already knows, and it is still matched by its own keyword pair. The
        # derived pair is what makes the *state* able to invalidate the candidate.
        invalidate_when=["已经得知后续结果", *matter_invalidation(matter)],
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


def _speaks_for_a_taken_subject(memory: Memory, spoken_for: Sequence[str]) -> bool:
    """Return whether a memory is about a subject some matter already owns.

    The memory-shaped spelling of :func:`_subject_is_spoken_for`, which carries the
    reasoning behind the bar.

    Args:
        memory: The memory that might become a candidate.
        spoken_for: Subjects that are spoken for
            (:func:`~companion_runtime.unfinished.subject_guards`).

    Returns:
        ``True`` when the memory and one of those subjects are about the same thing.
    """
    return _subject_is_spoken_for(memory.summary, spoken_for)


def _subject_is_spoken_for(text: str, spoken_for: Sequence[str]) -> bool:
    """Return whether ``text`` is about a subject some matter already owns.

    The bar is deliberately looser than the one deduplication and contradiction use:
    two shared *characters* rather than two shared CJK bigrams. The cost of a false
    positive here is small - one candidate is not generated, and if the matter is live
    its own follow-up still asks - while the cost of a false negative is the
    user-visible one this fixes ("面试过了！谢谢你那天惦记我" shares only the bigram
    面试 with the matter title "等待面试结果", and asking again about a result the user
    just reported is exactly what must not happen).

    Both the memory path and the newer reply path ask this question, which is why it
    lives on its own rather than inside :func:`_speaks_for_a_taken_subject`.

    Args:
        text: The summary or message that might become a candidate.
        spoken_for: Subjects that are spoken for - the live matters plus the ones that
            settled recently enough to still hold their subject
            (:func:`~companion_runtime.unfinished.subject_guards`).

    Returns:
        ``True`` when ``text`` and one of those subjects are about the same thing.
    """
    if not spoken_for:
        return False
    tokens = set(tokenize(text))
    if not tokens:
        return False
    for subject in spoken_for:
        if len(tokens & set(tokenize(subject))) >= DEDUPE_MIN_SHARED_TOKENS:
            return True
    return False


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
        invalidate_when=["用户表示不想聊这个话题", *memory_invalidation(memory)],
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


# --------------------------------------------------------------------------------------
# share: something of the character's own to offer
# --------------------------------------------------------------------------------------


def _strongest_emotion(
    emotions: Sequence[EmotionEvent],
    *,
    direction: str | None = None,
    min_intensity: float,
) -> EmotionEvent | None:
    """Return the strongest active emotion matching a direction and an intensity bar.

    Args:
        emotions: Active emotion events (any order; the caller's ordering is not
            trusted, because a stored list is sorted by a projection query rather than
            by this rule).
        direction: Restrict to one direction (``+`` / ``-`` / ``0``); ``None`` accepts
            any.
        min_intensity: Intensity at or above which the emotion counts.

    Returns:
        The matching event, or ``None`` when nothing clears the bar.
    """
    candidates = [
        event
        for event in emotions
        if float(event.intensity) >= float(min_intensity)
        and (direction is None or event.direction == direction)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda event: float(event.intensity))


def _share_candidate(
    activation: ActivatedMemory,
    memory: Memory,
    *,
    emotion: EmotionEvent | None,
    now: datetime,
    config: RuntimeConfig,
) -> CandidateIntent:
    """Build a ``share`` candidate from a memory the character holds about the user.

    This is the shape the whitelist declared and nothing produced. Its content is the
    stored memory summary, not a template: the character is offering something it
    actually keeps about the user ("我记得你 ..."), which is a different act from the
    curiosity candidate that asks about the same memory. The two never both fire for one
    memory - the kind decides which shape it becomes.

    Args:
        activation: The memory's current activation in the working set.
        memory: The stored memory, of a preference or relationship kind.
        emotion: The strongest active emotion, when it is strong enough to colour the
            offer. It is cited as ``emotion:<id>`` in addition to the memory source.
        now: Reference time.
        config: Runtime configuration.

    Returns:
        The candidate, ready for the pool manager.
    """
    excerpt = (memory.summary or "").strip()[:40]
    topics = ", ".join(memory.topics[:3])
    sources = [f"{MEMORY_SOURCE_PREFIX}{memory.memory_id}"]
    if emotion is not None:
        sources.append(f"{EMOTION_SOURCE_PREFIX}{emotion.emotion_event_id}")
    return CandidateIntent(
        candidate_id=new_id("candidate"),
        type="share",
        intent=f"主动提起我记得的事：{excerpt}",
        goal="把自己记得的东西拿出来分享，而不是又一次向用户提问",
        target=topics or memory.kind,
        sources=sources,
        constraints=["不要变成考问用户是否还记得", "一次只说一件事"],
        # No precondition: the premise of a share is "this memory is in the working
        # set", and the precondition matcher reads the working *cue* text, which does
        # not carry the activation pool. A text precondition here could only block a
        # legitimate share by accident; the derived invalidation below is what watches
        # the memory itself.
        preconditions=[],
        invalidate_when=["用户表示不想聊这个话题", *memory_invalidation(memory)],
        confidence=clamp(0.3 + 0.5 * memory.importance + (0.1 if emotion is not None else 0.0)),
        status=CandidateStatus.NEW.value,
        internal_need=clamp(0.3 + 0.5 * activation.activation),
        unfinished_relevance=0.0,
        emotion_relevance=(
            clamp(emotion.intensity) if emotion is not None else clamp(activation.activation * 0.5)
        ),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=config.candidate.default_ttl_seconds),
        proposed_by="rule",
    )


# --------------------------------------------------------------------------------------
# repair: after the user reacted badly to something the character did
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RepairEvidence:
    """One concrete negative reaction the character should make amends for.

    Built from stored state only - an interaction observation or a boundary the user
    declared - so that a repair candidate can always name where it came from.
    """

    #: ``"negative_observation"`` or ``"boundary"``.
    kind: str
    #: What the character did that the user reacted to, in the operator's language.
    label: str
    #: Candidate sources: raw event ids (resolvable by the routing layer) plus
    #: ``situation:`` / ``emotion:`` records that exist for the same incident.
    sources: list[str] = field(default_factory=list)
    #: How explicit the negative reaction was, used for confidence.
    explicitness: float = 0.5
    #: The identifier the invalidation condition is keyed by (observation, boundary
    #: or event id).
    key: str = ""
    #: Timestamp of the evidence, used to prefer the newest incident.
    observed_at: datetime | None = None


def _observation_fields(record: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(action, outcome)`` from an observation in either supported shape.

    Callers pass what the projection returned (``action_json`` / ``outcome_json``
    columns) and tests pass an :class:`~companion_runtime.typing.InteractionObservation`
    dataclass. Both are real state; accepting both keeps the rule readable.

    Args:
        record: An observation dataclass or a stored mapping.

    Returns:
        The action and outcome mappings, empty when the record carries neither.
    """
    if isinstance(record, Mapping):
        action = record.get("action") or record.get("action_json") or {}
        outcome = record.get("outcome") or record.get("outcome_json") or {}
    else:
        action = getattr(record, "action", None) or {}
        outcome = getattr(record, "outcome", None) or {}
    action_view = dict(action) if isinstance(action, Mapping) else {}
    outcome_view = dict(outcome) if isinstance(outcome, Mapping) else {}
    return action_view, outcome_view


def _observation_event_ids(record: Any) -> list[str]:
    """Return the raw event ids an observation rests on."""
    if isinstance(record, Mapping):
        raw = record.get("source_event_ids") or []
    else:
        raw = getattr(record, "source_event_ids", None) or []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw if item]


def _observation_timestamp(record: Any) -> datetime | None:
    """Return an observation's timestamp, whichever shape it arrives in."""
    if isinstance(record, Mapping):
        raw = record.get("created_at")
    else:
        raw = getattr(record, "created_at", None)
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        return parse_datetime(raw)
    return None


def _situation_source_for(
    situations: Sequence[Mapping[str, Any]], event_ids: Sequence[str]
) -> str | None:
    """Return the ``situation:<id>`` of the row that recorded one of ``event_ids``.

    Only an exact match on the row's own ``source_id`` counts. Guessing by text would
    attach a source the candidate did not actually come from, which is worse than
    attaching none: sources are what the routing layer reads.

    Args:
        situations: Active working-situation rows.
        event_ids: Event identifiers the evidence rests on.

    Returns:
        ``situation:<item_id>``, or ``None`` when no row records those events.
    """
    wanted = {str(item) for item in event_ids if item}
    if not wanted:
        return None
    for item in situations:
        if str(item.get("source_kind") or "") != "event":
            continue
        if str(item.get("source_id") or "") not in wanted:
            continue
        item_id = str(item.get("item_id") or "")
        if item_id:
            return f"{SITUATION_SOURCE_PREFIX}{item_id}"
    return None


def _negative_observation_evidence(
    observations: Sequence[Any], situations: Sequence[Mapping[str, Any]], *, now: datetime
) -> RepairEvidence | None:
    """Return the newest negative reaction to the character's own outreach.

    Only observations about something the character *did* count: an observation whose
    action was proactive and whose outcome was an explicit negative reaction or a
    touched boundary. A non-reply is deliberately not negative evidence (see
    :mod:`companion_runtime.user_model`), so it cannot produce an apology.

    Args:
        observations: Interaction observations, newest first preferred.
        situations: Active working-situation rows, for the ``situation:`` source.
        now: Reference time.

    Returns:
        The evidence, or ``None`` when the character has nothing to make amends for.
    """
    best: RepairEvidence | None = None
    for record in observations:
        action, outcome = _observation_fields(record)
        if not action.get("proactive"):
            continue
        explicit_negative = bool(outcome.get("explicit_negative"))
        boundary_touched = bool(outcome.get("boundary_touched"))
        if not (explicit_negative or boundary_touched):
            continue
        observed_at = _observation_timestamp(record)
        if (
            observed_at is not None
            and (now - observed_at).total_seconds() > REPAIR_EVIDENCE_MAX_AGE_SECONDS
        ):
            continue
        event_ids = _observation_event_ids(record)
        if not event_ids:
            # Without an event behind it there is nothing the routing layer could
            # resolve, and a candidate that cannot be routed is worse than none.
            continue
        label = _ACTION_LABELS.get(str(action.get("type") or ""), "主动联系")
        reaction = "用户当时明确表示了反感" if explicit_negative else "用户当时明确划了界限"
        sources = list(event_ids)
        situation_source = _situation_source_for(situations, event_ids)
        if situation_source:
            sources.append(situation_source)
        if isinstance(record, Mapping):
            observation_id = str(record.get("observation_id") or "")
        else:
            observation_id = str(getattr(record, "observation_id", "") or "")
        evidence = RepairEvidence(
            kind="negative_observation",
            label=f"{label}（{reaction}）",
            sources=sources,
            explicitness=0.8 if explicit_negative else 0.65,
            key=observation_id,
            observed_at=observed_at,
        )
        if best is None or _is_newer(evidence.observed_at, best.observed_at):
            best = evidence
    return best


def _boundary_evidence(boundaries: Sequence[Boundary], *, now: datetime) -> RepairEvidence | None:
    """Return the newest live boundary that is a complaint about the character.

    A temporal boundary ("我现在很忙") is about the user's availability, so it is not
    evidence that the character did anything wrong; only topic, permanent and
    conditional boundaries are read as negative evidence (see
    :data:`REPAIR_BOUNDARY_TYPES`).

    Args:
        boundaries: Boundaries in force.
        now: Reference time.

    Returns:
        The evidence, or ``None``.
    """
    best: RepairEvidence | None = None
    for boundary in boundaries:
        if boundary.type not in REPAIR_BOUNDARY_TYPES:
            continue
        if not boundary.is_active(now):
            continue
        source = str(boundary.source_event_id or "")
        if not source:
            continue
        label = _BOUNDARY_SCOPE_LABELS.get(boundary.scope, boundary.note or boundary.scope)
        evidence = RepairEvidence(
            kind="boundary",
            label=f"越界（{label}）",
            sources=[source],
            explicitness=0.7,
            key=boundary.boundary_id,
            observed_at=boundary.starts_at,
        )
        if best is None or _is_newer(evidence.observed_at, best.observed_at):
            best = evidence
    return best


def _is_newer(left: datetime | None, right: datetime | None) -> bool:
    """Return whether ``left`` is strictly newer than ``right`` (``None`` loses)."""
    if left is None:
        return False
    if right is None:
        return True
    try:
        return left > right
    except TypeError:
        # A naive stamp cannot be compared with an aware one; treat the known one as
        # newer rather than letting the comparison raise inside generation.
        return False


def _repair_candidate(
    evidence: RepairEvidence,
    *,
    emotion: EmotionEvent | None,
    now: datetime,
    config: RuntimeConfig,
) -> CandidateIntent:
    """Build a ``repair`` candidate from one piece of negative evidence.

    The candidate apologises for what the evidence says actually happened - the
    character's own action type plus the user's explicit reaction, or the boundary the
    user declared - so the content is a rendering of stored state rather than an
    invented grievance.

    Args:
        evidence: The negative reaction being repaired.
        emotion: The strongest active negative emotion, cited as ``emotion:<id>`` when
            present. It is what the repair impulse feels like from the inside (patch
            v0.2 §12), not the evidence itself.
        now: Reference time.
        config: Runtime configuration.

    Returns:
        The candidate, ready for the pool manager.
    """
    sources = list(evidence.sources)
    if emotion is not None:
        sources.append(f"{EMOTION_SOURCE_PREFIX}{emotion.emotion_event_id}")
    observed_at = evidence.observed_at or now
    expired = observed_at + timedelta(seconds=REPAIR_EVIDENCE_MAX_AGE_SECONDS)
    invalidate_when = ["用户表示已经不再介意"]
    if emotion is not None:
        # The impulse to repair is carried by the emotion; once that has decayed, the
        # apology would be an obligation the character no longer feels.
        invalidate_when.append(f"{EMOTION_FADED_PREFIX}{emotion.emotion_event_id}")
    if evidence.kind == "boundary" and evidence.key:
        # A repair that exists because the user drew a line stops being grounded in
        # anything once that line is withdrawn or expires.
        invalidate_when.append(f"{BOUNDARY_RELEASED_PREFIX}{evidence.key}")
    return CandidateIntent(
        candidate_id=new_id("candidate"),
        type="repair",
        intent=f"为上次的{evidence.label}向用户道歉，不再辩解",
        goal="修复关系，让用户知道我听懂了他的反应",
        # One live repair at a time: an apology is about the relationship, not about a
        # subject, so two of them are the same intention.
        target="relationship_repair",
        sources=sources,
        constraints=["不要辩解或索取安慰", "不要连着道歉", "允许用户不回应"],
        preconditions=["用户仍对上一次主动联系有负面反应"],
        invalidate_when=invalidate_when,
        confidence=clamp(0.35 + 0.5 * evidence.explicitness + (0.1 if emotion else 0.0)),
        status=CandidateStatus.NEW.value,
        internal_need=clamp(0.55 + 0.4 * (emotion.intensity if emotion else 0.0)),
        unfinished_relevance=0.0,
        emotion_relevance=clamp(emotion.intensity) if emotion is not None else 0.0,
        created_at=now,
        updated_at=now,
        expires_at=min(expired, now + timedelta(seconds=config.candidate.default_ttl_seconds)),
        proposed_by="rule",
    )


# --------------------------------------------------------------------------------------
# reply: an answer the user is still waiting for
# --------------------------------------------------------------------------------------


def unanswered_questions(
    recent_events: Sequence[RawEvent],
    *,
    now: datetime,
    max_age_seconds: float = REPLY_QUESTION_MAX_AGE_SECONDS,
) -> list[RawEvent]:
    """Return recent user questions the character has not answered, newest first.

    "Answered" is read from the event log, not guessed: a user message is settled when
    an assistant message or a proactive send was recorded in the same conversation at or
    after it. The runtime records both (the host reports its own replies through
    ``/v1/events``, and a proactive delivery writes one), so a question that survives
    this test is one the character genuinely left hanging.

    Args:
        recent_events: Events to consider, normally a recent window of the log.
        now: Reference time.
        max_age_seconds: How old a question may be and still be worth answering.

    Returns:
        The unanswered questions, newest first. Empty when the window carries none.
    """
    answered: list[RawEvent] = []
    questions: list[RawEvent] = []
    reply_types = {EventType.ASSISTANT_MESSAGE.value, EventType.PROACTIVE_SENT.value}
    for event in recent_events:
        if event.event_type == EventType.USER_MESSAGE.value:
            text = event.content or ""
            if not text.strip():
                continue
            if not any(marker in text for marker in QUESTION_MARKERS):
                continue
            if (now - event.timestamp).total_seconds() > max_age_seconds:
                continue
            questions.append(event)
        elif event.event_type in reply_types:
            answered.append(event)
    outstanding = [
        question
        for question in questions
        if not any(
            reply.conversation_id == question.conversation_id
            and _is_newer(reply.timestamp, question.timestamp)
            for reply in answered
        )
    ]
    outstanding.sort(key=lambda event: event.timestamp, reverse=True)
    return outstanding


def _reply_candidate(
    question: RawEvent,
    *,
    situation_source: str | None,
    now: datetime,
    config: RuntimeConfig,
) -> CandidateIntent:
    """Build a ``reply`` candidate for one unanswered user question.

    The intent quotes the user's own words, so the content comes from the event log
    rather than from a template about "answering questions".

    Args:
        question: The unanswered user message.
        situation_source: ``situation:<id>`` of the working-situation row that recorded
            the question, when one exists.
        now: Reference time.
        config: Runtime configuration.

    Returns:
        The candidate, ready for the pool manager.
    """
    excerpt = summarize_text(question.content or "", limit=32)
    sources = [question.event_id]
    if situation_source:
        sources.append(situation_source)
    return CandidateIntent(
        candidate_id=new_id("candidate"),
        type="reply",
        intent=f"回答用户之前问过的问题：{excerpt}",
        goal="把没答上的话答上，不让用户的提问悬着",
        target=excerpt,
        sources=sources,
        constraints=["不要假装当时没听见", "直接回答，不要反问"],
        # The only derived precondition in this module, and the only shape whose premise
        # the working cue can actually confirm: the question's own words are in the cue
        # while the question is part of the working set. Once it has scrolled out, the
        # character should not answer it out of the blue - blocking is the safe
        # direction (see motivation.precondition_holds).
        preconditions=[f"用户仍在等这个回答：{excerpt}"],
        invalidate_when=[
            f"{QUESTION_ANSWERED_PREFIX}{question.event_id}",
            "用户表示不需要回答",
        ],
        confidence=clamp(0.45 + 0.3 * _question_freshness(question, now=now)),
        status=CandidateStatus.NEW.value,
        internal_need=clamp(0.5 + 0.3 * _question_freshness(question, now=now)),
        unfinished_relevance=0.0,
        emotion_relevance=0.0,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=config.candidate.default_ttl_seconds),
        proposed_by="rule",
    )


def _question_freshness(question: RawEvent, *, now: datetime) -> float:
    """Return how urgent an unanswered question still is, in ``[0, 1]``.

    Linearly decaying over :data:`REPLY_QUESTION_MAX_AGE_SECONDS`; a question asked a
    minute ago is worth answering now, one from two days ago is barely worth it.
    """
    age = max(0.0, (now - question.timestamp).total_seconds())
    window = max(1.0, float(REPLY_QUESTION_MAX_AGE_SECONDS))
    return clamp(1.0 - age / window)


def generate(
    *,
    state: RuntimeState,
    config: RuntimeConfig,
    unfinished: Sequence[UnfinishedMatter] = (),
    activated: Sequence[tuple[ActivatedMemory, Memory]] = (),
    existing: Sequence[CandidateIntent] = (),
    spoken_for: Sequence[str] = (),
    now: datetime | None = None,
    emotion_intensity: float = 0.0,
    observations: Sequence[Any] = (),
    emotions: Sequence[EmotionEvent] = (),
    situations: Sequence[Mapping[str, Any]] = (),
    boundaries: Sequence[Boundary] = (),
    recent_events: Sequence[RawEvent] = (),
) -> list[CandidateIntent]:
    """Generate candidate intents with rules only (degradation Level 0).

    The first six inputs are the original ones; every input after ``emotion_intensity``
    was added so that the ``share`` / ``repair`` / ``reply`` shapes could be produced
    from real state (design §37-§43). They all default to empty, so a caller that does
    not supply them gets exactly the behaviour it had before: no share, no repair and no
    reply candidate, and the same follow-up / curiosity / contact set as always.

    Args:
        state: Runtime state.
        config: Runtime configuration.
        unfinished: Live unfinished matters.
        activated: Activation pool joined with memory content.
        existing: Currently live candidates, used to avoid duplicates.
        spoken_for: Subjects an obligation already owns
            (:func:`~companion_runtime.unfinished.subject_guards`); a memory or a
            question about one of them does not become a second candidate.
        now: Reference time.
        emotion_intensity: Peak active emotion intensity.
        observations: Interaction observations, newest first
            (``projections.user_model.list_observations()``). Only observations about a
            proactive action with a negative outcome are used.
        emotions: Active emotion events (``projections.emotion.list_active()``). Used as
            the ``emotion:<id>`` source and as the emotional colouring of a share or a
            repair.
        situations: Active working-situation rows
            (``projections.situation.list_active()``). A row whose ``source_id`` matches
            the evidence becomes a ``situation:<id>`` source.
        boundaries: Boundaries in force (``projections.boundaries.active(now)``). A
            topic/permanent/conditional boundary is negative evidence; a temporal one is
            not.
        recent_events: A recent window of the event log
            (``events.read(EventQuery(limit=...))``), used to find questions the
            character never answered.

    Returns:
        Newly generated candidates (not yet filtered by the pool manager). At most
        :data:`MAX_SHARE_PER_ROUND` shares, :data:`MAX_REPAIR_PER_ROUND` repairs and
        :data:`MAX_REPLY_PER_ROUND` replies are added per round, in addition to the
        per-matter and per-memory candidates.
    """
    stamp = now or utcnow()
    produced: list[CandidateIntent] = []
    existing_types = {candidate.type for candidate in existing}
    existing_targets = {candidate.target for candidate in existing}

    def _admit(candidate: CandidateIntent, *, require_confidence: bool = True) -> bool:
        """Return whether a candidate is new and confident enough to be proposed.

        ``require_confidence`` is ``False`` for the follow-up path, which never applied
        the floor: a live obligation is a reason to speak by itself, and tightening the
        floor must not silently drop it.
        """
        if candidate.target in existing_targets:
            return False
        if require_confidence and candidate.confidence < config.candidate.confidence_floor:
            return False
        produced.append(candidate)
        existing_targets.add(candidate.target)
        return True

    for matter in unfinished:
        if matter.status not in {
            UnfinishedStatus.OPEN.value,
            UnfinishedStatus.WAITING.value,
            UnfinishedStatus.DUE.value,
        }:
            continue
        _admit(
            _unfinished_candidate(matter, now=stamp, config=config),
            require_confidence=False,
        )

    strongest_emotion = _strongest_emotion(emotions, min_intensity=SHARE_EMOTION_MIN_INTENSITY)
    shares = 0
    for activation, memory in activated[:4]:
        if _speaks_for_a_taken_subject(memory, spoken_for):
            # A memory about a subject an unfinished matter already owns must not
            # become a *second* candidate about it. The live matter path asks its own
            # follow-up; the memory path asking as well is how "面试过了！谢谢你那天
            # 惦记我" turned into another question about the interview result - after
            # the user had just reported it.
            continue
        # One memory yields exactly one shape: what the character holds *about the user*
        # is something to offer (share), anything else is something to ask about
        # (curious_question). Offering and asking about the same memory would be two
        # candidates for one thought.
        if memory.kind in SHARE_MEMORY_KINDS:
            if shares >= MAX_SHARE_PER_ROUND:
                continue
            candidate = _share_candidate(
                activation,
                memory,
                emotion=strongest_emotion,
                now=stamp,
                config=config,
            )
            if _admit(candidate):
                shares += 1
            continue
        _admit(_memory_candidate(activation, memory, now=stamp, config=config))

    # --- reply: something the user asked that was never answered
    replies = 0
    for question in unanswered_questions(recent_events, now=stamp):
        if replies >= MAX_REPLY_PER_ROUND:
            break
        if _subject_is_spoken_for(question.content or "", spoken_for):
            # The obligation detector already owns this subject and its own follow-up
            # asks; a reply candidate would be a second intention about one message.
            continue
        candidate = _reply_candidate(
            question,
            situation_source=_situation_source_for(situations, [question.event_id]),
            now=stamp,
            config=config,
        )
        if _admit(candidate):
            replies += 1

    # --- repair: something the character did that the user reacted badly to.
    # The observation store is consulted first because it is the direct evidence (the
    # user's reaction to a specific message the character sent); a boundary the user
    # declared is the second-best evidence and stands in when no observation exists. At
    # most one repair is proposed per round: it concerns one incident, and two live
    # repairs would share a target and be merged by the pool manager anyway.
    if MAX_REPAIR_PER_ROUND > 0:
        evidence = _negative_observation_evidence(
            observations, situations, now=stamp
        ) or _boundary_evidence(boundaries, now=stamp)
        if evidence is not None:
            _admit(
                _repair_candidate(
                    evidence,
                    emotion=_strongest_emotion(
                        emotions,
                        direction=EmotionDirection.NEGATIVE.value,
                        min_intensity=REPAIR_EMOTION_MIN_INTENSITY,
                    ),
                    now=stamp,
                    config=config,
                )
            )

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
                        # Conditions are unioned rather than replaced: the refresh may
                        # have learned a new way for this candidate to die (a memory that
                        # is now watchable for supersession), but a condition already on
                        # the candidate - including a legacy hand-written one - must not
                        # be dropped by a refresh.
                        "invalidate_when": sorted(
                            set(current.invalidate_when) | set(proposal.invalidate_when)
                        ),
                        "preconditions": sorted(
                            set(current.preconditions) | set(proposal.preconditions)
                        ),
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

    ``repair`` is proactive: an apology is initiated by the character, not asked for by
    the user, so a "do not contact me" boundary outranks it. Until this shape had a
    producer the distinction was moot in the shipped configuration (no rule produced a
    repair, and the model path is disabled by default); now that one exists, letting it
    through the hard gate would spend a commit on a message the delivery layer refuses.
    """
    return candidate.type in {
        "contact",
        "check_in",
        "follow_up",
        "curious_question",
        "share",
        "repair",
    }


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

    Three kinds of condition are matched, in this order:

    1. the legacy keyword table (:func:`_condition_keywords`) - unchanged, so every
       condition that used to work still works;
    2. a *derived* condition (see :data:`DERIVED_INVALIDATION_PREFIXES`), which matches
       only when every content token of the condition appears in the text. The ingest
       path writes "未尽之事已了结：<title>" into the working situation when an
       obligation is settled - the exact string the generator derived - so a follow-up
       dies in the same round the user reports the result, while an unrelated mention of
       the same subject does not kill it;
    3. any other (model-written) condition, which matches when at least
       :data:`GENERIC_CONDITION_MIN_TOKENS` of its content tokens appear. Before this,
       an unknown condition returned no keywords and was silently dead, so a candidate
       built from a real premise could not be invalidated by anything the model wrote.

    Conditions that name internal state rather than text (a memory being archived, a
    question being answered) cannot be seen here at all; those are evaluated by
    :func:`invalidated_by_source_state` against the projections.

    Args:
        candidate: Candidate to test.
        situation_text: Current working-situation text.
        user_message: Latest user message.

    Returns:
        The invalidation condition that matched, or ``None``.
    """
    haystack = f"{situation_text} {user_message}".lower()
    for condition in candidate.invalidate_when:
        if _condition_matches(condition, haystack):
            return condition
    return None


def _condition_matches(condition: str, haystack: str) -> bool:
    """Return whether one invalidation condition is satisfied by ``haystack``.

    Args:
        condition: A natural-language condition, possibly a derived one.
        haystack: Lower-cased situation text plus user message.

    Returns:
        ``True`` when the condition counts as satisfied.
    """
    needle = (condition or "").strip().lower()
    if not needle:
        return False
    alternatives = _condition_alternatives(needle)
    if alternatives:
        # "The user said one of these" - any single phrasing is enough, because they are
        # alternative ways of saying the same thing rather than parts of one signal.
        return any(phrase.lower() in haystack for phrase in alternatives)
    keywords = [token for token in _condition_keywords(needle) if token]
    if keywords:
        return all(keyword in haystack for keyword in keywords)
    if _derived_condition(needle) is not None:
        tokens = _content_tokens(needle)
        return bool(tokens) and all(token in haystack for token in tokens)
    tokens = _content_tokens(needle, bigrams_only=True)
    if not tokens:
        return False
    matched = sum(1 for token in tokens if token in haystack)
    required = 1 if len(tokens) == 1 else GENERIC_CONDITION_MIN_TOKENS
    return matched >= required


def _derived_condition(condition: str) -> tuple[str, str] | None:
    """Split a derived condition into ``(prefix, key)``, or return ``None``.

    Args:
        condition: A candidate's condition string.

    Returns:
        The prefix from :data:`DERIVED_INVALIDATION_PREFIXES` and everything after it,
        or ``None`` when the condition is not one this module derived.
    """
    for prefix in DERIVED_INVALIDATION_PREFIXES:
        if condition.startswith(prefix):
            return prefix, condition[len(prefix) :].strip()
    return None


def _content_tokens(text: str, *, bigrams_only: bool = False) -> list[str]:
    """Extract the discriminating tokens of a natural-language condition.

    Latin words and CJK bigrams are taken from
    :func:`~companion_runtime.utility.topic_tokens`; single CJK characters come from
    :func:`~companion_runtime.utility.tokenize` and are what makes a short condition like
    "别聊" matchable at all. Filler words are dropped, mirroring
    ``motivation._condition_tokens``. Kept local rather than importing that private
    helper so that ``candidate`` does not depend on the motivational layer; the rule is
    the same, which is what keeps a condition and a precondition consistent.

    Args:
        text: Source text.
        bigrams_only: Drop single CJK characters. Used by the generic fallback, where a
            shared character is not evidence of anything: "体检有点紧张" shares 体 and 检
            with "用户已经知道体检结果了" and must not retire the candidate.

    Returns:
        A sorted list of distinct tokens.
    """
    tokens = set(topic_tokens(text))
    if not bigrams_only:
        tokens |= set(tokenize(text))
    return sorted(token for token in tokens if token and token not in _CONDITION_STOPWORDS)


def _condition_keywords(condition: str) -> list[str]:
    """Extract a few decisive keywords from a natural-language invalidation rule.

    Conditions written before this module derived its own (and the two human-written
    ones the pool has always carried) have hand-picked keyword *pairs*: the condition
    counts as satisfied when both appear. That is deliberately stricter than the generic
    token rule, and it is preserved exactly - "面试有点紧张" must not retire a
    follow-up about the interview result.

    A condition whose signal is "the user said one of these things" is not a pair at all,
    and lives in :data:`_CONDITION_ALTERNATIVES` instead.

    Args:
        condition: A candidate's condition string.

    Returns:
        The keyword pair, or an empty list when the condition is not in the table.
    """
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


#: Conditions satisfied by *any one* of several phrasings of the same act, because the
#: signal is the act rather than a subject. Used by the conditions this module writes for
#: the repair and reply shapes: "用户表示已经不再介意" is true whether the user said
#: 不怪你, 原谅 or 没事了, and requiring all of them would make the condition dead.
_CONDITION_ALTERNATIVES: Mapping[str, tuple[str, ...]] = {
    "用户表示已经不再介意": ("不怪你", "原谅", "没事了", "别放心上", "没生气", "翻篇"),
    "用户表示不需要回答": ("不用回", "不用答", "算了", "别回", "没事了"),
}


def _condition_alternatives(condition: str) -> tuple[str, ...]:
    """Return the alternative phrasings of a condition, or an empty tuple.

    Args:
        condition: A candidate's condition string.

    Returns:
        The phrases, any one of which satisfies the condition.
    """
    for key, phrases in _CONDITION_ALTERNATIVES.items():
        if key in condition:
            return phrases
    return ()


def _matters_for(
    key: str,
    *,
    sourced: Sequence[str],
    by_id: Mapping[str, UnfinishedMatter],
    by_title: Mapping[str, Sequence[UnfinishedMatter]],
) -> list[UnfinishedMatter]:
    """Return the matters a derived matter condition is about.

    The candidate's own ``unfinished:<id>`` source wins when it has one, because that is
    the record the candidate was actually built from - and if that record is not in the
    supplied set, the answer is "gone", not "some other matter with the same title". The
    condition's key (the matter's title) is the fallback for a candidate that carries no
    matter source; it may legitimately name several matters, because an obligation that
    expired can be re-opened with the same title later.

    Args:
        key: The condition's key - a matter title.
        sourced: Matter identifiers named in the candidate's sources.
        by_id: Supplied matters, keyed by identifier.
        by_title: Supplied matters, grouped by title.

    Returns:
        The matching matters, possibly empty.
    """
    if sourced:
        return [by_id[identifier] for identifier in sourced if identifier in by_id]
    return list(by_title.get(key, ()))


def invalidated_by_source_state(
    candidate: CandidateIntent,
    *,
    unfinished: Sequence[UnfinishedMatter] | None = None,
    memories: Sequence[Memory] | Mapping[str, Memory] | None = None,
    boundaries: Sequence[Boundary] | None = None,
    emotions: Sequence[EmotionEvent] | None = None,
    recent_events: Sequence[RawEvent] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return the derived condition the *state* has satisfied, if any.

    ``invalidate_when`` conditions are natural language, and the pool manager matches
    them against the working-situation text, which is the right test for "the user said
    something that kills this". It is the wrong test for "the record this candidate was
    built from is gone": an archived memory and an answered question never appear as
    situation text, so a candidate grounded in them used to live until its TTL. This
    function reads the records themselves and returns the candidate's own condition
    string, so the reason recorded on the retirement is one an operator can read.

    Only conditions this module derived are evaluated; a condition with no recognizable
    prefix is not this function's business (and returning ``None`` for it is honest
    rather than a silent "still valid").

    A collection left as ``None`` is treated as *no information*: its conditions are not
    evaluated, so a caller that has only matters in hand cannot accidentally retire every
    memory-sourced candidate. A collection passed as empty means "there is nothing
    here", which for a record-backed condition means the record is gone.

    Args:
        candidate: Candidate to test.
        unfinished: Matters to look matter-sourced conditions up in. Pass every status
            (``list_all``) so a resolved matter can be told from a deleted one.
        memories: Memories to look memory-sourced conditions up in (a sequence or the
            ``{memory_id: Memory}`` mapping ``get_memories`` returns). Pass every status
            (``list_memories``), not only the active ones.
        boundaries: Boundaries to look boundary-sourced conditions up in. Include
            revoked and expired ones: a released boundary is exactly what invalidates a
            boundary-derived repair.
        emotions: Currently active emotion events. A cited emotion that is absent from a
            supplied list has decayed.
        recent_events: A recent window of the event log, used to tell whether the
            question a reply candidate is about has been answered since.
        now: Reference time for boundary activity; defaults to the wall clock.

    Returns:
        The condition string carried by the candidate that the state satisfies, or
        ``None``.
    """
    stamp = now or utcnow()
    # A matter-backed candidate names its matter in ``sources``; that identifier is the
    # exact key, while the condition string carries the *title* so an operator can read
    # it. Both are used: the sources decide which matter is meant (two matters may share
    # a title), and the title is the fallback for a candidate that has no source.
    sourced_matters = [
        source[len(UNFINISHED_SOURCE_PREFIX) :]
        for source in candidate.sources
        if source.startswith(UNFINISHED_SOURCE_PREFIX)
    ]
    by_title: dict[str, list[UnfinishedMatter]] = {}
    if unfinished is not None:
        for matter in unfinished:
            by_title.setdefault(matter.title, []).append(matter)
    matters = (
        None
        if unfinished is None
        else {matter.unfinished_id: matter for matter in unfinished}
    )
    if memories is None:
        by_id: Mapping[str, Memory] | None = None
    elif isinstance(memories, Mapping):
        by_id = memories
    else:
        by_id = {memory.memory_id: memory for memory in memories}
    boundary_by_id = (
        None if boundaries is None else {boundary.boundary_id: boundary for boundary in boundaries}
    )
    emotion_ids = None if emotions is None else {event.emotion_event_id for event in emotions}
    events_by_id = (
        None if recent_events is None else {event.event_id: event for event in recent_events}
    )
    answered = [] if recent_events is None else [
        event
        for event in recent_events
        if event.event_type
        in {EventType.ASSISTANT_MESSAGE.value, EventType.PROACTIVE_SENT.value}
    ]
    for condition in candidate.invalidate_when:
        derived = _derived_condition((condition or "").strip())
        if derived is None:
            continue
        prefix, key = derived
        if not key:
            continue
        if prefix in {MATTER_RESOLVED_PREFIX, MATTER_RELEASED_PREFIX} and matters is not None:
            named = _matters_for(
                key, sourced=sourced_matters, by_id=matters, by_title=by_title
            )
            if prefix == MATTER_RESOLVED_PREFIX:
                if any(matter.status == UnfinishedStatus.RESOLVED.value for matter in named):
                    return condition
                continue
            if any(
                matter.status
                in {
                    UnfinishedStatus.CANCELLED.value,
                    UnfinishedStatus.EXPIRED.value,
                    UnfinishedStatus.INVALIDATED.value,
                }
                for matter in named
            ):
                return condition
            if not named:
                # Nothing in the supplied set answers to this condition: the record is
                # gone, which is exactly what the condition says.
                return condition
        elif prefix == MEMORY_ARCHIVED_PREFIX and by_id is not None:
            memory = by_id.get(key)
            if memory is None or memory.status == MemoryStatus.ARCHIVED.value:
                return condition
        elif prefix == MEMORY_SUPERSEDED_PREFIX and by_id is not None:
            memory = by_id.get(key)
            if memory is not None and is_superseded(memory):
                return condition
        elif prefix == QUESTION_ANSWERED_PREFIX and events_by_id is not None:
            question = events_by_id.get(key)
            if question is not None and any(
                reply.conversation_id == question.conversation_id
                and _is_newer(reply.timestamp, question.timestamp)
                for reply in answered
            ):
                return condition
        elif prefix == BOUNDARY_RELEASED_PREFIX and boundary_by_id is not None:
            boundary = boundary_by_id.get(key)
            if boundary is None or not boundary.is_active(stamp):
                return condition
        elif prefix == EMOTION_FADED_PREFIX and emotion_ids is not None:
            if key not in emotion_ids:
                return condition
    return None


def expires_in_seconds(candidate: CandidateIntent, now: datetime) -> float | None:
    """Return seconds until a candidate expires, or ``None`` when unbounded."""
    if candidate.expires_at is None:
        return None
    return (candidate.expires_at - now).total_seconds()
