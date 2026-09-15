"""Regression tests for the memory pipeline: formation, deduplication, forgetting.

The audit (``docs/audit/`` block A §13-§20, block C "必须补的" #1) found that the
``memories`` table had no writer in a default deployment, that deduplication could
never merge a near-identical restatement whose topics differed, that
``supersedes``/``superseded_by_hint`` were written and never read, and that
``MemoryStatus.LOW_ACTIVATION`` had no assignment point anywhere.

Each test states the invariant it protects:

* long-term memory must form with no provider configured and no key present;
* a restatement above the exact threshold merges on content alone, and topic
  overlap is required only below it;
* a superseded memory must not be recalled or injected, and must say why;
* a faded memory leaves the working set but is recoverable by reinforcement;
* the maintenance wake the scheduler promises is the wake that does the work;
* every documented return shape matches the code.
"""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest

from companion_runtime import context as context_module
from companion_runtime import memory as memory_module
from companion_runtime import scheduler as scheduler_module
from companion_runtime.cli import main as cli_main
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import MemoryProjection
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    ActivatedMemory,
    Memory,
    MemoryCandidate,
    MemoryKind,
    MemoryStatus,
)
from companion_runtime.utility import exponential_decay

from conftest import BASE_TIME, build_config

#: The preference statement used throughout: it clears ``candidate_min_value`` and
#: is classified as a user preference, which is the kind the pipeline classifies
#: most reliably.
PREFERENCE_TEXT = "记住，我不喜欢别人连续追问我在干嘛。"


def _memory_projection() -> tuple[Database, MemoryProjection]:
    """Return a migrated in-memory database and its memory projection."""
    db = Database(":memory:")
    db.migrate()
    return db, MemoryProjection(db)


def _stored_memory(
    *,
    summary: str,
    topics: list[str],
    memory_id: str = "mem_stored",
) -> Memory:
    """Build one stored preference memory for the deduplication tests."""
    return Memory(
        memory_id=memory_id,
        kind=MemoryKind.USER_PREFERENCE.value,
        summary=summary,
        topics=list(topics),
        importance=0.7,
        confidence=0.8,
        source_event_ids=["evt_stored"],
        created_at=BASE_TIME,
    )


def _consolidate_against(
    *,
    stored_summary: str,
    stored_topics: list[str],
    candidate_summary: str,
    candidate_topics: list[str],
) -> tuple[list[Memory], int]:
    """Consolidate one candidate against one stored memory.

    Args:
        stored_summary: Summary of the memory already in the archive.
        stored_topics: Topic tags of the stored memory.
        candidate_summary: Summary of the pending candidate.
        candidate_topics: Topic tags of the pending candidate.

    Returns:
        The memories that exist afterwards, and how many candidates were skipped
        (a skip is what a successful merge looks like).
    """
    db, projection = _memory_projection()
    config = RuntimeConfig()
    try:
        with db.transaction() as conn:
            projection.upsert_memory(
                conn,
                _stored_memory(summary=stored_summary, topics=stored_topics),
            )
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_new",
                    summary=candidate_summary,
                    kind=MemoryKind.USER_PREFERENCE.value,
                    source_event_ids=["evt_new"],
                    value=0.8,
                    topics=list(candidate_topics),
                    created_at=BASE_TIME,
                ),
            )
            result = memory_module.consolidate(projection, conn, config=config, now=BASE_TIME)
        return projection.list_memories(), result.skipped
    finally:
        db.close()


