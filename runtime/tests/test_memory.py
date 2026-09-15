"""Tests for memory candidates, consolidation, retrieval and activation."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from companion_runtime import memory as memory_module
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import MemoryProjection
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    Actor,
    EventType,
    Memory,
    MemoryCandidate,
    MemoryKind,
    MemoryStatus,
    RawEvent,
    RuntimeState,
)
from companion_runtime.utility import utcnow

from conftest import BASE_TIME, build_config


def make_event(content: str, *, event_id: str = "evt_1", timestamp=None) -> RawEvent:
    """Build a user-message event for unit tests."""
    return RawEvent(
        event_id=event_id,
        event_type=EventType.USER_MESSAGE.value,
        timestamp=timestamp or BASE_TIME,
        actor=Actor.USER.value,
        conversation_id="c1",
        content=content,
    )


# --------------------------------------------------------------------------------------
# candidate scoring
# --------------------------------------------------------------------------------------


def test_preference_statement_scores_higher_than_small_talk() -> None:
    """Durable preferences outrank transient chatter."""
    config = RuntimeConfig()
    state = RuntimeState()
    preference = memory_module.score_candidate(
        text="我不喜欢别人连续追问我在干嘛",
        source_events=[],
        state=state,
        unfinished=[],
        emotion_salience=0.3,
        config=config,
    )
    chatter = memory_module.score_candidate(
        text="今天中午吃炒饭",
        source_events=[],
        state=state,
        unfinished=[],
        emotion_salience=0.0,
        config=config,
    )
    assert preference.total > chatter.total
    assert preference.transience < chatter.transience + 1e-9


def test_value_decomposition_matches_documented_formula() -> None:
    """The components follow the documented additive formula."""
    config = RuntimeConfig()
    value = memory_module.score_candidate(
        text="记住，我以后都不喜欢被连续追问",
        source_events=[],
        state=RuntimeState(),
        unfinished=[],
        emotion_salience=0.5,
        config=config,
    )
    components = value.to_dict()
    # ``total`` is the clamped, scaled sum of the six signed components.
    raw = (
        value.future_use
        + value.repetition
        + value.user_emphasis
        + value.emotional_salience
        + value.unfinished_relevance
        + value.stability
        - value.transience
    )
    assert components["total"] == pytest.approx(max(0.0, min(1.0, raw / 3.0)), abs=1e-6)
    assert value.user_emphasis > 0.0
    assert value.repetition > 0.0


def test_weak_candidate_is_not_proposed() -> None:
    """Content below the value floor never becomes a memory candidate."""
    config = build_config()
    config.memory.candidate_min_value = 0.55
    config.memory.episodic_importance = 0.0
    proposal = memory_module.propose_from_event(
        make_event("在吗"),
        state=RuntimeState(),
        unfinished=[],
        emotion_salience=0.0,
        config=config,
    )
    assert proposal is None


def test_preference_content_is_classified_as_user_preference() -> None:
    """A preference statement is stored under the preference kind."""
    proposal = memory_module.propose_from_event(
        make_event("我不喜欢别人连续追问我在干嘛"),
        state=RuntimeState(),
        unfinished=[],
        emotion_salience=0.4,
        config=RuntimeConfig(),
    )
    assert proposal is not None
    assert proposal.kind == MemoryKind.USER_PREFERENCE.value
    assert proposal.status == "pending"
    assert proposal.topics


def test_non_user_events_never_become_memories() -> None:
    """Only user messages can seed a memory candidate in this version."""
    assistant = RawEvent(
        event_id="evt_a",
        event_type=EventType.ASSISTANT_MESSAGE.value,
        timestamp=BASE_TIME,
        actor=Actor.ASSISTANT.value,
        content="我不喜欢别人连续追问我在干嘛",
    )
    assert (
        memory_module.propose_from_event(
            assistant,
            state=RuntimeState(),
            unfinished=[],
            emotion_salience=1.0,
            config=RuntimeConfig(),
        )
        is None
    )


# --------------------------------------------------------------------------------------
# consolidation
# --------------------------------------------------------------------------------------


def test_consolidation_promotes_candidate_and_seeds_activation() -> None:
    """A strong candidate becomes a dual-representation long-term memory."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    try:
        candidate = MemoryCandidate(
            candidate_id="mcd_1",
            summary="用户不喜欢被连续追问",
            kind=MemoryKind.USER_PREFERENCE.value,
            source_event_ids=["evt_1"],
            value=0.8,
            topics=["追问", "不", "喜欢"],
        )
        with db.transaction() as conn:
            projection.upsert_candidate(conn, candidate)
            result = memory_module.consolidate(projection, conn, config=config, now=BASE_TIME)
        assert len(result.consolidated) == 1
        memory = projection.get_memory(result.consolidated[0])
        assert memory is not None
        assert memory.summary == candidate.summary
        assert memory.structured["value_breakdown"] == pytest.approx(0.8)
        assert memory.status == MemoryStatus.ACTIVE.value
        assert projection.list_activated()
        refreshed = projection.get_candidate("mcd_1")
        assert refreshed.status == "consolidated"
        assert refreshed.consolidated_memory_id == memory.memory_id
    finally:
        db.close()


