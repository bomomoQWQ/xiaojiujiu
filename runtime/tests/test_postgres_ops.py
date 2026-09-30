"""Contract tests for PostgreSQL backup/restore command construction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("psycopg")

import companion_runtime.postgres_ops as postgres_ops
from companion_runtime.postgres_ops import (
    BackupManifest,
    build_createdb_command,
    build_dropdb_command,
    build_pg_dump_command,
    build_pg_restore_command,
    load_manifest,
    migration_hash,
    parse_dsn,
)

DSN = "postgresql://runtime:s3cr%40t@db.internal:5433/companion?sslmode=require"


def test_parse_dsn_uses_conninfo_and_separates_password() -> None:
    target = parse_dsn(DSN)

    assert target.password == "s3cr@t"
    assert target.host == "db.internal"
    assert target.port == "5433"
    assert target.database == "companion"
    assert "s3cr" not in target.conninfo
    assert "password" not in target.conninfo.lower()


def test_pg_dump_command_has_no_secret_and_no_shell_syntax(tmp_path: Path) -> None:
    archive = tmp_path / "backup.dump"
    spec = build_pg_dump_command(DSN, archive, schema="runtime_v2", base_env={"PATH": "x"})

    joined = " ".join(spec.argv)
    assert spec.argv[0] == "pg_dump"
    assert spec.argv[1:4] == ("--format=custom", "--no-owner", "--no-privileges")
    assert spec.argv[spec.argv.index("--schema") + 1] == "runtime_v2"
    assert spec.argv[spec.argv.index("--file") + 1] == str(archive)
    assert "s3cr" not in joined
    assert spec.env == {"PATH": "x", "PGPASSWORD": "s3cr@t"}
    assert not any(token in joined for token in ("|", ";", "&&"))


def test_pg_dump_can_bind_exported_snapshot(tmp_path: Path) -> None:
    spec = build_pg_dump_command(
        DSN,
        tmp_path / "backup.dump",
        schema="runtime_v2",
        snapshot="00000003-0000001B-1",
        base_env={},
    )

    assert spec.argv[spec.argv.index("--snapshot") + 1] == "00000003-0000001B-1"
    assert "s3cr" not in " ".join(spec.argv)


def test_pg_restore_command_is_checked_clean_schema_restore(tmp_path: Path) -> None:
    spec = build_pg_restore_command(
        DSN, tmp_path / "backup.dump", schema="runtime_v2", base_env={}
    )

    assert spec.argv[0] == "pg_restore"
    assert "--exit-on-error" in spec.argv
    assert "--clean" in spec.argv
    assert "--if-exists" in spec.argv
    assert "--no-owner" in spec.argv
    assert "--no-privileges" in spec.argv
    assert "s3cr" not in " ".join(spec.argv)
    assert spec.env["PGPASSWORD"] == "s3cr@t"


def test_database_lifecycle_commands_target_maintenance_database() -> None:
    create = build_createdb_command(DSN, "cr_restore_deadbeef", base_env={})
    drop = build_dropdb_command(DSN, "cr_restore_deadbeef", base_env={})

    assert create.argv[0] == "createdb"
    assert create.argv[-1] == "cr_restore_deadbeef"
    assert "dbname=postgres" in create.argv[2]
    assert drop.argv[:3] == ("dropdb", "--if-exists", "--force")
    assert drop.argv[-1] == "cr_restore_deadbeef"
    assert "s3cr" not in " ".join(create.argv + drop.argv)


@pytest.mark.parametrize("name", ["bad-name", "x;drop", "", "9starts_with_digit"])
def test_temporary_database_identifier_is_restricted(name: str) -> None:
    with pytest.raises(ValueError, match="simple PostgreSQL identifier"):
        build_createdb_command(DSN, name)


@pytest.mark.parametrize("schema", ["bad-name", 'x" public', "", "9runtime"])
def test_schema_identifier_is_restricted(schema: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="simple PostgreSQL identifier"):
        build_pg_dump_command(DSN, tmp_path / "x.dump", schema=schema)


def test_migration_hash_is_stable_and_order_sensitive() -> None:
    ledger = ({"version": 1, "checksum": "a"}, {"version": 2, "checksum": "b"})

    assert migration_hash(ledger) == migration_hash(tuple(dict(row) for row in ledger))
    assert migration_hash(ledger) != migration_hash(tuple(reversed(ledger)))


def test_manifest_round_trip_contains_required_evidence(tmp_path: Path) -> None:
    ledger = ({"version": 1, "checksum": "abc"},)
    manifest = BackupManifest(
        manifest_version=1,
        created_at="2026-03-09T10:00:00+00:00",
        server="db.internal:5433",
        database="companion",
        schema="runtime_v2",
        git_revision="deadbeef",
        migration_ledger=ledger,
        migration_hash=migration_hash(ledger),
        table_counts={"schema_migrations_v2": 1, "user_model_v2": 42},
        archive_sha256="f" * 64,
    )
    path = tmp_path / "backup.manifest.json"
    path.write_text(manifest.to_json(), encoding="utf-8")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(("server", "database", "schema", "git_revision", "migration_ledger")) <= raw.keys()
    assert raw["migration_hash"] == migration_hash(ledger)
    assert raw["archive_sha256"] == "f" * 64
    assert load_manifest(path) == manifest


def test_stale_pgpassword_is_removed_when_dsn_has_no_password(tmp_path: Path) -> None:
    spec = build_pg_dump_command(
        "dbname=companion host=db.internal user=runtime",
        tmp_path / "x.dump",
        schema="runtime_v2",
        base_env={"PGPASSWORD": "stale", "PATH": "x"},
    )

    assert "PGPASSWORD" not in spec.env


def test_runner_explicitly_disables_shell(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(postgres_ops.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    spec = build_pg_dump_command(DSN, "x.dump", schema="runtime_v2", base_env={})

    postgres_ops._run(spec)

    assert calls[0][0] == (spec.argv,)
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["check"] is True
