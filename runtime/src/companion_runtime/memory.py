"""Memory system: candidates, long-term memories, retrieval and activation.

The full chain is::

    raw events -> working situation -> memory candidates -> consolidation
                -> long-term memory -> retrieval (lexical RAG) -> activation pool

Consolidation, the only writer of the ``memories`` table, is **rule-based and needs
no model**. The Runtime drives it unattended from
:meth:`companion_runtime.runtime.Runtime.endogenous_round` (gated on
:func:`needs_consolidation`) and an operator can drive one pass by hand with
``companion-runtime consolidate``. A semantic provider may *replace* a candidate's
summary with a model-written one, but it is never required for a memory to form,
and a memory the rules built says so through ``structured["proposed_by"]``.

Forgetting is a chain rather than a flag: ``active -> low_activation -> archived``
(:class:`~companion_runtime.typing.MemoryStatus`). A faded memory leaves the
working set while staying in the database, and reinforcement puts it back.

Three deliberate simplifications for the first version:

* retrieval uses an FTS-like lexical overlap score instead of embeddings, with a
  clear seam (:meth:`MemoryStore.retrieve`) for an embedding sidecar;
* forgetting means *archival*, never deletion;
* conflict handling is one-directional and deterministic: when a newer statement
  contradicts an older memory, the older one is withdrawn from retrieval
  (:func:`is_superseded`) rather than re-described in prose - "used to ..., now
  ..." is a language task, and no model is guaranteed to exist.

Every memory keeps a dual representation: structured fields for the Runtime and a
natural-language summary for RAG, the strong semantic API and the main LLM.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from .config import RuntimeConfig
from .projections import MemoryProjection
from .typing import (
    ActivatedMemory,
    EventType,
    Memory,
    MemoryCandidate,
    MemoryKind,
    MemoryStatus,
    RawEvent,
    RuntimeState,
    UnfinishedMatter,
    new_id,
)
from .utility import (
    clamp,
    ensure_aware,
    exponential_decay,
    summarize_text,
    tokenize,
    topic_tokens,
    utcnow,
)

LOGGER = logging.getLogger("companion_runtime.memory")


# --------------------------------------------------------------------------------------
# Candidate scoring
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class CandidateValue:
    """Decomposition of a memory candidate's worth."""

    future_use: float = 0.0
    repetition: float = 0.0
    user_emphasis: float = 0.0
    emotional_salience: float = 0.0
    unfinished_relevance: float = 0.0
    stability: float = 0.0
    transience: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-serialisable rendering."""
        return {
            key: round(float(value), 6)
            for key, value in dataclasses.asdict(self).items()
        }


#: Phrases that mark a statement as durable and preference-like.
PREFERENCE_MARKERS = (
    "不喜欢",
    "喜欢",
    "讨厌",
    "习惯",
    "总是",
    "从来",
    "一直",
    "以后",
    "记住",
    "别再",
    "不要",
    "最",
    "对我来说",
    "prefer",
    "always",
    "never",
    "hate",
    "love",
)

EMPHASIS_MARKERS = ("记住", "重要", "一定要", "千万", "别忘", "remember", "important")

#: Phrases that are almost certainly transient small talk.
TRANSIENT_MARKERS = ("吃", "午饭", "晚饭", "天气", "几点", "哈哈", "在吗", "嗯", "哦")

#: Similarity at or above which two summaries count as the same fact outright.
DEDUPE_EXACT_RATIO = 0.85

#: Similarity required when the candidate's topics are a subset of the stored
#: memory's topics, i.e. the new statement is about something already known.
DEDUPE_TOPIC_RATIO = 0.6

#: How much of the *shorter* summary must appear in the longer one before the two are
#: treated as one fact said at different lengths ("用户喜欢咖啡" / "用户喜欢手冲咖啡").
#: Symmetric similarity under-reports these because it divides by the longer string.
DEDUPE_CONTAINMENT_RATIO = 0.8

#: ...but containment alone is far too permissive, because a short summary is
#: trivially contained in a long one. A shared token also has to be more than
#: incidental: "用户为这次面试准备了很久" contains the whole topic "面试", and that is
#: not the same fact as "用户明天要去面试".
DEDUPE_MIN_SHARED_TOKENS = 2

#: Provenance recorded when the deterministic rule path formed the memory, i.e.
#: when the summary is the one the rule-based proposal already extracted. It must
#: never read as if a model wrote it.
PROVENANCE_RULE = "rule"

#: Provenance recorded when an outside model supplied the summary text.
PROVENANCE_SEMANTIC_API = "semantic_api"

#: Structured key under which the replacement hint is stored on a superseded memory.
SUPERSEDED_HINT_KEY = "superseded_by_hint"

#: Structured key under which the replacement time is stored on a superseded memory.
SUPERSEDED_AT_KEY = "superseded_at"

#: Structured key under which the identifiers a memory replaced are stored.
SUPERSEDES_KEY = "supersedes"

#: Why a superseded memory is withheld from retrieval, activation and the prompt.
SUPERSEDED_REASON = "superseded_by_newer_memory"

#: Activation added by one reinforcement (a restatement of the same fact).
REINFORCEMENT_ACTIVATION = 0.35

#: How much a shared token with the working situation is worth, relative to one
#: shared token with the current sentence. Below 1.0 because the situation is
#: background: it colours recall, it does not dominate it.
SITUATION_WEIGHT = 0.7

#: Markers of a statement about the *relationship* rather than about a fact
#: (design §16's fourth kind). Deliberately conservative: it takes an explicit
#: relational act, not merely a warm sentence.
RELATIONSHIP_MARKERS = (
    "谢谢你",
    "谢谢",
    "陪我",
    "陪你",
    "在乎",
    "在意",
    "惦记",
    "想你",
    "想我",
    "对不起",
    "抱歉",
    "信任",
    "安全感",
    "离不开",
    "习惯了有",
    "thank you",
    "miss you",
    "sorry",
)

#: Markers that make a message a *question* rather than a statement. A question is
#: something that happened (episodic at most): "我生日是什么时候来着" must not be
#: filed as stable knowledge about the user, which is exactly what the marker "我生日"
#: alone did.
QUESTION_MARKERS = ("?", "？", "吗", "呢", "什么时候", "多少", "为什么", "怎么", "哪", "记不记得", "还记")

#: Markers of a durable fact about the user (design §16's "stable knowledge").
STABLE_MARKERS = (
    "我叫",
    "我是",
    "我的生日",
    "我生日",
    "生日是",
    "出生",
    "我住在",
    "住在",
    "我家在",
    "老家",
    "工作在",
    "上班",
    "职业",
    "专业",
    "毕业",
    "手机号",
    "微信号",
    "邮箱",
    "全名",
)

#: Markers of an explicit *withdrawal*: the user is taking something back rather than
#: replacing it with a stated alternative. "改成/换成" are deliberately not here -
#: they announce a change whose new state carries its own polarity ("改喝茶了" is not
#: a negative statement about tea).
CORRECTION_MARKERS = ("不喝", "不吃", "戒了", "戒掉", "不再", "不想要", "不打算")

#: Negation cues that may sit immediately in front of a positive marker and flip it.
#: Phrase matching alone cannot express this: "不太喜欢", "没那么喜欢" and "不喜欢" all
#: mean the opposite of "喜欢", and the first two do not contain the third as a
#: substring - which is how a correction came to be read as agreement.
NEGATION_CUES = ("不", "没", "别", "无", "非", "don't", "do not", "not", "no longer", "never")

#: How many characters before a positive marker are searched for a negation cue.
#: "我现在不太喜欢" needs three ("不太"); a longer window starts matching negations
#: that belong to another clause.
NEGATION_WINDOW = 3

#: Positive markers whose polarity a negation cue can flip.
POSITIVE_MARKERS = ("喜欢", "爱", "想要", "偏爱", "prefer", "like", "love")

#: Markers that are negative on their own.
NEGATIVE_MARKERS = ("讨厌", "厌恶", "受不了", "hate", "dislike", "can't stand")


def score_candidate(
    *,
    text: str,
    source_events: Sequence[RawEvent],
    state: RuntimeState,
    unfinished: Sequence[UnfinishedMatter],
    emotion_salience: float,
    config: RuntimeConfig,
) -> CandidateValue:
    """Score how much a piece of content deserves to become long-term memory.

    Conceptually ``M = future_use + repetition + user_emphasis + emotional_salience
    + unfinished_relevance + stability - transience``.

    Args:
        text: Candidate content.
        source_events: Events the candidate derives from. Accepted for interface
            stability and diagnostics only: the score is computed from the text, the
            state, the live matters and the emotional salience, so one source event
            or a whole batch produces the same value.
        state: Runtime state (values modulate importance).
        unfinished: Live unfinished matters.
        emotion_salience: Peak emotional intensity around the source events.
        config: Runtime configuration. Also accepted for interface stability: every
            weight below is a module constant, and only :func:`propose_from_event`
            compares the returned total against ``config.memory.candidate_min_value``.

    Returns:
        A :class:`CandidateValue` including the signed total in ``[0, 1]``.
    """
    lowered = (text or "").lower()
    words = tokenize(text)
    length_factor = clamp(len(words) / 20.0)

    future_use = clamp(0.35 * length_factor + 0.25)
    repetition = 0.0
    if any(marker in lowered for marker in PREFERENCE_MARKERS):
        future_use = clamp(future_use + 0.30)
        repetition = 0.35

    user_emphasis = 0.55 if any(marker in lowered for marker in EMPHASIS_MARKERS) else 0.0
    transience = 0.30 if any(marker in lowered for marker in TRANSIENT_MARKERS) else 0.0
    if len(words) <= 3:
        transience = clamp(transience + 0.15)

    stability = 0.30 + 0.30 * state.values.stability_commitment
    if any(marker in lowered for marker in ("以后", "一直", "总是", "从来", "always", "never")):
        stability = clamp(stability + 0.25)

    unfinished_relevance = 0.0
    for matter in unfinished:
        if matter.title and any(token in lowered for token in tokenize(matter.title)):
            unfinished_relevance = max(unfinished_relevance, clamp(matter.priority))

    emotional_salience = clamp(emotion_salience)
    emotional_salience *= 0.6 + 0.6 * state.values.relationship_maintenance

    total = (
        future_use
        + repetition
        + user_emphasis
        + emotional_salience
        + unfinished_relevance
        + stability
        - transience
    )
    value = CandidateValue(
        future_use=future_use,
        repetition=repetition,
        user_emphasis=user_emphasis,
        emotional_salience=emotional_salience,
        unfinished_relevance=unfinished_relevance,
        stability=stability,
        transience=transience,
        total=clamp(total / 3.0),
    )
    return value


def propose_from_event(
    event: RawEvent,
    *,
    state: RuntimeState,
    unfinished: Sequence[UnfinishedMatter],
    emotion_salience: float,
    config: RuntimeConfig,
    created_at: datetime | None = None,
) -> MemoryCandidate | None:
    """Build a memory candidate from one event, or ``None`` when too weak.

    Args:
        event: Source event.
        state: Runtime state.
        unfinished: Live unfinished matters.
        emotion_salience: Peak emotional intensity around the event.
        config: Runtime configuration.
        created_at: Instant the candidate is created at, on the caller's timeline.
            The Runtime passes the moment it ingested the event, because the
            maintenance interval that decides when the candidate is consolidated is
            measured against that clock; defaulting to the wall clock would make a
            replayed or simulated timeline consolidate on the wrong schedule.

    Returns:
        A pending :class:`MemoryCandidate` when it clears
        ``config.memory.candidate_min_value``.
    """
    if event.event_type != EventType.USER_MESSAGE.value:
        return None
    text = (event.content or "").strip()
    if not text:
        return None
    value = score_candidate(
        text=text,
        source_events=[event],
        state=state,
        unfinished=unfinished,
        emotion_salience=emotion_salience,
        config=config,
    )
    if value.total < config.memory.candidate_min_value:
        return None

    kind = MemoryKind.EPISODIC.value
    lowered = text.lower()
    is_question = any(marker in text for marker in QUESTION_MARKERS)
    if any(marker in lowered for marker in PREFERENCE_MARKERS):
        kind = MemoryKind.USER_PREFERENCE.value
    elif not is_question and any(marker in lowered for marker in STABLE_MARKERS):
        kind = MemoryKind.STABLE_KNOWLEDGE.value
    elif not is_question and any(marker in lowered for marker in RELATIONSHIP_MARKERS):
        # A relational act is not a preference and not an episode: it is the material
        # the relationship model is built from (design §16's fourth kind, which had no
        # producer at all - the kind existed only in the enum and the importance table).
        kind = MemoryKind.RELATIONSHIP.value

    return MemoryCandidate(
        candidate_id=new_id("memory_candidate"),
        summary=summarize_text(text),
        kind=kind,
        source_event_ids=[event.event_id],
        value=value.total,
        status="pending",
        created_at=ensure_aware(created_at) or utcnow(),
        topics=tokenize(text)[:6],
        confidence=0.5,
    )


def kind_importance(kind: str, config: RuntimeConfig) -> float:
    """Return the configured baseline importance for a memory kind."""
    return {
        MemoryKind.EPISODIC.value: config.memory.episodic_importance,
        MemoryKind.STABLE_KNOWLEDGE.value: config.memory.stable_knowledge_importance,
        MemoryKind.USER_PREFERENCE.value: config.memory.preference_importance,
        MemoryKind.RELATIONSHIP.value: config.memory.relationship_importance,
    }.get(kind, config.memory.episodic_importance)


# --------------------------------------------------------------------------------------
# Consolidation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ConsolidationResult:
    """Outcome of a background consolidation pass."""

    consolidated: list[str]
    archived: list[str]
    skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "consolidated": list(self.consolidated),
            "archived": list(self.archived),
            "skipped": self.skipped,
        }


def consolidate(
    projection: MemoryProjection,
    connection: sqlite3.Connection,
    *,
    config: RuntimeConfig,
    now: datetime | None = None,
    limit: int = 20,
    summarizer: Any = None,
) -> ConsolidationResult:
    """Promote pending candidates into long-term memories.

    This is the only writer of the ``memories`` table, and it needs nothing but the
    rules: the summary of a promoted memory is the one the candidate already
    carries. A ``summarizer`` may replace it with a model-written text, and only a
    text that actually differs is recorded as model provenance
    (``structured["proposed_by"]``), so a rule-formed memory never claims a model
    wrote it.

    Conflicting new information does not delete the old memory; it lowers its
    confidence, links the new one through the ``supersedes`` structured field and
    marks the old one with ``superseded_by_hint``. The read side of that pair is
    :func:`is_superseded`: a replaced memory leaves retrieval, activation and the
    prompt. Re-describing the pair in prose ("used to ..., but now ...") is a
    language task and is *not* done here - the deterministic half is that the
    replaced fact stops being asserted.

    Args:
        projection: Memory storage.
        connection: Write connection.
        config: Runtime configuration.
        now: Reference time.
        limit: Maximum number of candidates to process.
        summarizer: Optional callable ``(candidate) -> str`` for a richer summary.

    Returns:
        A :class:`ConsolidationResult`.
    """
    stamp = now or utcnow()
    pending = projection.pending_candidates(limit=limit * 3)
    # Which candidates to process is a question of worth (highest value first), but
    # the order they are *applied* in must be the order they were said: a correction
    # can only redefine a statement that is already stored, so applying a batch by
    # value would let an older statement be written after the newer one that
    # corrected it - and then contradict it.
    pending = sorted(pending, key=lambda c: (-c.value, c.created_at or stamp))[:limit]
    pending.sort(key=lambda c: c.created_at or stamp)

    consolidated: list[str] = []
    archived: list[str] = []
    skipped = 0

    for candidate in pending:
        if candidate.value < config.memory.candidate_min_value:
            projection.set_candidate_status(connection, candidate.candidate_id, "rejected")
            skipped += 1
            continue

        existing = _find_duplicate(projection, candidate)
        if existing is not None:
            # The same content was already remembered: strengthen the existing
            # memory instead of storing a near-duplicate row. Saying the same thing
            # again is also the reinforcement that lifts a faded memory back into
            # the working set.
            existing.importance = clamp(existing.importance + 0.05 * candidate.value)
            existing.confidence = clamp(existing.confidence + 0.05)
            existing.source_event_ids = sorted(
                set(existing.source_event_ids) | set(candidate.source_event_ids)
            )
            reinforce(projection, connection, existing, config=config, now=stamp)
            projection.upsert_memory(connection, existing)
            projection.set_candidate_status(
                connection,
                candidate.candidate_id,
                "merged",
                consolidated_memory_id=existing.memory_id,
            )
            skipped += 1
            continue

        summary = candidate.summary
        provenance = PROVENANCE_RULE
        if summarizer is not None:
            try:
                proposed = str(summarizer(candidate)) or summary
            except Exception:  # pragma: no cover - defensive boundary around a model
                LOGGER.exception("Memory summarizer failed; keeping candidate summary")
            else:
                if proposed != summary:
                    summary = proposed
                    provenance = PROVENANCE_SEMANTIC_API

        supersedes = _find_conflicts(projection, candidate, connection, now=stamp)
        # An older statement can be consolidated *after* the newer one that corrects it
        # (the maintenance pass may reach it later). It is still history, so it is
        # stored - but as a replaced memory, not as something the character believes:
        # "新信息不负责删除过去，而负责重新定义过去与现在的关系" (design §18).
        superseded_by = _find_newer_contradiction(projection, candidate)
        importance = clamp(
            0.5 * kind_importance(candidate.kind, config) + 0.5 * candidate.value
        )
        #: When the statement was made, not when it was filed: this is what makes
        #: "newer redefines older" decidable, and it is what the recency term wants.
        stated_at = ensure_aware(candidate.created_at) or stamp
        structured: dict[str, Any] = {
            "value_breakdown": candidate.value,
            "confidence": candidate.confidence,
            # Who produced this memory. The rule path must not read as a model.
            "proposed_by": provenance,
            SUPERSEDES_KEY: supersedes,
            "topics": candidate.topics,
        }
        if superseded_by is not None:
            structured[SUPERSEDED_HINT_KEY] = superseded_by.summary
            structured[SUPERSEDED_AT_KEY] = isoformat_or_none(superseded_by.created_at)
        memory = Memory(
            memory_id=new_id("memory"),
            kind=candidate.kind,
            summary=summary,
            structured=structured,
            topics=list(candidate.topics),
            importance=importance,
            confidence=candidate.confidence,
            status=MemoryStatus.ACTIVE.value,
            source_event_ids=list(candidate.source_event_ids),
            created_at=stated_at,
            updated_at=stamp,
        )
        projection.upsert_memory(connection, memory)
        if superseded_by is not None:
            # The pair is recorded from both sides, so the newer memory can say what
            # it replaced even though the older row arrived later.
            superseded_by.structured = dict(superseded_by.structured) | {
                SUPERSEDES_KEY: sorted(
                    set(superseded_by.structured.get(SUPERSEDES_KEY) or []) | {memory.memory_id}
                )
            }
            projection.upsert_memory(connection, superseded_by)
        projection.set_candidate_status(
            connection,
            candidate.candidate_id,
            "consolidated",
            consolidated_memory_id=memory.memory_id,
        )
        projection.upsert_activation(
            connection,
            ActivatedMemory(
                memory_id=memory.memory_id,
                activation=clamp(0.35 + 0.5 * importance),
                last_recalled_at=stamp,
                recall_count=1,
                reason="consolidation",
            ),
        )
        consolidated.append(memory.memory_id)

    archived.extend(archive_stale(projection, connection, config=config, now=stamp))
    return ConsolidationResult(consolidated=consolidated, archived=archived, skipped=skipped)


def is_superseded(memory: Memory) -> bool:
    """Return whether a newer statement has replaced this memory's content.

    The write side is :func:`_find_conflicts`, which records
    ``superseded_by_hint``/``superseded_at`` on the replaced memory and lists its
    identifier in the replacement's ``supersedes`` field. This is the read side: a
    replaced memory is no longer asserted, so retrieval, activation and the prompt
    all skip it. It stays in the database - an old fact is history, not an error.
    """
    return bool(str(memory.structured.get(SUPERSEDED_HINT_KEY) or "").strip())


def supersession_record(memory: Memory) -> dict[str, Any]:
    """Return the operator-facing supersession view of one memory.

    ``/memories`` prints a memory's structured fields verbatim, but "why is this
    fact never recalled again" should not require knowing that
    ``superseded_by_hint`` is the field to look at, so the reason is stated.

    Returns:
        ``superseded``, ``superseded_by_hint``, ``superseded_at``, ``supersedes``,
        ``retrievable`` and ``retrieval_reason``.
    """
    superseded = is_superseded(memory)
    if superseded:
        reason = SUPERSEDED_REASON
    elif memory.status != MemoryStatus.ACTIVE.value:
        reason = f"status:{memory.status}"
    else:
        reason = "active"
    return {
        "superseded": superseded,
        "superseded_by_hint": memory.structured.get(SUPERSEDED_HINT_KEY),
        "superseded_at": memory.structured.get(SUPERSEDED_AT_KEY),
        "supersedes": list(memory.structured.get(SUPERSEDES_KEY) or []),
        "retrievable": reason == "active",
        "retrieval_reason": reason,
    }


def reinforce(
    projection: MemoryProjection,
    connection: sqlite3.Connection,
    memory: Memory,
    *,
    config: RuntimeConfig,
    now: datetime,
    amount: float = REINFORCEMENT_ACTIVATION,
) -> ActivatedMemory:
    """Raise a memory's activation, promoting it when it crosses the threshold.

    ``LOW_ACTIVATION`` is not a deletion: it is the middle rung of the
    ``active -> low_activation -> archived`` chain. A memory leaves it as soon as
    something reinforces it, and a restatement of the same fact - which is what
    consolidation sees when a duplicate candidate arrives - is exactly that
    evidence.

    Args:
        projection: Memory storage.
        connection: Write connection.
        memory: The memory being reinforced. Its status is updated in place, so the
            caller is responsible for persisting the row (``upsert_memory``).
        config: Runtime configuration.
        now: Reference time.
        amount: How much activation one reinforcement adds.

    Returns:
        The updated activation entry.
    """
    current = _activation_of(projection, memory.memory_id)
    base = current.activation if current is not None else 0.0
    activation = clamp(base + amount)
    updated = ActivatedMemory(
        memory_id=memory.memory_id,
        activation=activation,
        last_recalled_at=now,
        recall_count=(current.recall_count if current is not None else 0) + 1,
        reason="reinforced",
    )
    projection.upsert_activation(connection, updated)
    if (
        memory.status == MemoryStatus.LOW_ACTIVATION.value
        and activation >= config.memory.activation_threshold
    ):
        memory.status = MemoryStatus.ACTIVE.value
    return updated


def _activation_of(
    projection: MemoryProjection, memory_id: str
) -> ActivatedMemory | None:
    """Return the activation entry of one memory, or ``None`` when it has none."""
    for activated in projection.list_activated(limit=500):
        if activated.memory_id == memory_id:
            return activated
    return None


def _find_duplicate(projection: MemoryProjection, candidate: MemoryCandidate) -> Memory | None:
    """Return an existing memory stating the same fact, if any.

    Deduplication is about *content*, never about provenance. The candidate and the
    memory must actually say the same thing, and the module's thresholds describe
    three different cases:

    * ``_similarity >= DEDUPE_EXACT_RATIO`` (0.85): the same fact restated. This
      branch does *not* require overlapping topics - topic lists are extracted at
      proposal time and drift, and a restatement that is 85% identical is the same
      fact however the two rows were tagged;
    * ``_similarity >= DEDUPE_TOPIC_RATIO`` (0.6) **and** at least one shared topic:
      similar wording about something already known. Here the topic overlap is what
      keeps two different facts with similar phrasing apart;
    * one summary containing the other (``DEDUPE_CONTAINMENT_RATIO``), again with a
      shared topic: the "same fact, said at more length" case.

    Sharing a source event is explicitly not enough. One message routinely yields
    several facts ("我生日是三月三号，喜欢手冲咖啡"), so every candidate built from it
    carries the same single ``event_id``; treating that overlap as proof of
    duplication silently merged distinct facts into whichever one was consolidated
    first, and the merged memory then asserted the union of two unrelated claims.

    A memory that a newer statement replaced is never a merge target. It is withheld
    from retrieval (:func:`is_superseded`), so folding a fresh statement into it
    would make that statement disappear without a trace instead of being asserted.
    """
    summary = (candidate.summary or "").strip()
    topics = set(candidate.topics or ())
    if not summary and not topics:
        return None
    candidate_polarity = polarity_of(summary)
    for memory in projection.list_memories(
        status=[MemoryStatus.ACTIVE.value, MemoryStatus.LOW_ACTIVATION.value], limit=300
    ):
        if is_superseded(memory):
            continue
        stored = (memory.summary or "").strip()
        if summary and stored and summary == stored:
            return memory
        if not summary or not stored:
            continue
        # A restatement is never a statement that says the opposite. Similar wording
        # with the other polarity is a *contradiction*, and merging it would fold the
        # correction into the memory it corrects - the summary shown to the model would
        # then be the old one while the new evidence disappeared into it.
        other_polarity = polarity_of(stored)
        if (
            candidate_polarity is not None
            and other_polarity is not None
            and candidate_polarity != other_polarity
        ):
            continue
        ratio = _similarity(summary, stored)
        if ratio >= DEDUPE_EXACT_RATIO:
            return memory
        overlap = len(topics & set(memory.topics))
        if not overlap:
            continue
        if ratio >= DEDUPE_TOPIC_RATIO:
            return memory
        # One summary containing the other is the "same fact, said at more length"
        # case, which a symmetric score under-reports because it divides by the
        # longer sentence - and which a substring test misses entirely when the
        # extra words sit in the middle ("用户喜欢咖啡" / "用户喜欢手冲咖啡").
        if _contains(summary, stored) or _contains(stored, summary):
            return memory
    return None


def _contains(shorter: str, longer: str) -> bool:
    """Return whether ``longer`` says everything ``shorter`` says, and more.

    The comparison is token-based and one-directional: at least
    ``DEDUPE_MIN_SHARED_TOKENS`` of ``shorter``'s topical tokens must also appear in
    ``longer``, and they must cover ``DEDUPE_CONTAINMENT_RATIO`` of ``shorter``. The
    caller applies it in both directions, so a substring test is deliberately not
    used: the extra words may sit in the middle of the longer sentence.
    """
    shorter_tokens = topic_tokens(shorter)
    if len(shorter_tokens) < DEDUPE_MIN_SHARED_TOKENS:
        return False
    shared = shorter_tokens & topic_tokens(longer)
    if len(shared) < DEDUPE_MIN_SHARED_TOKENS:
        return False
    return len(shared) / len(shorter_tokens) >= DEDUPE_CONTAINMENT_RATIO


def _similarity(left: str, right: str) -> float:
    """Return a deterministic similarity in ``[0, 1]`` for two short summaries.

    Token overlap (Jaccard) over CJK *bigrams* is used rather than an edit distance
    or single characters. Single CJK characters are far too common to indicate a
    shared subject - every summary about the user shares 用 and 户 - while bigrams
    make the overlap mean "these two sentences are about the same thing". Integer
    token counts also make the threshold exactly reproducible.
    """
    left_tokens = topic_tokens(left)
    right_tokens = topic_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _is_newer(candidate_at: datetime | None, memory_at: datetime | None) -> bool:
    """Return whether a candidate may redefine a memory, judged by time.

    Args:
        candidate_at: When the new statement was made (``None`` means unknown).
        memory_at: When the stored memory was formed.

    Returns:
        ``True`` when the candidate is at least as new as the memory. An unknown
        timestamp on either side cannot prove staleness, so it does not block the
        replacement - the alternative would be a system that can never correct itself.
    """
    if candidate_at is None or memory_at is None:
        return True
    return ensure_aware(candidate_at) >= ensure_aware(memory_at)


def polarity_of(text: str) -> str | None:
    """Return ``"positive"``/``"negative"`` for a statement about a preference.

    The naive version of this check is a list of negative phrases plus a list of
    positive ones. It reads "我现在不太喜欢咖啡了" as *positive*, because the phrase
    "不喜欢" does not occur in "不太喜欢" as a substring - so a correction looked like
    agreement, and both the old and the new statement stayed in the belief set at
    once. That is the one failure a companion must not have.

    Polarity is therefore decided structurally: a negative marker anywhere is
    negative; otherwise a positive marker is negative when a negation cue sits
    within :data:`NEGATION_WINDOW` characters in front of it, and positive
    otherwise. "喜欢咖啡" is positive, "不太喜欢咖啡" and "不喜欢咖啡" are negative,
    "讨厌咖啡" is negative.

    Args:
        text: The statement.

    Returns:
        The polarity, or ``None`` when the sentence takes no position.
    """
    lowered = (text or "").lower()
    if not lowered:
        return None
    if any(marker in lowered for marker in NEGATIVE_MARKERS):
        return "negative"
    if any(marker in lowered for marker in CORRECTION_MARKERS):
        return "negative"
    for marker in POSITIVE_MARKERS:
        start = lowered.find(marker)
        while start != -1:
            window = lowered[max(0, start - NEGATION_WINDOW) : start]
            if any(cue in window for cue in NEGATION_CUES):
                return "negative"
            start = lowered.find(marker, start + 1)
    if any(marker in lowered for marker in POSITIVE_MARKERS):
        return "positive"
    return None


def _find_conflicts(
    projection: MemoryProjection,
    candidate: MemoryCandidate,
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> list[str]:
    """Find existing memories that the candidate appears to supersede.

    A conflict is a same-kind, still-live memory *about the same thing* that carries
    the opposite polarity (:func:`polarity_of`), e.g. "likes coffee" now vs "doesn't
    like coffee" before. A correction phrased as a replacement ("不太喜欢…了，改喝茶")
    is one of those cases, which is why polarity is decided structurally rather than
    by matching a list of negative phrases.

    "About the same thing" is measured with the same bigram tokens
    :func:`_similarity` uses, not with the stored single-character topic tags:
    topics are the first six characters of a message, so "不喜欢别人连续追问" and
    "喜欢手冲咖啡" share 我/喜/欢 and would otherwise look like one subject. Two
    shared bigrams keep "喜欢咖啡" vs "不喜欢咖啡" contradictory while leaving
    unrelated clauses alone.

    The replaced memory keeps its row - it is marked through
    :data:`SUPERSEDED_HINT_KEY`/:data:`SUPERSEDED_AT_KEY` and read out of retrieval
    by :func:`is_superseded`, never deleted. ``now`` is the caller's reference time
    rather than the wall clock, so the recorded replacement time lives on the same
    timeline as the rest of the pass.

    Args:
        projection: Memory storage.
        candidate: The candidate that may replace older statements.
        connection: Write connection.
        now: Reference time of the consolidation pass.

    Returns:
        Identifiers of superseded memories, whose confidence is lowered.
    """
    subject = topic_tokens(candidate.summary)
    if not subject:
        return []
    polarity = polarity_of(candidate.summary)
    if polarity is None:
        return []

    superseded: list[str] = []
    for memory in projection.list_memories(status=MemoryStatus.ACTIVE.value, limit=200):
        if memory.kind != candidate.kind:
            continue
        if len(subject & topic_tokens(memory.summary)) < DEDUPE_MIN_SHARED_TOKENS:
            continue
        if not _is_newer(candidate.created_at, memory.created_at):
            # A statement can only redefine one that came *before* it. Without this
            # guard the outcome depends on the order a batch happens to be processed
            # in - consolidation sorts by value, not by time - and an older memory
            # could withdraw the newer statement that corrected it, leaving the
            # character asserting the version the user had already replaced.
            continue
        other_polarity = polarity_of(memory.summary)
        if other_polarity is None or other_polarity == polarity:
            continue
        memory.confidence = clamp(memory.confidence * 0.8)
        memory.structured = dict(memory.structured) | {
            SUPERSEDED_HINT_KEY: candidate.summary,
            SUPERSEDED_AT_KEY: isoformat_or_none(now),
        }
        projection.upsert_memory(connection, memory)
        superseded.append(memory.memory_id)
    return superseded


def _find_newer_contradiction(
    projection: MemoryProjection, candidate: MemoryCandidate
) -> Memory | None:
    """Return a newer memory that already contradicts this older statement.

    The mirror image of :func:`_find_conflicts` for a statement that reaches the
    maintenance pass *after* the one that corrected it. The statement is still
    stored - history is not rewritten - but it is marked as replaced by the newer
    memory instead of being asserted alongside it, which is what would otherwise
    leave the character holding two opposite beliefs at once.

    Args:
        projection: Memory storage.
        candidate: The older statement being consolidated.

    Returns:
        The newest contradicting memory, or ``None``.
    """
    subject = topic_tokens(candidate.summary)
    polarity = polarity_of(candidate.summary)
    if not subject or polarity is None:
        return None
    newest: Memory | None = None
    for memory in projection.list_memories(status=MemoryStatus.ACTIVE.value, limit=200):
        if memory.kind != candidate.kind or is_superseded(memory):
            continue
        if len(subject & topic_tokens(memory.summary)) < DEDUPE_MIN_SHARED_TOKENS:
            continue
        if _is_newer(candidate.created_at, memory.created_at):
            continue
        other = polarity_of(memory.summary)
        if other is None or other == polarity:
            continue
        if newest is None or (
            memory.created_at is not None
            and (newest.created_at is None or memory.created_at > newest.created_at)
        ):
            newest = memory
    return newest


def _is_recall(cue_bigrams: int, query_match: int, matched: int) -> bool:
    """Return whether a cue brought the memory to mind rather than merely scoring it.

    Two shared bigrams with the sentence or with one situation entry is the module's
    usual bar for "these two texts are about the same thing" (deduplication and
    contradiction use the same number). A short cue is the exception: "咖啡" is one
    bigram, so a whole-question cue of that length counts when the memory contains it.

    Args:
        cue_bigrams: How many bigrams the current sentence has.
        query_match: How many of them the memory shares.
        matched: The best overlap with any cue term, the sentence included.

    Returns:
        ``True`` when the memory was actually recalled.
    """
    if query_match >= DEDUPE_MIN_SHARED_TOKENS or matched >= DEDUPE_MIN_SHARED_TOKENS:
        return True
    return 0 < cue_bigrams <= 2 and query_match == cue_bigrams


def _was_brought_to_mind(hit: RetrievalHit) -> bool:
    """Return whether a retrieval hit was recalled by a *cue* rather than by existing.

    Every memory scores something: ``0.3 * importance`` plus recency means an
    important memory clears the activation gate forever, whether or not the current
    moment has anything to do with it. The pool is supposed to hold what was brought
    to mind, so a hit counts only when one of the *matching* cue terms fired - the
    sentence, the working situation, or an open matter.

    The emotion term is deliberately not part of this test: it is
    ``cue intensity x importance``, i.e. it scales every memory by its own importance
    rather than selecting one, so counting it would make every important memory
    permanently hot - which is exactly the failure this predicate exists to prevent.

    A single shared bigram does not count either. CJK bigrams like 是/我 appear in
    almost every sentence, and one of them was enough to keep an unrelated memory
    permanently warm; the module's other decisions (deduplication, contradiction) all
    require :data:`DEDUPE_MIN_SHARED_TOKENS` shared tokens for the same reason. The
    rule itself lives in :func:`_is_recall` and arrives here as ``hit.recalled``.

    Args:
        hit: One retrieval hit.

    Returns:
        ``True`` when a cue term matched this memory meaningfully.
    """
    return bool(hit.recalled or hit.unfinished > 0.0)


def archive_stale(
    projection: MemoryProjection,
    connection: sqlite3.Connection,
    *,
    config: RuntimeConfig,
    now: datetime,
    low_threshold: float = 0.10,
) -> list[str]:
    """Archive memories whose activation has faded below a threshold.

    This is the last rung of ``active -> low_activation -> archived``: to be
    archived a memory must be both below ``low_threshold`` *and* untouched for two
    weeks. A memory that merely faded is demoted instead
    (:meth:`MemoryStore._demote_if_faded`) and can still be reinforced.

    Returns:
        Identifiers of newly archived memories.
    """
    archived: list[str] = []
    for activated in projection.list_activated(limit=200):
        age_days = 0.0
        if activated.last_recalled_at is not None:
            age_days = (now - activated.last_recalled_at).total_seconds() / 86400.0
        if activated.activation >= low_threshold or age_days < 14.0:
            continue
        memory = projection.get_memory(activated.memory_id)
        if memory is None or memory.status == MemoryStatus.ARCHIVED.value:
            continue
        projection.set_memory_status(connection, activated.memory_id, MemoryStatus.ARCHIVED.value)
        archived.append(activated.memory_id)
    return archived


# --------------------------------------------------------------------------------------
# Retrieval and activation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RetrievalCue:
    """Cues that may trigger an involuntary recall."""

    query_text: str = ""
    unfinished_titles: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    emotion_intensity: float = 0.0
    now: datetime | None = None
    #: What the working situation currently holds (facts and inferences). This is a
    #: cue of its own: an unrelated sentence can still bring back a memory, because
    #: the *situation* - not the sentence - is what it touches (design §20).
    situation_terms: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Normalise optional collections and the reference time."""
        self.unfinished_titles = list(self.unfinished_titles or [])
        self.topics = list(self.topics or [])
        if self.now is None:
            self.now = utcnow()


