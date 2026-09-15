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

import math
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
from companion_runtime.projections import MemoryProjection, Projections
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    ActivatedMemory,
    Actor,
    EventType,
    Memory,
    MemoryCandidate,
    MemoryKind,
    MemoryStatus,
    RawEvent,
    UnfinishedMatter,
    UnfinishedStatus,
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


def test_a_correction_written_as_a_soft_negation_is_read_as_a_negation() -> None:
    """``不太喜欢`` is the opposite of ``喜欢``, and the code must know it.

    The check used to be a list of negative phrases, so "我现在不太喜欢咖啡了" matched
    only the *positive* marker "喜欢" - a correction then looked like agreement and the
    old and the new statement both stayed asserted. Polarity is decided structurally
    now: a negation cue inside the window in front of a positive marker flips it.
    """
    assert memory_module.polarity_of("我平时喜欢喝咖啡，一天两杯。") == "positive"
    assert memory_module.polarity_of("其实我现在不太喜欢咖啡了，改喝茶。") == "negative"
    assert memory_module.polarity_of("我不喜欢咖啡") == "negative"
    assert memory_module.polarity_of("我现在没那么喜欢咖啡了") == "negative"
    assert memory_module.polarity_of("我讨厌咖啡") == "negative"
    assert memory_module.polarity_of("我不喝咖啡了") == "negative"
    assert memory_module.polarity_of("我明天下午三点面试") is None


def test_a_correction_replaces_the_older_statement_whichever_order_it_arrives_in() -> None:
    """The newer statement wins, in both consolidation orders.

    Two things have to hold: a newer candidate replaces an older memory, and an older
    candidate that reaches the pass *after* the newer one must not replace it - it is
    history, so it is stored as already-replaced instead. Consolidation selects by
    value but applies in time order, and ``_is_newer`` is the guard.
    """
    old_summary = "我平时喜欢喝咖啡，一天两杯。"
    new_summary = "其实我现在不太喜欢咖啡了，改喝茶。"
    stored = memory_module.Memory(
        memory_id="mem_coffee",
        kind=MemoryKind.USER_PREFERENCE.value,
        summary=old_summary,
        topics=["咖啡"],
        importance=0.7,
        confidence=0.8,
        created_at=BASE_TIME,
    )
    correction = {
        "candidate_id": "mcd_correction",
        "summary": new_summary,
        "kind": MemoryKind.USER_PREFERENCE.value,
        "source_event_ids": ["evt_tea"],
        "value": 0.8,
        "topics": ["咖啡"],
        "created_at": BASE_TIME + timedelta(days=1),
    }
    # Same words as the statement it corrects, but made *before* it.
    late_old = correction | {
        "candidate_id": "mcd_late_old",
        "summary": old_summary,
        "source_event_ids": ["evt_coffee"],
        "created_at": BASE_TIME - timedelta(days=1),
    }

    # Case 1: the correction arrives second and replaces what it corrects.
    db, projection = _memory_projection()
    try:
        with db.transaction() as conn:
            projection.upsert_memory(conn, stored)
            projection.upsert_candidate(conn, MemoryCandidate(**correction))
            memory_module.consolidate(projection, conn, config=RuntimeConfig(), now=BASE_TIME)
        replacement = [m for m in projection.list_memories(status=None) if m.memory_id != "mem_coffee"]
        old = projection.get_memory("mem_coffee")
        assert len(replacement) == 1
        assert replacement[0].structured[memory_module.SUPERSEDES_KEY] == ["mem_coffee"]
        assert memory_module.is_superseded(old)
        assert memory_module.supersession_record(old)["superseded_by_hint"] == new_summary
    finally:
        db.close()

    # Case 2: an *older* statement reaches the pass late. It is stored - history is not
    # rewritten - but as replaced, so the character does not end up holding both.
    db, projection = _memory_projection()
    try:
        with db.transaction() as conn:
            projection.upsert_memory(
                conn,
                memory_module.Memory(
                    memory_id="mem_current",
                    kind=MemoryKind.USER_PREFERENCE.value,
                    summary=new_summary,
                    topics=["咖啡"],
                    importance=0.7,
                    confidence=0.8,
                    created_at=BASE_TIME,
                ),
            )
            projection.upsert_candidate(conn, MemoryCandidate(**late_old))
            memory_module.consolidate(projection, conn, config=RuntimeConfig(), now=BASE_TIME)
        late = [m for m in projection.list_memories(status=None) if m.memory_id != "mem_current"]
        assert len(late) == 1, "the older statement is still stored"
        assert late[0].summary == old_summary
        assert memory_module.is_superseded(late[0]), "but it is not asserted"
        assert late[0].structured[memory_module.SUPERSEDED_HINT_KEY] == new_summary
        current = projection.get_memory("mem_current")
        assert not memory_module.is_superseded(current), "the newer belief stands"
        assert late[0].memory_id in current.structured[memory_module.SUPERSEDES_KEY]
    finally:
        db.close()


