"""Durability, checkpointing and backup.

The Runtime runs unattended for months on a small VPS, so the durability story has
to be explicit rather than assumed. This module implements it and documents the
exact guarantees in its docstrings; the README repeats the operator-facing view.

Durability model
----------------

* **Journal mode** - ``journal_mode = WAL``. Readers never block the writer and the
  writer never blocks readers, which is what lets the HTTP sidecar answer while a
  cognitive round is writing.
* **Synchronous** - ``synchronous = NORMAL``. In WAL mode this means a committed
  transaction is durable against *process* crashes: the commit record is written
  and ``fsync``-ed as part of the WAL, so a crash or a kill -9 loses nothing that
  ``COMMIT`` returned. It is *not* durable against sudden power loss / a blue
  screen: the last committed transactions can be lost, but the database is never
  corrupted - SQLite replays the WAL up to the last valid frame and discards the
  torn tail.
* **Transactions** - every mutation runs inside ``BEGIN IMMEDIATE``. The
  single-writer rule means there is exactly one writer, so a torn write cannot
  produce a half-applied cognitive round: either the whole round is in the WAL or
  none of it is.
* **Atomicity of the append-only log** - a raw event and the projection change it
  caused share one transaction, so history and projection can never disagree about
  whether something happened.

Recovery after an unclean shutdown
----------------------------------

Recovery is automatic and happens on the next ``connect``:

1. SQLite finds the WAL, checks its checksums and replays every complete frame.
2. A torn final frame (the power was cut mid-write) is discarded.
3. The database is left in the state of the last *complete* commit.

Because the WAL is only replayed while it exists, the operator-facing risk is a
WAL that grows very large: :func:`checkpoint` truncates it. It is safe to run at
any time and is the recommended periodic maintenance step.

``VACUUM`` is deliberately *not* used as a durability step: it rewrites the whole
database and needs free disk space equal to its size. :func:`backup` uses
``VACUUM INTO``, which produces a consistent snapshot in a single statement while
the Runtime keeps serving.

Verification
------------

:func:`verify` runs ``PRAGMA integrity_check`` plus structural checks that the
projection is self-consistent. :func:`restore` copies a snapshot into place and
verifies it, so a recovery drill is two calls: ``verify`` then ``restore``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .db import Database
from .utility import ensure_aware, isoformat, utcnow

LOGGER = logging.getLogger("companion_runtime.maintenance")

#: Tables that make up the append-only history.
HISTORY_TABLES: tuple[str, ...] = (
    "raw_events",
    "interpretation_versions",
    "reappraisals",
    "interaction_observations",
    "attempt_events",
    "background_tasks",
)

#: Tables that make up the current projection.
PROJECTION_TABLES: tuple[str, ...] = (
    "runtime_state",
    "working_situation_items",
    "active_emotion_events",
    "emotion_explanations",
    "boundaries",
    "unfinished_matters",
    "memory_candidates",
    "memories",
    "activated_memories",
    "user_model_params",
    "candidate_intents",
    "action_attempts",
    "outbox",
)


def as_database(target: Any) -> Database:
    """Accept either a :class:`Database` or a Runtime-like object.

    Operators routinely have a Runtime in hand (``runtime.db`` is the connection
    wrapper) and would otherwise have to remember which level each maintenance
    helper wants. Accepting both keeps the CLI, the tests and the HTTP layer
    identical.

    Args:
        target: A :class:`~companion_runtime.db.Database`, or an object exposing
            one through a ``db`` attribute.

    Returns:
        The database handle.

    Raises:
        TypeError: If neither shape matches.
    """
    if isinstance(target, Database):
        return target
    inner = getattr(target, "db", None)
    if isinstance(inner, Database):
        return inner
    raise TypeError(f"expected a Database or a Runtime, got {type(target).__name__}")


@dataclass(slots=True)
class CheckpointResult:
    """Outcome of a WAL checkpoint."""

    mode: str
    busy: int = 0
    log_frames: int = 0
    checkpointed_frames: int = 0
    wal_bytes_before: int = 0
    wal_bytes_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "mode": self.mode,
            "busy": self.busy,
            "log_frames": self.log_frames,
            "checkpointed_frames": self.checkpointed_frames,
            "wal_bytes_before": self.wal_bytes_before,
            "wal_bytes_after": self.wal_bytes_after,
            "reclaimed_bytes": max(0, self.wal_bytes_before - self.wal_bytes_after),
        }


@dataclass(slots=True)
class BackupResult:
    """Outcome of a backup or restore operation."""

    path: str
    bytes_written: int = 0
    duration_seconds: float = 0.0
    sha256: str | None = None
    integrity: str = "unknown"
    tables: dict[str, int] = field(default_factory=dict)
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "path": self.path,
            "bytes_written": self.bytes_written,
            "duration_seconds": round(self.duration_seconds, 3),
            "sha256": self.sha256,
            "integrity": self.integrity,
            "tables": dict(self.tables),
            "created_at": isoformat(self.created_at),
        }


@dataclass(slots=True)
class VerifyResult:
    """Outcome of an integrity and consistency check."""

    ok: bool
    integrity: str
    journal_mode: str = "unknown"
    checks: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "ok": self.ok,
            "integrity": self.integrity,
            "journal_mode": self.journal_mode,
            "checks": [dict(check) for check in self.checks],
            "counts": dict(self.counts),
        }


# --------------------------------------------------------------------------------------
# pragmas and checkpointing
# --------------------------------------------------------------------------------------


def journal_mode(db: Database | Any) -> str:
    """Return the active SQLite journal mode."""
    handle = as_database(db)
    row = handle.query_one("PRAGMA journal_mode")
    return str(row[0]) if row is not None else "unknown"


def synchronous_mode(db: Database | Any) -> str:
    """Return the active SQLite synchronous level."""
    handle = as_database(db)
    row = handle.query_one("PRAGMA synchronous")
    return str(row[0]) if row is not None else "unknown"


def wal_path(database_path: str | os.PathLike[str]) -> Path:
    """Return the ``-wal`` sidecar path for a database file."""
    return Path(f"{database_path}-wal")


def shm_path(database_path: str | os.PathLike[str]) -> Path:
    """Return the ``-shm`` sidecar path for a database file."""
    return Path(f"{database_path}-shm")


def _size_or_zero(path: Path) -> int:
    """Return the file size, or 0 when it does not exist."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def checkpoint(db: Database | Any, *, mode: str = "TRUNCATE") -> CheckpointResult:
    """Fold the write-ahead log back into the main database file.

    Safe to run at any time, including while the Runtime is serving. ``TRUNCATE``
    also shrinks the WAL to zero bytes, which is what keeps a long-running sidecar
    from accumulating an unbounded ``-wal`` file. Under concurrent readers the call
    may return early with ``busy > 0``; that is benign and simply means some frames
    will be checkpointed next time.

    Args:
        db: Open database handle.
        mode: ``PASSIVE``, ``FULL``, ``RESTART`` or ``TRUNCATE``.

    Returns:
        A :class:`CheckpointResult`.

    Raises:
        ValueError: If ``mode`` is not a valid checkpoint mode.
    """
    normalised = mode.upper()
    if normalised not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError(f"unsupported checkpoint mode: {mode}")
    handle = as_database(db)
    is_file = handle.path != ":memory:"
    wal = wal_path(handle.path)
    before = _size_or_zero(wal) if is_file else 0

    # ``PRAGMA wal_checkpoint`` may not run inside a transaction - doing so raises
    # "database table is locked". The database layer runs in autocommit mode
    # between explicit transactions, so issuing the pragma here (outside any
    # ``transaction()`` block, under the connection lock) is exactly right. The
    # checkpoint call itself is atomic from SQLite's point of view.
    with handle.read() as conn:
        row = conn.execute(f"PRAGMA wal_checkpoint({normalised})").fetchone()
    # PRAGMA wal_checkpoint returns (busy, log_frames, checkpointed_frames).
    busy, log_frames, checkpointed = (int(value or 0) for value in (row or (0, 0, 0))[:3])
    after = _size_or_zero(wal) if is_file else 0
    result = CheckpointResult(
        mode=normalised,
        busy=busy,
        log_frames=log_frames,
        checkpointed_frames=checkpointed,
        wal_bytes_before=before,
        wal_bytes_after=after,
    )
    LOGGER.info(
        "WAL checkpoint %s: busy=%d frames=%d checkpointed=%d reclaimed=%d bytes",
        normalised,
        busy,
        log_frames,
        checkpointed,
        result.to_dict()["reclaimed_bytes"],
    )
    return result


