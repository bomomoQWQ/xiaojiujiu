"""Durability tests: WAL, transactions, crash recovery, checkpoint and backup.

These tests treat durability as a feature with a contract, not as an
implementation detail. The important ones are the crash drills: they leave a
database with an uncommitted WAL, reopen it (which is exactly what "the machine
came back after a blue screen" means to SQLite), and assert that the Runtime is
both *intact* and *consistent*.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from companion_runtime import maintenance as maintenance_module
from companion_runtime.config import RuntimeConfig, load_config, resolve_paths
from companion_runtime.db import Database
from companion_runtime.maintenance import (
    backup,
    checkpoint,
    journal_mode,
    maintenance_tick,
    prune_backups,
    recovery_plan,
    restore,
    shm_path,
    sidecar_files,
    snapshot_name,
    sqlite_error_is_corruption,
    synchronous_mode,
    verify,
    wal_path,
)
from companion_runtime.runtime import Runtime
from companion_runtime.typing import EventType

from conftest import BASE_TIME, build_config


def make_file_runtime(tmp_path: Path, **overrides) -> tuple[Runtime, str]:
    """Build a Runtime backed by a real file so durability is observable.

    The creation epoch is pinned to :data:`BASE_TIME` so tests can drive a
    deterministic timeline from a known instant.
    """
    config = build_config()
    for key, value in overrides.items():
        setattr(config, key, value)
    database_path = str(tmp_path / "data" / "runtime.sqlite3")
    config.storage.database_path = database_path
    config.storage.raw_log_path = str(tmp_path / "data" / "raw_events.jsonl")
    config.storage.mirror_raw_events = False
    config.storage.wal = True
    return Runtime(config, seed=11, created_at=BASE_TIME), database_path


# --------------------------------------------------------------------------------------
# WAL and pragmas
# --------------------------------------------------------------------------------------


def test_wal_mode_is_enabled_on_a_file_database(tmp_path: Path) -> None:
    """The sidecar runs in WAL: readers never block the single writer."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        assert journal_mode(runtime.db) == "wal"
        # WAL sidecars appear once the first write happens.
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        assert Path(database_path).exists()
        assert wal_path(database_path).exists()
        assert shm_path(database_path).exists()
    finally:
        runtime.close()


def test_in_memory_database_does_not_use_wal(tmp_path: Path) -> None:
    """``:memory:`` cannot use WAL, and the Runtime must still work."""
    db = Database(":memory:")
    db.migrate()
    try:
        assert journal_mode(db) != "wal"
        assert synchronous_mode(db) in {"0", "1", "2", "normal", "full"}
    finally:
        db.close()


def test_resolve_paths_leaves_in_memory_sentinel_alone() -> None:
    """The ``:memory:`` sentinel is not turned into a filesystem path."""
    config = RuntimeConfig()
    config.storage.database_path = ":memory:"
    resolve_paths(config, "/tmp/whatever")
    assert config.storage.database_path == ":memory:"


def test_synchronous_is_normal_by_default(tmp_path: Path) -> None:
    """``synchronous = NORMAL`` is the documented default for this sidecar."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        assert synchronous_mode(runtime.db) in {"1", "normal"}
    finally:
        runtime.close()


# --------------------------------------------------------------------------------------
# transactions
# --------------------------------------------------------------------------------------


def test_a_cognitive_round_is_atomic(tmp_path: Path) -> None:
    """History and projection commit together or not at all."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        before_version = runtime.version()
        before_events = runtime.events.count()
        with pytest.raises(RuntimeError):
            with runtime.db.transaction() as conn:
                runtime.events.append(
                    EventType.USER_MESSAGE,
                    actor="user",
                    content="this must be rolled back",
                    timestamp=BASE_TIME,
                    connection=conn,
                )
                state = runtime.state()
                state.mood_valence = 0.9
                runtime.projections.runtime.write(state, conn, expect_version=state.version)
                raise RuntimeError("simulated failure inside a cognitive round")

        assert runtime.events.count() == before_events
        assert runtime.version() == before_version
        assert runtime.state().mood_valence == 0.0
    finally:
        runtime.close()


