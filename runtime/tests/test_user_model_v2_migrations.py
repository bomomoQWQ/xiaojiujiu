"""Tests for the PostgreSQL-native v2 migration runner."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from companion_runtime.user_model_v2_schema import USER_MODEL_SCHEMA_VERSION
from companion_runtime.user_model_v2_migrations import (
    migrate,
    migration_bootstrap_statements,
    migration_records,
    quote_schema_name,
)


class Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, present=()):
        self.present = list(present)
        self.calls: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith("SELECT version"):
            return Result(self.present)
        return Result()


def test_schema_identifier_is_narrow_and_quoted() -> None:
    assert quote_schema_name("runtime_v2") == '"runtime_v2"'
    for bad in ("", "has-dash", "x;drop schema public", "a.b", 'x"y'):
        with pytest.raises(ValueError, match="simple PostgreSQL identifier"):
            quote_schema_name(bad)


def test_records_have_stable_nonempty_checksums() -> None:
    first = migration_records()
    second = migration_records()
    assert first == second
    assert tuple(record.version for record in first) == tuple(range(1, USER_MODEL_SCHEMA_VERSION + 1))
    assert all(len(record.checksum) == 64 for record in first)


def test_bootstrap_sets_the_configured_schema_and_native_ledger() -> None:
    ddl = "\n".join(migration_bootstrap_statements("runtime_v2"))
    assert 'CREATE SCHEMA IF NOT EXISTS "runtime_v2"' in ddl
    assert 'SET LOCAL search_path TO "runtime_v2", public' in ddl
    assert "TIMESTAMPTZ" in ddl
    assert "BIGINT" in ddl
    assert "?" not in ddl


def test_migrate_applies_every_statement_then_records_the_checksum() -> None:
    connection = FakeConnection()
    result = migrate(
        connection,
        schema="runtime_v2",
        migrations=((1, ("CREATE TABLE one(id BIGINT)", "CREATE TABLE two(id BIGINT)")),),
        applied_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert result.applied == (1,)
    assert result.already_present == ()
    assert any(sql == "CREATE TABLE one(id BIGINT)" for sql, _ in connection.calls)
    insert = next((sql, params) for sql, params in connection.calls if sql.startswith("INSERT INTO"))
    assert insert[1][0] == 1
    assert len(insert[1][1]) == 64
    assert insert[1][2].tzinfo is not None


def test_migrate_is_idempotent_and_verifies_immutable_checksums() -> None:
    migrations = ((1, ("SELECT 1",)),)
    checksum = migration_records(migrations)[0].checksum
    connection = FakeConnection(present=((1, checksum),))
    result = migrate(connection, schema="runtime_v2", migrations=migrations)
    assert result.applied == ()
    assert result.already_present == (1,)
    assert not any(sql == "SELECT 1" for sql, _ in connection.calls)

    changed = FakeConnection(present=((1, "0" * 64),))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        migrate(changed, schema="runtime_v2", migrations=migrations)


def test_migrate_refuses_unknown_future_database_versions() -> None:
    connection = FakeConnection(present=((99, "a" * 64),))
    with pytest.raises(RuntimeError, match="unknown future migrations"):
        migrate(connection, schema="runtime_v2", migrations=((1, ("SELECT 1",)),))


def test_naive_migration_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        migrate(
            FakeConnection(),
            schema="runtime_v2",
            migrations=((1, ("SELECT 1",)),),
            applied_at=datetime(2026, 9, 30),
        )
