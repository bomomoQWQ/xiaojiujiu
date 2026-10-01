#!/usr/bin/env python3
"""Offline read-only pre-apply audit gate for 「浪潮」 mechanical-history snapshots."""

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
from typing import Any, Final, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = REPO_ROOT / "runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from companion_runtime.mechanical_history_audit import audit_mechanical_plan  # noqa: E402
from companion_runtime.mechanical_history_import import plan_sqlite_mechanical_import  # noqa: E402

MANIFEST_NAME: Final[str] = "manifest.json"
SUMMARY_NAME: Final[str] = "langchao-mechanical-history-audit-summary.json"
SUMMARY_FORMAT: Final[str] = "langchao/mechanical-history-audit-summary-v1"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuditOperatorError(RuntimeError):
    """Snapshot selection or verification failed before semantic audit."""


def _canonical_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scope_hash(scope: str) -> str:
    return _sha_bytes(_canonical_bytes(scope))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_people(snapshot_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        manifest = json.loads((snapshot_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuditOperatorError(f"missing {MANIFEST_NAME}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditOperatorError(f"cannot read valid {MANIFEST_NAME}: {type(exc).__name__}") from exc
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        raise AuditOperatorError("unsupported or malformed snapshot manifest")
    raw_people = manifest.get("people")
    if not isinstance(raw_people, list):
        raise AuditOperatorError("snapshot manifest people must be a list")
    root = snapshot_dir.resolve()
    people: dict[str, dict[str, Any]] = {}
    for raw in raw_people:
        if not isinstance(raw, dict):
            raise AuditOperatorError("malformed person record")
        scope, filename, snapshot = raw.get("scope_key"), raw.get("snapshot_filename"), raw.get("snapshot")
        if not isinstance(scope, str) or not scope.strip() or scope in people:
            raise AuditOperatorError("empty or duplicate scope in manifest")
        if not isinstance(filename, str) or not filename.strip() or not isinstance(snapshot, dict):
            raise AuditOperatorError("malformed snapshot record")
        path = (snapshot_dir / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AuditOperatorError("snapshot filename escapes snapshot directory") from exc
        digest, size = snapshot.get("sha256"), snapshot.get("size")
        if not isinstance(digest, str) or not HEX_SHA256.fullmatch(digest):
            raise AuditOperatorError("invalid manifest snapshot hash")
        if type(size) is not int or size < 0:
            raise AuditOperatorError("invalid manifest snapshot size")
        people[scope] = {"path": path, "sha256": digest, "size": size}
    return people


def _selection(people: Mapping[str, Any], requested: list[str], all_scopes: bool) -> list[str]:
    if all_scopes:
        return sorted(people)
    if not requested:
        raise AuditOperatorError("select at least one --scope or use --all")
    if len(requested) != len(set(requested)):
        raise AuditOperatorError("duplicate --scope selection")
    missing = [scope for scope in requested if scope not in people]
    if missing:
        raise AuditOperatorError("selected scope is absent from snapshot manifest")
    return requested


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--scope", action="append", default=[])
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--allow-historical-pruning", action="store_true")
    parser.add_argument("--strict-soft", action="store_true")
    parser.add_argument("--as-of", help="fixed timezone-aware timestamp for reproducible boundary state")
    return parser


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    started = datetime.now(timezone.utc)
    as_of = args.as_of or started.isoformat()
    summary: dict[str, Any] = {
        "format": SUMMARY_FORMAT,
        "as_of": as_of,
        "allow_historical_pruning": bool(args.allow_historical_pruning),
        "strict_soft": bool(args.strict_soft),
        "status": "failed",
        "scopes": [],
    }
    try:
        people = _load_people(args.snapshot_dir)
        selected = _selection(people, args.scope, args.all)
        for scope in selected:
            record = people[scope]
            path = Path(record["path"])
            try:
                actual_size, actual_sha = path.stat().st_size, _sha_file(path)
            except OSError as exc:
                raise AuditOperatorError(f"cannot read selected snapshot: {type(exc).__name__}") from exc
            if actual_size != record["size"] or actual_sha != record["sha256"]:
                raise AuditOperatorError("snapshot hash/size mismatch")
            plan = plan_sqlite_mechanical_import(path, scope)
            audit = audit_mechanical_plan(
                plan,
                allow_historical_pruning=args.allow_historical_pruning,
                strict_soft=args.strict_soft,
                as_of=as_of,
            )
            payload = audit.to_dict()
            report_name = f"scope-{audit.scope_key_sha256}.json"
            report_bytes = _canonical_bytes(payload, pretty=True)
            _atomic_write(args.report_dir / report_name, report_bytes)
            summary["scopes"].append({
                "scope_key_sha256": audit.scope_key_sha256,
                "snapshot_sha256": actual_sha,
                "plan_sha256": plan.plan_sha256,
                "audit_sha256": audit.audit_sha256,
                "report": report_name,
                "report_sha256": _sha_bytes(report_bytes),
                "hard_errors": len(audit.hard_errors),
                "soft_warnings": len(audit.soft_warnings),
                "ok": audit.ok,
            })
        summary["scopes"].sort(key=lambda item: item["scope_key_sha256"])
        summary["status"] = "passed" if all(item["ok"] for item in summary["scopes"]) else "failed"
        return (0 if summary["status"] == "passed" else 1), summary
    except (AuditOperatorError, OSError, ValueError) as exc:
        # Error classes are useful but paths, scopes and row bodies are not.
        summary["error"] = type(exc).__name__
        return 1, summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code, summary = run(args)
    try:
        data = _canonical_bytes(summary, pretty=True)
        destination = args.report_dir / SUMMARY_NAME
        _atomic_write(destination, data)
        _atomic_write(destination.with_suffix(destination.suffix + ".sha256"),
                      (_sha_bytes(data) + "\n").encode("ascii"))
    except OSError as exc:
        print(f"浪潮 mechanical-history audit report failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    stream = sys.stderr if code else sys.stdout
    print(f"浪潮 mechanical-history audit {summary['status']}; summary: {destination}", file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