def test_the_four_long_term_kinds_all_have_a_producer() -> None:
    """Design §16 asks for four kinds; each must be reachable from a user message."""
    from companion_runtime.memory import propose_from_event
    from companion_runtime.projections import Projections

    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    config = RuntimeConfig()
    state = projections.runtime.ensure()
    try:
        cases = {
            "我平时喜欢手冲咖啡。": MemoryKind.USER_PREFERENCE.value,
            "我生日是三月三号。": MemoryKind.STABLE_KNOWLEDGE.value,
            "面试过了！谢谢你那天惦记我。": MemoryKind.RELATIONSHIP.value,
            "今天又加班到十点。": MemoryKind.EPISODIC.value,
        }
        for index, (text, expected) in enumerate(cases.items()):
            event = RawEvent(
                event_id=f"evt_kind_{index}",
                event_type=EventType.USER_MESSAGE.value,
                actor=Actor.USER.value,
                content=text,
                timestamp=BASE_TIME,
            )
            candidate = propose_from_event(
                event,
                state=state,
                unfinished=[],
                emotion_salience=0.4,
                config=config,
                created_at=BASE_TIME,
            )
            assert candidate is not None, f"{text!r} produced no candidate at all"
            assert candidate.kind == expected, f"{text!r} -> {candidate.kind}, want {expected}"
    finally:
        db.close()


def test_the_working_situation_is_a_recall_cue() -> None:
    """Design §20: an unrelated sentence can recall through the *situation*.

    The situation term used to be ``0.25 * lexical``, i.e. a copy of the sentence
    score, so "the situation" recalled nothing the sentence did not already contain.
    """
    db, projection = _memory_projection()
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projection, config)
    try:
        with db.transaction() as conn:
            projection.upsert_memory(
                conn,
                memory_module.Memory(
                    memory_id="mem_work",
                    kind=MemoryKind.EPISODIC.value,
                    summary="用户最近工作量很大，经常加班到很晚。",
                    topics=["工作"],
                    importance=0.6,
                    confidence=0.7,
                    created_at=BASE_TIME,
                ),
            )
        sentence_only = memory_module.RetrievalCue(query_text="在吗", now=BASE_TIME)
        with_situation = memory_module.RetrievalCue(
            query_text="在吗",
            situation_terms=["用户最近几天工作量较大"],
            now=BASE_TIME,
        )
        none_found = store.retrieve(sentence_only, rng=random.Random(0))
        found = store.retrieve(with_situation, rng=random.Random(0))

        # A sentence that says nothing about work scores on importance and recency
        # alone; the situation is what puts the memory at the top, and the hit says so.
        baseline = next(hit for hit in none_found if hit.memory.memory_id == "mem_work")
        assert baseline.lexical == 0.0 and baseline.situation == 0.0
        assert found[0].memory.memory_id == "mem_work"
        assert found[0].situation > 0.0, "the recall is attributed to the situation"
        assert found[0].score > baseline.score
    finally:
        db.close()


