"""Explicit, offline application of a reviewed v1 :class:`MigrationPlan`.

The planner is the only component that reads SQLite.  This module consumes its in-memory
output and a caller-supplied PostgreSQL service repository; it never discovers paths,
opens source databases, or connects itself to Runtime/Jev.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .user_model_v2_import import MigrationPlan, QuarantineRecord, ReplyCandidate
from .user_model_v2_repository import canonical_json


@dataclass(frozen=True, slots=True)
class TableReconciliation:
    source_path: str
    table: str
    source_sha256: str
    source_rows: int
    rejected_rows: int


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Stable source-to-destination accounting for one explicitly selected scope."""

    scope_key: str
    plan_sha256: str
    dry_run: bool
    source_tables: tuple[TableReconciliation, ...]
    exposure_candidates: int
    reliable_reply_candidates: int
    rejected_candidates: int
    quarantine_candidates: int
    exposures_written: int
    exposures_duplicate: int
    labels_written: int
    labels_duplicate: int
    quarantine_written: int
    quarantine_duplicate: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require_scope(scope_key: str) -> str:
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key is required and must be non-empty")
    return scope_key


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def _transaction(connection: Any) -> Any:
    factory = getattr(connection, "transaction", None)
    return factory() if factory is not None else nullcontext()


def _chunks(items: tuple[Any, ...], size: int) -> Iterable[tuple[Any, ...]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _exists(connection: Any, table: str, scope: str, key: str) -> bool:
    # table is selected exclusively by this module, never by caller input.
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE scope_key = %s AND idempotency_key = %s",
        (scope, key),
    ).fetchone()
    return row is not None


def _reply_payload(candidate: ReplyCandidate) -> dict[str, Any]:
    return {
        "label_id": candidate.label_id,
        "exposure_id": candidate.exposure_id,
        "scope_key": candidate.scope_key,
        "target": "reply",
        "status": "observed_positive",
        "value": True,
        "observed_at": candidate.observed_at,
        "source_event_ids": list(candidate.evidence),
        "migration_evidence_class": "reliable_R_candidate",
    }


def _audit_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "\0".join((kind, *parts))))


def _source_reconciliation(plan: MigrationPlan, scope: str) -> tuple[TableReconciliation, ...]:
    result: list[TableReconciliation] = []
    for manifest in plan.manifests:
        if manifest.scope_key != scope:
            continue
        for table in manifest.tables:
            result.append(
                TableReconciliation(
                    source_path=manifest.source_path,
                    table=table.table,
                    source_sha256=table.sha256,
                    source_rows=table.row_count,
                    rejected_rows=sum(count for _reason, count in table.reject_reasons),
                )
            )
    return tuple(sorted(result, key=lambda item: (item.source_path, item.table)))


def _reliable_replies(plan: MigrationPlan, scope: str) -> tuple[ReplyCandidate, ...]:
    """Return at most one reliable reply fact per exposure.

    Several historical user events can point at the same delivered action. They are
    not independent reply labels and must not become successive active revisions.
    Keep the earliest deterministic fact; the extras are quarantined below.
    """
    candidates = sorted(
        (
            item for item in plan.reply_candidates
            if item.scope_key == scope and item.importable and item.observed_at is not None
        ),
        key=lambda item: (str(item.exposure_id), str(item.observed_at), item.label_id),
    )
    first_by_exposure: dict[str, ReplyCandidate] = {}
    for item in candidates:
        first_by_exposure.setdefault(str(item.exposure_id), item)
    return tuple(first_by_exposure.values())


def _quarantine_records(plan: MigrationPlan, scope: str) -> tuple[QuarantineRecord, ...]:
    records = [item for item in plan.quarantine if item.scope_key == scope]
    chosen = {item.label_id for item in _reliable_replies(plan, scope)}
    for item in plan.reply_candidates:
        if item.scope_key != scope:
            continue
        if item.importable and item.observed_at is not None and item.label_id in chosen:
            continue
        payload = asdict(item)
        duplicate = item.importable and item.observed_at is not None
        records.append(
            QuarantineRecord(
                scope_key=scope,
                source_table="raw_events",
                source_row=item.evidence[0] if item.evidence else f"reply:{item.label_id}",
                classification=("reply_duplicate_exposure" if duplicate else f"reply_{item.status}"),
                reason=(
                    "additional reply fact for an exposure already assigned one R label"
                    if duplicate else item.reason or "reply candidate failed reliability gate"
                ),
                payload_sha256=hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
            )
        )
    return tuple(sorted(records, key=lambda item: (item.source_table, item.source_row,
                                                    item.payload_sha256)))


def _base_report(plan: MigrationPlan, scope: str, *, dry_run: bool) -> ReconciliationReport:
    exposures = tuple(item for item in plan.exposures if item.scope_key == scope)
    replies = tuple(item for item in plan.reply_candidates if item.scope_key == scope)
    reliable = _reliable_replies(plan, scope)
    quarantine = _quarantine_records(plan, scope)
    return ReconciliationReport(
        scope_key=scope,
        plan_sha256=plan.plan_sha256,
        dry_run=dry_run,
        source_tables=_source_reconciliation(plan, scope),
        exposure_candidates=len(exposures),
        reliable_reply_candidates=len(reliable),
        rejected_candidates=len(replies) - len(reliable),
        quarantine_candidates=len(quarantine),
        exposures_written=0,
        exposures_duplicate=0,
        labels_written=0,
        labels_duplicate=0,
        quarantine_written=0,
        quarantine_duplicate=0,
    )


