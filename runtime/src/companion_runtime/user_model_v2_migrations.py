"""Versioned PostgreSQL migration runner for the v2 user-model schema.

The stopped v1 SQLite files are migration *inputs*, never a service backend.  This
runner owns the PostgreSQL-native schema and records an immutable checksum for each
applied migration.  Re-running is idempotent; changing an already-applied migration is
a hard error rather than a silent schema fork.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .user_model_v2_schema import MIGRATIONS

_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """One immutable migration and the checksum of its SQL payload."""

    version: int
    statements: tuple[str, ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Result of one migration pass."""

    schema: str
    applied: tuple[int, ...]
    already_present: tuple[int, ...]
    current_version: int


def quote_schema_name(schema: str) -> str:
    """Validate and quote a PostgreSQL schema identifier.

    Configuration controls the identifier, but identifier placeholders do not exist
    in PostgreSQL.  A deliberately narrow grammar keeps interpolation safe and keeps
    operational names portable.
    """

    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("storage.schema must be a simple PostgreSQL identifier")
    return '"' + schema + '"'


def migration_records(
    migrations: Sequence[tuple[int, tuple[str, ...]]] = MIGRATIONS,
) -> tuple[MigrationRecord, ...]:
    """Return validated migrations with deterministic SHA-256 checksums."""

    records: list[MigrationRecord] = []
    previous = 0
    for version, statements in migrations:
        if not isinstance(version, int) or version <= previous:
            raise ValueError("migration versions must be strictly increasing positive integers")
        if not statements or any(not str(statement).strip() for statement in statements):
            raise ValueError(f"migration {version} must contain non-empty SQL statements")
        payload = "\n-- statement --\n".join(statement.strip() for statement in statements)
        records.append(
            MigrationRecord(
                version=version,
                statements=tuple(statements),
                checksum=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            )
        )
        previous = version
    return tuple(records)


def migration_bootstrap_statements(schema: str) -> tuple[str, ...]:
    """Return the schema/migration-ledger DDL used before versioned migrations."""

    quoted = quote_schema_name(schema)
    return (
        f"CREATE SCHEMA IF NOT EXISTS {quoted}",
        f"SET LOCAL search_path TO {quoted}, public",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations_v2 (
            version BIGINT PRIMARY KEY CHECK (version > 0),
            checksum TEXT NOT NULL CHECK (btrim(checksum) <> ''),
            applied_at TIMESTAMPTZ NOT NULL,
            runner_version TEXT NOT NULL CHECK (btrim(runner_version) <> '')
        )
        """,
    )


def migrate(
    connection: Any,
    *,
    schema: str,
    runner_version: str = "user-model-v2-migrator/1",
    migrations: Sequence[tuple[int, tuple[str, ...]]] = MIGRATIONS,
    applied_at: datetime | None = None,
) -> MigrationResult:
    """Apply pending migrations in one caller-owned transaction.

    ``connection`` is the psycopg/DB-API connection already protected by the
    Runtime's single-writer/advisory-lock discipline.  The function commits nothing;
    callers decide the transaction boundary so migration and activation can never be
    partially published.
    """

    if not str(runner_version or "").strip():
        raise ValueError("runner_version must be non-empty")
    stamp = applied_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("applied_at must be timezone-aware")

    records = migration_records(migrations)
    for statement in migration_bootstrap_statements(schema):
        connection.execute(statement)

    present_rows = connection.execute(
        "SELECT version, checksum FROM schema_migrations_v2 ORDER BY version"
    ).fetchall()
    present = {int(row[0]): str(row[1]) for row in present_rows}

    known_versions = {record.version for record in records}
    unknown = sorted(set(present) - known_versions)
    if unknown:
        raise RuntimeError(f"database contains unknown future migrations: {unknown}")

    applied: list[int] = []
    already: list[int] = []
    for record in records:
        existing = present.get(record.version)
        if existing is not None:
            if existing != record.checksum:
                raise RuntimeError(
                    f"migration {record.version} checksum mismatch: "
                    "the applied migration is immutable"
                )
            already.append(record.version)
            continue
        for statement in record.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations_v2(version, checksum, applied_at, runner_version) "
            "VALUES (%s, %s, %s, %s)",
            (record.version, record.checksum, stamp, runner_version),
        )
        applied.append(record.version)

    current = max((record.version for record in records), default=0)
    return MigrationResult(
        schema=schema,
        applied=tuple(applied),
        already_present=tuple(already),
        current_version=current,
    )


__all__ = [
    "MigrationRecord",
    "MigrationResult",
    "migrate",
    "migration_bootstrap_statements",
    "migration_records",
    "quote_schema_name",
]
