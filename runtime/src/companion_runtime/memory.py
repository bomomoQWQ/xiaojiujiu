"""Memory system: candidates, long-term memories, retrieval and activation.

The full chain is::

    raw events -> working situation -> memory candidates -> consolidation
                -> long-term memory -> retrieval (lexical RAG) -> activation pool

Two deliberate simplifications for the first version:

* retrieval uses an FTS-like lexical overlap score instead of embeddings, with a
  clear seam (:meth:`MemoryStore.retrieve`) for an embedding sidecar;
* forgetting means *archival*, never deletion.

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
from .utility import clamp, exponential_decay, summarize_text, tokenize, utcnow

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
        source_events: Events the candidate derives from.
        state: Runtime state (values modulate importance).
        unfinished: Live unfinished matters.
        emotion_salience: Peak emotional intensity around the source events.
        config: Runtime configuration.

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
) -> MemoryCandidate | None:
    """Build a memory candidate from one event, or ``None`` when too weak.

    Args:
        event: Source event.
        state: Runtime state.
        unfinished: Live unfinished matters.
        emotion_salience: Peak emotional intensity around the event.
        config: Runtime configuration.

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
    if any(marker in lowered for marker in PREFERENCE_MARKERS):
        kind = MemoryKind.USER_PREFERENCE.value
    elif any(marker in lowered for marker in ("我叫", "我是", "我的生日", "住在", "工作是")):
        kind = MemoryKind.STABLE_KNOWLEDGE.value

    return MemoryCandidate(
        candidate_id=new_id("memory_candidate"),
        summary=summarize_text(text),
        kind=kind,
        source_event_ids=[event.event_id],
        value=value.total,
        status="pending",
        created_at=utcnow(),
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

    Conflicting new information does not delete the old memory; it lowers its
    confidence and links the new one through the ``supersedes`` structured field,
    so the pair can later be re-described as "used to ... but now ...".

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
    # Prefer high-value, then strict ordering for determinism.
    pending = sorted(pending, key=lambda c: (-c.value, c.created_at or stamp))[:limit]

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
            # memory instead of storing a near-duplicate row.
            existing.importance = clamp(existing.importance + 0.05 * candidate.value)
            existing.confidence = clamp(existing.confidence + 0.05)
            existing.source_event_ids = sorted(
                set(existing.source_event_ids) | set(candidate.source_event_ids)
            )
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
        if summarizer is not None:
            try:
                summary = str(summarizer(candidate)) or summary
            except Exception:  # pragma: no cover - defensive boundary around a model
                LOGGER.exception("Memory summarizer failed; keeping candidate summary")

        supersedes = _find_conflicts(projection, candidate, connection)
        importance = clamp(
            0.5 * kind_importance(candidate.kind, config) + 0.5 * candidate.value
        )
        memory = Memory(
            memory_id=new_id("memory"),
            kind=candidate.kind,
            summary=summary,
            structured={
                "value_breakdown": candidate.value,
                "confidence": candidate.confidence,
                "supersedes": supersedes,
                "topics": candidate.topics,
            },
            topics=list(candidate.topics),
            importance=importance,
            confidence=candidate.confidence,
            status=MemoryStatus.ACTIVE.value,
            source_event_ids=list(candidate.source_event_ids),
            created_at=stamp,
            updated_at=stamp,
        )
        projection.upsert_memory(connection, memory)
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


def _find_duplicate(projection: MemoryProjection, candidate: MemoryCandidate) -> Memory | None:
    """Return an existing memory with the same content, if any.

    Deduplication is lexical: the same source-event set, or an identical summary,
    means the fact is already remembered.
    """
    sources = set(candidate.source_event_ids)
    for memory in projection.list_memories(
        status=[MemoryStatus.ACTIVE.value, MemoryStatus.LOW_ACTIVATION.value], limit=300
    ):
        if memory.summary.strip() == candidate.summary.strip():
            return memory
        if sources and sources & set(memory.source_event_ids):
            return memory
    return None


def _find_conflicts(
    projection: MemoryProjection, candidate: MemoryCandidate, connection: sqlite3.Connection
) -> list[str]:
    """Find existing memories that the candidate appears to supersede.

    A conflict is a same-kind memory from the same topic set with opposite
    polarity markers (e.g. "likes coffee" now vs "doesn't like coffee" before).

    Returns:
        Identifiers of superseded memories, whose confidence is lowered.
    """
    candidate_tokens = set(candidate.topics)
    if not candidate_tokens:
        return []
    lowered = candidate.summary.lower()
    polarity = None
    if any(marker in lowered for marker in ("不喜欢", "讨厌", "不再", "don't", "hate", "no longer")):
        polarity = "negative"
    elif any(marker in lowered for marker in ("喜欢", "love", "like", "prefer")):
        polarity = "positive"
    if polarity is None:
        return []

    superseded: list[str] = []
    for memory in projection.list_memories(status=MemoryStatus.ACTIVE.value, limit=200):
        if memory.kind != candidate.kind:
            continue
        overlap = len(candidate_tokens & set(memory.topics))
        if overlap < 1:
            continue
        other = memory.summary.lower()
        other_polarity = None
        if any(marker in other for marker in ("不喜欢", "讨厌", "不再", "don't", "hate", "no longer")):
            other_polarity = "negative"
        elif any(marker in other for marker in ("喜欢", "love", "like", "prefer")):
            other_polarity = "positive"
        if other_polarity is None or other_polarity == polarity:
            continue
        memory.confidence = clamp(memory.confidence * 0.8)
        memory.structured = dict(memory.structured) | {
            "superseded_by_hint": candidate.summary,
            "superseded_at": isoformat_or_none(utcnow()),
        }
        projection.upsert_memory(connection, memory)
        superseded.append(memory.memory_id)
    return superseded


def archive_stale(
    projection: MemoryProjection,
    connection: sqlite3.Connection,
    *,
    config: RuntimeConfig,
    now: datetime,
    low_threshold: float = 0.10,
) -> list[str]:
    """Archive memories whose activation has faded below a threshold.

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
        }


class MemoryStore:
    """Retrieval over long-term memory plus the activation pool."""

    def __init__(self, projection: MemoryProjection, config: RuntimeConfig) -> None:
        """Store the projection and configuration."""
        self._projection = projection
        self._config = config

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
        - recently_recalled_penalty + epsilon``.

        Args:
            cue: Retrieval cue.
            limit: Maximum number of hits.
            rng: Random source for the exploration epsilon.
            candidates: Pre-fetched candidate memories (defaults to active set).

        Returns:
            Hits ordered by descending score.
        """
        source = rng or random.Random()
        pool = list(candidates or self._projection.list_memories(limit=300))
        if not pool:
            return []

        query_tokens = set(tokenize(cue.query_text)) | set(cue.topics or [])
        unfinished_tokens: set[str] = set()
        for title in cue.unfinished_titles:
            unfinished_tokens |= set(tokenize(title))
        activations = {a.memory_id: a for a in self._projection.list_activated(limit=500)}

        hits: list[RetrievalHit] = []
        for memory in pool:
            memory_tokens = set(memory.topics) | set(tokenize(memory.summary))
            lexical = 0.0
            if query_tokens and memory_tokens:
                overlap = query_tokens & memory_tokens
                lexical = len(overlap) / math.sqrt(len(query_tokens) * max(1, len(memory_tokens)) / 4.0)
                lexical = clamp(lexical)
            situation = clamp(0.25 * lexical) if query_tokens else 0.0
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
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(1, limit)]

    def tick_activation(self, *, dt_seconds: float) -> list[str]:
        """Compute decayed activation values for the pool (pure calculation).

        Args:
            dt_seconds: Elapsed seconds.

        Returns:
            Memory identifiers whose activation has fallen to zero.
        """
        if dt_seconds <= 0.0:
            return []
        factor = exponential_decay(self._config.memory.activation_decay_rate, dt_seconds)
        dead: list[str] = []
        for activated in self._projection.list_activated(limit=500):
            value = activated.activation * factor
            activated.activation = value
            if value < 1e-4:
                dead.append(activated.memory_id)
        return dead

    def activate(
        self,
        connection: sqlite3.Connection,
        hits: Iterable[RetrievalHit],
        *,
        now: datetime,
        pool_size: int | None = None,
    ) -> list[ActivatedMemory]:
        """Fold retrieval hits into the activation pool.

        Activation accumulates with a saturating update and a recall is recorded,
        so the same memory is not recalled again immediately.

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
            current = existing.get(hit.memory.memory_id)
            base = current.activation if current is not None else 0.0
            updated = ActivatedMemory(
                memory_id=hit.memory.memory_id,
                activation=clamp(base + (1.0 - base) * clamp(hit.score)),
                last_recalled_at=now,
                recall_count=(current.recall_count if current else 0) + 1,
                reason=f"retrieval:{round(hit.score, 3)}",
            )
            self._projection.upsert_activation(connection, updated)
            touched.append(updated)

        for memory_id in self.decay_pool(connection, dt_seconds=1.0):
            LOGGER.debug("Activation dropped for %s", memory_id)

        pool = self._projection.list_activated(limit=500)
        pool.sort(key=lambda item: item.activation, reverse=True)
        for stale in pool[size:]:
            # The pool is a bounded working set: whatever falls outside the top-N
            # by activation is dropped, even when it was touched this round.
            self._projection.delete_activation(connection, stale.memory_id)
        return touched

    def decay_pool(self, connection: sqlite3.Connection, *, dt_seconds: float) -> list[str]:
        """Decay the whole activation pool in the database.

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
                self._projection.delete_activation(connection, activated.memory_id)
                removed.append(activated.memory_id)
                continue
            activated.activation = value
            self._projection.upsert_activation(connection, activated)
        return removed

    def activated_memories(self, limit: int = 8) -> list[tuple[ActivatedMemory, Memory]]:
        """Return the activation pool joined with memory content."""
        pool = self._projection.list_activated(limit=limit)
        memories = self._projection.get_memories([a.memory_id for a in pool])
        pairs: list[tuple[ActivatedMemory, Memory]] = []
        for activated in pool:
            memory = memories.get(activated.memory_id)
            if memory is not None:
                pairs.append((activated, memory))
        return pairs

    def activation_strength(self) -> float:
        """Return mean activation of the pool, used as an approach-drive input."""
        pool = self._projection.list_activated(limit=20)
        if not pool:
            return 0.0
        return clamp(sum(item.activation for item in pool) / len(pool))


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
) -> RetrievalCue:
    """Assemble the internal retrieval cue used when nothing external arrives.

    Args:
        state: Runtime state.
        recent_events: Recent history used for the query text.
        unfinished: Live unfinished matters.
        active_emotions: Active emotion events contributing intensity.
        now: Reference time.

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
    )


def needs_consolidation(
    projection: MemoryProjection, *, config: RuntimeConfig, now: datetime
) -> bool:
    """Return whether a background consolidation pass is due.

    The check is time-based so the pass stays a P3 maintenance job: cheap, lazy
    and skippable.
    """
    pending = projection.pending_candidates(limit=config.memory.candidate_max_open)
    if not pending:
        return False
    newest = max((c.created_at for c in pending if c.created_at is not None), default=None)
    if newest is None:
        return True
    return (now - newest).total_seconds() >= config.memory.consolidation_interval_seconds


def pool_times(memories: Sequence[Memory]) -> list[datetime]:
    """Return creation times of memories (helper for scheduling)."""
    return [m.created_at for m in memories if m.created_at is not None]