@pytest.fixture()
def client(runtime: Runtime) -> Iterator[Any]:
    """A TestClient bound to the Runtime under test, when FastAPI is installed."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from companion_runtime.api import create_app

    with TestClient(create_app(runtime, runtime.config)) as test_client:
        yield test_client


# --------------------------------------------------------------------------------------
# 1. memories form with no model at all
# --------------------------------------------------------------------------------------


def test_a_default_deployment_forms_memories_without_any_provider(runtime: Runtime) -> None:
    """An unattended round must promote a candidate with the shipped defaults.

    The shipped default is ``semantic.provider = "disabled"``: if memory formation
    depended on a model, ``/memories`` would stay empty forever in every standard
    deployment.
    """
    assert runtime.semantic_provider.available() is False

    ingested = runtime.process_user_message(content=PREFERENCE_TEXT, timestamp=BASE_TIME)
    assert ingested.memory_candidate_id is not None

    outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(hours=2), force=True)

    assert outcome.consolidation["ran"] is True
    assert outcome.consolidation["consolidated"]
    memories = runtime.projections.memory.list_memories()
    assert [memory.summary for memory in memories] == [PREFERENCE_TEXT]
    # Provenance must not claim a model wrote what the rules extracted.
    assert memories[0].structured["proposed_by"] == memory_module.PROVENANCE_RULE
    # The activation pool is seeded by the same pass, which is what later lets the
    # memory be recalled and injected without any query arriving.
    assert runtime.projections.memory.list_activated()
    assert runtime.projections.memory.get_candidate(ingested.memory_candidate_id).status == (
        "consolidated"
    )


def test_the_endogenous_round_skips_a_pass_that_is_not_due(runtime: Runtime) -> None:
    """Consolidation stays a lazy P3 job: "not due" is reported, not hidden."""
    outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
    assert outcome.consolidation == {"ran": False, "reason": "not_due"}
    assert runtime.projections.memory.list_memories() == []


def test_consolidation_waits_for_the_configured_interval(runtime: Runtime) -> None:
    """A fresh candidate is not promoted before the interval has elapsed."""
    runtime.process_user_message(content=PREFERENCE_TEXT, timestamp=BASE_TIME)

    early = runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
    assert early.consolidation["ran"] is False
    assert runtime.projections.memory.list_memories() == []

    due = runtime.endogenous_round(now=BASE_TIME + timedelta(hours=2), force=True)
    assert due.consolidation["ran"] is True
    assert len(runtime.projections.memory.list_memories()) == 1


def test_a_failing_consolidation_never_breaks_the_round(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proactive decision matters more than the maintenance pass."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("consolidation exploded")

    monkeypatch.setattr(memory_module, "consolidate", _explode)
    runtime.process_user_message(content=PREFERENCE_TEXT, timestamp=BASE_TIME)

    outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(hours=2), force=True)

    assert outcome.consolidation == {"ran": False, "reason": "error"}
    assert "acted" in outcome.decision["outcome"]


def test_rule_based_provenance_is_recorded_for_rule_summaries() -> None:
    """A summarizer that keeps the candidate's own text is not a model author."""
    db, projection = _memory_projection()
    try:
        with db.transaction() as conn:
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_rule",
                    summary="用户喜欢手冲咖啡",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    value=0.8,
                    topics=["咖啡"],
                    created_at=BASE_TIME,
                ),
            )
            result = memory_module.consolidate(
                projection,
                conn,
                config=RuntimeConfig(),
                now=BASE_TIME,
                summarizer=lambda candidate: candidate.summary,
            )
        stored = projection.get_memory(result.consolidated[0])
        assert stored.summary == "用户喜欢手冲咖啡"
        assert stored.structured["proposed_by"] == memory_module.PROVENANCE_RULE
    finally:
        db.close()


def test_model_written_summary_is_recorded_as_model_provenance() -> None:
    """When a model does write the summary, the memory says so."""
    db, projection = _memory_projection()
    try:
        with db.transaction() as conn:
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_model",
                    summary="用户喜欢手冲咖啡",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    value=0.8,
                    topics=["咖啡"],
                    created_at=BASE_TIME,
                ),
            )
            result = memory_module.consolidate(
                projection,
                conn,
                config=RuntimeConfig(),
                now=BASE_TIME,
                summarizer=lambda candidate: "用户偏好手冲咖啡，并多次强调。",
            )
        stored = projection.get_memory(result.consolidated[0])
        assert stored.summary == "用户偏好手冲咖啡，并多次强调。"
        assert stored.structured["proposed_by"] == memory_module.PROVENANCE_SEMANTIC_API
    finally:
        db.close()


def test_the_maintenance_anchor_is_the_instant_the_round_consolidates(runtime: Runtime) -> None:
    """The wake the scheduler promises is the wake that actually does the work.

    ``scheduler._maintenance_due`` and ``memory.needs_consolidation`` are two views
    of one rule; if they disagree, the scheduler promises a maintenance wake that
    the round then skips.
    """
    runtime.process_user_message(content=PREFERENCE_TEXT, timestamp=BASE_TIME)
    due = scheduler_module._maintenance_due(runtime, BASE_TIME)

    interval = timedelta(seconds=runtime.config.memory.consolidation_interval_seconds)
    assert due == BASE_TIME + interval
    assert (
        memory_module.needs_consolidation(
            runtime.projections.memory, config=runtime.config, now=due - timedelta(seconds=1)
        )
        is False
    )
    assert (
        memory_module.needs_consolidation(
            runtime.projections.memory, config=runtime.config, now=due
        )
        is True
    )

    woken = runtime.endogenous_round(now=due, force=True)
    assert woken.consolidation["ran"] is True
    assert len(runtime.projections.memory.list_memories()) == 1


