"""PostgreSQL-only logical backup and destructive restore-drill contract.

This module intentionally lives beside, rather than inside, the legacy SQLite
``maintenance`` module.  Command construction is pure and always produces an argv
sequence plus a separate environment mapping; no command is ever run through a
shell and passwords never appear in argv, manifests, or diagnostics.

A backup is a PostgreSQL custom-format archive accompanied by a JSON manifest.  A
restore drill creates a disposable database, restores the archive there, compares
the migration ledger and per-table row counts, and then drops the database.  It
never restores over the source database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
except ImportError:  # pragma: no cover - package metadata requires psycopg
    psycopg = None
    sql = None
    conninfo_to_dict = None
    make_conninfo = None

from .user_model_v2_migrations import quote_schema_name

MANIFEST_VERSION = 1
_SAFE_DATABASE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A subprocess invocation with secrets separated from argv."""

    argv: tuple[str, ...]
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """Non-secret libpq target plus the password used only in child environments."""

    conninfo: str
    password: str | None
    host: str
    port: str
    user: str
    database: str

    @property
    def server(self) -> str:
        """Return a non-secret server identity suitable for a manifest."""
        return f"{self.host or 'local'}:{self.port or '5432'}"


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Metadata required to establish that a logical restore is equivalent."""

    manifest_version: int
    created_at: str
    server: str
    database: str
    schema: str
    git_revision: str | None
    migration_ledger: tuple[dict[str, Any], ...]
    migration_hash: str
    table_counts: dict[str, int]
    archive_sha256: str

    def to_json(self) -> str:
        """Serialize deterministically so manifests are easy to diff and sign."""
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class DrillResult:
    """Successful restore-drill evidence."""

    temporary_database: str
    schema: str
    migration_hash: str
    table_counts: dict[str, int]


def _require_psycopg() -> None:
    if conninfo_to_dict is None or make_conninfo is None or psycopg is None:
        raise RuntimeError("PostgreSQL operations require psycopg 3")


def parse_dsn(dsn: str) -> ConnectionTarget:
    """Parse a DSN with psycopg's conninfo parser and remove its password.

    Both URI and keyword/value libpq forms are accepted.  The returned ``conninfo``
    can safely be placed in argv because the password is moved to ``PGPASSWORD``.
    """
    _require_psycopg()
    values = conninfo_to_dict(dsn)
    password = values.pop("password", None)
    database = str(values.get("dbname") or "")
    if not database:
        raise ValueError("PostgreSQL DSN must identify a database")
    return ConnectionTarget(
        conninfo=make_conninfo(**values),
        password=None if password is None else str(password),
        host=str(values.get("host") or ""),
        port=str(values.get("port") or "5432"),
        user=str(values.get("user") or ""),
        database=database,
    )


def command_environment(
    target: ConnectionTarget, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a child environment, putting the password only in ``PGPASSWORD``."""
    env = dict(os.environ if base is None else base)
    if target.password is None:
        env.pop("PGPASSWORD", None)
    else:
        env["PGPASSWORD"] = target.password
    return env


def build_pg_dump_command(
    dsn: str,
    archive: str | Path,
    *,
    schema: str,
    snapshot: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> CommandSpec:
    """Purely construct a custom-format, schema-scoped ``pg_dump`` invocation.

    ``snapshot`` is an exported PostgreSQL snapshot.  Supplying it lets manifest
    queries and ``pg_dump`` observe exactly the same committed database state.
    """
    quote_schema_name(schema)
    target = parse_dsn(dsn)
    parts = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--schema",
        schema,
        "--file",
        os.fspath(archive),
    ]
    if snapshot is not None:
        if not str(snapshot).strip():
            raise ValueError("snapshot must be non-empty")
        parts.extend(("--snapshot", str(snapshot)))
    parts.extend(("--dbname", target.conninfo))
    return CommandSpec(tuple(parts), command_environment(target, base_env))


