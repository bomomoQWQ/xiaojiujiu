"""Read-only inspection of a Runtime database: what actually landed on the backend.

This is the "look at the backend after a run" tool. It answers the questions the
simulations assert on, but as a report you read rather than a check that passes:
which events the host really sent, what became a memory candidate, what is in the
working set, and what the psychological state currently is.

It never writes: the connection is opened ``mode=ro``, so it is safe to run
against a live sidecar.

Usage::

    # Local file
    python scripts/inspect_runtime_backend.py --db runtime/data/runtime.sqlite3

    # The container that is serving, through its own interpreter
    docker exec -i xxj-runtime-test python - < scripts/inspect_runtime_backend.py

Both routes are the same read: the Docker defaults put the database at
``/data/companion.sqlite3``, which is the default here.

On a Windows console whose code page is not UTF-8, set ``PYTHONUTF8=1`` (or
``chcp 65001``) first, or the Chinese text prints as mojibake even though the
database holds it correctly.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = "/data/companion.sqlite3"

#: Printed in this order; anything else in the schema is still counted.
PRIORITY_TABLES = [
    "raw_events",
    "event_semantics",
    "interaction_observations",
    "memory_candidates",
    "memories",
    "activated_memories",
    "working_situation_items",
    "unfinished_matters",
    "boundaries",
    "active_emotion_events",
    "reappraisals",
    "candidate_intents",
    "outbox",
    "action_attempts",
    "runtime_state",
]

#: Long text columns are trimmed so the report stays readable.
TRIM = 160


def _trim(value: object) -> object:
    """Return ``value`` with long strings shortened."""
    if isinstance(value, str) and len(value) > TRIM:
        return value[:TRIM] + "..."
    return value


def _rows(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    """Run a query and return the rows as trimmed dictionaries."""
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(sql, params)
    except sqlite3.Error as error:
        return [{"error": str(error)}]
    return [{key: _trim(row[key]) for key in row.keys()} for row in cursor]


def report(db_path: str, *, limit: int) -> None:
    """Print the full backend report for one database."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    tables = [
        row["name"]
        for row in _rows(
            connection,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        )
    ]
    ordered = [name for name in PRIORITY_TABLES if name in tables]
    ordered += [name for name in tables if name not in ordered and not name.startswith("sqlite_")]

    print("=== row counts ===")
    for table in ordered:
        rows = _rows(connection, f"SELECT COUNT(*) AS n FROM {table}")
        print(f"  {table}: {rows[0].get('n') if rows else '?'}")

    print("=== events the host actually sent ===")
    for row in _rows(
        connection,
        "SELECT event_type, actor, COUNT(*) AS n FROM raw_events "
        "GROUP BY event_type, actor ORDER BY n DESC",
    ):
        print(" ", row)

    print("=== conversations ===")
    for row in _rows(
        connection,
        "SELECT conversation_id, COUNT(*) AS n, MIN(timestamp) AS first_at, "
        "MAX(timestamp) AS last_at FROM raw_events GROUP BY conversation_id "
        "ORDER BY n DESC LIMIT 10",
    ):
        print(" ", row)

    print(f"=== last {limit} raw events ===")
    for row in _rows(
        connection,
        "SELECT timestamp, event_type, actor, content, metadata_json FROM raw_events "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ):
        print(" ", row)

    print("=== memory candidates (pending first) ===")
    for row in _rows(
        connection,
        "SELECT candidate_id, kind, status, value, summary, consolidated_memory_id, "
        "created_at FROM memory_candidates ORDER BY status, value DESC LIMIT ?",
        (limit,),
    ):
        print(" ", row)

    print("=== memories ===")
    for row in _rows(
        connection,
        "SELECT memory_id, kind, status, summary, created_at FROM memories "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ):
        print(" ", row)

    print("=== activation pool ===")
    for row in _rows(
        connection,
        "SELECT memory_id, activation, recall_count, reason, last_recalled_at "
        "FROM activated_memories ORDER BY activation DESC LIMIT ?",
        (limit,),
    ):
        print(" ", row)

    print("=== working situation ===")
    for row in _rows(
        connection,
        "SELECT kind, confidence, salience, content, status, expires_at "
        "FROM working_situation_items ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ):
        print(" ", row)

    print("=== runtime state ===")
    for row in _rows(connection, "SELECT * FROM runtime_state"):
        print(" ", row)

    print("=== outbox ===")
    for row in _rows(
        connection,
        "SELECT * FROM outbox ORDER BY rowid DESC LIMIT ?",
        (limit,),
    ):
        print(" ", row)

    connection.close()


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"SQLite file to read (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="rows to show per table (default: 8)",
    )
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"no database at {args.db}; pass --db or run this inside the container")
        return 2
    report(args.db, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
