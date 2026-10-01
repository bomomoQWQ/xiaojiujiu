from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

from companion_runtime.mechanical_history_import import TABLE_SPECS

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "audit_mechanical_history.py"
SPEC = importlib.util.spec_from_file_location("audit_mechanical_history", SCRIPT)
assert SPEC and SPEC.loader
operator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)


def _sqlite_type(kind: str) -> str:
    return {"text": "TEXT", "jsonb": "TEXT", "timestamptz": "TEXT", "boolean": "INTEGER",
            "bigint": "INTEGER", "double": "REAL"}[kind]


def _snapshot(tmp_path: Path, *, missing_reference: bool = False) -> Path:
    folder = tmp_path / "snapshots"
    folder.mkdir()
    path = folder / "person.sqlite3"
    with sqlite3.connect(path) as connection:
        for spec in TABLE_SPECS:
            if spec.rebuildable:
                continue
            definitions = ", ".join(f'"{name}" {_sqlite_type(kind)}' for name, kind, _ in spec.columns)
            connection.execute(f'CREATE TABLE "{spec.name}" ({definitions})')
        if missing_reference:
            spec = next(item for item in TABLE_SPECS if item.name == "memories")
            values = []
            for column, kind, nullable in spec.columns:
                if column == "memory_id": value = "secret-memory"
                elif column == "source_event_ids": value = '["secret-pruned-event"]'
                elif column == "status": value = "active"
                elif nullable: value = None
                elif kind == "text": value = "secret-body"
                elif kind == "jsonb": value = "[]" if column == "topics_json" else "{}"
                elif kind == "timestamptz": value = "2025-01-01T00:00:00+00:00"
                elif kind == "double": value = 0.5
                elif kind == "bigint": value = 1
                elif kind == "boolean": value = 1
                values.append(value)
            connection.execute(
                f'INSERT INTO memories ({",".join(spec.column_names)}) VALUES ({",".join("?" for _ in values)})',
                values,
            )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"manifest_version": 1, "people": [{
        "scope_key": "secret:scope", "snapshot_filename": path.name,
        "snapshot": {"sha256": digest, "size": path.stat().st_size},
    }]}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def test_cli_writes_atomic_scope_report_summary_and_sha_without_secrets(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    reports = tmp_path / "reports"
    code = operator.main([
        "--snapshot-dir", str(snapshot), "--all", "--report-dir", str(reports),
        "--as-of", "2025-06-01T00:00:00+00:00",
    ])
    assert code == 0
    summary_path = reports / operator.SUMMARY_NAME
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "passed" and len(summary["scopes"]) == 1
    digest_path = summary_path.with_suffix(summary_path.suffix + ".sha256")
    assert digest_path.read_text().strip() == hashlib.sha256(summary_path.read_bytes()).hexdigest()
    scope_report = reports / summary["scopes"][0]["report"]
    assert hashlib.sha256(scope_report.read_bytes()).hexdigest() == summary["scopes"][0]["report_sha256"]
    encoded = summary_path.read_text() + scope_report.read_text()
    assert "secret:scope" not in encoded
    assert not list(reports.glob(".*.json.*"))


def test_cli_nonzero_on_hard_and_strict_soft_upgrade(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, missing_reference=True)
    hard_code = operator.main([
        "--snapshot-dir", str(snapshot), "--all", "--report-dir", str(tmp_path / "hard"),
        "--as-of", "2025-06-01T00:00:00+00:00",
    ])
    assert hard_code == 1
    allowed_code = operator.main([
        "--snapshot-dir", str(snapshot), "--all", "--report-dir", str(tmp_path / "allowed"),
        "--allow-historical-pruning", "--as-of", "2025-06-01T00:00:00+00:00",
    ])
    assert allowed_code == 0
    strict_code = operator.main([
        "--snapshot-dir", str(snapshot), "--all", "--report-dir", str(tmp_path / "strict"),
        "--allow-historical-pruning", "--strict-soft",
        "--as-of", "2025-06-01T00:00:00+00:00",
    ])
    assert strict_code == 1
