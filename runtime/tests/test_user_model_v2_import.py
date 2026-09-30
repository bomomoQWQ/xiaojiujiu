from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companion_runtime.user_model_v2_import import SOURCE_TABLES, plan_migration


def _fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE action_attempts (
            attempt_id TEXT PRIMARY KEY, candidate_id TEXT, state TEXT, intent TEXT,
            goal TEXT, created_at TEXT, updated_at TEXT, committed_at TEXT, outbox_id TEXT
        );
        CREATE TABLE attempt_events (
            attempt_event_id TEXT PRIMARY KEY, attempt_id TEXT, from_state TEXT,
            to_state TEXT, reason TEXT, created_at TEXT
        );
        CREATE TABLE raw_events (
            event_id TEXT PRIMARY KEY, event_type TEXT, timestamp TEXT, actor TEXT,
            conversation_id TEXT, content TEXT, metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE outbox (
            outbox_id TEXT PRIMARY KEY, kind TEXT, payload_json TEXT, status TEXT,
            acked_at TEXT, created_at TEXT, conversation_id TEXT
        );
        CREATE TABLE decisions (
            decision_id TEXT PRIMARY KEY, decided_at TEXT, chosen_candidate_id TEXT,
            action_probability REAL, payload_json TEXT
        );
        CREATE TABLE interaction_observations (
            observation_id TEXT PRIMARY KEY, created_at TEXT, attempt_id TEXT,
            action_json TEXT, context_json TEXT, outcome_json TEXT
        );
        """
    )
    connection.commit()
    connection.close()


def test_migratable_delivery_and_reply_candidate(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _fixture(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO action_attempts VALUES (?,?,?,?,?,?,?,?,?)",
            ("same-id", "cand-1", "sent", "check in", "help", "2025-01-01T00:00:00+00:00",
             "2025-01-01T00:01:00+00:00", "2025-01-01T00:00:30+00:00", "send-1"),
        )
        connection.execute(
            "INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
            ("send-1", "send", '{"attempt_id":"same-id","text":"hi"}', "delivered",
             "2025-01-01T00:01:00+00:00", "2025-01-01T00:00:10+00:00", "chat"),
        )
        connection.execute(
            "INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?)",
            ("reply-1", "user_message", "2025-01-01T00:02:00+00:00", "user", "chat",
             "hello", '{"attempt_id":"same-id"}', "2025-01-01T00:02:00+00:00"),
        )
        connection.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?)",
            ("d1", "2025-01-01T00:00:00+00:00", "cand-1", 0.25, "{}"),
        )
    plan = plan_migration({source: "person:one"})
    assert plan.dry_run is True
    assert len(plan.exposures) == 1
    assert plan.exposures[0].propensity == pytest.approx(0.25)
    assert "outbox:send-1" in plan.exposures[0].evidence
    assert len(plan.reply_candidates) == 1
    assert plan.reply_candidates[0].importable
    assert plan.reply_candidates[0].exposure_id == plan.exposures[0].exposure_id
    manifest = plan.manifests[0]
    assert manifest.sqlite_read_only
    assert {table.table for table in manifest.tables} == set(SOURCE_TABLES)
    assert all(len(table.sha256) == 64 for table in manifest.tables)


def test_legacy_false_and_soft_labels_are_quarantined_not_imported(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _fixture(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO interaction_observations VALUES (?,?,?,?,?,?)",
            ("obs", "2025-01-01T00:03:00+00:00", "a", "{}", "{}",
             '{"positive":false,"continued":false,"positive_probability":0.91}'),
        )
    plan = plan_migration({source: "person:one"})
    assert not plan.exposures
    assert not plan.reply_candidates
    assert len(plan.quarantine) == 1
    assert plan.quarantine[0].classification == "legacy_unknown"
    audit = next(t for t in plan.manifests[0].tables if t.table == "interaction_observations")
    assert dict(audit.reject_reasons)["legacy_unknown"] == 1


def test_missing_sources_are_manifested_and_not_inferred(tmp_path: Path) -> None:
    source = tmp_path / "partial.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE action_attempts (attempt_id TEXT, state TEXT, updated_at TEXT)")
        connection.execute(
            "INSERT INTO action_attempts VALUES (?,?,?)",
            ("not-sent", "rendered", "2025-01-01T00:00:00+00:00"),
        )
    plan = plan_migration({source: "person:one"})
    assert not plan.exposures
    missing = [table for table in plan.manifests[0].tables if not table.present]
    assert len(missing) == len(SOURCE_TABLES) - 1
    assert all(dict(table.reject_reasons) == {"missing_source_table": 1} for table in missing)


def test_two_scopes_with_same_local_id_do_not_collide_and_are_stable(tmp_path: Path) -> None:
    paths = [tmp_path / "one.db", tmp_path / "two.db"]
    for path in paths:
        _fixture(path)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO attempt_events VALUES (?,?,?,?,?,?)",
                ("event", "same-id", "committed", "sent", None,
                 "2025-01-01T00:01:00+00:00"),
            )
    mapping = {paths[0]: "person:one", paths[1]: "person:two"}
    first = plan_migration(mapping)
    second = plan_migration(mapping)
    assert first == second
    assert len({item.exposure_id for item in first.exposures}) == 2
    assert len({item.idempotency_key for item in first.exposures}) == 2
    assert first.plan_sha256 == second.plan_sha256


def test_dry_run_is_zero_write_even_when_false(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _fixture(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO attempt_events VALUES (?,?,?,?,?,?)",
            ("event", "a", "committed", "sent", None, "2025-01-01T00:01:00+00:00"),
        )
    before = source.read_bytes()
    plan = plan_migration({source: "person:one"}, dry_run=True)
    after = source.read_bytes()
    assert before == after
    assert plan.dry_run is True
    # Planner-only non-dry operation still emits a plan and does not own a destination.
    plan_non_dry = plan_migration({source: "person:one"}, dry_run=False)
    assert source.read_bytes() == after
    assert plan_non_dry.dry_run is False


def test_reply_with_multiple_exposures_is_unattributable(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _fixture(source)
    with sqlite3.connect(source) as connection:
        connection.executemany(
            "INSERT INTO attempt_events VALUES (?,?,?,?,?,?)",
            [
                ("e1", "a1", "committed", "sent", None, "2025-01-01T00:00:00+00:00"),
                ("e2", "a2", "committed", "sent", None, "2025-01-01T00:01:00+00:00"),
            ],
        )
        connection.execute(
            "INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?)",
            ("reply", "user_message", "2025-01-01T00:02:00+00:00", "user", None,
             "hey", "{}", "2025-01-01T00:02:00+00:00"),
        )
    plan = plan_migration({source: "person:one"})
    assert plan.reply_candidates[0].status == "unattributable"
    assert plan.reply_candidates[0].observed_at is None