def test_the_cli_can_run_one_consolidation_pass_by_hand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``companion-runtime consolidate`` promotes candidates with no provider."""
    config = build_config()
    config.storage.database_path = str(tmp_path / "data" / "runtime.sqlite3")
    runtime = Runtime(config=config, seed=11, created_at=BASE_TIME)
    try:
        runtime.process_user_message(content=PREFERENCE_TEXT, timestamp=BASE_TIME)
    finally:
        runtime.close()

    code = cli_main(
        [
            "--base-dir",
            str(tmp_path),
            "consolidate",
            "--now",
            (BASE_TIME + timedelta(hours=2)).isoformat(),
        ]
    )
    assert code == 0
    reported = capsys.readouterr().out.strip()
    assert '"consolidated"' in reported

    reopened = Runtime(config=config, seed=11, created_at=BASE_TIME)
    try:
        assert [memory.summary for memory in reopened.projections.memory.list_memories()] == [
            PREFERENCE_TEXT
        ]
    finally:
        reopened.close()


def test_memories_are_visible_through_the_operator_surface(client: Any, runtime: Runtime) -> None:
    """What formed must be inspectable: ``/memories`` is the operator's view."""
    runtime.process_user_message(content=PREFERENCE_TEXT, timestamp=BASE_TIME)
    runtime.endogenous_round(now=BASE_TIME + timedelta(hours=2), force=True)

    body = client.get("/memories").json()

    assert [memory["summary"] for memory in body["memories"]] == [PREFERENCE_TEXT]
    assert body["memories"][0]["retrievable"] is True
    assert body["memories"][0]["retrieval_reason"] == "active"
    assert body["activated"], "a formed memory must seed the activation pool"


# --------------------------------------------------------------------------------------
# 2. deduplication is decided by content
# --------------------------------------------------------------------------------------


def test_a_near_identical_restatement_merges_even_when_topics_differ() -> None:
    """Above ``DEDUPE_EXACT_RATIO`` the text alone decides: topic tags drift.

    The thresholds are ordered ``DEDUPE_TOPIC_RATIO < DEDUPE_EXACT_RATIO``, so the
    exact band must be checked *before* the topic-overlap requirement - otherwise
    the exact band can never change the outcome and the documented case "an
    identical summary merges" does not exist.
    """
    assert memory_module.DEDUPE_TOPIC_RATIO < memory_module.DEDUPE_EXACT_RATIO
    memories, skipped = _consolidate_against(
        stored_summary="用户喜欢手冲咖啡。",
        stored_topics=["饮品"],
        candidate_summary="用户喜欢手冲咖啡",
        candidate_topics=["咖啡"],
    )

    assert skipped == 1, "a restatement at ratio 1.0 must merge, whatever the topics"
    assert len(memories) == 1
    assert memories[0].memory_id == "mem_stored"
    # A merge is a reinforcement of the existing row, not a new one: the evidence
    # accumulates on the row that survived.
    assert memories[0].source_event_ids == ["evt_new", "evt_stored"]


def test_a_merely_similar_restatement_merges_only_with_a_shared_topic() -> None:
    """Between the two thresholds, the shared topic is what proves one fact.

    The same pair is consolidated twice: once with disjoint topic tags (two facts
    stay two facts) and once with an overlapping tag (the restatement merges).
    """
    ratio = memory_module._similarity(
        "用户不喜欢别人连续追问我在干嘛", "用户不喜欢别人连续追问我干嘛"
    )
    assert memory_module.DEDUPE_TOPIC_RATIO <= ratio < memory_module.DEDUPE_EXACT_RATIO

    apart, skipped_apart = _consolidate_against(
        stored_summary="用户不喜欢别人连续追问我在干嘛",
        stored_topics=["追问"],
        candidate_summary="用户不喜欢别人连续追问我干嘛",
        candidate_topics=["连续"],
    )
    assert skipped_apart == 0
    assert len(apart) == 2, "without a shared topic the two rows are two facts"

    merged, skipped_merged = _consolidate_against(
        stored_summary="用户不喜欢别人连续追问我在干嘛",
        stored_topics=["追问", "边界"],
        candidate_summary="用户不喜欢别人连续追问我干嘛",
        candidate_topics=["追问"],
    )
    assert skipped_merged == 1
    assert len(merged) == 1, "with a shared topic the restatement is one fact"