def test_a_fact_just_learned_is_shown_even_when_the_working_set_is_saturated() -> None:
    """The section must carry what the user just said, not only what is already hot.

    In a long conversation a handful of memories share words with the live matters,
    are recalled every round, and pin at activation 1.0. A fact stated minutes ago
    then ranks last of eight and never reaches a four-line section - which is how the
    black-box simulation caught it: the character had just been told about the user's
    coffee and the prompt did not mention it.
    """
    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projections.memory, config)
    try:
        with db.transaction() as conn:
            for index in range(6):
                old = memory_module.Memory(
                    memory_id=f"mem_hot_{index}",
                    kind=MemoryKind.EPISODIC.value,
                    summary=f"用户提过的第 {index} 件旧事，和面试有关。",
                    topics=["面试"],
                    importance=0.6,
                    confidence=0.8,
                    created_at=BASE_TIME - timedelta(days=3),
                )
                projections.memory.upsert_memory(conn, old)
                projections.memory.upsert_activation(
                    conn,
                    ActivatedMemory(
                        memory_id=old.memory_id,
                        activation=1.0,
                        last_recalled_at=BASE_TIME,
                        recall_count=50,
                    ),
                )
            fresh = memory_module.Memory(
                memory_id="mem_just_learned",
                kind=MemoryKind.USER_PREFERENCE.value,
                summary="对了，我喝咖啡只喝手冲，不加糖。",
                topics=["咖啡"],
                importance=0.53,
                confidence=0.5,
                created_at=BASE_TIME,
            )
            projections.memory.upsert_memory(conn, fresh)
            projections.memory.upsert_activation(
                conn,
                ActivatedMemory(
                    memory_id=fresh.memory_id,
                    activation=0.55,
                    last_recalled_at=BASE_TIME,
                    recall_count=1,
                ),
            )

        # An unrelated sentence: nothing recalls the coffee fact, and the six hot
        # memories are recalled by the unfinished matter.
        cue = memory_module.RetrievalCue(
            query_text="在忙什么呢",
            unfinished_titles=["等待面试结果"],
            now=BASE_TIME,
        )
        selected = context_module.select_memories(
            projections, limit=4, cue=cue, store=store, now=BASE_TIME
        )
        by_id = {item["memory_id"]: item for item in selected}
        assert "mem_just_learned" in by_id, (
            f"the fact just learned is missing: {[item['summary'] for item in selected]}"
        )
        assert by_id["mem_just_learned"]["selection"] == "recent"
        assert len(selected) <= 4

        # Once it is no longer fresh it has to earn its place through recall or
        # importance, like everything else.
        much_later = BASE_TIME + timedelta(hours=context_module.FRESH_WINDOW_HOURS + 1)
        later = context_module.select_memories(
            projections, limit=4, cue=cue, store=store, now=much_later
        )
        assert all(item["selection"] != "recent" for item in later)
    finally:
        db.close()


def test_losing_the_pool_row_also_changes_the_status() -> None:
    """The pool *is* the working set, so a memory that leaves it must say so.

    Two ways out exist - decaying below the tiny floor, and being crowded out of the
    bounded pool - and both used to delete the row while leaving ``status = active``.
    The operator surface then reported a memory that could never be recalled into the
    prompt as "retrievable", and the prompt's durable source could still inject it.
    """
    db, projection = _memory_projection()
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projection, config)
    try:
        _seed_faded_memory(projection, db, activation=0.5, memory_id="mem_fades")
        _seed_faded_memory(projection, db, activation=0.4, memory_id="mem_crowded")
        with db.transaction() as conn:
            # Decay far past the floor: the row is dropped.
            store.decay_pool(conn, dt_seconds=10_000_000.0)
        assert projection.list_activated(limit=10) == []
        for memory_id in ("mem_fades", "mem_crowded"):
            assert projection.get_memory(memory_id).status == MemoryStatus.LOW_ACTIVATION.value

        # ... and being crowded out of a one-slot pool demotes too.
        with db.transaction() as conn:
            projection.set_memory_status(
                conn, "mem_fades", MemoryStatus.ACTIVE.value
            )
            projection.set_memory_status(
                conn, "mem_crowded", MemoryStatus.ACTIVE.value
            )
            hit_high = memory_module.RetrievalHit(
                memory=projection.get_memory("mem_fades"),
                score=0.9,
                matched_tokens=3,
                recalled=True,
            )
            hit_low = memory_module.RetrievalHit(
                memory=projection.get_memory("mem_crowded"),
                score=0.5,
                matched_tokens=3,
                recalled=True,
            )
            store.activate(conn, [hit_high, hit_low], now=BASE_TIME, pool_size=1)
        assert projection.get_memory("mem_fades").status == MemoryStatus.ACTIVE.value
        assert projection.get_memory("mem_crowded").status == (
            MemoryStatus.LOW_ACTIVATION.value
        ), "crowded out of the pool is out of the working set"
    finally:
        db.close()


