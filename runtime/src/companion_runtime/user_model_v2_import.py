"""Offline planner for importing stopped v1 SQLite evidence into user-model v2.

This module is deliberately disconnected from every service start path.  It opens each
legacy database through a read-only SQLite URI, inventories the relevant source tables,
and returns an immutable :class:`MigrationPlan`.  The plan contains repository-shaped
records but performs no PostgreSQL (or SQLite) writes itself.

Legacy outcome booleans and soft scores are not trustworthy v2 labels.  They are retained
in the audit quarantine as ``legacy_unknown``.  An exposure is emitted only when a source
row proves one of the following facts: an attempt reached ``sent``, a
``proactive_sent`` raw event exists, or a send outbox row was acknowledged.  Reply labels
are only candidates when a timestamped user event can be attributed to exactly one
exposure (or explicitly names one); ambiguity and missing time remain non-label audit
records.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from .user_model_v2_repository import canonical_json, stable_idempotency_key

SOURCE_TABLES = (
    "action_attempts",
    "attempt_events",
    "raw_events",
    "outbox",
    "decisions",
    "interaction_observations",
)
LEGACY_LABEL_KEYS = frozenset(
    {
        "positive",
        "continued",
        "continued_topic",
        "explicit_positive",
        "positive_probability",
        "continue_probability",
        "reply_probability",
        "feedback",
        "reward",
        "score",
        "label",
    }
)


@dataclass(frozen=True, slots=True)
class SourceTableAudit:
    table: str
    present: bool
    row_count: int
    sha256: str
    reject_reasons: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class SourceManifest:
    source_path: str
    scope_key: str
    sqlite_read_only: bool
    tables: tuple[SourceTableAudit, ...]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ExposureCandidate:
    exposure_id: str
    scope_key: str
    occurred_at: str
    action: Mapping[str, Any]
    context: Mapping[str, Any]
    propensity: float
    evidence: tuple[str, ...]
    idempotency_key: str
    source_path: str
    local_attempt_id: str | None = None

    def repository_kwargs(self) -> dict[str, Any]:
        """Return arguments suitable for ``UserModelV2Repository.insert_exposure``."""
        return {
            "scope_key": self.scope_key,
            "exposure_id": self.exposure_id,
            "occurred_at": datetime.fromisoformat(self.occurred_at),
            "action": dict(self.action),
            "context": dict(self.context),
            "propensity": self.propensity,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class ReplyCandidate:
    label_id: str
    exposure_id: str | None
    scope_key: str
    status: str
    observed_at: str | None
    evidence: tuple[str, ...]
    reason: str | None
    idempotency_key: str

    @property
    def importable(self) -> bool:
        return self.status == "candidate" and self.exposure_id is not None


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    scope_key: str
    source_table: str
    source_row: str
    classification: str
    reason: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    manifests: tuple[SourceManifest, ...]
    exposures: tuple[ExposureCandidate, ...]
    reply_candidates: tuple[ReplyCandidate, ...]
    quarantine: tuple[QuarantineRecord, ...]
    dry_run: bool
    plan_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _Evidence:
    occurred_at: str
    ref: str
    rank: int
    conversation_id: str | None = None
    attempt_id: str | None = None
    action: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    propensity: float | None = None


def _readonly_uri(path: Path) -> str:
    # Path.as_uri correctly handles drive letters and non-ASCII paths; quote keeps URI
    # metacharacters in a literal filename from becoming query parameters.
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _timestamp(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _stable_uuidish(kind: str, scope: str, material: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_json({"kind": kind, "scope_key": scope, "material": material}).encode()
    ).hexdigest()
    # Repository accepts UUID | str.  UUID-shaped IDs ease direct repository consumption.
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _row_ref(table: str, row: Mapping[str, Any], ordinal: int) -> str:
    for key in (
        "attempt_event_id", "attempt_id", "event_id", "outbox_id", "decision_id",
        "observation_id",
    ):
        if row.get(key) not in (None, ""):
            return f"{table}:{row[key]}"
    return f"{table}:row:{ordinal}"


def _canonical_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): row[key] for key in sorted(row)}


def _read_source(path: Path) -> tuple[dict[str, list[dict[str, Any]]], list[SourceTableAudit]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(_readonly_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        audits: list[SourceTableAudit] = []
        for table in SOURCE_TABLES:
            if table not in existing:
                rows_by_table[table] = []
                audits.append(
                    SourceTableAudit(
                        table=table,
                        present=False,
                        row_count=0,
                        sha256=hashlib.sha256(b"[]").hexdigest(),
                        reject_reasons=(("missing_source_table", 1),),
                    )
                )
                continue
            # Table names come exclusively from SOURCE_TABLES, never input.
            raw = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()]
            raw.sort(key=lambda row: canonical_json(_canonical_row(row)))
            rows_by_table[table] = raw
            digest = hashlib.sha256(
                canonical_json([_canonical_row(row) for row in raw]).encode("utf-8")
            ).hexdigest()
            audits.append(SourceTableAudit(table, True, len(raw), digest))
        return rows_by_table, audits
    finally:
        conn.close()


def _nested_values(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, item
            yield from _nested_values(item, path)


def _attempt_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("attempt_id", "action_attempt_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _plan_source(
    path: Path, scope: str
) -> tuple[SourceManifest, list[ExposureCandidate], list[ReplyCandidate], list[QuarantineRecord]]:
    rows, base_audits = _read_source(path)
    rejects: dict[str, dict[str, int]] = {table: {} for table in SOURCE_TABLES}
    quarantine: list[QuarantineRecord] = []

    def reject(table: str, reason: str) -> None:
        rejects[table][reason] = rejects[table].get(reason, 0) + 1

    def quarantine_legacy(table: str, row: Mapping[str, Any], ordinal: int) -> None:
        found: dict[str, Any] = {}
        for column, value in row.items():
            if column.endswith("_json") or column in {"outcome", "metadata", "payload"}:
                parsed = _json(value, {})
                for key, item in _nested_values(parsed):
                    if key.rsplit(".", 1)[-1].lower() in LEGACY_LABEL_KEYS:
                        found[key] = item
            elif column.lower() in LEGACY_LABEL_KEYS:
                found[column] = value
        if found:
            quarantine.append(
                QuarantineRecord(
                    scope_key=scope,
                    source_table=table,
                    source_row=_row_ref(table, row, ordinal),
                    classification="legacy_unknown",
                    reason="legacy positive/continued/default-false/soft labels are not v2 truth",
                    payload_sha256=hashlib.sha256(canonical_json(found).encode()).hexdigest(),
                )
            )
            reject(table, "legacy_unknown")

    for table, table_rows in rows.items():
        for ordinal, row in enumerate(table_rows):
            quarantine_legacy(table, row, ordinal)

    attempts = {str(row.get("attempt_id")): row for row in rows["action_attempts"] if row.get("attempt_id")}
    evidences: dict[str, list[_Evidence]] = {}
    anonymous: list[_Evidence] = []

    def add_evidence(evidence: _Evidence) -> None:
        if evidence.attempt_id:
            evidences.setdefault(evidence.attempt_id, []).append(evidence)
        else:
            anonymous.append(evidence)

    for ordinal, row in enumerate(rows["attempt_events"]):
        if str(row.get("to_state", "")).upper() != "SENT":
            continue
        stamp = _timestamp(row.get("created_at"))
        if not stamp:
            reject("attempt_events", "sent_missing_time")
            continue
        add_evidence(_Evidence(stamp, _row_ref("attempt_events", row, ordinal), 1,
                               attempt_id=_attempt_id(row)))

    for ordinal, row in enumerate(rows["action_attempts"]):
        if str(row.get("state", "")).upper() != "SENT":
            continue
        stamp = _timestamp(row.get("committed_at") or row.get("updated_at") or row.get("created_at"))
        if not stamp:
            reject("action_attempts", "sent_missing_time")
            continue
        action = {"intent": row.get("intent", ""), "goal": row.get("goal", "")}
        add_evidence(_Evidence(stamp, _row_ref("action_attempts", row, ordinal), 2,
                               attempt_id=_attempt_id(row), action=action))

    for ordinal, row in enumerate(rows["raw_events"]):
        if str(row.get("event_type", "")).lower() != "proactive_sent":
            continue
        stamp = _timestamp(row.get("timestamp") or row.get("created_at"))
        if not stamp:
            reject("raw_events", "proactive_sent_missing_time")
            continue
        metadata = _json(row.get("metadata_json"), {})
        aid = _attempt_id(metadata) or _attempt_id(row)
        action = {"content": row.get("content"), "event_type": "proactive_sent"}
        add_evidence(_Evidence(stamp, _row_ref("raw_events", row, ordinal), 3,
                               conversation_id=row.get("conversation_id"), attempt_id=aid,
                               action=action, context={"metadata": metadata}))

    for ordinal, row in enumerate(rows["outbox"]):
        kind = str(row.get("kind", "")).lower()
        acknowledged = str(row.get("status", "")).lower() == "delivered" and row.get("acked_at")
        if kind != "send" or not acknowledged:
            continue
        stamp = _timestamp(row.get("acked_at"))
        if not stamp:
            reject("outbox", "send_ack_missing_time")
            continue
        payload = _json(row.get("payload_json"), {})
        aid = _attempt_id(payload)
        if aid is None:
            outbox_id = str(row.get("outbox_id", ""))
            linked = [key for key, item in attempts.items() if str(item.get("outbox_id", "")) == outbox_id]
            aid = linked[0] if len(linked) == 1 else None
        add_evidence(_Evidence(stamp, _row_ref("outbox", row, ordinal), 4,
                               conversation_id=row.get("conversation_id"), attempt_id=aid,
                               action=dict(payload), context={"outbox_id": row.get("outbox_id")}))

    groups: list[tuple[str | None, list[_Evidence]]] = list(evidences.items())
    groups.extend((None, [item]) for item in anonymous)
    exposures: list[ExposureCandidate] = []
    for aid, facts in groups:
        facts.sort(key=lambda item: (-item.rank, item.occurred_at, item.ref))
        strongest = facts[0]
        attempt = attempts.get(aid or "", {})
        action = strongest.action or {"intent": attempt.get("intent", ""), "goal": attempt.get("goal", "")}
        context = dict(strongest.context or {})
        if strongest.conversation_id:
            context.setdefault("conversation_id", strongest.conversation_id)
        if aid:
            context.setdefault("legacy_attempt_id", aid)
        probability = strongest.propensity
        if probability is None:
            probability = 1.0
            decision_matches = [
                row for row in rows["decisions"]
                if row.get("chosen_candidate_id") is not None
                and row.get("chosen_candidate_id") == attempt.get("candidate_id")
            ]
            if len(decision_matches) == 1:
                try:
                    probability = float(decision_matches[0].get("action_probability", 1.0))
                except (TypeError, ValueError):
                    probability = 1.0
        identity = {"source_path": str(path.resolve()), "attempt_id": aid,
                    "evidence": sorted(item.ref for item in facts)}
        exposure_id = _stable_uuidish("legacy-exposure", scope, identity)
        payload = {"exposure_id": exposure_id, "occurred_at": strongest.occurred_at,
                   "action": action, "context": context, "propensity": probability,
                   "evidence": sorted(item.ref for item in facts)}
        exposures.append(
            ExposureCandidate(
                exposure_id=exposure_id, scope_key=scope, occurred_at=strongest.occurred_at,
                action=action, context=context, propensity=max(0.0, min(1.0, probability)),
                evidence=tuple(sorted(item.ref for item in facts)),
                idempotency_key=stable_idempotency_key("legacy-exposure", scope, payload),
                source_path=str(path.resolve()), local_attempt_id=aid,
            )
        )
    exposures.sort(key=lambda item: (item.occurred_at, item.exposure_id))

    by_attempt = {item.local_attempt_id: item for item in exposures if item.local_attempt_id}
    replies: list[ReplyCandidate] = []
    for ordinal, row in enumerate(rows["raw_events"]):
        event_type = str(row.get("event_type", "")).lower()
        actor = str(row.get("actor", "")).lower()
        if event_type not in {"user_message", "message", "reply"} and actor not in {"user", "human"}:
            continue
        ref = _row_ref("raw_events", row, ordinal)
        stamp = _timestamp(row.get("timestamp") or row.get("created_at"))
        metadata = _json(row.get("metadata_json"), {})
        explicit = _attempt_id(metadata)
        matched: ExposureCandidate | None = by_attempt.get(explicit) if explicit else None
        reason: str | None = None
        status = "candidate"
        if not stamp:
            status, reason = "unknown", "reply_missing_time"
            reject("raw_events", reason)
        elif explicit and matched is None:
            status, reason = "unknown", "reply_names_missing_exposure"
            reject("raw_events", reason)
        elif matched is None:
            reply_time = datetime.fromisoformat(stamp)
            conversation = row.get("conversation_id")
            possible = [
                item for item in exposures
                if datetime.fromisoformat(item.occurred_at) <= reply_time
                and (conversation is None or item.context.get("conversation_id") in {None, conversation})
            ]
            if len(possible) == 1:
                matched = possible[0]
            elif len(possible) > 1:
                status, reason = "unattributable", "multiple_prior_exposures"
                reject("raw_events", reason)
            else:
                status, reason = "unknown", "no_prior_exposure"
                reject("raw_events", reason)
        identity = {"source_path": str(path.resolve()), "reply": ref,
                    "exposure_id": matched.exposure_id if matched else None, "status": status}
        label_id = _stable_uuidish("legacy-reply", scope, identity)
        payload = {**identity, "observed_at": stamp, "reason": reason}
        replies.append(
            ReplyCandidate(
                label_id=label_id,
                exposure_id=matched.exposure_id if matched else None,
                scope_key=scope,
                status=status,
                observed_at=stamp if status == "candidate" else None,
                evidence=(ref,) + (() if matched is None else matched.evidence),
                reason=reason,
                idempotency_key=stable_idempotency_key("legacy-reply", scope, payload),
            )
        )

    audits = tuple(
        SourceTableAudit(
            table=item.table, present=item.present, row_count=item.row_count, sha256=item.sha256,
            reject_reasons=tuple(sorted(rejects[item.table].items()))
            if item.present else item.reject_reasons,
        )
        for item in base_audits
    )
    manifest_payload = {
        "source_path": str(path.resolve()), "scope_key": scope,
        "tables": [asdict(item) for item in audits],
    }
    manifest = SourceManifest(
        source_path=str(path.resolve()), scope_key=scope, sqlite_read_only=True, tables=audits,
        manifest_sha256=hashlib.sha256(canonical_json(manifest_payload).encode()).hexdigest(),
    )
    return manifest, exposures, replies, quarantine


def plan_migration(
    source_scopes: Mapping[str | Path, str], *, dry_run: bool = True
) -> MigrationPlan:
    """Build a deterministic, zero-write migration plan from stopped v1 databases.

    ``source_scopes`` is intentionally a mapping rather than a list: every source path
    must have an explicit scope key.  Scope participates in all IDs and idempotency keys,
    so equal local IDs in different databases cannot collide.

    ``dry_run`` is recorded for orchestration/audit.  This planner never writes in either
    mode; a caller may consume the returned plan with a repository only after review.
    """
    if not isinstance(source_scopes, Mapping) or not source_scopes:
        raise ValueError("source_scopes must be a non-empty path -> scope_key mapping")
    normalized: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for raw_path, raw_scope in source_scopes.items():
        scope = str(raw_scope).strip()
        if not scope:
            raise ValueError(f"source {raw_path!s} has no explicit scope_key")
        path = Path(raw_path).resolve()
        key = str(path).casefold()
        if key in seen:
            raise ValueError(f"duplicate source path: {path}")
        seen.add(key)
        normalized.append((path, scope))
    normalized.sort(key=lambda item: (str(item[0]).casefold(), item[1]))

    manifests: list[SourceManifest] = []
    exposures: list[ExposureCandidate] = []
    replies: list[ReplyCandidate] = []
    quarantine: list[QuarantineRecord] = []
    for path, scope in normalized:
        manifest, source_exposures, source_replies, source_quarantine = _plan_source(path, scope)
        manifests.append(manifest)
        exposures.extend(source_exposures)
        replies.extend(source_replies)
        quarantine.extend(source_quarantine)
    exposures.sort(key=lambda item: (item.scope_key, item.occurred_at, item.exposure_id))
    replies.sort(key=lambda item: (item.scope_key, item.idempotency_key))
    quarantine.sort(key=lambda item: (item.scope_key, item.source_table, item.source_row))
    body = {
        "manifests": [asdict(item) for item in manifests],
        "exposures": [asdict(item) for item in exposures],
        "reply_candidates": [asdict(item) for item in replies],
        "quarantine": [asdict(item) for item in quarantine],
        "dry_run": bool(dry_run),
    }
    return MigrationPlan(
        manifests=tuple(manifests), exposures=tuple(exposures),
        reply_candidates=tuple(replies), quarantine=tuple(quarantine),
        dry_run=bool(dry_run),
        plan_sha256=hashlib.sha256(canonical_json(body).encode()).hexdigest(),
    )


# Discoverable aliases for offline tooling; none are imported by runtime startup.
build_migration_plan = plan_migration
plan_sqlite_v1_import = plan_migration

__all__ = [
    "ExposureCandidate", "MigrationPlan", "QuarantineRecord", "ReplyCandidate",
    "SOURCE_TABLES", "SourceManifest", "SourceTableAudit", "build_migration_plan",
    "plan_migration", "plan_sqlite_v1_import",
]