# --------------------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------------------


def _table_counts(db: Database | Any, table: str) -> int:
    """Return the row count of one table."""
    handle = as_database(db)
    row = handle.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - fixed allow-list
    return int(row["n"]) if row is not None else 0


def verify(
    db: Database | Any,
    *,
    tables: Sequence[str] = HISTORY_TABLES + PROJECTION_TABLES,
    expect_wal: bool | None = None,
) -> VerifyResult:
    """Run integrity and structural consistency checks.

    Checks performed:

    1. ``PRAGMA integrity_check`` equals ``ok``;
    2. the schema version row exists;
    3. the runtime state row exists and its version is non-negative;
    4. no action attempt references a missing outbox row;
    5. no outbox row is leased past its lease expiry without being reclaimable;
    6. every ``attempt_events`` row references an existing attempt;
    7. row counts per table.

    Args:
        db: Open database handle.
        tables: Tables whose row counts should be reported.
        expect_wal: When given, assert that the journal mode matches.

    Returns:
        A :class:`VerifyResult`.
    """
    checks: list[dict[str, Any]] = []
    handle = as_database(db)
    db = handle
    row = db.query_one("PRAGMA integrity_check")
    integrity = str(row[0]) if row is not None else "unknown"
    checks.append(
        {"name": "integrity_check", "ok": integrity == "ok", "detail": integrity}
    )

    mode = journal_mode(db)

    schema_row = db.query_one("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    checks.append(
        {
            "name": "schema_version_present",
            "ok": schema_row is not None,
            "detail": None if schema_row is None else str(schema_row["value"]),
        }
    )

    state_row = db.query_one("SELECT version FROM runtime_state LIMIT 1")
    state_ok = state_row is not None and int(state_row["version"]) >= 0
    checks.append(
        {
            "name": "runtime_state_row",
            "ok": state_ok,
            "detail": None if state_row is None else int(state_row["version"]),
        }
    )

    orphan_attempts = db.query(
        "SELECT aa.attempt_id FROM action_attempts aa WHERE aa.outbox_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.outbox_id = aa.outbox_id)"
    )
    checks.append(
        {
            "name": "attempt_outbox_references",
            "ok": not orphan_attempts,
            "detail": [row["attempt_id"] for row in orphan_attempts],
        }
    )

    orphan_transitions = db.query(
        "SELECT ae.attempt_event_id FROM attempt_events ae WHERE NOT EXISTS "
        "(SELECT 1 FROM action_attempts aa WHERE aa.attempt_id = ae.attempt_id)"
    )
    checks.append(
        {
            "name": "transition_attempt_references",
            "ok": not orphan_transitions,
            "detail": [row["attempt_event_id"] for row in orphan_transitions],
        }
    )

    stuck_leases = db.query(
        "SELECT COUNT(*) AS n FROM outbox WHERE status = 'leased' AND lease_expires_at IS NULL"
    )
    stuck_count = int(stuck_leases[0]["n"]) if stuck_leases else 0
    checks.append(
        {
            "name": "leases_have_expiry",
            "ok": stuck_count == 0,
            "detail": stuck_count,
        }
    )

    current = checkpoint(db, mode="PASSIVE")
    checks.append(
        {
            "name": "wal_checkpoint_reachable",
            "ok": True,
            "detail": current.to_dict(),
        }
    )

    if expect_wal is not None:
        want = "wal" if expect_wal else mode
        checks.append(
            {
                "name": "journal_mode",
                "ok": (mode == "wal") if expect_wal else (mode != "wal"),
                "detail": mode,
            }
        )

    counts = {table: _table_counts(db, table) for table in tables}
    return VerifyResult(
        ok=all(check["ok"] for check in checks),
        integrity=integrity,
        journal_mode=mode,
        checks=checks,
        counts=counts,
    )