def test_only_a_real_cue_match_puts_a_memory_into_the_pool() -> None:
    """Importance and recency alone must not keep a memory permanently warm.

    Every memory scores ``0.3 * importance`` plus recency, so an important memory
    clears the activation gate forever whether or not anything recalled it. Folding
    those hits into the pool pinned the same entries at activation 1.0 and the working
    set stopped moving: nothing ever faded, and every new fact was ranked last.
    """
    db, projection = _memory_projection()
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projection, config)
    try:
        with db.transaction() as conn:
            projection.upsert_memory(
                conn,
                memory_module.Memory(
                    memory_id="mem_unrelated",
                    kind=MemoryKind.STABLE_KNOWLEDGE.value,
                    summary="用户住在城南，工作在城北。",
                    topics=["住"],
                    importance=0.9,
                    confidence=0.8,
                    created_at=BASE_TIME,
                ),
            )
        cue = memory_module.RetrievalCue(query_text="今天天气不错", now=BASE_TIME)
        hits = store.retrieve(cue, rng=random.Random(0))
        unrelated = next(hit for hit in hits if hit.memory.memory_id == "mem_unrelated")
        assert unrelated.matched_tokens == 0, "nothing in the cue mentions where they live"
        with db.transaction() as conn:
            touched = store.activate(conn, hits, now=BASE_TIME)
        assert touched == [], "an unmatched memory must not enter the working set"
        assert projection.list_activated(limit=10) == []

        # One shared bigram in a longer sentence is a coincidence, not a recollection.
        cue = memory_module.RetrievalCue(query_text="他住在哪里呢", now=BASE_TIME)
        hits = store.retrieve(cue, rng=random.Random(0))
        unrelated = next(hit for hit in hits if hit.memory.memory_id == "mem_unrelated")
        assert unrelated.matched_tokens == 1 and unrelated.recalled is False
        with db.transaction() as conn:
            touched = store.activate(conn, hits, now=BASE_TIME)
        assert touched == [], "one shared bigram in a sentence is not a recall"

        # A real match does put it in.
        cue = memory_module.RetrievalCue(query_text="他现在住在城南吗", now=BASE_TIME)
        hits = store.retrieve(cue, rng=random.Random(0))
        with db.transaction() as conn:
            touched = store.activate(conn, hits, now=BASE_TIME)
        assert [item.memory_id for item in touched] == ["mem_unrelated"]

        # A whole short question counts too: "咖啡" is one bigram, and a memory that
        # contains the whole cue is what it is about.
        with db.transaction() as conn:
            store.decay_pool(conn, dt_seconds=10_000_000.0)
        short = memory_module.RetrievalCue(query_text="城南", now=BASE_TIME)
        hits = store.retrieve(short, rng=random.Random(0))
        assert next(hit for hit in hits if hit.memory.memory_id == "mem_unrelated").recalled
        with db.transaction() as conn:
            touched = store.activate(conn, hits, now=BASE_TIME)
        assert [item.memory_id for item in touched] == ["mem_unrelated"]
    finally:
        db.close()


def test_a_question_is_not_filed_as_a_durable_fact() -> None:
    """``我生日是什么时候来着`` is something that happened, not knowledge about the user.

    The marker list alone read it as stable knowledge - the very marker ("我生日") that
    is supposed to recognise the statement of a birthday - and the character then held
    the *question* as a durable fact about the user.
    """
    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    config = RuntimeConfig()
    state = projections.runtime.ensure()
    try:
        question = RawEvent(
            event_id="evt_question",
            event_type=EventType.USER_MESSAGE.value,
            actor=Actor.USER.value,
            content="我生日是什么时候来着",
            timestamp=BASE_TIME,
        )
        statement = RawEvent(
            event_id="evt_statement",
            event_type=EventType.USER_MESSAGE.value,
            actor=Actor.USER.value,
            content="我生日是三月三号。",
            timestamp=BASE_TIME,
        )
        asked = memory_module.propose_from_event(
            question,
            state=state,
            unfinished=[],
            emotion_salience=0.4,
            config=config,
            created_at=BASE_TIME,
        )
        told = memory_module.propose_from_event(
            statement,
            state=state,
            unfinished=[],
            emotion_salience=0.4,
            config=config,
            created_at=BASE_TIME,
        )
        assert told is not None and told.kind == MemoryKind.STABLE_KNOWLEDGE.value
        assert asked is not None and asked.kind == MemoryKind.EPISODIC.value, (
            f"a question was filed as {asked.kind if asked else None}"
        )
    finally:
        db.close()