def build_pg_restore_command(
    dsn: str, archive: str | Path, *, schema: str, base_env: Mapping[str, str] | None = None
) -> CommandSpec:
    """Purely construct a clean restore into the DSN's database."""
    quote_schema_name(schema)
    target = parse_dsn(dsn)
    argv = (
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        "--clean",
        "--if-exists",
        "--schema",
        schema,
        "--dbname",
        target.conninfo,
        os.fspath(archive),
    )
    return CommandSpec(argv, command_environment(target, base_env))


def _with_database(target: ConnectionTarget, database: str) -> ConnectionTarget:
    if not _SAFE_DATABASE.fullmatch(database):
        raise ValueError("temporary database must be a simple PostgreSQL identifier")
    values = conninfo_to_dict(target.conninfo)
    values["dbname"] = database
    return ConnectionTarget(
        conninfo=make_conninfo(**values),
        password=target.password,
        host=target.host,
        port=target.port,
        user=target.user,
        database=database,
    )


def build_createdb_command(
    dsn: str, database: str, *, base_env: Mapping[str, str] | None = None
) -> CommandSpec:
    """Construct creation of a disposable database through a maintenance DB."""
    target = parse_dsn(dsn)
    maintenance = _with_database(target, "postgres")
    if not _SAFE_DATABASE.fullmatch(database):
        raise ValueError("temporary database must be a simple PostgreSQL identifier")
    return CommandSpec(
        ("createdb", "--maintenance-db", maintenance.conninfo, database),
        command_environment(target, base_env),
    )


def build_dropdb_command(
    dsn: str, database: str, *, base_env: Mapping[str, str] | None = None
) -> CommandSpec:
    """Construct forced cleanup of a disposable database."""
    target = parse_dsn(dsn)
    maintenance = _with_database(target, "postgres")
    if not _SAFE_DATABASE.fullmatch(database):
        raise ValueError("temporary database must be a simple PostgreSQL identifier")
    return CommandSpec(
        ("dropdb", "--if-exists", "--force", "--maintenance-db", maintenance.conninfo, database),
        command_environment(target, base_env),
    )


def _run(spec: CommandSpec) -> None:
    """Run one checked command without a shell or captured secret-bearing output."""
    subprocess.run(spec.argv, env=spec.env, check=True, shell=False)


def sha256_file(path: str | Path) -> str:
    """Hash a file without loading the archive into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migration_hash(ledger: Sequence[Mapping[str, Any]]) -> str:
    """Hash the ordered migration versions/checksums in a stable representation."""
    canonical = [
        {"version": int(row["version"]), "checksum": str(row["checksum"])}
        for row in ledger
    ]
    payload = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _inventory(connection: Any, schema: str) -> tuple[tuple[dict[str, Any], ...], dict[str, int]]:
    """Read the migration ledger and exact ordinary-table row counts."""
    quote_schema_name(schema)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT version, checksum FROM "
            + sql.Identifier(schema, "schema_migrations_v2").as_string(connection)
            + " ORDER BY version"
        )
        ledger = tuple(
            {"version": int(version), "checksum": str(checksum)}
            for version, checksum in cursor.fetchall()
        )
        cursor.execute(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = %s ORDER BY tablename",
            (schema,),
        )
        tables = [str(row[0]) for row in cursor.fetchall()]
        counts: dict[str, int] = {}
        for table in tables:
            cursor.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            ))
            counts[table] = int(cursor.fetchone()[0])
    return ledger, counts


def _git_revision(cwd: str | Path | None = None) -> str | None:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=cwd,
        check=False,
        shell=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def create_backup(
    dsn: str,
    archive: str | Path,
    *,
    schema: str,
    manifest_path: str | Path | None = None,
    git_cwd: str | Path | None = None,
) -> BackupManifest:
    """Create a custom archive and its non-secret verification manifest."""
    _require_psycopg()
    target = parse_dsn(dsn)
    archive_path = Path(archive)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep this transaction open while pg_dump imports its snapshot: inventory
    # and archive then describe one committed state even if the live service writes.
    with psycopg.connect(dsn) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = str(connection.execute("SELECT pg_export_snapshot()").fetchone()[0])
        ledger, counts = _inventory(connection, schema)
        _run(build_pg_dump_command(dsn, archive_path, schema=schema, snapshot=snapshot))
    manifest = BackupManifest(
        manifest_version=MANIFEST_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        server=target.server,
        database=target.database,
        schema=schema,
        git_revision=_git_revision(git_cwd),
        migration_ledger=ledger,
        migration_hash=migration_hash(ledger),
        table_counts=counts,
        archive_sha256=sha256_file(archive_path),
    )
    destination = Path(manifest_path) if manifest_path else archive_path.with_suffix(
        archive_path.suffix + ".manifest.json"
    )
    destination.write_text(manifest.to_json(), encoding="utf-8")
    return manifest


def load_manifest(path: str | Path) -> BackupManifest:
    """Load and minimally validate a backup manifest."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("unsupported PostgreSQL backup manifest version")
    raw["migration_ledger"] = tuple(raw["migration_ledger"])
    raw["table_counts"] = {str(k): int(v) for k, v in raw["table_counts"].items()}
    return BackupManifest(**raw)


