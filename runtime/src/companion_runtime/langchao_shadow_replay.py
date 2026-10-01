"""Isolated PostgreSQL audit replay for Runtime-v2/浪潮 shadow comparisons.

This module intentionally has no delivery adapter, platform client, legacy Runtime, or
write/dispatch capability. It migrates an explicitly isolated schema, reads already-persisted
Runtime-v2 decision audits/committed snapshots and 浪潮 shadow audits as-of a fixed
instant, and emits deterministic comparison reports.  With no persisted candidate
witness the honest result is ``coverage == 0``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

from .user_model_v2_migrations import migrate, quote_schema_name
from .user_model_v2_schema import USER_MODEL_SCHEMA_VERSION

REPORT_VERSION = "langchao-shadow-replay.v1"
_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReplaySafetyError(RuntimeError):
    """A fail-closed isolation or authority gate rejected the replay."""


def parse_as_of(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def schema_for_scope(scope: str) -> str:
    """Match ``scripts/runtime_fleet.py``'s stable per-scope schema derivation."""
    cleaned = "".join(char if char.isalnum() else "-" for char in scope)
    slug = (cleaned.strip("-").lower()[-40:] or "person")
    raw = f"cr_{slug.replace('-', '_')}"
    if len(raw.encode("utf-8")) > 63:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        raw = raw.encode("utf-8")[:50].decode("utf-8", "ignore") + "_" + suffix
    if not _SCHEMA.fullmatch(raw):
        raise ValueError(f"derived unsafe schema name for scope {scope!r}")
    return raw


def dsn_host(dsn: str) -> str:
    """Extract only the hostname used by the allowlist gate; never return credentials."""
    text = str(dsn or "").strip()
    if not text:
        raise ReplaySafetyError("DSN environment variable is empty")
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ReplaySafetyError("DSN must identify a PostgreSQL host")
        return unquote(parsed.hostname).lower().rstrip(".")
    match = re.search(r"(?:^|\s)host\s*=\s*('([^']*)'|\"([^\"]*)\"|(\S+))", text)
    host = next((part for part in match.groups()[1:] if part is not None), None) if match else None
    if not host:
        raise ReplaySafetyError("DSN must contain an explicit host")
    return str(host).lower().rstrip(".")


def require_allowed_host(dsn: str, allowed_hosts: Iterable[str]) -> str:
    host = dsn_host(dsn)
    allowed = {str(item).strip().lower().rstrip(".") for item in allowed_hosts if str(item).strip()}
    if not allowed:
        raise ReplaySafetyError("host allowlist is empty")
    if host not in allowed:
        raise ReplaySafetyError(f"PostgreSQL host {host!r} is not allowlisted")
    return host