def test_consolidation_is_idempotent_for_the_same_content() -> None:
    """Re-running consolidation merges rather than duplicating."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    try:
        for index in range(2):
            with db.transaction() as conn:
                projection.upsert_candidate(
                    conn,
                    MemoryCandidate(
                        candidate_id=f"mcd_{index}",
                        summary="用户喜欢咖啡",
                        kind=MemoryKind.USER_PREFERENCE.value,
                        source_event_ids=["evt_1"],
                        value=0.8,
                        topics=["咖啡", "喜欢"],
                    ),
                )
        with db.transaction() as conn:
            first = memory_module.consolidate(projection, conn, config=config, now=BASE_TIME)
            second = memory_module.consolidate(projection, conn, config=config, now=BASE_TIME)
        # One row is created, the duplicate merges into it, and re-running is a
        # no-op because nothing is pending any more.
        assert len(first.consolidated) == 1
        assert first.skipped == 1
        assert second.consolidated == []
        assert len(projection.list_memories()) == 1
        statuses = {c.candidate_id: c.status for c in projection.list_candidates()}
        assert statuses == {"mcd_0": "consolidated", "mcd_1": "merged"}
        for candidate in projection.list_candidates():
            assert candidate.consolidated_memory_id == first.consolidated[0]
    finally:
        db.close()


def test_conflicting_preference_lowers_old_confidence_without_deleting() -> None:
    """New information re-describes the past instead of erasing it."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    old = Memory(
        memory_id="mem_old",
        kind=MemoryKind.USER_PREFERENCE.value,
        summary="用户不喜欢咖啡",
        topics=["咖啡"],
        importance=0.7,
        confidence=0.9,
        source_event_ids=["evt_old"],
        created_at=BASE_TIME,
    )
    try:
        with db.transaction() as conn:
            projection.upsert_memory(conn, old)
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_new",
                    summary="用户现在喜欢咖啡了",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    source_event_ids=["evt_new"],
                    value=0.8,
                    topics=["咖啡", "喜欢"],
                ),
            )
            result = memory_module.consolidate(projection, conn, config=config, now=BASE_TIME)
        assert len(result.consolidated) == 1
        survivor = projection.get_memory("mem_old")
        assert survivor is not None, "the old memory must not be deleted"
        assert survivor.confidence < 0.9
        assert survivor.structured.get("superseded_by_hint")
        assert projection.get_memory(result.consolidated[0]) is not None
    finally:
        db.close()


def test_weak_candidate_is_rejected_not_consolidated() -> None:
    """A candidate under the floor is explicitly rejected."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    config.memory.candidate_min_value = 0.9
    try:
        with db.transaction() as conn:
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_weak", summary="在吗", value=0.1, source_event_ids=["e"]
                ),
            )
            result = memory_module.consolidate(projection, conn, config=config, now=BASE_TIME)
        assert result.consolidated == []
        assert result.skipped == 1
        assert projection.get_candidate("mcd_weak").status == "rejected"
    finally:
        db.close()


def test_archival_is_not_deletion() -> None:
    """Stale memories are archived, and remain readable."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    memory = Memory(
        memory_id="mem_1",
        kind=MemoryKind.EPISODIC.value,
        summary="很久以前的事",
        importance=0.2,
        confidence=0.3,
        status=MemoryStatus.ACTIVE.value,
        created_at=BASE_TIME - timedelta(days=60),
    )
    try:
        from companion_runtime.typing import ActivatedMemory

        with db.transaction() as conn:
            projection.upsert_memory(conn, memory)
            projection.upsert_activation(
                conn,
                ActivatedMemory(
                    memory_id="mem_1",
                    activation=0.01,
                    last_recalled_at=BASE_TIME - timedelta(days=30),
                    recall_count=1,
                ),
            )
            archived = memory_module.archive_stale(
                projection, conn, config=config, now=BASE_TIME
            )
        assert archived == ["mem_1"]
        stored = projection.get_memory("mem_1")
        assert stored is not None
        assert stored.status == MemoryStatus.ARCHIVED.value
        assert stored.archived_at is not None
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# retrieval and activation
# --------------------------------------------------------------------------------------


