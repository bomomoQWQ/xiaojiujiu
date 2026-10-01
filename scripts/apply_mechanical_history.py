#!/usr/bin/env python3
"""Offline operator CLI for importing 「浪潮」 mechanical-history snapshots.

The command is fail-closed and dry-run by default.  It verifies every selected
snapshot against ``manifest.json``, regenerates each import plan from SQLite, runs
the current PostgreSQL migrations in the isolated target schema, and writes a
redacted atomic report.  Applying changes requires both a plan confirmation file
and an independently supplied stopped-fleet marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = REPO_ROOT / "runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from companion_runtime.mechanical_history_import import (  # noqa: E402
    MechanicalMigrationPlan,
    plan_sqlite_mechanical_import,
)
from companion_runtime.mechanical_history_import_apply import (  # noqa: E402
    MechanicalHistoryApplyError,
    reconcile_mechanical_plan,
)
from companion_runtime.user_model_v2_migrations import migrate  # noqa: E402

MANIFEST_NAME: Final[str] = "manifest.json"
REPORT_NAME: Final[str] = "langchao-mechanical-history-report.json"
REPORT_FORMAT: Final[str] = "langchao/mechanical-history-operator-report-v1"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OperatorError(RuntimeError):
    """An operator input or safety invariant failed."""


class ScopeFailure(OperatorError):
    """A scope failed after a report entry could be assembled."""

    def __init__(self, message: str, entry: dict[str, Any]):
        super().__init__(message)
        self.entry = entry


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derive_target_schema(scope_key: str) -> str:
    """Derive the fleet schema using exactly ``scripts/runtime_fleet.py``'s rules."""
    cleaned = "".join(char if char.isalnum() else "-" for char in scope_key)
    slug = cleaned.strip("-").lower()[-40:] or "person"
    raw_schema = f"cr_{slug.replace('-', '_')}"
    if len(raw_schema.encode("utf-8")) > 63:
        suffix = hashlib.sha256(raw_schema.encode("utf-8")).hexdigest()[:12]
        raw_schema = raw_schema.encode("utf-8")[:50].decode("utf-8", "ignore") + "_" + suffix
    return raw_schema