# --------------------------------------------------------------------------------------
# backup and restore
# --------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(
    db: Database | Any,
    destination: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    checkpoint_first: bool = True,
) -> BackupResult:
    """Write a consistent snapshot of the database to ``destination``.

    ``VACUUM INTO`` is used because it produces a transactionally consistent copy
    in a single statement while the Runtime keeps serving: it reads through the
    WAL, so committed transactions that have not been checkpointed yet *are*
    included. The output therefore does not need the ``-wal`` sidecar to be
    restorable, which makes it safe to copy around with ordinary file tools.

    Args:
        db: Open database handle.
        destination: Target file path; must not already exist unless ``overwrite``.
        overwrite: Replace an existing file.
        checkpoint_first: Run a ``TRUNCATE`` checkpoint first, purely to keep the
            WAL small. It does not affect snapshot correctness.

    Returns:
        A :class:`BackupResult` describing the snapshot.

    Raises:
        ValueError: If ``db`` is an in-memory database, which has no file to copy.
        FileExistsError: If the target exists and ``overwrite`` is false.
    """
    handle = as_database(db)
    if handle.path == ":memory:":
        raise ValueError("cannot back up an in-memory database to a file")
    start = utcnow()
    target = Path(destination)
    if target.exists():
        if not overwrite:
            raise FileExistsError(f"backup target already exists: {target}")
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)

    if checkpoint_first:
        checkpoint(handle, mode="TRUNCATE")

    # VACUUM INTO needs the target to be absent; the path is escaped as a literal.
    # It must also run outside an explicit transaction.
    literal = str(target).replace("'", "''")
    with handle.read() as conn:
        conn.execute(f"VACUUM INTO '{literal}'")

    verification = verify(handle)
    result = BackupResult(
        path=str(target),
        bytes_written=_size_or_zero(target),
        duration_seconds=(utcnow() - start).total_seconds(),
        sha256=_sha256(target),
        integrity=verification.integrity,
        tables=verification.counts,
        created_at=start,
    )
    LOGGER.info(
        "Backup written to %s (%d bytes, sha256=%s)",
        result.path,
        result.bytes_written,
        (result.sha256 or "")[:12],
    )
    return result


