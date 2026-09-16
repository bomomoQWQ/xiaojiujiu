"""Export a whole beta fleet to plain files on disk.

Runs **inside a container** that has the fleet's data volume mounted read-only, so
one container reads all the per-person SQLite files without touching a live
Runtime's connections. The output is meant to outlive the beta: JSONL for the logs,
JSON for the summaries, and nothing that needs this codebase to read back (see
``replay_session.py`` and ``beta_daily_report.py``).

Everything a person's Runtime knows is copied, because the point of the week is to
decide what to change and that question is only answerable from the whole record:

    events.jsonl         raw_events, metadata decoded
    decisions.jsonl      every motivational verdict, acted or not, utilities included
    state_samples.jsonl  the mood / impulse / restraint curve
    tables/*.jsonl       every other table, one file each
    summary.json         counts, the decision-reason histogram, the unresolved ratio
    runtime.log          the supervisor's log file for that person

Usage (from the host)::

    docker run --rm \\
      -v astrbot_test_runtime-fleet-data:/data:ro \\
      -v /mnt/xz/xiaojiujiu-beta:/export \\
      -v <checkout>/scripts:/scripts:ro \\
      python:3.12-slim python /scripts/export_beta_data.py --note "day-1"

``--export-root`` gets a timestamped directory per run (``--label`` overrides it).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

#: Copied verbatim, newest rows last; the big two are streamed separately.
EXTRA_TABLES = (
    "event_semantics",
    "interpretation_versions",
    "reappraisals",
    "interaction_observations",
    "working_situation_items",
    "unfinished_matters",
    "boundaries",
    "memory_candidates",
    "memories",
    "activated_memories",
    "candidate_intents",
    "action_attempts",
    "attempt_events",
    "outbox",
    "emotion_explanations",
    "active_emotion_events",
    "background_tasks",
    "user_model_params",
    "runtime_state",
)


def connect(path: Path) -> sqlite3.Connection:
    """Open one person's database read-only."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def write_rows(path: Path, rows: list[dict[str, object]]) -> int:
    """Write rows as JSONL and return how many were written."""
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def table_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    """Return a whole table as dicts, tolerating a table that does not exist yet."""
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    except sqlite3.Error:
        return []


def export_person(db_path: Path, target: Path, log_path: Path | None) -> dict[str, object]:
    """Export one person's database and return a summary of what was written."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "tables").mkdir(exist_ok=True)
    connection = connect(db_path)

    events = table_rows(connection, "raw_events")
    for row in events:
        for column in ("metadata_json", "source_event_ids"):
            if isinstance(row.get(column), str):
                try:
                    row[column] = json.loads(row[column])
                except json.JSONDecodeError:
                    pass
    decisions = table_rows(connection, "decisions")
    for row in decisions:
        if isinstance(row.get("payload_json"), str):
            try:
                row["payload_json"] = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                pass
    samples = table_rows(connection, "state_samples")
    for row in samples:
        if isinstance(row.get("payload_json"), str):
            try:
                row["payload_json"] = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                pass

    counts = {
        "events.jsonl": write_rows(target / "events.jsonl", events),
        "decisions.jsonl": write_rows(target / "decisions.jsonl", decisions),
        "state_samples.jsonl": write_rows(target / "state_samples.jsonl", samples),
    }
    for table in EXTRA_TABLES:
        rows = table_rows(connection, table)
        if not rows:
            continue
        counts[f"tables/{table}.jsonl"] = write_rows(target / "tables" / f"{table}.jsonl", rows)
    connection.close()

    if log_path is not None and log_path.exists():
        shutil.copyfile(log_path, target / "runtime.log")

    reasons: dict[str, int] = {}
    for row in decisions:
        key = str(row.get("reason") or "?")
        reasons[key] = reasons.get(key, 0) + 1
    user_turns = sum(1 for row in events if row.get("event_type") == "user_message")
    assistant_turns = sum(1 for row in events if row.get("event_type") == "assistant_message")
    renders = sum(1 for row in events if row.get("content") == "context_rendered")
    summary = {
        "person": target.name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "context_renders": renders,
        "decisions": len(decisions),
        "decisions_acted": sum(1 for row in decisions if row.get("acted")),
        "decision_reasons": reasons,
        "state_samples": len(samples),
        "first_event_at": events[0]["created_at"] if events else None,
        "last_event_at": events[-1]["created_at"] if events else None,
    }
    (target / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return summary


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", default="/data", help="the fleet's volume")
    parser.add_argument("--export-root", default="/export", help="where to write")
    parser.add_argument("--label", default=None, help="run directory name")
    parser.add_argument("--note", default="", help="free-form note stored in the manifest")
    args = parser.parse_args()

    data_root = Path(args.data_root)
    label = args.label or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    target_root = Path(args.export_root) / label
    (target_root / "people").mkdir(parents=True, exist_ok=True)

    databases = sorted(path for path in data_root.glob("*/companion.sqlite3"))
    print(f"found {len(databases)} runtimes under {data_root}", flush=True)
    summaries = []
    for index, db_path in enumerate(databases, start=1):
        person = db_path.parent.name
        log_path = data_root / "logs" / f"{person}.log"
        try:
            summary = export_person(db_path, target_root / "people" / person, log_path)
        except Exception as error:  # noqa: BLE001 - one bad database must not stop the run
            print(f"  [{index}/{len(databases)}] {person}: FAILED {error}", flush=True)
            continue
        summaries.append(summary)
        print(
            f"  [{index}/{len(databases)}] {person}: {summary['user_turns']} user, "
            f"{summary['assistant_turns']} assistant, {summary['decisions']} decisions, "
            f"{summary['context_renders']} renders",
            flush=True,
        )

    manifest = {
        "label": label,
        "note": args.note,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "people": summaries,
    }
    (target_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"wrote {target_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