def _load_manifest(snapshot_dir: Path) -> dict[str, Any]:
    manifest_path = snapshot_dir / MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OperatorError(f"missing {MANIFEST_NAME} in snapshot directory") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorError(f"cannot read valid {MANIFEST_NAME}: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("manifest_version") != 1:
        raise OperatorError("unsupported or malformed snapshot manifest")
    people = payload.get("people")
    if not isinstance(people, list):
        raise OperatorError("snapshot manifest people must be a list")
    return payload


def _manifest_people(manifest: Mapping[str, Any], snapshot_dir: Path) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    root = snapshot_dir.resolve()
    for raw in manifest["people"]:
        if not isinstance(raw, dict):
            raise OperatorError("snapshot manifest contains a malformed person record")
        scope = raw.get("scope_key")
        filename = raw.get("snapshot_filename")
        snapshot = raw.get("snapshot")
        if not isinstance(scope, str) or not scope.strip():
            raise OperatorError("snapshot manifest contains an empty scope_key")
        if scope in selected:
            raise OperatorError(f"duplicate scope in snapshot manifest: {scope}")
        if not isinstance(filename, str) or not filename.strip() or not isinstance(snapshot, dict):
            raise OperatorError(f"malformed snapshot record for scope {scope}")
        path = (snapshot_dir / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise OperatorError(f"snapshot filename escapes snapshot directory for scope {scope}") from exc
        expected_hash = snapshot.get("sha256")
        expected_size = snapshot.get("size")
        if not isinstance(expected_hash, str) or not HEX_SHA256.fullmatch(expected_hash):
            raise OperatorError(f"invalid manifest snapshot hash for scope {scope}")
        if not isinstance(expected_size, int) or expected_size < 0:
            raise OperatorError(f"invalid manifest snapshot size for scope {scope}")
        selected[scope] = {"path": path, "sha256": expected_hash, "size": expected_size}
    return selected


def _select_scopes(
    people: Mapping[str, dict[str, Any]], requested: Iterable[str], select_all: bool
) -> list[str]:
    requested_list = list(requested)
    if select_all:
        return list(people)
    if not requested_list:
        raise OperatorError("select at least one --scope or use --all")
    if len(set(requested_list)) != len(requested_list):
        raise OperatorError("duplicate --scope selection")
    unknown = [scope for scope in requested_list if scope not in people]
    if unknown:
        raise OperatorError(f"scope is absent from snapshot manifest: {unknown[0]}")
    return requested_list


def _verify_and_plan(scope: str, record: Mapping[str, Any]) -> MechanicalMigrationPlan:
    path = Path(record["path"])
    try:
        actual_size = path.stat().st_size
        actual_hash = _sha256_file(path)
    except OSError as exc:
        raise OperatorError(f"cannot read snapshot for scope {scope}: {type(exc).__name__}") from exc
    if actual_size != record["size"] or actual_hash != record["sha256"]:
        raise OperatorError(f"snapshot hash/size mismatch for scope {scope}")
    return plan_sqlite_mechanical_import(path, scope)


def _load_confirmations(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise OperatorError(f"cannot read confirmation file: {type(exc).__name__}") from exc
    confirmations: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise OperatorError(f"invalid confirmation line {number}; expected scope=plan_sha")
        scope, digest = (part.strip() for part in line.split("=", 1))
        if not scope or not HEX_SHA256.fullmatch(digest):
            raise OperatorError(f"invalid confirmation line {number}; expected scope=plan_sha")
        if scope in confirmations:
            raise OperatorError(f"duplicate confirmation for scope {scope}")
        confirmations[scope] = digest
    return confirmations


def _plan_counts(plan: MechanicalMigrationPlan) -> dict[str, Any]:
    return {
        "source_rows": sum(item.row_count for item in plan.tables),
        "planned_rows": len(plan.rows),
        "quarantined_rows": len(plan.quarantine),
        "tables": [
            {
                "table": item.table,
                "source_rows": item.row_count,
                "planned_rows": item.planned_count,
                "quarantined_rows": item.quarantined_count,
                "canonical_rows_sha256": item.canonical_rows_sha256,
            }
            for item in plan.tables
        ],
    }


def _redacted_reconciliation(report: Any) -> dict[str, Any]:
    conflict_columns: dict[str, set[str]] = {}
    for row in report.rows:
        if row.differences:
            conflict_columns.setdefault(row.table, set()).update(item.column for item in row.differences)
    return {
        "run_id": report.run_id,
        "status": report.status,
        "dry_run": report.dry_run,
        "tables": [
            {
                "table": item.table,
                "inserted": item.inserted,
                "exact_duplicate": item.exact_duplicate,
                "conflict": item.conflict,
                "selected_pk_sha256": item.selected_pk_sha256,
                "target_rows_sha256": item.target_rows_sha256,
            }
            for item in report.tables
        ],
        "conflict_columns": {
            table: sorted(columns) for table, columns in sorted(conflict_columns.items())
        },
    }


def _scope_entry(plan: MechanicalMigrationPlan, record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scope": plan.scope_key,
        "target_schema": derive_target_schema(plan.scope_key),
        "snapshot_sha256": record["sha256"],
        "plan_sha256": plan.plan_sha256,
        "counts": _plan_counts(plan),
        "reconciliation": None,
    }


def _connect(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - package metadata requires it
        raise OperatorError("psycopg 3 is required for PostgreSQL import") from exc
    return psycopg.connect(dsn, autocommit=False, row_factory=dict_row)


def _reconcile_scope(
    plan: MechanicalMigrationPlan,
    record: Mapping[str, Any],
    dsn: str,
    *,
    apply: bool,
    connect: Any = _connect,
) -> dict[str, Any]:
    entry = _scope_entry(plan, record)
    schema = entry["target_schema"]
    try:
        connection = connect(dsn)
        with connection:
            with connection.transaction():
                migrate(connection, schema=schema, runner_version="langchao-history-operator/1")
                report = reconcile_mechanical_plan(
                    plan,
                    connection,
                    dry_run=not apply,
                    target_schema=schema,
                )
        entry["reconciliation"] = _redacted_reconciliation(report)
        return entry
    except MechanicalHistoryApplyError as exc:
        if exc.report is not None:
            entry["reconciliation"] = _redacted_reconciliation(exc.report)
        entry["error"] = "mechanical history reconciliation failed"
        raise ScopeFailure(str(exc), entry) from exc
    except Exception as exc:
        entry["error"] = f"database operation failed ({type(exc).__name__})"
        raise ScopeFailure(entry["error"], entry) from exc


def _atomic_report(report_dir: Path, payload: Mapping[str, Any]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    destination = report_dir / REPORT_NAME
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{REPORT_NAME}.", dir=report_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--dsn-env", default="CR_PG_DSN")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--scope", action="append", default=[])
    selection.add_argument("--all", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default; reconcile without inserts")
    mode.add_argument("--apply", action="store_true", help="write after both confirmations")
    parser.add_argument("--confirm-plan-sha", type=Path, metavar="FILE")
    parser.add_argument("--require-stopped-marker", type=Path, metavar="FILE")
    parser.add_argument("--report-dir", required=True, type=Path)
    return parser


def run(args: argparse.Namespace, *, connect: Any = _connect) -> tuple[int, dict[str, Any]]:
    applied = bool(args.apply)
    payload: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if applied else "dry_run",
        "stopped_marker": None,
        "status": "failed",
        "scopes": [],
    }
    try:
        if applied:
            if args.confirm_plan_sha is None:
                raise OperatorError("--apply requires --confirm-plan-sha FILE")
            if args.require_stopped_marker is None or not args.require_stopped_marker.is_file():
                raise OperatorError("--apply requires an existing --require-stopped-marker FILE")
            payload["stopped_marker"] = {
                "required": True,
                "path": str(args.require_stopped_marker.resolve()),
                "present": True,
                "sha256": _sha256_file(args.require_stopped_marker),
            }
        else:
            payload["stopped_marker"] = {"required": False, "present": False}

        manifest = _load_manifest(args.snapshot_dir)
        people = _manifest_people(manifest, args.snapshot_dir)
        scopes = _select_scopes(people, args.scope, args.all)
        plans = [(scope, people[scope], _verify_and_plan(scope, people[scope])) for scope in scopes]
        # Publish the freshly generated plan identities even when a later confirmation
        # or reconciliation fails, so operators can create/correct the confirmation
        # file without relying on any stored plan JSON.
        payload["scopes"] = [_scope_entry(plan, record) for _scope, record, plan in plans]

        if any(plan.quarantine for _scope, _record, plan in plans):
            raise OperatorError("one or more live plans contain quarantined rows")

        if applied:
            confirmations = _load_confirmations(args.confirm_plan_sha)
            for scope, _record, plan in plans:
                if confirmations.get(scope) != plan.plan_sha256:
                    raise OperatorError(f"plan hash confirmation mismatch for scope {scope}")
            extras = set(confirmations) - set(scopes)
            if extras:
                raise OperatorError("confirmation file contains unselected scope(s)")

        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise OperatorError(f"PostgreSQL DSN environment variable is empty: {args.dsn_env}")

        for index, (_scope, record, plan) in enumerate(plans):
            try:
                payload["scopes"][index] = _reconcile_scope(
                    plan, record, dsn, apply=applied, connect=connect
                )
            except ScopeFailure as exc:
                payload["scopes"][index] = exc.entry
                raise
        payload["status"] = "applied" if applied else "dry_run"
        return 0, payload
    except ScopeFailure:
        payload["error"] = "one or more scopes failed reconciliation"
        return 1, payload
    except (OperatorError, OSError, ValueError) as exc:
        payload["error"] = str(exc)
        return 1, payload


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    code, payload = run(args)
    try:
        report = _atomic_report(args.report_dir, payload)
    except OSError as exc:
        print(f"浪潮 mechanical-history report failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if code:
        print(f"浪潮 mechanical-history operation failed; report: {report}", file=sys.stderr)
    else:
        print(f"浪潮 mechanical-history {payload['status']}; report: {report}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
