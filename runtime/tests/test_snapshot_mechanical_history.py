from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "snapshot_mechanical_history.py"
SPEC = importlib.util.spec_from_file_location("snapshot_mechanical_history", SCRIPT)
assert SPEC and SPEC.loader
snapshotter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshotter)


def _make_database(directory: Path, *, event: str = "committed") -> Path:
    directory.mkdir(parents=True)
    database = directory / "companion.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 17")
        connection.execute("CREATE TABLE raw_events (event_id TEXT, content TEXT)")
        connection.execute("INSERT INTO raw_events VALUES ('main', 'main')")
        connection.execute("CREATE TABLE memories (memory_id TEXT, summary TEXT)")
        connection.execute("INSERT INTO memories VALUES ('m1', ?)", (event,))
    return database


def _state(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_size, stat.st_mtime_ns


def test_discovers_only_top_level_active_fleet_directories(tmp_path: Path) -> None:
    source = tmp_path / "fleet"
    source.mkdir()
    _make_database(source / "default-friendmessage-20002")
    _make_database(source / "default-friendmessage-20001")
    _make_database(source / "default-friendmessage-20003_retired")
    _make_database(source / "default-friendmessage-20004_wiped-20261001")
    _make_database(source / "archive" / "default-friendmessage-nested")
    output = tmp_path / "snapshot"

    manifest = snapshotter.snapshot_mechanical_history(
        source, output, created_at="2026-10-01T01:02:03Z"
    )

    assert [person["slug"] for person in manifest["people"]] == [
        "default-friendmessage-20001",
        "default-friendmessage-20002",
    ]
    assert [person["scope_key"] for person in manifest["people"]] == [
        "default:FriendMessage:20001",
        "default:FriendMessage:20002",
    ]
    assert sorted(path.name for path in output.iterdir()) == [
        "default-friendmessage-20001.sqlite3",
        "default-friendmessage-20002.sqlite3",
        "manifest.json",
    ]


def test_wal_commit_is_in_backup_and_source_files_are_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "fleet"
    person = source / "default-friendmessage-123"
    person.mkdir(parents=True)
    database = person / "companion.sqlite3"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE raw_events (event_id TEXT, content TEXT)")
        writer.commit()
        writer.execute("INSERT INTO raw_events VALUES ('wal-only', '浪潮')")
        writer.commit()
        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        assert wal.stat().st_size > 0 and shm.stat().st_size > 0
        before_main = _state(database)
        before_wal = _state(wal)
        output = tmp_path / "snapshot"

        # A subprocess matches the stopped-fleet contract: its SQLite connection has
        # no shared in-process state with this writer.  The read-only snapshot must
        # still consume committed WAL frames without changing the main/WAL files.
        command = [
            sys.executable,
            str(SCRIPT),
            "--source-root",
            str(source),
            "--output-dir",
            str(output),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr

        assert before_main == _state(database)
        assert before_wal == _state(wal)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        snapshot = output / manifest["people"][0]["snapshot_filename"]
        with sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as reader:
            assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert reader.execute("SELECT * FROM raw_events").fetchall() == [
                ("wal-only", "浪潮")
            ]
    finally:
        writer.close()


def test_manifest_has_stable_utf8_structure_and_table_audit(tmp_path: Path) -> None:
    source = tmp_path / "源"
    database = _make_database(source / "default-friendmessage-42", event="浪潮记忆")
    output = tmp_path / "out"
    manifest = snapshotter.snapshot_mechanical_history(
        source, output, created_at="2026-10-01T01:02:03Z"
    )

    raw = (output / "manifest.json").read_bytes()
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded == manifest
    assert raw.endswith(b"\n")
    assert b"\\u6d6a" not in raw
    assert list(manifest) == ["manifest_version", "created_at", "source_root", "people"]
    assert manifest["manifest_version"] == 1
    assert manifest["created_at"] == "2026-10-01T01:02:03Z"
    assert manifest["source_root"] == str(source.resolve())

    person = manifest["people"][0]
    assert person["source_path"] == str(database.resolve())
    assert person["snapshot_filename"] == "default-friendmessage-42.sqlite3"
    assert person["sqlite"] == {
        "source_integrity_check": "ok",
        "snapshot_integrity_check": "ok",
        "user_version": 17,
    }
    assert person["tables"]["raw_events"] == {
        "present": True,
        "count": 1,
        "columns": ["event_id", "content"],
    }
    assert person["tables"]["event_semantics"] == {
        "present": False,
        "count": 0,
        "columns": [],
    }
    assert set(person["tables"]) == set(snapshotter.MECHANICAL_TABLES)
    for key in ("main", "wal", "shm"):
        record = person["source_files"][key]
        if record is not None:
            assert set(record) == {"sha256", "size", "mtime", "mtime_ns"}
            assert len(record["sha256"]) == 64
    assert set(person["snapshot"]) == {"sha256", "size"}


def test_identical_inputs_and_timestamp_produce_identical_manifest_bytes(tmp_path: Path) -> None:
    source = tmp_path / "fleet"
    _make_database(source / "default-friendmessage-7")
    first = tmp_path / "first"
    second = tmp_path / "second"
    stamp = "2026-10-01T00:00:00Z"

    snapshotter.snapshot_mechanical_history(source, first, created_at=stamp)
    snapshotter.snapshot_mechanical_history(source, second, created_at=stamp)

    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    assert (first / "default-friendmessage-7.sqlite3").read_bytes() == (
        second / "default-friendmessage-7.sqlite3"
    ).read_bytes()


def test_rejects_missing_database_without_publishing_output(tmp_path: Path) -> None:
    source = tmp_path / "fleet"
    (source / "default-friendmessage-9").mkdir(parents=True)
    output = tmp_path / "out"

    with pytest.raises(snapshotter.SnapshotError, match="missing companion.sqlite3"):
        snapshotter.snapshot_mechanical_history(source, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".out.tmp-*"))


def test_rejects_nonempty_output_before_reading_sources(tmp_path: Path) -> None:
    source = tmp_path / "fleet"
    _make_database(source / "default-friendmessage-9")
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("do not replace", encoding="utf-8")

    with pytest.raises(snapshotter.SnapshotError, match="not empty"):
        snapshotter.snapshot_mechanical_history(source, output)

    assert sentinel.read_text(encoding="utf-8") == "do not replace"


def test_rejects_duplicate_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "fleet"
    database = _make_database(source / "default-friendmessage-a")
    original_iterdir = Path.iterdir

    # Feed discovery the same identity twice.  This exercises the guard directly and
    # works on case-insensitive filesystems where case-only duplicate names are illegal.
    def duplicate_iterdir(path: Path):
        if path == source.resolve():
            return iter((database.parent, database.parent))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", duplicate_iterdir)
    with pytest.raises(snapshotter.SnapshotError, match="duplicate slug"):
        snapshotter.snapshot_mechanical_history(source, tmp_path / "out")


def test_rejects_integrity_failure_and_removes_temporary_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "fleet"
    _make_database(source / "default-friendmessage-9")
    output = tmp_path / "out"
    original = snapshotter._check_integrity

    def fail_source(connection: sqlite3.Connection, label: str) -> str:
        if label.startswith("source "):
            raise snapshotter.SnapshotError("source integrity_check failed: corrupt")
        return original(connection, label)

    monkeypatch.setattr(snapshotter, "_check_integrity", fail_source)
    with pytest.raises(snapshotter.SnapshotError, match="integrity_check failed"):
        snapshotter.snapshot_mechanical_history(source, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".out.tmp-*"))


def test_existing_empty_output_is_replaced_atomically(tmp_path: Path) -> None:
    source = tmp_path / "fleet"
    _make_database(source / "default-friendmessage-9")
    output = tmp_path / "out"
    output.mkdir()
    old_identity = os.stat(output).st_ino

    snapshotter.snapshot_mechanical_history(source, output)

    assert (output / "manifest.json").is_file()
    assert os.stat(output).st_ino != old_identity