def verify_isolated_marker(path: str | Path, expected_sha256: str) -> dict[str, str]:
    """Verify marker bytes only; marker contents are deliberately never interpreted."""
    marker = Path(path)
    expected = str(expected_sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ReplaySafetyError("isolated marker hash must be a lowercase SHA-256 digest")
    try:
        actual = hashlib.sha256(marker.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReplaySafetyError(f"isolated marker is unreadable: {marker}") from exc
    if actual != expected:
        raise ReplaySafetyError("isolated marker SHA-256 mismatch")
    return {"path": str(marker), "sha256": actual}


def _row(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    raise TypeError("replay requires mapping rows (psycopg dict_row)")


def _json(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, Mapping) else {}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish a complete report with same-directory atomic replacement."""
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _count(connection: Any, table: str, *, scope: str | None = None) -> int:
    # Table names are constants below, never user input.
    if scope is None:
        row = connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    else:
        row = connection.execute(
            f"SELECT count(*) AS n FROM {table} WHERE scope_key = %s", (scope,)
        ).fetchone()
    value = _row(row)["n"]
    return int(value)


def protected_counts(connection: Any, *, scope: str) -> dict[str, int]:
    """Counters that must remain unchanged by replay."""
    return {
        "outbox": _count(connection, "outbox"),
        "attempts": _count(connection, "action_attempts"),
        "exposures": _count(connection, "interaction_exposures_v2", scope=scope),
        "expectations": _count(connection, "expectations_v2", scope=scope),
    }


def active_authority(connection: Any, *, scope: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT r.engine_key, r.mode, r.may_dispatch, a.authority_id,
                  a.revision, a.pointer_version
             FROM langchao_authority_active a
             JOIN langchao_authority_revisions r
               ON r.scope_key=a.scope_key AND r.authority_id=a.authority_id
              AND r.revision=a.revision
            WHERE a.scope_key=%s""",
        (scope,),
    ).fetchone()
    if row is None:
        raise ReplaySafetyError(f"scope {scope!r} has no active authority")
    result = _row(row)
    if (result.get("engine_key"), result.get("mode"), bool(result.get("may_dispatch"))) != (
        "runtime_v2", "live", True
    ):
        raise ReplaySafetyError(
            f"scope {scope!r} authority is not runtime_v2/live; replay refused"
        )
    return result


@dataclass(frozen=True, slots=True)
class ScopeComparison:
    scope: str
    schema: str
    as_of: str
    candidate_rows: int
    covered_rows: int
    coverage: float
    agreements: int
    agreement: float | None
    defers: int
    unknown: int
    baseline_candidate_counts: Mapping[str, int]
    shadow_candidate_counts: Mapping[str, int]
    source: str = "persisted_shadow_runs"
    executed_pure_assess: bool = False
    limitation: str = (
        "offline audit replay; no Runtime/legacy bridge was constructed and no candidate was committed"
    )


def compare_persisted(connection: Any, *, scope: str, schema: str, as_of: datetime) -> ScopeComparison:
    audit_rows = connection.execute(
        """SELECT decision_id, audit, updated_at
             FROM runtime_v2_decision_audits
            WHERE scope_key=%s AND updated_at <= %s
            ORDER BY updated_at, decision_id""",
        (scope, as_of),
    ).fetchall()
    shadow_rows = connection.execute(
        """SELECT idempotency_key, candidate_id, defer_reason, comparison, recorded_at
             FROM langchao_shadow_runs
            WHERE scope_key=%s AND recorded_at <= %s
            ORDER BY recorded_at, run_id""",
        (scope, as_of),
    ).fetchall()
    shadows: dict[str, dict[str, Any]] = {}
    for raw in shadow_rows:
        item = _row(raw)
        key = str(item.get("idempotency_key") or "")
        decision_id = key.removeprefix("runtime-v2:") if key.startswith("runtime-v2:") else key
        shadows[decision_id] = item
    covered = agreements = defers = unknown = 0
    baseline_counts: dict[str, int] = {}
    shadow_counts: dict[str, int] = {}
    # The v2 audit is the denominator/candidate witness. A shadow row covers it only
    # when its stable runtime-v2:<decision_id> key is present. This avoids the false
    # 100% produced by treating existing shadow rows as their own denominator.
    rows = audit_rows
    for raw in rows:
        audit_row = _row(raw)
        decision_id = str(audit_row.get("decision_id") or "")
        audit = _json(audit_row.get("audit"))
        run = _json(audit.get("run"))
        baseline = run.get("chosen")
        events = audit.get("events") if isinstance(audit.get("events"), list) else []
        baseline_defer = None
        for event in reversed(events):
            if isinstance(event, Mapping) and event.get("stage") == "reconciled":
                details = _json(event.get("details"))
                baseline_defer = details.get("reason") or details.get("outcome")
                break
        shadow_item = shadows.get(decision_id)
        if shadow_item is None:
            unknown += 1
            if baseline is not None:
                key = str(baseline)
                baseline_counts[key] = baseline_counts.get(key, 0) + 1
            continue
        comparison = _json(shadow_item.get("comparison"))
        # Prefer the comparison's frozen baseline semantics over later-expanded audit
        # details, while retaining the audit as the independent coverage witness.
        baseline = comparison.get("baseline_candidate_id", baseline)
        baseline_defer = comparison.get("baseline_defer_reason", baseline_defer)
        shadow = comparison.get("shadow_candidate_id", shadow_item.get("candidate_id"))
        shadow_defer = comparison.get("shadow_defer_reason", shadow_item.get("defer_reason"))
        covered += 1
        if baseline == shadow and baseline_defer == shadow_defer:
            agreements += 1
        if baseline is not None:
            key = str(baseline)
            baseline_counts[key] = baseline_counts.get(key, 0) + 1
        if shadow is not None:
            key = str(shadow)
            shadow_counts[key] = shadow_counts.get(key, 0) + 1
        if shadow is None:
            defers += 1
    total = len(rows)
    return ScopeComparison(
        scope=scope,
        schema=schema,
        as_of=_iso(as_of),
        candidate_rows=total,
        covered_rows=covered,
        coverage=(covered / total if total else 0.0),
        agreements=agreements,
        agreement=(agreements / covered if covered else None),
        defers=defers,
        unknown=unknown,
        baseline_candidate_counts=dict(sorted(baseline_counts.items())),
        shadow_candidate_counts=dict(sorted(shadow_counts.items())),
    )


def replay_scope(connection: Any, *, scope: str, schema: str, as_of: datetime) -> dict[str, Any]:
    """Migrate one isolated schema, audit persisted replay, and prove protected deltas zero."""
    migration = migrate(connection, schema=schema)
    if migration.current_version != USER_MODEL_SCHEMA_VERSION:
        raise RuntimeError(f"schema {schema} did not migrate to v{USER_MODEL_SCHEMA_VERSION}")
    connection.execute(f"SET search_path TO {quote_schema_name(schema)}, public")
    authority_before = active_authority(connection, scope=scope)
    before = protected_counts(connection, scope=scope)
    comparison = compare_persisted(connection, scope=scope, schema=schema, as_of=as_of)
    after = protected_counts(connection, scope=scope)
    authority_after = active_authority(connection, scope=scope)
    deltas = {name: after[name] - before[name] for name in before}
    if any(deltas.values()):
        raise ReplaySafetyError(f"protected side-effect delta is non-zero for {scope!r}: {deltas}")
    if authority_after != authority_before:
        raise ReplaySafetyError(f"authority changed during replay for {scope!r}")
    return {
        "report_version": REPORT_VERSION,
        "comparison": asdict(comparison),
        "migration": {
            "current_version": migration.current_version,
            "applied": list(migration.applied),
            "already_present": list(migration.already_present),
        },
        "protected_counts": {"before": before, "after": after, "delta": deltas},
        "authority": {
            "before": authority_before,
            "after": authority_after,
            "unchanged": True,
            "required": "runtime_v2/live",
            "claim_created": False,
        },
        "safety": {
            "sent": False,
            "onebot_called": False,
            "baseline_committed": False,
            "writes_allowed": [
                "schema_migration_v19",
                "langchao_shadow_contracts_state_audit_if_pure_mode_is_added",
            ],
        },
    }


def run_replay(
    *,
    dsn: str,
    scopes: Sequence[str],
    as_of: datetime,
    report_dir: str | Path,
    allowed_hosts: Sequence[str],
    marker_path: str | Path,
    marker_sha256: str,
    connect: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run all scopes, writing one JSON report per scope plus ``summary.json``."""
    if not scopes or any(not str(scope).strip() for scope in scopes):
        raise ValueError("at least one non-empty --scope is required")
    if len(set(scopes)) != len(scopes):
        raise ValueError("--scope values must be unique")
    host = require_allowed_host(dsn, allowed_hosts)
    marker = verify_isolated_marker(marker_path, marker_sha256)
    if connect is None:
        import psycopg
        from psycopg.rows import dict_row

        connect = lambda value: psycopg.connect(value, row_factory=dict_row)  # noqa: E731
    root = Path(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    for scope in scopes:
        schema = schema_for_scope(scope)
        connection = connect(dsn)
        try:
            with connection.transaction():
                report = replay_scope(connection, scope=scope, schema=schema, as_of=as_of)
            reports.append(report)
        finally:
            connection.close()
        safe_name = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        _write_json_atomic(root / f"scope-{safe_name}.json", report)
    comparisons = [item["comparison"] for item in reports]
    total_rows = sum(item["candidate_rows"] for item in comparisons)
    total_covered = sum(item["covered_rows"] for item in comparisons)
    total_agreements = sum(item["agreements"] for item in comparisons)
    summary = {
        "report_version": REPORT_VERSION,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "as_of": _iso(as_of),
        "scope_count": len(scopes),
        "host": host,
        "isolated_marker": marker,
        "candidate_rows": total_rows,
        "covered_rows": total_covered,
        "coverage": total_covered / total_rows if total_rows else 0.0,
        "agreements": total_agreements,
        "agreement": total_agreements / total_covered if total_covered else None,
        "defers": sum(item["defers"] for item in comparisons),
        "unknown": sum(item["unknown"] for item in comparisons),
        "all_protected_deltas_zero": all(
            not any(report["protected_counts"]["delta"].values()) for report in reports
        ),
        "all_authority_runtime_v2_live_unchanged": all(
            report["authority"]["unchanged"] for report in reports
        ),
        "baseline_committed": False,
        "sent": False,
        "onebot_called": False,
        "scopes": comparisons,
    }
    _write_json_atomic(root / "summary.json", summary)
    return summary


def dsn_from_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ReplaySafetyError(f"required DSN environment variable {name!r} is empty")
    return value


__all__ = [
    "REPORT_VERSION",
    "ReplaySafetyError",
    "ScopeComparison",
    "active_authority",
    "compare_persisted",
    "dsn_from_env",
    "dsn_host",
    "parse_as_of",
    "protected_counts",
    "replay_scope",
    "require_allowed_host",
    "run_replay",
    "schema_for_scope",
    "verify_isolated_marker",
]
