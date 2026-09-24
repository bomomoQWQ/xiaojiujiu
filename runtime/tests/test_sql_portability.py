"""SQL portability: portable upsert identity and the stored timestamp formats.

The Runtime is gaining a PostgreSQL backend while SQLite stays the default, so the
statements that build rows have to mean the same thing on both engines. These tests
pin the *behaviour* of the portable forms, never their text:

* a second write to the same identity updates one row in place, carrying the second
  values, and leaves the columns the update path does not list at their stored value;
* the guarded insert that seeds ``runtime_state`` is a no-op when the row already
  exists, even when the existence check missed it;
* the two timestamps the Runtime formats by hand keep their exact stored shape.

A future backend may rewrite a statement freely as long as these invariants hold.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.maintenance import snapshot_name
from companion_runtime.motivation import CONTACT_DAY_META_KEY, rollover_contact_day
from companion_runtime.typing import (
    ActionAttempt,
    ActivatedMemory,
    AttemptState,
    Boundary,
    BoundaryType,
    CandidateIntent,
    CandidateStatus,
    EmotionEvent,
    Memory,
    MemoryCandidate,
    MemoryKind,
    MemoryStatus,
    UnfinishedMatter,
    UnfinishedStatus,
    ValueProfile,
)
from companion_runtime.utility import local_day_key
from conftest import BASE_TIME


def _count(runtime, table: str) -> int:
    """Return the number of rows in ``table``.

    Args:
        runtime: Runtime whose database is inspected.
        table: Fixed table name (never caller-supplied).

    Returns:
        The row count.
    """
    row = runtime.db.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - fixed names
    return int(row["n"])


def _column(runtime, table: str, column: str, key: str, value: str):
    """Return one raw column of the row identified by ``key = value``.

    Args:
        runtime: Runtime whose database is inspected.
        table: Fixed table name.
        column: Fixed column name.
        key: Column to match on.
        value: Value to match.

    Returns:
        The stored column value.
    """
    row = runtime.db.query_one(
        f"SELECT {column} FROM {table} WHERE {key} = ?",  # noqa: S608 - fixed names
        (value,),
    )
    return row[column]


# --------------------------------------------------------------------------------------
# upsert identity: one row per key, second values win
# --------------------------------------------------------------------------------------


def test_a_repeated_working_situation_projection_refreshes_one_row(runtime) -> None:
    """The same fact projected twice is one row carrying the second values.

    With no identifier supplied the item is keyed by ``(content, kind)``, so a
    repeated projection has to refresh the existing row rather than pile a
    duplicate into the bounded working set. ``created_at`` is not part of the update
    path: it records when the item entered the working set.
    """
    situation = runtime.projections.situation
    with runtime.db.transaction() as conn:
        first = situation.upsert(conn, kind="topic", content="the same fact", salience=0.2)
    created_at = _column(runtime, "working_situation_items", "created_at", "item_id", first)

    with runtime.db.transaction() as conn:
        second = situation.upsert(conn, kind="topic", content="the same fact", salience=0.9)

    assert second == first
    assert _count(runtime, "working_situation_items") == 1
    rows = situation.list_active()
    assert len(rows) == 1
    assert float(rows[0]["salience"]) == pytest.approx(0.9)
    assert rows[0]["created_at"] == created_at


def test_a_repeated_emotion_write_updates_the_row_and_re_activates_it(runtime) -> None:
    """Re-writing one emotion event updates it in place and re-activates it.

    The update path sets ``status = 'active'``, so a fresh impact for an event whose
    earlier impact already decayed revives that row instead of leaving a decayed
    duplicate behind. ``created_at`` and ``decay_rate`` are not part of the update
    path, so a second write carrying a later creation time does not move them.
    """
    emotion = runtime.projections.emotion
    with runtime.db.transaction() as conn:
        emotion.upsert(
            conn,
            EmotionEvent(
                emotion_event_id="emo-1",
                source_event_id="evt-1",
                direction="positive",
                intensity=0.2,
                activation=0.3,
                semantic_label="warmth",
                created_at=BASE_TIME,
            ),
        )
    assert len(emotion.list_active()) == 1
    with runtime.db.transaction() as conn:
        emotion.deactivate(conn, ["emo-1"])
    assert emotion.list_active() == []

    with runtime.db.transaction() as conn:
        emotion.upsert(
            conn,
            EmotionEvent(
                emotion_event_id="emo-1",
                source_event_id="evt-2",
                direction="negative",
                intensity=0.8,
                activation=0.9,
                semantic_label="sting",
                created_at=BASE_TIME + timedelta(days=1),
            ),
        )

    active = emotion.list_active()
    assert _count(runtime, "active_emotion_events") == 1
    assert len(active) == 1
    assert active[0].direction == "negative"
    assert active[0].intensity == pytest.approx(0.8)
    assert active[0].activation == pytest.approx(0.9)
    assert active[0].semantic_label == "sting"
    assert active[0].created_at == BASE_TIME
    assert active[0].decay_rate == pytest.approx(0.08)


def test_a_repeated_boundary_write_updates_it_in_place_and_clears_revocation(runtime) -> None:
    """A re-issued boundary updates its row and can clear an earlier revocation.

    ``revoked_at`` *is* part of the update path, so re-issuing a boundary with
    ``revoked_at=None`` makes it live again - which is what keeps a re-granted
    boundary from staying permanently revoked. ``created_at`` is not, so the row
    keeps the moment it was first written.
    """
    boundaries = runtime.projections.boundaries
    with runtime.db.transaction() as conn:
        boundaries.upsert(
            conn,
            Boundary(
                boundary_id="b-1",
                type=BoundaryType.TOPIC.value,
                scope="topic:politics",
                note="first",
            ),
        )
    created_at = _column(runtime, "boundaries", "created_at", "boundary_id", "b-1")

    with runtime.db.transaction() as conn:
        boundaries.upsert(
            conn,
            Boundary(
                boundary_id="b-1",
                type=BoundaryType.TOPIC.value,
                scope="topic:politics",
                note="first",
                revoked_at=BASE_TIME,
            ),
        )
    assert boundaries.get("b-1").revoked_at == BASE_TIME
    assert boundaries.list_all() == []
    assert len(boundaries.list_all(include_revoked=True)) == 1

    with runtime.db.transaction() as conn:
        boundaries.upsert(
            conn,
            Boundary(
                boundary_id="b-1",
                type=BoundaryType.TOPIC.value,
                scope="topic:politics",
                allow_reply=False,
                allow_proactive=True,
                note="second",
            ),
        )

    stored = boundaries.get("b-1")
    assert _count(runtime, "boundaries") == 1
    assert stored.note == "second"
    assert stored.allow_reply is False
    assert stored.allow_proactive is True
    assert stored.revoked_at is None
    assert _column(runtime, "boundaries", "created_at", "boundary_id", "b-1") == created_at


def test_a_repeated_unfinished_write_updates_it_in_place(runtime) -> None:
    """Re-writing a matter updates every field it carries and keeps ``created_at``.

    ``created_at`` is the one column the update path does not list, so a later write
    that carries a different creation time must not move it.
    """
    matters = runtime.projections.unfinished
    with runtime.db.transaction() as conn:
        matters.upsert(
            conn,
            UnfinishedMatter(
                unfinished_id="u-1", title="first", priority=0.2, created_at=BASE_TIME
            ),
        )

    waiting_until = BASE_TIME + timedelta(hours=2)
    with runtime.db.transaction() as conn:
        matters.upsert(
            conn,
            UnfinishedMatter(
                unfinished_id="u-1",
                title="second",
                status=UnfinishedStatus.WAITING.value,
                priority=0.9,
                waiting_until=waiting_until,
                source_event_ids=["evt-1"],
                resolution_conditions=["they answer"],
                created_at=BASE_TIME + timedelta(days=1),
            ),
        )

    stored = matters.get("u-1")
    assert _count(runtime, "unfinished_matters") == 1
    assert stored.title == "second"
    assert stored.status == UnfinishedStatus.WAITING.value
    assert stored.priority == pytest.approx(0.9)
    assert stored.waiting_until == waiting_until
    assert stored.source_event_ids == ["evt-1"]
    assert stored.resolution_conditions == ["they answer"]
    assert stored.created_at == BASE_TIME


def test_a_repeated_memory_candidate_write_updates_it_in_place(runtime) -> None:
    """Re-writing a candidate updates it and keeps ``created_at`` from the first write."""
    candidates = runtime.projections.memory
    with runtime.db.transaction() as conn:
        candidates.upsert_candidate(
            conn,
            MemoryCandidate(
                candidate_id="c-1",
                summary="first",
                kind=MemoryKind.EPISODIC.value,
                value=0.1,
                created_at=BASE_TIME,
            ),
        )
    with runtime.db.transaction() as conn:
        candidates.upsert_candidate(
            conn,
            MemoryCandidate(
                candidate_id="c-1",
                summary="second",
                kind=MemoryKind.USER_PREFERENCE.value,
                value=0.9,
                status="consolidated",
                consolidated_memory_id="m-1",
                topics=["coffee"],
                confidence=0.8,
                created_at=BASE_TIME + timedelta(days=1),
            ),
        )

    stored = candidates.get_candidate("c-1")
    assert _count(runtime, "memory_candidates") == 1
    assert stored.summary == "second"
    assert stored.kind == MemoryKind.USER_PREFERENCE.value
    assert stored.value == pytest.approx(0.9)
    assert stored.status == "consolidated"
    assert stored.consolidated_memory_id == "m-1"
    assert stored.topics == ["coffee"]
    assert stored.confidence == pytest.approx(0.8)
    assert stored.created_at == BASE_TIME


def test_a_repeated_memory_write_updates_it_in_place_and_can_archive_it(runtime) -> None:
    """Re-writing a memory updates its retention fields and keeps ``created_at``.

    Archival is a status change on the existing row, not a second row, and the
    ``archived_at`` stamp is written by the update path - so forgetting a memory
    cannot silently leave the older active row in place.
    """
    memories = runtime.projections.memory
    with runtime.db.transaction() as conn:
        memories.upsert_memory(
            conn,
            Memory(
                memory_id="m-1",
                kind=MemoryKind.EPISODIC.value,
                summary="first",
                created_at=BASE_TIME,
            ),
        )

    archived_at = BASE_TIME + timedelta(days=2)
    with runtime.db.transaction() as conn:
        memories.upsert_memory(
            conn,
            Memory(
                memory_id="m-1",
                kind=MemoryKind.STABLE_KNOWLEDGE.value,
                summary="second",
                structured={"key": "value"},
                topics=["tea"],
                importance=0.9,
                confidence=0.8,
                status=MemoryStatus.ARCHIVED.value,
                source_event_ids=["evt-1"],
                created_at=BASE_TIME + timedelta(days=1),
                archived_at=archived_at,
            ),
        )

    stored = memories.get_memory("m-1")
    assert _count(runtime, "memories") == 1
    assert stored.summary == "second"
    assert stored.kind == MemoryKind.STABLE_KNOWLEDGE.value
    assert stored.structured == {"key": "value"}
    assert stored.topics == ["tea"]
    assert stored.importance == pytest.approx(0.9)
    assert stored.confidence == pytest.approx(0.8)
    assert stored.status == MemoryStatus.ARCHIVED.value
    assert stored.source_event_ids == ["evt-1"]
    assert stored.archived_at == archived_at
    assert stored.created_at == BASE_TIME


def test_a_repeated_activation_write_updates_the_single_pool_entry(runtime) -> None:
    """The activation pool keeps one row per memory, carrying the newest numbers.

    Every column of this row is in the update path, so nothing here may be frozen:
    a recall that does not raise the activation count would make the pool useless.
    """
    with runtime.db.transaction() as conn:
        runtime.projections.memory.upsert_activation(
            conn,
            ActivatedMemory(memory_id="m-1", activation=0.2, recall_count=1, reason="first"),
        )
    with runtime.db.transaction() as conn:
        runtime.projections.memory.upsert_activation(
            conn,
            ActivatedMemory(
                memory_id="m-1",
                activation=0.9,
                last_recalled_at=BASE_TIME,
                recall_count=5,
                reason="second",
            ),
        )

    pooled = runtime.projections.memory.list_activated()
    assert _count(runtime, "activated_memories") == 1
    assert len(pooled) == 1
    assert pooled[0].activation == pytest.approx(0.9)
    assert pooled[0].last_recalled_at == BASE_TIME
    assert pooled[0].recall_count == 5
    assert pooled[0].reason == "second"


def test_a_repeated_candidate_intent_write_updates_it_in_place(runtime) -> None:
    """Re-writing a candidate intent updates the pool row and keeps ``created_at``."""
    intents = runtime.projections.candidates
    with runtime.db.transaction() as conn:
        intents.upsert(
            conn,
            CandidateIntent(
                candidate_id="ci-1", type="reach_out", intent="first", created_at=BASE_TIME
            ),
        )

    expires_at = BASE_TIME + timedelta(hours=1)
    with runtime.db.transaction() as conn:
        intents.upsert(
            conn,
            CandidateIntent(
                candidate_id="ci-1",
                type="share",
                intent="second",
                goal="goal",
                target="user",
                sources=["evt-1"],
                confidence=0.9,
                status=CandidateStatus.RETIRED.value,
                internal_need=0.8,
                expires_at=expires_at,
                retired_reason="superseded",
                created_at=BASE_TIME + timedelta(days=1),
            ),
        )

    stored = intents.get("ci-1")
    assert _count(runtime, "candidate_intents") == 1
    assert stored.type == "share"
    assert stored.intent == "second"
    assert stored.goal == "goal"
    assert stored.sources == ["evt-1"]
    assert stored.confidence == pytest.approx(0.9)
    assert stored.status == CandidateStatus.RETIRED.value
    assert stored.internal_need == pytest.approx(0.8)
    assert stored.expires_at == expires_at
    assert stored.retired_reason == "superseded"
    assert stored.created_at == BASE_TIME


def test_a_repeated_attempt_write_updates_it_in_place(runtime) -> None:
    """Re-writing an action attempt updates its lifecycle fields and keeps ``created_at``.

    The transition fields are the point of the update path: an attempt that is
    committed and rendered must not leave the older ``proposed`` row behind.
    """
    attempts = runtime.projections.attempts
    with runtime.db.transaction() as conn:
        attempts.upsert(
            conn,
            ActionAttempt(
                attempt_id="a-1",
                candidate_id=None,
                state=AttemptState.PROPOSED.value,
                intent="first",
                created_at=BASE_TIME,
            ),
        )
    with runtime.db.transaction() as conn:
        attempts.upsert(
            conn,
            ActionAttempt(
                attempt_id="a-1",
                candidate_id="ci-1",
                state=AttemptState.COMMITTED.value,
                intent="second",
                goal="goal",
                based_on_version=3,
                created_at=BASE_TIME + timedelta(days=1),
                committed_at=BASE_TIME,
                rendered_text="hello",
                superseded_by_event_ids=["evt-1"],
                outbox_id="o-1",
            ),
        )

    stored = attempts.get("a-1")
    assert _count(runtime, "action_attempts") == 1
    assert stored.candidate_id == "ci-1"
    assert stored.state == AttemptState.COMMITTED.value
    assert stored.intent == "second"
    assert stored.goal == "goal"
    assert stored.based_on_version == 3
    assert stored.committed_at == BASE_TIME
    assert stored.rendered_text == "hello"
    assert stored.superseded_by_event_ids == ["evt-1"]
    assert stored.outbox_id == "o-1"
    assert stored.created_at == BASE_TIME


# --------------------------------------------------------------------------------------
# guarded insert: seed once, ignore a racing duplicate
# --------------------------------------------------------------------------------------


def test_the_runtime_row_is_seeded_once_and_a_racing_insert_is_ignored(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure`` seeds one row per runtime id and a duplicate insert changes nothing.

    The seed statement is a guarded insert rather than a plain one, which is the
    portable spelling of ``INSERT OR IGNORE``: when the row appeared between the
    existence check and the insert, the insert is a no-op. The row another writer
    already stored must therefore survive intact - an update-on-conflict here would
    silently reset a live Runtime's version, epoch and mood to seed values.
    """
    projection = runtime.projections.runtime
    state = projection.ensure(now=BASE_TIME)
    with runtime.db.transaction() as conn:
        state.mood_valence = 0.9
        projection.write(state, conn)

    advanced = projection.read()
    assert advanced.version == 1
    assert advanced.mood_valence == pytest.approx(0.9)

    # Make the existence check miss, exactly as it would if the row had been
    # created by another writer between the SELECT and the INSERT.
    real_query_one = runtime.db.query_one
    calls = {"n": 0}

    def miss_the_first_lookup(sql, params=()):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_query_one(sql, params)

    monkeypatch.setattr(runtime.db, "query_one", miss_the_first_lookup)

    again = projection.ensure(
        now=BASE_TIME + timedelta(hours=1), values=ValueProfile(curiosity=0.01)
    )

    assert calls["n"] >= 2  # the guarded insert really was attempted
    assert _count(runtime, "runtime_state") == 1
    assert again.version == advanced.version
    assert again.epoch_at == BASE_TIME
    assert again.mood_valence == pytest.approx(0.9)
    assert again.values.curiosity == pytest.approx(ValueProfile().curiosity)