@dataclass(slots=True)
class RetrievalHit:
    """One scored retrieval result with its score decomposition."""

    memory: Memory
    lexical: float = 0.0
    situation: float = 0.0
    unfinished: float = 0.0
    emotion: float = 0.0
    recency: float = 0.0
    recently_recalled_penalty: float = 0.0
    epsilon: float = 0.0
    score: float = 0.0
    #: How many CJK bigrams the best-matching cue term shared with this memory. A
    #: single shared bigram is a coincidence ("是", "我" appear in everything), so
    #: callers that ask "did this moment really bring it to mind" want a count, not a
    #: flag.
    matched_tokens: int = 0
    #: Whether a cue actually brought this memory to mind: two shared bigrams with the
    #: sentence or with one situation entry (the module's usual "same subject" bar), or
    #: a short cue that the memory contains entirely ("咖啡" asked as a whole question).
    #: Callers must not re-derive this from the scores: importance and recency alone
    #: carry every memory past any threshold.
    recalled: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "memory": self.memory.to_dict(),
            "lexical": round(self.lexical, 6),
            "situation": round(self.situation, 6),
            "unfinished": round(self.unfinished, 6),
            "emotion": round(self.emotion, 6),
            "recency": round(self.recency, 6),
            "recently_recalled_penalty": round(self.recently_recalled_penalty, 6),
            "epsilon": round(self.epsilon, 6),
            "score": round(self.score, 6),
            "matched_tokens": self.matched_tokens,
            "recalled": self.recalled,
        }


