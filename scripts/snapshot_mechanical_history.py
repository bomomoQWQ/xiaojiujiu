#!/usr/bin/env python3
"""Create a verified M0 mechanical-history snapshot for 「浪潮」.

The fleet must already be stopped and its volume extracted.  Only top-level
``default-friendmessage-*`` directories are considered.  Each legacy SQLite
store is opened read-only (including any sibling WAL/SHM files), checked,
copied with SQLite's backup API, then reopened as an immutable database and
checked again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

MANIFEST_VERSION: Final[int] = 1
DIRECTORY_PREFIX: Final[str] = "default-friendmessage-"
DATABASE_NAME: Final[str] = "companion.sqlite3"
# M0's retained mechanical history.  Short-lived working projections and the
# retired decision/user-model/action ledgers are intentionally not selected.
MECHANICAL_TABLES: Final[tuple[str, ...]] = (
    "raw_events",
    "event_semantics",
    "interpretation_versions",
    "memories",
    "memory_candidates",
    "unfinished_matters",
    "boundaries",
)


class SnapshotError(RuntimeError):
    """The requested source cannot be snapshotted safely."""


def _readonly_uri(path: Path, *, immutable: bool = False) -> str:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"{path.resolve().as_uri()}?{query}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        raise SnapshotError(f"expected a regular file: {path}")
    return {
        "sha256": _sha256(path),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "mtime_ns": stat.st_mtime_ns,
    }


def _source_files(database: Path) -> dict[str, dict[str, Any] | None]:
    return {
        "main": _file_record(database),
        "wal": _file_record(Path(f"{database}-wal")),
        "shm": _file_record(Path(f"{database}-shm")),
    }


def _check_integrity(connection: sqlite3.Connection, label: str) -> str:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    result = "; ".join(str(row[0]) for row in rows)
    if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
        raise SnapshotError(f"{label} integrity_check failed: {result}")
    return result


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _database_audit(connection: sqlite3.Connection) -> tuple[int, dict[str, dict[str, Any]]]:
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    tables: dict[str, dict[str, Any]] = {}
    for table in MECHANICAL_TABLES:
        if table not in existing:
            tables[table] = {"present": False, "count": 0, "columns": []}
            continue
        quoted = _quote_identifier(table)
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        ]
        count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        tables[table] = {"present": True, "count": count, "columns": columns}
    return user_version, tables


def _discover(source_root: Path) -> list[tuple[str, str, Path]]:
    if not source_root.is_dir():
        raise SnapshotError(f"source root is not a directory: {source_root}")
    discovered: list[tuple[str, str, Path]] = []
    seen: set[str] = set()
    for entry in sorted(source_root.iterdir(), key=lambda item: item.name):
        name = entry.name
        if not entry.is_dir() or not name.startswith(DIRECTORY_PREFIX):
            continue
        if name.endswith("_retired") or "_wiped-" in name:
            continue
        person_id = name[len(DIRECTORY_PREFIX):]
        if not person_id:
            continue
        # Slugs are compared case-insensitively: they are DNS/filesystem identities,
        # and accepting case-only duplicates would be ambiguous after transport.
        slug = name.lower()
        identity = slug.casefold()
        if identity in seen:
            raise SnapshotError(f"duplicate slug: {slug}")
        seen.add(identity)
        database = entry / DATABASE_NAME
        if not database.is_file():
            raise SnapshotError(f"missing {DATABASE_NAME}: {entry}")
        discovered.append((slug, f"default:FriendMessage:{person_id}", database))
    return discovered


def _snapshot_one(database: Path, destination: Path) -> dict[str, Any]:
    before = _source_files(database)
    # Even a mode=ro SQLite connection may update read marks in an existing -shm
    # file.  Materialize the stopped source triplet byte-for-byte inside the private
    # build directory first, then let SQLite read that disposable triplet.  This also
    # keeps the original main/WAL/SHM completely untouched while retaining WAL frames.
    staged_dir = destination.parent / f".{destination.stem}.source"
    staged_dir.mkdir()
    staged_database = staged_dir / DATABASE_NAME
    shutil.copyfile(database, staged_database)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.is_file():
            shutil.copyfile(sidecar, Path(f"{staged_database}{suffix}"))
    if before != _source_files(database):
        raise SnapshotError(f"source files changed while staging: {database}")

    try:
        source = sqlite3.connect(_readonly_uri(staged_database), uri=True)
    except sqlite3.Error as exc:
        raise SnapshotError(f"cannot open source database {database}: {exc}") from exc
    try:
        source_integrity = _check_integrity(source, f"source {database}")
        user_version, tables = _database_audit(source)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise SnapshotError(f"cannot snapshot source database {database}: {exc}") from exc
    finally:
        source.close()
        shutil.rmtree(staged_dir, ignore_errors=True)

    after = _source_files(database)
    if before != after:
        raise SnapshotError(f"source files changed while snapshotting: {database}")

    try:
        frozen = sqlite3.connect(_readonly_uri(destination, immutable=True), uri=True)
        try:
            snapshot_integrity = _check_integrity(frozen, f"snapshot {destination}")
        finally:
            frozen.close()
    except sqlite3.Error as exc:
        raise SnapshotError(f"cannot verify snapshot {destination}: {exc}") from exc

    snapshot_record = _file_record(destination)
    assert snapshot_record is not None
    return {
        "source_files": before,
        "snapshot": {
            "sha256": snapshot_record["sha256"],
            "size": snapshot_record["size"],
        },
        "sqlite": {
            "source_integrity_check": source_integrity,
            "snapshot_integrity_check": snapshot_integrity,
            "user_version": user_version,
        },
        "tables": tables,
    }


def snapshot_mechanical_history(
    source_root: str | Path,
    output_dir: str | Path,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build and atomically publish one verified fleet snapshot.

    ``output_dir`` may be absent or an existing empty directory.  All work is
    performed in a sibling temporary directory; a failure never publishes a
    partial manifest or fleet snapshot.
    """
    source = Path(source_root).resolve()
    output = Path(output_dir).resolve()
    if output.exists():
        if not output.is_dir():
            raise SnapshotError(f"output path is not a directory: {output}")
        if any(output.iterdir()):
            raise SnapshotError(f"output directory is not empty: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    people = _discover(source)
    stamp = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        manifest_people: list[dict[str, Any]] = []
        for slug, scope_key, database in people:
            filename = f"{slug}.sqlite3"
            audit = _snapshot_one(database, temporary / filename)
            manifest_people.append({
                "scope_key": scope_key,
                "slug": slug,
                "source_path": str(database.resolve()),
                "snapshot_filename": filename,
                **audit,
            })
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "created_at": stamp,
            "source_root": str(source),
            "people": manifest_people,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if output.exists():
            if any(output.iterdir()):
                raise SnapshotError(f"output directory became non-empty: {output}")
            output.rmdir()
        os.replace(temporary, output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-root", required=True, help="stopped, extracted fleet volume root")
    parser.add_argument("--output-dir", required=True, help="new snapshot output directory")
    args = parser.parse_args(argv)
    try:
        manifest = snapshot_mechanical_history(args.source_root, args.output_dir)
    except (OSError, SnapshotError) as exc:
        print(f"snapshot failed: {exc}", file=sys.stderr)
        return 1
    print(f"snapshot: {args.output_dir} ({len(manifest['people'])} people)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