# --------------------------------------------------------------------------------------
# the two timestamps the Runtime formats by hand
# --------------------------------------------------------------------------------------


def test_snapshot_names_keep_the_compact_utc_stamp_format() -> None:
    """Backup filenames keep the exact stamp format, in UTC, from the caller's clock.

    The snapshot name is built by formatting a timestamp the Runtime already holds,
    so it is computed in Python rather than by a dialect-specific SQL function. The
    shape is load-bearing: retention sorts these names, and the ``Z`` says the stamp
    is UTC rather than an offset-bearing ISO string.
    """
    name = snapshot_name("runtime", now=BASE_TIME)
    assert name == "runtime-20260301T090000Z.sqlite3"
    assert re.fullmatch(r"runtime-\d{8}T\d{6}Z\.sqlite3", name)

    later = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert snapshot_name("runtime", now=later) == "runtime-20261231T235959Z.sqlite3"


def test_the_local_day_key_keeps_the_iso_calendar_date_format() -> None:
    """The daily contact budget is keyed by a zero-padded ``YYYY-MM-DD`` local date.

    The key is a local-clock question, so Python answers it (the machine timezone is
    not a database property) - noon local keeps the assertion true at every UTC
    offset. Zero padding is what keeps the key sortable as text, and the fixed shape
    is what the daily rollover compares against.
    """
    key = local_day_key(datetime(2026, 3, 5, 12, 0).astimezone())
    assert key == "2026-03-05"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", key)
    assert local_day_key(datetime(2026, 1, 9, 12, 0).astimezone()) == "2026-01-09"


def test_the_stored_contact_day_is_the_python_day_key(runtime) -> None:
    """The day key is computed in Python and *stored*, so a backend cannot change it.

    ``rollover_contact_day`` writes the key into ``state.meta`` and the runtime row
    persists it in the ``meta_json`` column. Pinning the stored value - not only the
    helper - is what makes a backend swap unable to alter what the next comparison
    reads back.
    """
    projection = runtime.projections.runtime
    projection.ensure(now=BASE_TIME)
    state = projection.read()
    assert rollover_contact_day(state, now=BASE_TIME) is True
    with runtime.db.transaction() as conn:
        projection.write(state, conn)

    stored = projection.read().meta[CONTACT_DAY_META_KEY]
    assert stored == local_day_key(BASE_TIME)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", stored)

    raw = _column(runtime, "runtime_state", "meta_json", "runtime_id", projection.runtime_id)
    assert json.loads(raw)[CONTACT_DAY_META_KEY] == stored