def test_a_memory_about_a_taken_subject_does_not_become_a_second_candidate() -> None:
    """The live matter asks its own follow-up; the memory path must not ask again.

    Once memories persist, the memory-curiosity path can pick up a fact about a
    subject an unfinished matter already owns - and that is how the black-box
    simulation saw the bot ask about the interview result *after* the user had
    reported it: the memory "面试过了！谢谢你那天惦记我" became a second question about
    the same matter.
    """
    from companion_runtime import candidate as candidate_module
    from companion_runtime.typing import ActivatedMemory

    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    config = RuntimeConfig()
    state = projections.runtime.ensure()
    try:
        interview = memory_module.Memory(
            memory_id="mem_interview",
            kind=MemoryKind.RELATIONSHIP.value,
            summary="面试过了！谢谢你那天惦记我。",
            topics=["面试"],
            importance=0.7,
            confidence=0.8,
            created_at=BASE_TIME,
        )
        birthday = memory_module.Memory(
            memory_id="mem_birthday",
            kind=MemoryKind.STABLE_KNOWLEDGE.value,
            summary="我生日是三月三号。",
            topics=["生日"],
            importance=0.8,
            confidence=0.8,
            created_at=BASE_TIME,
        )
        activated = [
            (ActivatedMemory(memory_id=item.memory_id, activation=0.8), item)
            for item in (interview, birthday)
        ]
        with db.transaction() as conn:
            for item in (interview, birthday):
                projections.memory.upsert_memory(conn, item)

        produced = candidate_module.generate(
            state=state,
            config=config,
            unfinished=[],
            activated=activated,
            existing=[],
            now=BASE_TIME,
            spoken_for=["等待面试结果"],
        )
        intents = [candidate.intent for candidate in produced]
        assert any("生日" in intent for intent in intents), (
            f"the unrelated memory must still be able to start a conversation: {intents}"
        )
        assert not any("面试" in intent for intent in intents), (
            f"the interview subject is spoken for: {intents}"
        )

        # With a live matter for that subject, the subject is covered by exactly one
        # candidate: the matter's own follow-up. (The Runtime passes the same
        # ``spoken_for`` list it builds from ``unfinished.subject_guards``.)
        matters = [
            UnfinishedMatter(
                unfinished_id="unf_interview",
                title="等待面试结果",
                status=UnfinishedStatus.WAITING.value,
                priority=0.8,
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
            )
        ]
        produced = candidate_module.generate(
            state=state,
            config=config,
            unfinished=matters,
            activated=activated,
            existing=[],
            now=BASE_TIME,
            spoken_for=[matter.title for matter in matters],
        )
        intents = [candidate.intent for candidate in produced]
        assert sum(1 for intent in intents if "面试" in intent) == 1, intents
    finally:
        db.close()


def test_the_prompt_carries_durable_facts_even_when_nothing_recalled_them() -> None:
    """The memory section is not just "what is currently activated".

    A section fed only by the activation pool goes empty as soon as the pool decays,
    and every durable fact vanishes with it - which is what happened: at day 3 of a
    quiet week the acting layer was handed no memories at all. Durable kinds
    (stable knowledge, preferences, relational experiences) are injected from
    importance, and a cue can add what this moment brings back.
    """
    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    try:
        with db.transaction() as conn:
            projections.memory.upsert_memory(
                conn,
                memory_module.Memory(
                    memory_id="mem_birthday",
                    kind=MemoryKind.STABLE_KNOWLEDGE.value,
                    summary="我生日是三月三号。",
                    topics=["生日"],
                    importance=0.8,
                    confidence=0.8,
                    created_at=BASE_TIME,
                ),
            )
        # Nothing is in the activation pool at all.
        assert projections.memory.list_activated(limit=10) == []
        selected = context_module.select_memories(projections, limit=4)
        assert [item["memory_id"] for item in selected] == ["mem_birthday"]
        assert selected[0]["selection"] == "durable"

        # A faded memory is not injected by importance alone ...
        with db.transaction() as conn:
            projections.memory.set_memory_status(
                conn, "mem_birthday", MemoryStatus.LOW_ACTIVATION.value
            )
        assert context_module.select_memories(projections, limit=4) == []

        # ... but a cue that matches it still brings it back.
        store = memory_module.MemoryStore(projections.memory, RuntimeConfig())
        cue = memory_module.RetrievalCue(query_text="我生日是什么时候", now=BASE_TIME)
        selected = context_module.select_memories(
            projections, limit=4, cue=cue, store=store
        )
        assert [item["memory_id"] for item in selected] == ["mem_birthday"]
        assert selected[0]["selection"] == "cue"
    finally:
        db.close()


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