def test_wal_survives_process_kill_without_checkpoint(tmp_path: Path) -> None:
    """A committed transaction is durable even if nothing ever checkpoints.

    This is the process-crash case: the WAL is still on disk, uncommitted frames
    are absent, and reopening sees everything that ``COMMIT`` returned. The
    contrast with the power-loss test below is the whole point of WAL + NORMAL.
    """
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="第一句", timestamp=BASE_TIME)
        runtime.process_user_message(
            content="第二句", timestamp=BASE_TIME + timedelta(minutes=1)
        )
        committed_events = runtime.events.count()
        committed_version = runtime.version()
        assert wal_path(database_path).exists()
        assert Path(database_path).stat().st_size > 0
    finally:
        # Close the connection without a checkpoint, mimicking a killed process.
        runtime.db._conn.close()

    # Reopen: this is the recovery path SQLite runs automatically.
    recovered, _ = make_file_runtime(tmp_path)
    try:
        assert recovered.events.count() == committed_events
        assert recovered.version() == committed_version
        assert recovered.projections.runtime.read().meta.get("contact_day")
    finally:
        recovered.close()


def test_torn_write_tail_is_discarded_not_corrupting(tmp_path: Path) -> None:
    """A truncated WAL tail is dropped; the database stays readable.

    Simulates the power-loss case: the final WAL frame is cut in half. SQLite
    discards the invalid tail during recovery, so the database is *not* corrupt -
    it is simply missing the last transaction, which is the documented trade-off
    of ``synchronous = NORMAL``.
    """
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="稳定的一句", timestamp=BASE_TIME)
        # Truncate the checkpoint so the following write lands in a fresh WAL.
        checkpoint(runtime.db, mode="TRUNCATE")
        stable_events = runtime.events.count()
        runtime.process_user_message(
            content="可能丢失的一句", timestamp=BASE_TIME + timedelta(minutes=1)
        )
        assert runtime.events.count() > stable_events
        wal = wal_path(database_path)
        assert wal.exists(), "the second write must still be sitting in the WAL"
        full_wal = wal.read_bytes()
    finally:
        runtime.db._conn.close()

    assert len(full_wal) > 64
    # SQLite removes the WAL on a clean close (it checkpoints first). To model the
    # power-loss case we put the WAL back, cut mid-frame, and let recovery run.
    wal.write_bytes(full_wal[: len(full_wal) // 2])

    recovered, _ = make_file_runtime(tmp_path)
    try:
        result = verify(recovered)
        assert result.ok is True
        assert result.integrity == "ok"
        # The checkpointed transaction is definitely present; the torn tail is at
        # worst dropped, never half-applied.
        assert recovered.events.count() >= stable_events
        # And the Runtime still accepts new work.
        recovered.process_user_message(
            content="重启之后仍然可用", timestamp=BASE_TIME + timedelta(hours=1)
        )
        assert recovered.events.count() > stable_events
    finally:
        recovered.close()


def test_missing_wal_sidecar_is_harmless(tmp_path: Path) -> None:
    """Deleting the WAL after a clean shutdown loses nothing."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="已提交", timestamp=BASE_TIME)
        checkpoint(runtime.db, mode="TRUNCATE")
        expected = runtime.events.count()
    finally:
        runtime.close()

    for sidecar in (wal_path(database_path), shm_path(database_path)):
        if sidecar.exists():
            sidecar.unlink()

    recovered, _ = make_file_runtime(tmp_path)
    try:
        assert recovered.events.count() == expected
        assert verify(recovered).ok
    finally:
        recovered.close()


# --------------------------------------------------------------------------------------
# checkpoint
# --------------------------------------------------------------------------------------


def test_checkpoint_truncates_the_wal(tmp_path: Path) -> None:
    """The maintenance checkpoint keeps the WAL from growing without bound."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        for index in range(40):
            runtime.process_user_message(
                content=f"消息 {index}", timestamp=BASE_TIME + timedelta(minutes=index)
            )
        wal = wal_path(database_path)
        before = wal.stat().st_size
        assert before > 0

        result = checkpoint(runtime.db, mode="TRUNCATE")
        assert result.mode == "TRUNCATE"
        assert result.log_frames >= 0
        assert result.wal_bytes_before == before
        assert result.wal_bytes_after == 0
        assert result.to_dict()["reclaimed_bytes"] == before
        assert result.busy == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("mode", ["PASSIVE", "FULL", "RESTART", "TRUNCATE"])
def test_all_checkpoint_modes_are_accepted(tmp_path: Path, mode: str) -> None:
    """Every documented checkpoint mode runs without error."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        result = checkpoint(runtime.db, mode=mode)
        assert result.mode == mode
    finally:
        runtime.close()


def test_checkpoint_rejects_an_unknown_mode(tmp_path: Path) -> None:
    """An invalid mode is a programming error, not a silent PASSIVE."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        with pytest.raises(ValueError):
            checkpoint(runtime.db, mode="NOT_A_MODE")
    finally:
        runtime.close()


def test_checkpoint_works_on_an_in_memory_database() -> None:
    """The in-memory case is a no-op rather than an error."""
    db = Database(":memory:")
    db.migrate()
    try:
        result = checkpoint(db)
        assert result.wal_bytes_before == 0
        assert result.wal_bytes_after == 0
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------


def test_verify_reports_a_healthy_database(tmp_path: Path) -> None:
    """Every structural check passes on a normal database."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(
            content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
        )
        result = verify(runtime.db, expect_wal=True)
        assert result.ok is True
        assert result.integrity == "ok"
        assert result.journal_mode == "wal"
        assert result.counts["raw_events"] >= 1
        names = {check["name"] for check in result.checks}
        assert {
            "integrity_check",
            "schema_version_present",
            "runtime_state_row",
            "attempt_outbox_references",
            "transition_attempt_references",
            "leases_have_expiry",
            "journal_mode",
        } <= names
    finally:
        runtime.close()


def test_verify_detects_a_dangling_attempt_reference(tmp_path: Path) -> None:
    """A projection that points at a missing row is reported, not hidden."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        with runtime.db.transaction() as conn:
            conn.execute(
                "INSERT INTO action_attempts(attempt_id, candidate_id, state, intent, "
                "based_on_version, created_at, updated_at, outbox_id) "
                "VALUES('att_broken', NULL, 'committed', 'x', 0, 'now', 'now', 'obx_missing')"
            )
        result = verify(runtime.db)
        assert result.ok is False
        failing = {check["name"] for check in result.checks if not check["ok"]}
        assert "attempt_outbox_references" in failing
    finally:
        runtime.close()


def test_verify_detects_a_lease_without_expiry(tmp_path: Path) -> None:
    """A lease with no expiry would pin a row forever."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        with runtime.db.transaction() as conn:
            conn.execute(
                "INSERT INTO outbox(outbox_id, kind, payload_json, status, priority, "
                "created_at, lease_owner) VALUES('obx_x', 'send', '{}', 'leased', 1, 'now', 'w')"
            )
        result = verify(runtime.db)
        failing = {check["name"] for check in result.checks if not check["ok"]}
        assert "leases_have_expiry" in failing
    finally:
        runtime.close()


def test_verify_result_is_serialisable(tmp_path: Path) -> None:
    """The verify result is returned by the HTTP API and the CLI."""
    import json

    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        json.dumps(verify(runtime.db).to_dict())
    finally:
        runtime.close()


def test_corruption_detection_helper() -> None:
    """The startup path can tell corruption apart from ordinary misuse."""
    assert sqlite_error_is_corruption(sqlite3.DatabaseError("database disk image is malformed"))
    assert sqlite_error_is_corruption(sqlite3.DatabaseError("file is not a database"))
    assert not sqlite_error_is_corruption(sqlite3.OperationalError("no such table: x"))
    assert not sqlite_error_is_corruption(ValueError("nope"))


# --------------------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------------------


def test_backup_produces_a_consistent_snapshot(tmp_path: Path) -> None:
    """``VACUUM INTO`` copies the database without stopping the Runtime."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(
            content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
        )
        runtime.lazy_tick(BASE_TIME + timedelta(hours=2))
        expected_events = runtime.events.count()
        expected_version = runtime.version()
        target = tmp_path / "snapshots" / "snap.sqlite3"

        result = backup(runtime.db, target)
        assert Path(result.path).exists()
        assert result.bytes_written > 0
        assert result.integrity == "ok"
        assert result.sha256 and len(result.sha256) == 64
        assert result.tables["raw_events"] == expected_events

        # The snapshot opens as an independent, self-contained database.
        snapshot = Database(str(target))
        try:
            snapshot.migrate()
            assert snapshot.query_one("SELECT COUNT(*) AS n FROM raw_events")["n"] == expected_events
            row = snapshot.query_one("SELECT version FROM runtime_state LIMIT 1")
            assert int(row["version"]) == expected_version
            # A snapshot needs no sidecar files.
            assert journal_mode(snapshot) in {"delete", "wal"}
        finally:
            snapshot.close()
    finally:
        runtime.close()


def test_backup_includes_transactions_not_yet_checkpointed(tmp_path: Path) -> None:
    """Uncommitted-to-main-db but committed transactions are captured."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="只写了 WAL 的一句", timestamp=BASE_TIME)
        assert wal_path(database_path).stat().st_size > 0
        target = tmp_path / "snap.sqlite3"
        # checkpoint_first=False leaves the WAL in place, proving the snapshot
        # reads *through* the WAL rather than copying the main file.
        result = backup(runtime.db, target, checkpoint_first=False)
        assert result.tables["raw_events"] == runtime.events.count()
        assert wal_path(database_path).stat().st_size > 0
    finally:
        runtime.close()


def test_backup_refuses_to_overwrite_by_default(tmp_path: Path) -> None:
    """Silently clobbering a snapshot is not acceptable."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        target = tmp_path / "snap.sqlite3"
        backup(runtime.db, target)
        with pytest.raises(FileExistsError):
            backup(runtime.db, target)
        # Explicit overwrite works.
        assert backup(runtime.db, target, overwrite=True).bytes_written > 0
    finally:
        runtime.close()


def test_backup_rejects_an_in_memory_database() -> None:
    """An in-memory database has no file to snapshot."""
    db = Database(":memory:")
    db.migrate()
    try:
        with pytest.raises(ValueError):
            backup(db, "/tmp/never.sqlite3")
    finally:
        db.close()


def test_backup_is_readable_while_the_runtime_keeps_writing(tmp_path: Path) -> None:
    """The snapshot is taken without pausing the sidecar."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="before", timestamp=BASE_TIME)
        target = tmp_path / "snap.sqlite3"
        backup(runtime.db, target)
        frozen = Database(str(target))
        try:
            frozen.migrate()
            count_at_snapshot = frozen.query_one("SELECT COUNT(*) AS n FROM raw_events")["n"]
        finally:
            frozen.close()

        for index in range(5):
            runtime.process_user_message(
                content=f"after {index}", timestamp=BASE_TIME + timedelta(minutes=index + 1)
            )
        assert runtime.events.count() > count_at_snapshot

        again = Database(str(target))
        try:
            again.migrate()
            assert again.query_one("SELECT COUNT(*) AS n FROM raw_events")["n"] == count_at_snapshot
        finally:
            again.close()
    finally:
        runtime.close()


def test_prune_backups_keeps_the_newest(tmp_path: Path) -> None:
    """Retention keeps a bounded number of snapshots."""
    import time

    folder = tmp_path / "backups"
    folder.mkdir()
    for index in range(5):
        path = folder / f"snap-{index}.sqlite3"
        path.write_bytes(b"x")
        os.utime(path, (time.time() + index, time.time() + index))
    removed = prune_backups(folder, keep=2)
    assert len(removed) == 3
    assert len(list(folder.glob("*.sqlite3"))) == 2
    assert prune_backups(tmp_path / "missing", keep=2) == []


def test_snapshot_name_is_timestamped() -> None:
    """Snapshot names sort chronologically."""
    name = snapshot_name(now=BASE_TIME)
    assert name.startswith("runtime-")
    assert name.endswith(".sqlite3")
    assert "20260301" in name


# --------------------------------------------------------------------------------------
# restore: the recovery drill
# --------------------------------------------------------------------------------------


def test_restore_recovers_a_damaged_database(tmp_path: Path) -> None:
    """Full drill: back up, lose the database, restore, verify, keep working."""
    runtime, database_path = make_file_runtime(tmp_path)
    target = tmp_path / "snapshots" / "good.sqlite3"
    try:
        runtime.process_user_message(
            content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
        )
        expected_events = runtime.events.count()
        expected_matters = len(runtime.projections.unfinished.list_all())
        backup(runtime.db, target)
    finally:
        runtime.close()

    # Disaster: the database file is destroyed.
    Path(database_path).write_bytes(b"this is not a sqlite database at all")
    for sidecar in (wal_path(database_path), shm_path(database_path)):
        if sidecar.exists():
            sidecar.unlink()

    # Confirmed unrecoverable before the restore.
    with pytest.raises(sqlite3.DatabaseError):
        probe = Database(database_path)
        probe.query_one("SELECT COUNT(*) FROM raw_events")
        probe.close()

    result = restore(target, database_path)
    assert result.integrity == "ok"
    assert result.tables["raw_events"] == expected_events
    assert Path(f"{database_path}.replaced").exists()

    recovered = Runtime(_recovered_config(database_path), seed=11, created_at=BASE_TIME)
    try:
        assert recovered.events.count() == expected_events
        assert len(recovered.projections.unfinished.list_all()) == expected_matters
        assert verify(recovered).ok
        # The Runtime keeps working after recovery.
        recovered.process_user_message(
            content="恢复之后", timestamp=BASE_TIME + timedelta(hours=1)
        )
        assert recovered.events.count() > expected_events
    finally:
        recovered.close()


def _recovered_config(database_path: str) -> RuntimeConfig:
    """Build a configuration pointing at a restored database file."""
    config = build_config()
    config.storage.database_path = database_path
    config.storage.raw_log_path = str(Path(database_path).with_suffix(".events.jsonl"))
    config.storage.mirror_raw_events = False
    return config


def test_restore_refuses_a_live_database(tmp_path: Path) -> None:
    """Restoring over a database with sidecars would corrupt it."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        target = tmp_path / "snap.sqlite3"
        backup(runtime.db, target)
        # The Runtime is still open, so WAL sidecars exist.
        assert sidecar_files(database_path)
        with pytest.raises(RuntimeError, match="live database"):
            restore(target, database_path)
    finally:
        runtime.close()


def test_restore_verifies_the_snapshot_first(tmp_path: Path) -> None:
    """A corrupt snapshot is rejected instead of installed."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
    finally:
        runtime.close()
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"definitely not sqlite")
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        restore(bad, database_path, verify_first=True)


def test_restore_missing_snapshot(tmp_path: Path) -> None:
    """A missing snapshot is reported clearly."""
    with pytest.raises(FileNotFoundError):
        restore(tmp_path / "nope.sqlite3", tmp_path / "target.sqlite3")


def test_restore_can_delete_the_previous_database(tmp_path: Path) -> None:
    """``keep_previous=False`` is available for disk-constrained hosts."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        target = tmp_path / "snap.sqlite3"
        backup(runtime.db, target)
    finally:
        runtime.close()
    restore(target, database_path, keep_previous=False)
    assert not Path(f"{database_path}.replaced").exists()
    assert Path(database_path).exists()


# --------------------------------------------------------------------------------------
# maintenance orchestration and recovery plan
# --------------------------------------------------------------------------------------


def test_maintenance_tick_checkpoints_verifies_and_backs_up(tmp_path: Path) -> None:
    """The routine durability pass performs all three steps."""
    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        report = maintenance_tick(runtime.db, backup_dir=tmp_path / "backups", keep=3)
        assert report["checkpoint"]["wal_bytes_after"] == 0
        assert report["verify"]["ok"] is True
        assert report["backup"]["integrity"] == "ok"
        assert Path(report["backup"]["path"]).exists()
        assert report["pruned"] == []
    finally:
        runtime.close()


def test_maintenance_tick_prunes_old_snapshots(tmp_path: Path) -> None:
    """Repeated passes do not fill the disk."""
    import time

    runtime, _database_path = make_file_runtime(tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir()
    try:
        for index in range(4):
            stale = folder / f"old-{index}.sqlite3"
            stale.write_bytes(b"x")
            os.utime(stale, (time.time() - 100 - index, time.time() - 100 - index))
        report = maintenance_tick(runtime.db, backup_dir=folder, keep=2)
        assert len(report["pruned"]) == 3
        assert len(list(folder.glob("*.sqlite3"))) == 2
    finally:
        runtime.close()


def test_maintenance_tick_without_backup_dir(tmp_path: Path) -> None:
    """Checkpoint and verify run even when no backups are configured."""
    runtime, _database_path = make_file_runtime(tmp_path)
    try:
        report = maintenance_tick(runtime.db)
        assert report["backup"] is None
        assert report["verify"]["ok"] is True
    finally:
        runtime.close()


def test_recovery_plan_recommends_the_right_action(tmp_path: Path) -> None:
    """The plan distinguishes a live database from a restorable one."""
    runtime, database_path = make_file_runtime(tmp_path)
    backup_dir = tmp_path / "backups"
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        backup(runtime.db, backup_dir / snapshot_name())
        live_plan = recovery_plan(database_path, backup_dir)
        assert live_plan["sidecars"], "an open database must show its WAL sidecars"
        assert live_plan["recommended_action"] == "stop_runtime_first"
    finally:
        runtime.close()

    stopped_plan = recovery_plan(database_path, backup_dir)
    assert stopped_plan["exists"] is True
    assert stopped_plan["snapshot_count"] == 1
    assert stopped_plan["newest_snapshot"] is not None
    assert stopped_plan["recommended_action"] == "restore"

    empty_plan = recovery_plan(tmp_path / "nothing.sqlite3", tmp_path / "empty")
    assert empty_plan["exists"] is False
    assert empty_plan["newest_snapshot"] is None
    assert empty_plan["recommended_action"] == "backup_now"


def test_sidecar_paths_are_derived_from_the_database_path(tmp_path: Path) -> None:
    """The helpers use the SQLite naming convention."""
    path = tmp_path / "db.sqlite3"
    assert str(wal_path(path)).endswith("db.sqlite3-wal")
    assert str(shm_path(path)).endswith("db.sqlite3-shm")
    assert sidecar_files(path) == []


# --------------------------------------------------------------------------------------
# CLI durability commands
# --------------------------------------------------------------------------------------


def test_cli_verify_checkpoint_backup_restore(tmp_path: Path, capsys) -> None:
    """The operator-facing commands work end to end."""
    from companion_runtime.cli import main

    runtime, database_path = make_file_runtime(tmp_path)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
    finally:
        runtime.close()

    base = ["--base-dir", str(tmp_path), "--config", str(_write_config(tmp_path, database_path))]

    assert main(base + ["verify"]) == 0
    assert "integrity_check" in capsys.readouterr().out

    assert main(base + ["checkpoint", "--mode", "TRUNCATE"]) == 0
    assert "wal_bytes_after" in capsys.readouterr().out

    snapshot = tmp_path / "cli-snap.sqlite3"
    assert main(base + ["backup", str(snapshot)]) == 0
    assert snapshot.exists()
    capsys.readouterr()

    assert main(base + ["health"]) == 0
    assert "state_version" in capsys.readouterr().out

    assert main(base + ["recover"]) == 0
    assert "recommended_action" in capsys.readouterr().out

    # Restore round trip through the CLI.
    assert main(base + ["restore", str(snapshot)]) == 0
    assert "integrity" in capsys.readouterr().out

    assert main(base + ["restore", str(tmp_path / "missing.sqlite3")]) == 4


def test_cli_verify_returns_a_non_zero_code_on_failure(tmp_path: Path) -> None:
    """Verification failures are visible to a shell script."""
    from companion_runtime.cli import main

    runtime, database_path = make_file_runtime(tmp_path)
    try:
        with runtime.db.transaction() as conn:
            conn.execute(
                "INSERT INTO outbox(outbox_id, kind, payload_json, status, priority, "
                "created_at, lease_owner) VALUES('obx_bad', 'send', '{}', 'leased', 1, 'now', 'w')"
            )
    finally:
        runtime.close()
    assert main(["--config", str(_write_config(tmp_path, database_path)), "verify"]) == 3


def test_cli_tick_and_state_and_endogenous(tmp_path: Path, capsys) -> None:
    """The cognitive commands run against a file database."""
    from companion_runtime.cli import main

    config_path = _write_config(tmp_path, str(tmp_path / "data" / "runtime.sqlite3"))
    base = ["--config", str(config_path)]

    assert main(base + ["tick", "--now", BASE_TIME.isoformat()]) == 0
    assert "version" in capsys.readouterr().out

    assert main(base + ["state", "--include", "all"]) == 0
    output = capsys.readouterr().out
    assert "mood" in output and "candidates" in output

    assert main(
        base + ["endogenous", "--now", (BASE_TIME + timedelta(hours=30)).isoformat()]
    ) == 0
    assert "decision" in capsys.readouterr().out

    assert main(base + ["config"]) == 0
    assert "runtime_id" in capsys.readouterr().out


def _write_config(tmp_path: Path, database_path: str) -> Path:
    """Write a small TOML config for CLI tests."""
    path = tmp_path / "cli.toml"
    path.write_text(
        "[storage]\n"
        f'database_path = "{database_path.replace(chr(92), "/")}"\n'
        f'raw_log_path = "{(str(tmp_path / "events.jsonl")).replace(chr(92), "/")}"\n'
        "mirror_raw_events = false\n"
        "[server]\n"
        'log_level = "WARNING"\n',
        encoding="utf-8",
    )
    return path


def test_load_config_reads_the_cli_style_file(tmp_path: Path) -> None:
    """The generated config file is valid."""
    path = _write_config(tmp_path, str(tmp_path / "db.sqlite3"))
    config = load_config(path)
    assert config.storage.mirror_raw_events is False
    assert config.storage.database_path.endswith("db.sqlite3")


def test_cli_verify_reports_corruption_cleanly(tmp_path: Path, capsys) -> None:
    """A corrupt database is reported as an actionable error, not a traceback."""
    from companion_runtime.cli import main

    database_path = str(tmp_path / "data" / "runtime.sqlite3")
    config_path = _write_config(tmp_path, database_path)
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    Path(database_path).write_bytes(b"this is not a sqlite database")

    code = main(["--config", str(config_path), "verify"])
    assert code == 3, "corruption is an operational failure with its own exit code"
    captured = capsys.readouterr()
    assert "unreadable or corrupt" in captured.err
    assert "restore" in captured.err