def restore(
    source: str | os.PathLike[str],
    database_path: str | os.PathLike[str],
    *,
    verify_first: bool = True,
    keep_previous: bool = True,
) -> BackupResult:
    """Replace a database file with a snapshot.

    The Runtime must be stopped first: this function checks for SQLite sidecar
    files (``-wal``/``-shm``) next to the target and refuses to run while they are
    present, because copying over a live database corrupts it.

    Args:
        source: Snapshot produced by :func:`backup`.
        database_path: Database file to replace.
        verify_first: Run ``integrity_check`` on the snapshot before installing it.
        keep_previous: Move the existing database to ``<path>.replaced`` instead of
            deleting it.

    Returns:
        A :class:`BackupResult` describing the installed snapshot.

    Raises:
        FileNotFoundError: If the snapshot does not exist.
        RuntimeError: If the snapshot fails verification, or the target looks live.
    """
    start = utcnow()
    snapshot = Path(source)
    if not snapshot.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot}")

    target = Path(database_path)
    if target.exists() and (wal_path(target).exists() or shm_path(target).exists()):
        raise RuntimeError(
            "refusing to restore over a live database: SQLite sidecar files are present. "
            "Stop the Runtime first (the -wal/-shm files disappear on a clean shutdown)."
        )

    if verify_first:
        probe = Database(str(snapshot))
        try:
            probe.migrate()
            result = verify(probe)
        finally:
            probe.close()
        if not result.ok:
            raise RuntimeError(f"snapshot failed verification: {result.integrity}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and keep_previous:
        shutil.copy2(target, f"{target}.replaced")
    shutil.copy2(snapshot, target)

    installed = Database(str(target))
    try:
        installed.migrate()
        check = verify(installed)
        counts = check.counts
        integrity = check.integrity
    finally:
        installed.close()

    return BackupResult(
        path=str(target),
        bytes_written=_size_or_zero(target),
        duration_seconds=(utcnow() - start).total_seconds(),
        sha256=_sha256(target),
        integrity=integrity,
        tables=counts,
        created_at=start,
    )


def snapshot_name(prefix: str = "runtime", *, now: datetime | None = None) -> str:
    """Return a timestamped snapshot filename.

    The trailing ``Z`` claims the stamp is UTC, so an aware ``now`` is converted
    rather than formatted as-is: handing in a ``+08:00`` instant used to produce a
    name like ``…T170000Z`` for what was really 09:00 UTC - a filename that lies, and
    snapshot names are the only record of when a backup was taken. A naive value is
    read as UTC, which is what :func:`~companion_runtime.utility.ensure_aware` does
    everywhere else.
    """
    moment = ensure_aware(now) or utcnow()
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}.sqlite3"