def test_distinct_facts_from_one_message_are_still_not_duplicates() -> None:
    """Sharing a source event is not sharing a fact (the property to preserve).

    Both candidates below come from one message and therefore cite the same event
    and the same topic tag; their content is what keeps them apart.
    """
    db, projection = _memory_projection()
    try:
        with db.transaction() as conn:
            for index, summary in enumerate(
                ["用户不喜欢被连续追问在干嘛", "用户的生日是三月三号"]
            ):
                projection.upsert_candidate(
                    conn,
                    MemoryCandidate(
                        candidate_id=f"mcd_{index}",
                        summary=summary,
                        kind=MemoryKind.EPISODIC.value,
                        source_event_ids=["evt_shared"],
                        value=0.8,
                        topics=["用户"],
                        created_at=BASE_TIME,
                    ),
                )
            result = memory_module.consolidate(
                projection, conn, config=RuntimeConfig(), now=BASE_TIME
            )
        assert len(result.consolidated) == 2
        assert {memory.summary for memory in projection.list_memories()} == {
            "用户不喜欢被连续追问在干嘛",
            "用户的生日是三月三号",
        }
    finally:
        db.close()


def test_a_superseded_memory_is_never_a_merge_target() -> None:
    """A statement must not be swallowed by a row nobody can retrieve again."""
    db, projection = _memory_projection()
    try:
        with db.transaction() as conn:
            projection.upsert_memory(
                conn,
                Memory(
                    memory_id="mem_old",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    summary="用户喜欢咖啡",
                    topics=["咖啡"],
                    importance=0.7,
                    confidence=0.8,
                    structured={
                        memory_module.SUPERSEDED_HINT_KEY: "用户不再喜欢咖啡",
                        memory_module.SUPERSEDED_AT_KEY: BASE_TIME.isoformat(),
                    },
                    created_at=BASE_TIME,
                ),
            )
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_again",
                    summary="用户喜欢咖啡",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    source_event_ids=["evt_again"],
                    value=0.8,
                    topics=["咖啡"],
                    created_at=BASE_TIME,
                ),
            )
            result = memory_module.consolidate(
                projection, conn, config=RuntimeConfig(), now=BASE_TIME
            )
        assert len(result.consolidated) == 1, "the restatement needs its own live row"
        assert projection.get_memory(result.consolidated[0]).summary == "用户喜欢咖啡"
        assert projection.get_candidate("mcd_again").status == "consolidated"
    finally:
        db.close()


def test_two_unrelated_preferences_are_not_a_contradiction() -> None:
    """Withdrawal needs a shared subject, not a shared verb.

    The stored negative preference and the new positive one are both tagged with the
    same single characters (我/喜/欢), so a topic-tag test would call them a
    contradiction and withdraw the first. "About the same thing" is a bigram
    question, and these two are about different things.
    """
    db, projection = _memory_projection()
    try:
        with db.transaction() as conn:
            projection.upsert_memory(
                conn,
                Memory(
                    memory_id="mem_interrogation",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    summary="记住，我不喜欢别人连续追问我在干嘛。",
                    topics=["记", "住", "我", "不", "喜", "欢"],
                    importance=0.7,
                    confidence=0.8,
                    created_at=BASE_TIME,
                ),
            )
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_coffee",
                    summary="我平时喜欢手冲咖啡，不加糖。",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    source_event_ids=["evt_coffee"],
                    value=0.8,
                    topics=["我", "平", "时", "喜", "欢", "手"],
                    created_at=BASE_TIME,
                ),
            )
            result = memory_module.consolidate(
                projection, conn, config=RuntimeConfig(), now=BASE_TIME
            )
        assert len(result.consolidated) == 1
        survivor = projection.get_memory("mem_interrogation")
        assert survivor.confidence == 0.8, "an unrelated statement must not lower it"
        assert memory_module.is_superseded(survivor) is False
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# 3. a superseded memory leaves retrieval, activation and the prompt
# --------------------------------------------------------------------------------------