def _seed_memories(projection: MemoryProjection, db: Database, config: RuntimeConfig) -> None:
    """Insert a small memory catalogue for retrieval tests."""
    catalogue = [
        Memory(
            memory_id="mem_interview",
            kind=MemoryKind.EPISODIC.value,
            summary="用户为这次面试准备了很久",
            topics=["面试", "准备"],
            importance=0.8,
            created_at=BASE_TIME - timedelta(days=1),
        ),
        Memory(
            memory_id="mem_interrogation",
            kind=MemoryKind.USER_PREFERENCE.value,
            summary="用户不喜欢被连续追问在干嘛",
            topics=["追问", "不喜欢"],
            importance=0.9,
            created_at=BASE_TIME - timedelta(days=5),
        ),
        Memory(
            memory_id="mem_coffee",
            kind=MemoryKind.STABLE_KNOWLEDGE.value,
            summary="用户喜欢手冲咖啡",
            topics=["咖啡", "喜欢"],
            importance=0.4,
            created_at=BASE_TIME - timedelta(days=30),
        ),
    ]
    with db.transaction() as conn:
        for memory in catalogue:
            projection.upsert_memory(conn, memory)


def test_retrieval_prefers_relevant_memories() -> None:
    """Lexical relevance plus importance ranks the right memory first."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    try:
        _seed_memories(projection, db, config)
        store = memory_module.MemoryStore(projection, config)
        cue = memory_module.RetrievalCue(
            query_text="面试 结果",
            unfinished_titles=["等待面试结果"],
            now=BASE_TIME,
        )
        hits = store.retrieve(cue, limit=3, rng=random.Random(0))
        assert hits
        assert hits[0].memory.memory_id == "mem_interview"
        assert hits[0].score >= hits[-1].score
    finally:
        db.close()


def test_retrieval_score_has_all_documented_terms() -> None:
    """The score decomposition exposes every documented contribution."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    try:
        _seed_memories(projection, db, config)
        store = memory_module.MemoryStore(projection, config)
        hits = store.retrieve(
            memory_module.RetrievalCue(query_text="面试", now=BASE_TIME),
            limit=1,
            rng=random.Random(0),
        )
        payload = hits[0].to_dict()
        for key in (
            "lexical",
            "situation",
            "unfinished",
            "emotion",
            "recency",
            "recently_recalled_penalty",
            "epsilon",
            "score",
        ):
            assert key in payload
    finally:
        db.close()