def _hours_to_fade(config: RuntimeConfig, *, activation: float) -> float:
    """Return how long ``activation`` takes to fall below the demotion gate.

    Derived from the configured rate rather than hard-coded, so a test that is about
    the *behaviour* of fading does not silently become a test about one number. The
    crossing instant itself is not enough - sitting exactly on the gate keeps a
    memory active - so a tenth of the interval is added.
    """
    rate = config.memory.activation_decay_rate
    crossing = math.log(activation / config.memory.activation_threshold) / rate / 3600.0
    return crossing * 1.1


def test_a_faded_memory_leaves_the_working_set_but_a_cue_still_recalls_it() -> None:
    """Demotion means "not on the character's mind" - it does not mean forgotten.

    The earlier version of this test asserted that a faded memory could no longer be
    retrieved at all. That assertion *was* the defect: a fact the user stated once and
    never repeated faded within hours and then became permanently unreachable, so
    asking about it returned nothing. What fading must do is leave the working set
    (the unprompted prompt section) while staying recallable, and a recall puts it
    back.
    """
    db, projection = _memory_projection()
    config = RuntimeConfig()
    store = memory_module.MemoryStore(projection, config)
    cue = memory_module.RetrievalCue(query_text="咖啡", now=BASE_TIME)
    try:
        _seed_faded_memory(projection, db)
        assert [
            hit.memory.memory_id for hit in store.retrieve(cue, rng=random.Random(0))
        ] == ["mem_coffee"]

        hours = _hours_to_fade(config, activation=0.5)
        with db.transaction() as conn:
            store.decay_pool(conn, dt_seconds=hours * 3600.0)

        faded = projection.get_memory("mem_coffee")
        assert faded.status == MemoryStatus.LOW_ACTIVATION.value
        assert store.activated_memories(limit=8) == [], "a faded memory is not on its mind"
        assert memory_module.supersession_record(faded)["retrieval_reason"] == (
            f"status:{MemoryStatus.LOW_ACTIVATION.value}"
        )

        # ... but the cue still finds it, and being recalled reinstates it.
        hits = store.retrieve(cue, rng=random.Random(0))
        assert [hit.memory.memory_id for hit in hits] == ["mem_coffee"]
        with db.transaction() as conn:
            store.activate(conn, hits, now=BASE_TIME + timedelta(hours=hours))
        assert projection.get_memory("mem_coffee").status == MemoryStatus.ACTIVE.value
        assert [item.memory_id for _, item in store.activated_memories(limit=8)] == [
            "mem_coffee"
        ]

        # A restatement reinforces it as well, even without a cue.
        with db.transaction() as conn:
            current = next(
                item.activation
                for item in projection.list_activated(limit=500)
                if item.memory_id == "mem_coffee"
            )
            store.decay_pool(
                conn, dt_seconds=_hours_to_fade(config, activation=current) * 3600.0
            )
            assert projection.get_memory("mem_coffee").status == (
                MemoryStatus.LOW_ACTIVATION.value
            )
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
        assert projection.get_memory("mem_coffee").status == MemoryStatus.ACTIVE.value
    finally:
        db.close()


def test_a_fact_told_once_survives_the_next_two_days(runtime: Runtime) -> None:
    """The working set must outlive a single day, or "long-term memory" is a misnomer.

    With the rate this replaced (1.5e-4/s, a half-life of 1.3 hours) every memory was
    gone from the working set - and with it from the prompt's 【必要记忆】 - within
    half a day unless the user repeated it, which is the normal case that never
    happens.
    """
    fact = "记住，我不喜欢别人连续追问我在干嘛。"
    runtime.process_user_message(content=fact, timestamp=BASE_TIME)
    runtime.endogenous_round(now=BASE_TIME + timedelta(hours=2), force=True)
    assert len(runtime.projections.memory.list_memories()) == 1

    # Two days pass with no further mention of it.
    two_days = BASE_TIME + timedelta(days=2)
    runtime.endogenous_round(now=two_days, force=True)

    memory = runtime.projections.memory.list_memories()[0]
    assert memory.status == MemoryStatus.ACTIVE.value, "two quiet days is not forgetting"
    bundle = context_module.build(runtime=runtime, now=two_days)
    assert fact in [item["summary"] for item in bundle.memories], (
        "a durable fact must still be in front of the acting layer"
    )
    # Fading is pinned separately (the demotion tests); this test is about the promise
    # that a fact told once is still *there* the day after tomorrow.


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