def test_a_superseded_memory_is_withdrawn_and_says_why(runtime: Runtime) -> None:
    """The read side of ``supersedes``/``superseded_by_hint``.

    A contradiction recorded by consolidation must actually change what the
    character recalls: the replaced fact stops being retrieved and stops being
    injected, while staying in the database and stating its reason to an operator.
    """
    projections = runtime.projections
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
    with runtime.db.transaction() as conn:
        projections.memory.upsert_memory(conn, old)
        projections.memory.upsert_activation(
            conn,
            ActivatedMemory(
                memory_id="mem_old",
                activation=0.9,
                last_recalled_at=BASE_TIME,
                recall_count=2,
            ),
        )
        projections.memory.upsert_candidate(
            conn,
            MemoryCandidate(
                candidate_id="mcd_new",
                summary="用户现在喜欢咖啡了",
                kind=MemoryKind.USER_PREFERENCE.value,
                source_event_ids=["evt_new"],
                value=0.8,
                topics=["咖啡", "喜欢"],
                created_at=BASE_TIME,
            ),
        )
        result = memory_module.consolidate(
            projections.memory, conn, config=runtime.config, now=BASE_TIME
        )
    assert len(result.consolidated) == 1
    new_id = result.consolidated[0]

    replaced = projections.memory.get_memory("mem_old")
    assert replaced is not None, "the replaced fact stays in the archive"
    assert memory_module.is_superseded(replaced)

    cue = memory_module.RetrievalCue(query_text="咖啡", now=BASE_TIME)
    recalled = [
        hit.memory.memory_id
        for hit in runtime.memory_store.retrieve(cue, rng=random.Random(0))
    ]
    assert recalled == [new_id], "the replaced fact must not be retrieved"

    activated = [
        memory.memory_id for _, memory in runtime.memory_store.activated_memories(limit=8)
    ]
    assert activated == [new_id], "the replaced fact must not enter the activation pool"

    bundle = context_module.build(runtime=runtime, now=BASE_TIME)
    summaries = [item["summary"] for item in bundle.memories]
    assert summaries == ["用户现在喜欢咖啡了"]
    block = context_module.render_block(bundle)
    assert "用户不喜欢咖啡" not in block
    assert "用户现在喜欢咖啡了" in block, "the surviving fact must still be injected"

    record = memory_module.supersession_record(replaced)
    assert record["superseded"] is True
    assert record["retrievable"] is False
    assert record["retrieval_reason"] == memory_module.SUPERSEDED_REASON
    assert record["superseded_by_hint"] == "用户现在喜欢咖啡了"


def test_the_reason_a_memory_is_withheld_is_visible_in_memories(
    client: Any, runtime: Runtime
) -> None:
    """``/memories`` must state why a stored fact can no longer be recalled."""
    projections = runtime.projections
    with runtime.db.transaction() as conn:
        projections.memory.upsert_memory(
            conn,
            Memory(
                memory_id="mem_old",
                kind=MemoryKind.USER_PREFERENCE.value,
                summary="用户不喜欢咖啡",
                topics=["咖啡"],
                importance=0.7,
                confidence=0.9,
                created_at=BASE_TIME,
            ),
        )
        projections.memory.upsert_candidate(
            conn,
            MemoryCandidate(
                candidate_id="mcd_new",
                summary="用户现在喜欢咖啡了",
                kind=MemoryKind.USER_PREFERENCE.value,
                source_event_ids=["evt_new"],
                value=0.8,
                topics=["咖啡", "喜欢"],
                created_at=BASE_TIME,
            ),
        )
        memory_module.consolidate(
            projections.memory, conn, config=runtime.config, now=BASE_TIME
        )

    body = client.get("/memories").json()
    by_id = {memory["memory_id"]: memory for memory in body["memories"]}

    assert by_id["mem_old"]["retrievable"] is False
    assert by_id["mem_old"]["retrieval_reason"] == memory_module.SUPERSEDED_REASON
    assert by_id["mem_old"]["superseded_by_hint"] == "用户现在喜欢咖啡了"
    assert by_id["mem_old"]["supersedes"] == [], "the replaced row replaced nothing"
    survivor = next(
        memory for memory in body["memories"] if memory["memory_id"] != "mem_old"
    )
    assert survivor["retrievable"] is True
    assert survivor["supersedes"] == ["mem_old"]


# --------------------------------------------------------------------------------------
# 4. forgetting: active -> low_activation -> archived
# --------------------------------------------------------------------------------------


def _seed_faded_memory(
    projection: MemoryProjection,
    db: Database,
    *,
    activation: float = 0.5,
    memory_id: str = "mem_coffee",
) -> None:
    """Store one live memory with the given activation."""
    with db.transaction() as conn:
        projection.upsert_memory(
            conn,
            Memory(
                memory_id=memory_id,
                kind=MemoryKind.STABLE_KNOWLEDGE.value,
                summary="用户喜欢手冲咖啡",
                topics=["咖啡"],
                importance=0.7,
                confidence=0.8,
                created_at=BASE_TIME - timedelta(days=1),
            ),
        )
        projection.upsert_activation(
            conn,
            ActivatedMemory(
                memory_id=memory_id,
                activation=activation,
                last_recalled_at=BASE_TIME,
                recall_count=1,
            ),
        )