def test_recent_recall_is_penalised() -> None:
    """A just-recalled memory is suppressed briefly."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    try:
        _seed_memories(projection, db, config)
        store = memory_module.MemoryStore(projection, config)
        cue = memory_module.RetrievalCue(query_text="面试", now=BASE_TIME)
        first = store.retrieve(cue, limit=3, rng=random.Random(0))
        with db.transaction() as conn:
            store.activate(conn, first[:1], now=BASE_TIME)
        second = store.retrieve(cue, limit=3, rng=random.Random(0))
        by_id = {hit.memory.memory_id: hit for hit in second}
        assert by_id["mem_interview"].recently_recalled_penalty > 0.0
    finally:
        db.close()


def test_activation_pool_decays_and_is_bounded() -> None:
    """Activation decays over time and the pool stays bounded.

    The cue used to share a single bigram with every memory, and a memory no cue
    actually recalled is not activated at all (``_was_brought_to_mind``). The pool
    therefore stayed empty: ``len(...) <= 2`` was ``0 <= 2`` and the decay loop ran
    zero iterations, so neither half of the docstring was tested. The cues below are
    genuine recalls, so the pool is populated and both halves assert a consequence.
    """
    from companion_runtime.utility import exponential_decay

    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    config.memory.activation_pool_size = 2
    try:
        _seed_memories(projection, db, config)
        store = memory_module.MemoryStore(projection, config)

        def recall(text: str):
            """Retrieve on a real cue and fold the recall into the pool."""
            hits = store.retrieve(
                memory_module.RetrievalCue(query_text=text, now=BASE_TIME),
                limit=3,
                rng=random.Random(2),
            )
            with db.transaction() as conn:
                return store.activate(conn, hits, now=BASE_TIME)

        # A real recall puts exactly that memory on the character's mind.
        assert [item.memory_id for item in recall("面试 准备")] == ["mem_interview"]
        pool = {item.memory_id: item.activation for item in projection.list_activated(limit=10)}
        assert pool == {"mem_interview": pytest.approx(1.0, abs=0.01)}

        # ...and it decays between recalls, in proportion to the elapsed time.
        before = dict(pool)
        with db.transaction() as conn:
            store.decay_pool(conn, dt_seconds=6 * 3600.0)
        after = {item.memory_id: item.activation for item in projection.list_activated(limit=10)}
        assert set(after) == set(before)
        for memory_id, value in after.items():
            assert value < before[memory_id]
        assert after["mem_interview"] == pytest.approx(
            before["mem_interview"]
            * exponential_decay(config.memory.activation_decay_rate, 6 * 3600.0),
            rel=1e-6,
        )

        # The pool is bounded: three recalled memories with a pool size of two leave
        # exactly two, and the one crowded out leaves the working set for good.
        recall("追问 不喜欢")
        recall("咖啡 喜欢")
        final = {item.memory_id: item.activation for item in projection.list_activated(limit=10)}
        assert len(final) == 2, final
        crowded_out = {"mem_interview", "mem_interrogation", "mem_coffee"} - set(final)
        assert len(crowded_out) == 1, final
        assert (
            projection.get_memory(crowded_out.pop()).status
            != MemoryStatus.ACTIVE.value
        ), "a memory outside the bounded pool is no longer in the working set"
    finally:
        db.close()


def test_retrieval_without_query_still_recalls() -> None:
    """Endogenous recall works with no user query at all."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    try:
        _seed_memories(projection, db, config)
        store = memory_module.MemoryStore(projection, config)
        cue = memory_module.build_cue(
            state=RuntimeState(),
            recent_events=[make_event("面试有点紧张", event_id="e1")],
            unfinished=[],
            active_emotions=[],
            now=BASE_TIME,
        )
        hits = store.retrieve(cue, limit=3, rng=random.Random(0))
        assert hits
    finally:
        db.close()


def test_needs_consolidation_respects_interval() -> None:
    """Consolidation is a lazily scheduled maintenance job."""
    db = Database(":memory:")
    db.migrate()
    projection = MemoryProjection(db)
    config = RuntimeConfig()
    config.memory.consolidation_interval_seconds = 3600.0
    try:
        assert memory_module.needs_consolidation(projection, config=config, now=BASE_TIME) is False
        with db.transaction() as conn:
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_1",
                    summary="x",
                    value=0.5,
                    created_at=BASE_TIME,
                ),
            )
        assert memory_module.needs_consolidation(
            projection, config=config, now=BASE_TIME + timedelta(minutes=30)
        ) is False
        assert memory_module.needs_consolidation(
            projection, config=config, now=BASE_TIME + timedelta(hours=2)
        ) is True
    finally:
        db.close()


def test_kind_importance_ordering() -> None:
    """Stable knowledge and preferences outrank episodic detail."""
    config = RuntimeConfig()
    assert memory_module.kind_importance(
        MemoryKind.STABLE_KNOWLEDGE.value, config
    ) > memory_module.kind_importance(MemoryKind.EPISODIC.value, config)
    assert memory_module.kind_importance("unknown-kind", config) == (
        config.memory.episodic_importance
    )


def test_runtime_creates_memory_candidate_from_preference(runtime: Runtime) -> None:
    """The foreground path turns a stated preference into a candidate."""
    outcome = runtime.process_user_message(
        content="记住，我不喜欢别人连续追问我在干嘛。", timestamp=BASE_TIME
    )
    assert outcome.memory_candidate_id is not None
    candidate = runtime.projections.memory.get_candidate(outcome.memory_candidate_id)
    assert candidate is not None
    assert candidate.kind == MemoryKind.USER_PREFERENCE.value