def prune_backups(directory: str | os.PathLike[str], *, keep: int = 7) -> list[str]:
    """Delete the oldest snapshots in ``directory`` beyond ``keep``.

    Args:
        directory: Directory holding snapshots named ``*.sqlite3``.
        keep: How many of the newest snapshots to retain.

    Returns:
        The paths that were deleted.
    """
    folder = Path(directory)
    if not folder.exists():
        return []
    snapshots = sorted(folder.glob("*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for stale in snapshots[max(0, keep) :]:
        try:
            stale.unlink()
            removed.append(str(stale))
        except OSError:  # pragma: no cover - filesystem dependent
            LOGGER.warning("Could not remove stale snapshot %s", stale)
    return removed


def maintenance_tick(
    db: Database | Any,
    *,
    backup_dir: str | os.PathLike[str] | None = None,
    keep: int = 7,
) -> dict[str, Any]:
    """Run the routine durability maintenance: checkpoint, verify, optionally back up.

    Intended to be called periodically (for example from the scheduler's P3
    maintenance slot or a cron job).

    Args:
        db: Open database handle.
        backup_dir: When given, also write and prune a snapshot there.
        keep: How many snapshots to retain when backing up.

    Returns:
        A mapping with ``checkpoint``, ``verify`` and ``backup`` sections.
    """
    result: dict[str, Any] = {
        "checkpoint": checkpoint(db, mode="TRUNCATE").to_dict(),
        "verify": verify(db).to_dict(),
        "backup": None,
        "pruned": [],
    }
    if backup_dir is not None:
        folder = Path(backup_dir)
        folder.mkdir(parents=True, exist_ok=True)
        result["backup"] = backup(db, folder / snapshot_name(), overwrite=True).to_dict()
        result["pruned"] = prune_backups(folder, keep=keep)
    return result


def sidecar_files(database_path: str | os.PathLike[str]) -> list[str]:
    """Return whichever SQLite sidecar files exist for a database path."""
    candidates = [wal_path(database_path), shm_path(database_path)]
    return [str(path) for path in candidates if path.exists()]


def sqlite_error_is_corruption(error: BaseException) -> bool:
    """Return whether a SQLite error indicates corruption rather than misuse.

    Used by the sidecar's startup path to decide between "refuse to start" and
    "restore the newest snapshot".
    """
    if not isinstance(error, sqlite3.DatabaseError):
        return False
    text = str(error).lower()
    return "malformed" in text or "corrupt" in text or "not a database" in text


def recovery_plan(database_path: str | os.PathLike[str], backup_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Return the actions a recovery drill should take for a given layout.

    Args:
        database_path: Live database path.
        backup_dir: Directory holding snapshots.

    Returns:
        A mapping describing the live database, any sidecars, and the newest
        snapshot available for restore.
    """
    folder = Path(backup_dir)
    snapshots = (
        sorted(folder.glob("*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
        if folder.exists()
        else []
    )
    return {
        "database_path": str(database_path),
        "exists": Path(database_path).exists(),
        "sidecars": sidecar_files(database_path),
        "newest_snapshot": str(snapshots[0]) if snapshots else None,
        "snapshot_count": len(snapshots),
        "recommended_action": (
            "restore"
            if snapshots and not sidecar_files(database_path)
            else ("stop_runtime_first" if sidecar_files(database_path) else "backup_now")
        ),
    }