def test_a_faded_memory_is_demoted_and_a_restatement_brings_it_back() -> None:
    """``LOW_ACTIVATION`` is reachable, and it is a demotion rather than a loss."""
    db, projection = _memory_projection()
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projection, config)
    cue = memory_module.RetrievalCue(query_text="咖啡", now=BASE_TIME)
    try:
        _seed_faded_memory(projection, db)
        assert [
            hit.memory.memory_id for hit in store.retrieve(cue, rng=random.Random(0))
        ] == ["mem_coffee"]

        # Four simulated hours take activation 0.5 below the 0.18 gate.
        with db.transaction() as conn:
            store.decay_pool(conn, dt_seconds=4 * 3600.0)

        faded = projection.get_memory("mem_coffee")
        assert faded.status == MemoryStatus.LOW_ACTIVATION.value
        assert store.retrieve(cue, rng=random.Random(0)) == []
        assert store.activated_memories(limit=8) == []
        assert memory_module.supersession_record(faded)["retrieval_reason"] == (
            f"status:{MemoryStatus.LOW_ACTIVATION.value}"
        )

        # Saying the same fact again is the reinforcement that revives it.
        with db.transaction() as conn:
            projection.upsert_candidate(
                conn,
                MemoryCandidate(
                    candidate_id="mcd_again",
                    summary="用户喜欢手冲咖啡",
                    kind=MemoryKind.STABLE_KNOWLEDGE.value,
                    source_event_ids=["evt_again"],
                    value=0.8,
                    topics=["咖啡"],
                    created_at=BASE_TIME,
                ),
            )
            result = memory_module.consolidate(projection, conn, config=config, now=BASE_TIME)

        assert result.consolidated == [], "the restatement merges instead of duplicating"
        assert result.skipped == 1
        revived = projection.get_memory("mem_coffee")
        assert revived.status == MemoryStatus.ACTIVE.value
        assert [
            hit.memory.memory_id for hit in store.retrieve(cue, rng=random.Random(0))
        ] == ["mem_coffee"]
    finally:
        db.close()


def test_demotion_happens_at_the_configured_activation_threshold() -> None:
    """The boundary is ``memory.activation_threshold``, not an invented constant."""
    db, projection = _memory_projection()
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projection, config)
    factor = exponential_decay(config.memory.activation_decay_rate, 1.0)
    try:
        _seed_faded_memory(
            projection, db, activation=(config.memory.activation_threshold / factor) * (1 + 1e-9),
            memory_id="mem_at_gate",
        )
        _seed_faded_memory(
            projection, db, activation=(config.memory.activation_threshold / factor) * 0.99,
            memory_id="mem_below_gate",
        )
        with db.transaction() as conn:
            store.decay_pool(conn, dt_seconds=1.0)

        assert (
            projection.get_memory("mem_at_gate").status == MemoryStatus.ACTIVE.value
        ), "a memory sitting exactly on the gate stays in the working set"
        assert (
            projection.get_memory("mem_below_gate").status
            == MemoryStatus.LOW_ACTIVATION.value
        )
    finally:
        db.close()


def test_demotion_never_resurrects_an_archived_memory() -> None:
    """Archival is a stronger statement than decay and must survive a decay pass."""
    db, projection = _memory_projection()
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projection, config)
    try:
        _seed_faded_memory(projection, db, activation=0.05)
        with db.transaction() as conn:
            projection.set_memory_status(
                conn, "mem_coffee", MemoryStatus.ARCHIVED.value
            )
            store.decay_pool(conn, dt_seconds=1.0)
        assert (
            projection.get_memory("mem_coffee").status == MemoryStatus.ARCHIVED.value
        )
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# 5. documentation matches the code
# --------------------------------------------------------------------------------------


def test_build_situation_returns_exactly_the_documented_sections(runtime: Runtime) -> None:
    """The working situation has the keys its docstring promises, and no others.

    ``active_items`` used to be documented but never returned; the activated
    memories live in the bundle's own ``memories`` section.
    """
    situation = context_module.build_situation(runtime.projections, now=BASE_TIME)
    assert set(situation) == {"facts", "inferences", "unfinished", "generated_at"}