def _validate_plan_scope(plan: MigrationPlan, scope: str) -> None:
    if not isinstance(plan, MigrationPlan):
        raise TypeError("plan must be a MigrationPlan produced by user_model_v2_import")
    known = {item.scope_key for item in plan.manifests}
    if scope not in known:
        raise ValueError(f"scope_key {scope!r} is not present in the migration plan")
    if any(item.scope_key != scope for item in plan.exposures if item.source_path in {
        manifest.source_path for manifest in plan.manifests if manifest.scope_key == scope
    }):
        raise ValueError("plan contains an exposure whose explicit source scope does not match")


def _write_quarantine(connection: Any, plan: MigrationPlan, item: QuarantineRecord) -> bool:
    cursor = connection.execute(
        """INSERT INTO user_model_migration_quarantine_v2
           (migration_quarantine_id, scope_key, plan_sha256, source_table, source_row,
            classification, reason, payload_sha256)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (scope_key, plan_sha256, source_table, source_row, payload_sha256)
           DO NOTHING RETURNING migration_quarantine_id""",
        (
            _audit_id("quarantine", item.scope_key, plan.plan_sha256, item.source_table,
                      item.source_row, item.payload_sha256),
            item.scope_key,
            plan.plan_sha256,
            item.source_table,
            item.source_row,
            item.classification,
            item.reason,
            item.payload_sha256,
        ),
    )
    return cursor.fetchone() is not None


def _write_audit(connection: Any, plan: MigrationPlan, report: ReconciliationReport) -> None:
    manifests = tuple(item for item in plan.manifests if item.scope_key == report.scope_key)
    manifest_hash = hashlib.sha256(
        canonical_json([item.manifest_sha256 for item in manifests]).encode("utf-8")
    ).hexdigest()
    connection.execute(
        """INSERT INTO user_model_migration_audits_v2
           (migration_audit_id, scope_key, plan_sha256, source_manifest_sha256,
            dry_run, reconciliation)
           VALUES (%s, %s, %s, %s, FALSE, %s::jsonb)
           ON CONFLICT (scope_key, plan_sha256, source_manifest_sha256, dry_run)
           DO NOTHING""",
        (
            _audit_id("audit", report.scope_key, plan.plan_sha256, manifest_hash),
            report.scope_key,
            plan.plan_sha256,
            manifest_hash,
            canonical_json(report.to_dict()),
        ),
    )


def apply_migration_plan(
    plan: MigrationPlan,
    service_repository: Any,
    *,
    scope_key: str,
    dry_run: bool = True,
    batch_size: int = 100,
) -> ReconciliationReport:
    """Report or atomically batch-apply one explicit scope from ``plan``.

    Only ``ReplyCandidate.importable`` records are activated, as positive reply (R)
    observations.  Unknown/unattributable and legacy soft-label material is written only
    to the quarantine audit table.  A dry run performs no repository/database operation.
    Each batch owns one PostgreSQL transaction; an exception therefore rolls that whole
    batch back.  Destination uniqueness plus pre-write checks make replay counts stable.
    """

    scope = _require_scope(scope_key)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    _validate_plan_scope(plan, scope)
    report = _base_report(plan, scope, dry_run=bool(dry_run))
    if dry_run:
        return report

    connection = getattr(service_repository, "connection", None)
    low_level = getattr(service_repository, "repository", None)
    if connection is None or low_level is None:
        raise TypeError("service_repository must expose PostgreSQL connection and repository")

    exposures = tuple(item for item in plan.exposures if item.scope_key == scope)
    reliable = _reliable_replies(plan, scope)
    quarantine = _quarantine_records(plan, scope)
    written_exposures = duplicate_exposures = 0
    written_labels = duplicate_labels = 0
    written_quarantine = duplicate_quarantine = 0

    for batch in _chunks(exposures, batch_size):
        with _transaction(connection):
            for item in batch:
                if _exists(connection, "interaction_exposures_v2", scope, item.idempotency_key):
                    duplicate_exposures += 1
                    continue
                low_level.insert_exposure(**item.repository_kwargs())
                written_exposures += 1

    for batch in _chunks(reliable, batch_size):
        with _transaction(connection):
            for item in batch:
                if _exists(connection, "interaction_target_labels_v2", scope, item.idempotency_key):
                    duplicate_labels += 1
                    continue
                observed_at = datetime.fromisoformat(item.observed_at)  # validated by planner
                activated = low_level.insert_label_revision_and_activate(
                    scope_key=scope,
                    target_label_id=item.label_id,
                    exposure_id=item.exposure_id,
                    labelled_at=observed_at,
                    target_name="reply",
                    target_value=_reply_payload(item),
                    evidence={"source_event_ids": list(item.evidence), "reliability": "R"},
                    confidence=1.0,
                    expected_pointer_version=None,
                    idempotency_key=item.idempotency_key,
                )
                if not activated:
                    raise RuntimeError("reliable reply label could not acquire its active pointer")
                written_labels += 1

    for batch in _chunks(quarantine, batch_size):
        with _transaction(connection):
            for item in batch:
                if _write_quarantine(connection, plan, item):
                    written_quarantine += 1
                else:
                    duplicate_quarantine += 1

    report = replace(
        report,
        dry_run=False,
        exposures_written=written_exposures,
        exposures_duplicate=duplicate_exposures,
        labels_written=written_labels,
        labels_duplicate=duplicate_labels,
        quarantine_written=written_quarantine,
        quarantine_duplicate=duplicate_quarantine,
    )
    with _transaction(connection):
        _write_audit(connection, plan, report)
    return report


# Offline-tooling alias; intentionally not imported by a Runtime composition root.
apply_user_model_v2_import = apply_migration_plan

__all__ = [
    "ReconciliationReport",
    "TableReconciliation",
    "apply_migration_plan",
    "apply_user_model_v2_import",
]