class MemoryStore:
    """Retrieval over long-term memory plus the activation pool."""

    def __init__(self, projection: MemoryProjection, config: RuntimeConfig) -> None:
        """Store the projection and configuration."""
        self._projection = projection
        self._config = config

    @property
    def activation_threshold(self) -> float:
        """Return the activation a recall must reach to enter the working set."""
        return self._config.memory.activation_threshold

    def _in_working_set(self, memory: Memory) -> bool:
        """Return whether a memory may be *injected* as what is on the character's mind.

        The working set is the activation pool: memories that are currently salient.
        A faded (``low_activation``) memory is deliberately not part of it - that is
        what fading means - and archival is stronger still.
        """
        return memory.status == MemoryStatus.ACTIVE.value and not is_superseded(memory)

    def _retrievable(self, memory: Memory) -> bool:
        """Return whether a *cue* may recall this memory.

        Retrievability is not the same question as salience. A fact the user stated
        once and never repeated fades out of the working set within a couple of days,
        but it is still something the character knows, and asking about it must bring
        it back - otherwise "it remembers me" is false for every fact that was not
        mentioned in the last few hours, which is the normal case.

        So a faded memory stays retrievable and a recall puts it back into the working
        set (:meth:`activate`). Only two statements withdraw a memory completely:
        archival (the Runtime decided it is no longer part of what the character
        knows) and supersession (a newer statement replaced it).
        """
        return (
            memory.status in {MemoryStatus.ACTIVE.value, MemoryStatus.LOW_ACTIVATION.value}
            and not is_superseded(memory)
        )

    def retrieve(
        self,
        cue: RetrievalCue,
        *,
        limit: int = 8,
        rng: random.Random | None = None,
        candidates: Sequence[Memory] | None = None,
    ) -> list[RetrievalHit]:
        """Score and rank memories for a retrieval cue.

        ``Score = lexical + situation + unfinished + emotion + recency
        + 0.3 * importance - recently_recalled_penalty + epsilon``.

        Faded memories take part: they are still known, and a cue that matches one is
        exactly how a fact comes back (see :meth:`_retrievable`). Archived and
        superseded memories never do.

        Args:
            cue: Retrieval cue.
            limit: Maximum number of hits.
            rng: Random source for the exploration epsilon.
            candidates: Pre-fetched candidate memories (defaults to the recallable set).

        Returns:
            Hits ordered by descending score.
        """
        source = rng or random.Random()
        pool = list(
            candidates
            if candidates is not None
            else self._projection.list_memories(
                status=[MemoryStatus.ACTIVE.value, MemoryStatus.LOW_ACTIVATION.value],
                limit=300,
            )
        )
        # A caller may hand in its own candidate set, so the eligibility filter is
        # applied here as well: an archived or superseded memory must not be
        # retrievable by any route.
        pool = [memory for memory in pool if self._retrievable(memory)]
        if not pool:
            return []

        query_tokens = set(tokenize(cue.query_text)) | set(cue.topics or [])
        # "Was this memory really brought to mind?" is asked with CJK *bigrams*, the
        # same measure deduplication and contradiction use: ``tokenize`` splits CJK into
        # single characters, and two characters in common ("我", "是") is a coincidence
        # rather than a recollection.
        query_bigrams = topic_tokens(cue.query_text)
        # The working situation is a cue in its own right (design §20): "what is going
        # on right now" brings back what it touches. It used to be faked as a quarter
        # of the lexical score, and then - once it was real - it was a single bag of
        # every situation entry, which saturated for every memory (they are all the
        # user's own sentences and share 用户/说/我). It is scored per entry now, by the
        # best-matching one and normalised by the shorter side.
        situation_tokens: list[set[str]] = [
            set(tokenize(term)) for term in cue.situation_terms if term
        ]
        situation_bigrams: list[set[str]] = [
            topic_tokens(term) for term in cue.situation_terms if term
        ]
        unfinished_tokens: set[str] = set()
        for title in cue.unfinished_titles:
            unfinished_tokens |= set(tokenize(title))
        activations = {a.memory_id: a for a in self._projection.list_activated(limit=500)}

        hits: list[RetrievalHit] = []
        for memory in pool:
            memory_tokens = set(memory.topics) | set(tokenize(memory.summary))
            memory_bigrams = topic_tokens(memory.summary)
            lexical = 0.0
            query_match = len(query_bigrams & memory_bigrams)
            matched = query_match
            if query_tokens and memory_tokens:
                overlap = query_tokens & memory_tokens
                lexical = len(overlap) / math.sqrt(len(query_tokens) * max(1, len(memory_tokens)) / 4.0)
                lexical = clamp(lexical)
            situation = 0.0
            for entry, entry_bigrams in zip(situation_tokens, situation_bigrams):
                if not entry or not memory_tokens:
                    continue
                shared = len(entry_bigrams & memory_bigrams)
                if shared < DEDUPE_MIN_SHARED_TOKENS:
                    continue
                matched = max(matched, shared)
                ratio = shared / max(1, min(len(entry_bigrams), len(memory_bigrams)))
                situation = max(situation, clamp(ratio) * SITUATION_WEIGHT)
            unfinished = 0.0
            if unfinished_tokens and memory_tokens:
                unfinished = clamp(len(unfinished_tokens & memory_tokens) / 3.0)
            emotion = clamp(cue.emotion_intensity * memory.importance * 0.5)
            age_seconds = 0.0
            if memory.created_at is not None and cue.now is not None:
                age_seconds = max(0.0, (cue.now - memory.created_at).total_seconds())
            recency = exponential_decay(1.0, age_seconds, half_life=14 * 86400.0) * 0.20
            penalty = 0.0
            previous = activations.get(memory.memory_id)
            if previous is not None and previous.last_recalled_at is not None and cue.now is not None:
                since = max(0.0, (cue.now - previous.last_recalled_at).total_seconds())
                if since < 3600.0:
                    penalty = self._config.memory.recent_recall_penalty * (1.0 - since / 3600.0)
            epsilon = source.uniform(0.0, self._config.memory.random_epsilon)
            score = (
                lexical
                + situation
                + unfinished
                + emotion
                + recency
                + 0.3 * memory.importance
                - penalty
                + epsilon
            )
            hits.append(
                RetrievalHit(
                    memory=memory,
                    lexical=lexical,
                    situation=situation,
                    unfinished=unfinished,
                    emotion=emotion,
                    recency=recency,
                    recently_recalled_penalty=penalty,
                    epsilon=epsilon,
                    score=score,
                    matched_tokens=matched,
                    recalled=_is_recall(len(query_bigrams), query_match, matched),
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(1, limit)]

    def activate(
        self,
        connection: sqlite3.Connection,
        hits: Iterable[RetrievalHit],
        *,
        now: datetime,
        pool_size: int | None = None,
    ) -> list[ActivatedMemory]:
        """Fold retrieval hits into the activation pool.

        Activation answers "how strongly is this on the character's mind *now*", so a
        recall raises it to the strength of that recall rather than adding to a
        lifetime total::

            activation = max(activation, clamp(hit.score))

        The accumulating version this replaced (``base + (1-base) * score``) converged
        every frequently recalled memory to 1.0 and kept it there, so the pool stopped
        ranking anything - an old memory that had been recalled fifty times outranked a
        fact the user had stated a minute ago, and the prompt section filled up with
        the same four entries forever. Between recall it decays
        (:meth:`decay_pool`), which is what makes room for something new.

        A recall also *reinstates*: a memory that had faded to ``low_activation`` and
        is then brought back by a matching cue returns to ``active``. Without this,
        recall would raise an activation value that nothing ever reads again - the
        memory would be permanently excluded from the prompt while being retrieved
        every time it mattered.

        Args:
            connection: Write connection.
            hits: Retrieval hits to fold in.
            now: Reference time.
            pool_size: Maximum pool size after activation.

        Returns:
            The updated activation entries.
        """
        size = pool_size or self._config.memory.activation_pool_size
        existing = {a.memory_id: a for a in self._projection.list_activated(limit=500)}
        touched: list[ActivatedMemory] = []
        for hit in hits:
            if hit.score < self._config.memory.activation_threshold:
                continue
            if not _was_brought_to_mind(hit):
                # Importance and recency alone can carry a score past the gate, and a
                # memory that nothing recalled must not be refreshed: doing so kept
                # every important-but-irrelevant memory permanently hot, so the working
                # set stopped being a working set and the oldest entries never faded.
                continue
            current = existing.get(hit.memory.memory_id)
            base = current.activation if current is not None else 0.0
            updated = ActivatedMemory(
                memory_id=hit.memory.memory_id,
                activation=clamp(max(base, clamp(hit.score))),
                last_recalled_at=now,
                recall_count=(current.recall_count if current else 0) + 1,
                reason=f"retrieval:{round(hit.score, 3)}",
            )
            self._projection.upsert_activation(connection, updated)
            self._reinstate_if_faded(connection, hit.memory)
            touched.append(updated)

        for memory_id in self.decay_pool(connection, dt_seconds=1.0):
            LOGGER.debug("Activation dropped for %s", memory_id)

        pool = self._projection.list_activated(limit=500)
        pool.sort(key=lambda item: item.activation, reverse=True)
        for stale in pool[size:]:
            # The pool is a bounded working set: whatever falls outside the top-N by
            # activation is dropped, even when it was touched this round. Being
            # crowded out *is* leaving the working set, so the status follows the row.
            self._demote(connection, stale.memory_id)
            self._projection.delete_activation(connection, stale.memory_id)
        return touched

    def decay_pool(self, connection: sqlite3.Connection, *, dt_seconds: float) -> list[str]:
        """Decay the whole activation pool in the database.

        A memory whose activation falls below ``memory.activation_threshold`` - the
        same boundary the activation gate uses on the way in - is demoted to
        :data:`~companion_runtime.typing.MemoryStatus.LOW_ACTIVATION`. Demotion is
        the middle rung of ``active -> low_activation -> archived``: the memory
        leaves the *working set* (the unprompted prompt section and the candidate
        generator's ``memory:`` sources) while staying retrievable by a matching cue,
        which puts it back (:meth:`activate`); a restatement reinforces it
        (:func:`reinforce`).

        Args:
            connection: Write connection.
            dt_seconds: Elapsed seconds.

        Returns:
            Identifiers dropped from the pool.
        """
        removed: list[str] = []
        for activated in self._projection.list_activated(limit=500):
            value = activated.activation * exponential_decay(
                self._config.memory.activation_decay_rate, dt_seconds
            )
            if value < 1e-4:
                # The row is gone, so the memory is out of the working set - and its
                # status has to say so. Dropping the row while leaving ``active``
                # behind made the operator surface report a memory that could never be
                # recalled into the prompt as "retrievable".
                self._demote(connection, activated.memory_id)
                self._projection.delete_activation(connection, activated.memory_id)
                removed.append(activated.memory_id)
                continue
            activated.activation = value
            self._projection.upsert_activation(connection, activated)
            self._demote_if_faded(connection, activated.memory_id, activation=value)
        return removed

    def _demote(self, connection: sqlite3.Connection, memory_id: str) -> None:
        """Move a memory to ``low_activation``: known, but not on the character's mind."""
        memory = self._projection.get_memory(memory_id)
        # Only ``active`` memories are demoted: archival is a stronger statement and a
        # decay pass must not quietly undo it.
        if memory is None or memory.status != MemoryStatus.ACTIVE.value:
            return
        self._projection.set_memory_status(
            connection, memory_id, MemoryStatus.LOW_ACTIVATION.value
        )

    def _demote_if_faded(
        self, connection: sqlite3.Connection, memory_id: str, *, activation: float
    ) -> None:
        """Move an active memory to ``low_activation`` once it fades below the gate."""
        if activation >= self._config.memory.activation_threshold:
            return
        self._demote(connection, memory_id)

    def _reinstate_if_faded(self, connection: sqlite3.Connection, memory: Memory) -> None:
        """Put a faded memory back into the working set because it was just recalled.

        This is the counterpart of :meth:`_demote_if_faded`: demotion means "not on the
        character's mind", and being recalled by a matching cue is proof that it is.
        Archival is never undone here - only ``low_activation`` is.
        """
        if memory.status != MemoryStatus.LOW_ACTIVATION.value:
            return
        self._projection.set_memory_status(
            connection, memory.memory_id, MemoryStatus.ACTIVE.value
        )

    def activated_memories(self, limit: int = 8) -> list[tuple[ActivatedMemory, Memory]]:
        """Return the activation pool joined with memory content.

        Archived and superseded memories are excluded. The pool is decayed and
        bounded but never scanned for status, so without the eligibility filter a
        memory that archival had removed from what the character knows would still be
        handed to the candidate generator and the prompt - and so would a fact that a
        newer statement replaced, which would make the character assert the version it
        corrected. Faded memories are excluded as well: leaving the working set is
        what demotion means. They are still recallable by cue.
        """
        pool = self._projection.list_activated_memories(
            status=MemoryStatus.ACTIVE.value, limit=limit
        )
        memories = self._projection.get_memories([a.memory_id for a in pool])
        pairs: list[tuple[ActivatedMemory, Memory]] = []
        for activated in pool:
            memory = memories.get(activated.memory_id)
            if memory is not None and self._in_working_set(memory):
                pairs.append((activated, memory))
        return pairs

    def activation_strength(self) -> float:
        """Return mean activation of the usable pool, used as an approach-drive input.

        Only memories that could actually be recalled count: a faded or replaced
        memory still decaying in the pool is not evidence that something is on the
        character's mind.
        """
        pool = self._projection.list_activated_memories(
            status=MemoryStatus.ACTIVE.value, limit=20
        )
        memories = self._projection.get_memories([item.memory_id for item in pool])
        usable = [
            item
            for item in pool
            if (memory := memories.get(item.memory_id)) is not None and self._in_working_set(memory)
        ]
        if not usable:
            return 0.0
        return clamp(sum(item.activation for item in usable) / len(usable))


def isoformat_or_none(value: datetime | None) -> str | None:
    """Return an ISO string or ``None`` (small local helper)."""
    return value.isoformat() if value is not None else None


def build_cue(
    *,
    state: RuntimeState,
    recent_events: Sequence[RawEvent],
    unfinished: Sequence[UnfinishedMatter],
    active_emotions: Sequence[Any],
    now: datetime | None = None,
    situation_terms: Sequence[str] = (),
) -> RetrievalCue:
    """Assemble the internal retrieval cue used when nothing external arrives.

    Args:
        state: Runtime state.
        recent_events: Recent history used for the query text.
        unfinished: Live unfinished matters.
        active_emotions: Active emotion events contributing intensity.
        now: Reference time.
        situation_terms: What the working situation currently holds. It is a cue in
            its own right (design §20): "用户最近几天工作量较大" should be able to bring
            back a memory about work, even when the newest sentence is about
            something else entirely.

    Returns:
        A :class:`RetrievalCue`; no user query is required for recall to happen.
    """
    text = " ".join((event.content or "") for event in recent_events[-4:])
    intensity = max((float(getattr(e, "intensity", 0.0)) for e in active_emotions), default=0.0)
    return RetrievalCue(
        query_text=text,
        unfinished_titles=[matter.title for matter in unfinished],
        topics=[],
        emotion_intensity=clamp(intensity + abs(state.mood_valence) * 0.5),
        now=now or utcnow(),
        situation_terms=[term for term in situation_terms if term],
    )


def situation_terms(projections: Any, *, limit: int = 6) -> list[str]:
    """Return the working situation's content as recall terms.

    Args:
        projections: The Runtime's projections.
        limit: Maximum number of entries to read.

    Returns:
        The content of the current facts and inferences, newest first.
    """
    terms: list[str] = []
    for item in projections.situation.list_active(limit=limit):
        content = str(item.get("content") or "").strip()
        if content:
            terms.append(content)
    return terms


def next_consolidation_due(
    projection: MemoryProjection, *, config: RuntimeConfig, now: datetime
) -> datetime | None:
    """Return the instant the next unattended consolidation pass becomes due.

    The scheduler anchor and the work it wakes must agree on one rule, so both
    :func:`needs_consolidation` and
    :func:`companion_runtime.scheduler._maintenance_due` are computed here: a pass
    is due one ``consolidation_interval_seconds`` after the *newest* candidate the
    pass would consider - the same ``candidate_max_open`` window
    :func:`consolidate` is willing to read. A candidate with no timestamp makes the
    pass due immediately, since there is no schedule to wait for.

    Args:
        projection: Memory storage.
        config: Runtime configuration.
        now: Reference time, used only for the untimestamped case.

    Returns:
        The due instant, or ``None`` when nothing is pending.
    """
    pending = projection.pending_candidates(limit=config.memory.candidate_max_open)
    if not pending:
        return None
    newest = max((c.created_at for c in pending if c.created_at is not None), default=None)
    if newest is None:
        return now
    return newest + timedelta(seconds=config.memory.consolidation_interval_seconds)


def needs_consolidation(
    projection: MemoryProjection, *, config: RuntimeConfig, now: datetime
) -> bool:
    """Return whether a background consolidation pass is due.

    The check is time-based so the pass stays a P3 maintenance job: cheap, lazy
    and skippable. It is exactly "the anchor has been reached"
    (:func:`next_consolidation_due`), so the wake the scheduler promises is the wake
    at which this returns ``True``.
    """
    due = next_consolidation_due(projection, config=config, now=now)
    return due is not None and due <= now