def restore_drill(
    dsn: str,
    archive: str | Path,
    manifest_path: str | Path,
    *,
    temporary_database: str | None = None,
    keep_database: bool = False,
) -> DrillResult:
    """Restore into a newly-created database and prove ledger/count equivalence.

    Cleanup is attempted even after restore or validation failure.  ``keep_database``
    is an explicit debugging escape hatch and defaults to the safe cleanup behavior.
    """
    _require_psycopg()
    manifest = load_manifest(manifest_path)
    archive_path = Path(archive)
    if sha256_file(archive_path) != manifest.archive_sha256:
        raise RuntimeError("backup archive SHA-256 does not match its manifest")
    name = temporary_database or f"cr_restore_{secrets.token_hex(6)}"
    source = parse_dsn(dsn)
    drill_target = _with_database(source, name)
    created = False
    try:
        _run(build_createdb_command(dsn, name))
        created = True
        # ``pg_dump --schema`` archives objects inside the schema but does not
        # reliably include CREATE SCHEMA itself. Create the validated destination
        # namespace explicitly before pg_restore materialises qualified tables.
        connect_env = dict(conninfo_to_dict(drill_target.conninfo))
        if source.password is not None:
            connect_env["password"] = source.password
        with psycopg.connect(**connect_env) as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{manifest.schema}"')
        restore_spec = build_pg_restore_command(
            drill_target.conninfo, archive_path, schema=manifest.schema,
            base_env=command_environment(source),
        )
        # Preserve the source password after constructing the password-free drill DSN.
        restore_spec.env.update(command_environment(source))
        _run(restore_spec)
        with psycopg.connect(**connect_env) as connection:
            ledger, counts = _inventory(connection, manifest.schema)
        actual_hash = migration_hash(ledger)
        if actual_hash != manifest.migration_hash or ledger != manifest.migration_ledger:
            raise RuntimeError("restored migration ledger does not match backup manifest")
        if counts != manifest.table_counts:
            raise RuntimeError(
                f"restored table counts do not match backup manifest: expected "
                f"{manifest.table_counts!r}, got {counts!r}"
            )
        return DrillResult(name, manifest.schema, actual_hash, counts)
    finally:
        if created and not keep_database:
            _run(build_dropdb_command(dsn, name))


def main(argv: Sequence[str] | None = None) -> int:
    """Small operator CLI used by the repository drill script."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--dsn", required=True)
    backup.add_argument("--schema", required=True)
    backup.add_argument("--archive", required=True)
    backup.add_argument("--manifest")
    drill = commands.add_parser("drill")
    drill.add_argument("--dsn", required=True)
    drill.add_argument("--archive", required=True)
    drill.add_argument("--manifest", required=True)
    drill.add_argument("--temporary-database")
    args = parser.parse_args(argv)
    if args.command == "backup":
        create_backup(args.dsn, args.archive, schema=args.schema, manifest_path=args.manifest)
    else:
        restore_drill(
            args.dsn,
            args.archive,
            args.manifest,
            temporary_database=args.temporary_database,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
