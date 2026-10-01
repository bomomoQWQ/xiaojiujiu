"""Transactional PostgreSQL applier for 「浪潮」 M0 mechanical-history plans.

The planner is intentionally pure; this module is the only place where a plan may
cross into the isolated PostgreSQL target.  Every identifier comes from a closed
whitelist (apart from the separately validated schema name), every value remains a
parameter, and a hard conflict aborts the complete import transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from .mechanical_history_import import (
    IMPORT_ORDER,
    TABLE_SPECS,
    MechanicalMigrationPlan,
    PlannedMechanicalRow,
)
from .user_model_v2_migrations import migration_records, quote_schema_name
from .user_model_v2_schema import USER_MODEL_SCHEMA_VERSION

DEFAULT_IMPORTER_VERSION: Final[str] = "langchao-mechanical-history-applier/1"
ADVISORY_LOCK_NAMESPACE: Final[int] = 0x4C414E474348414F  # ASCII ``LANGCHAO``

# Never infer identity from column position or database metadata.  This is the
# complete reviewed destination identity contract.
PRIMARY_KEYS: Final[dict[str, str]] = {
    "raw_events": "event_id",
    "event_semantics": "event_id",
    "interpretation_versions": "interpretation_id",
    "memories": "memory_id",
    "memory_candidates": "candidate_id",
    "unfinished_matters": "unfinished_id",
    "boundaries": "boundary_id",
    "activated_memories": "memory_id",
    "working_situation_items": "item_id",
}
_TABLES = {spec.name: spec for spec in TABLE_SPECS}
_KINDS = {
    spec.name: {column: kind for column, kind, _nullable in spec.columns}
    for spec in TABLE_SPECS
}


class MechanicalHistoryApplyError(RuntimeError):
    """A plan cannot be safely reconciled or atomically applied."""

    def __init__(self, message: str, *, report: "MechanicalReconciliation | None" = None):
        super().__init__(message)
        self.report = report


@dataclass(frozen=True, slots=True)
class ColumnDifference:
    """Non-sensitive evidence that one destination column differs."""

    column: str
    planned_sha256: str
    existing_sha256: str


@dataclass(frozen=True, slots=True)
class RowReconciliation:
    table: str
    primary_key: str
    outcome: str
    differences: tuple[ColumnDifference, ...] = ()


@dataclass(frozen=True, slots=True)
class TableReconciliation:
    table: str
    inserted: int
    exact_duplicate: int
    conflict: int
    selected_pk_sha256: str
    target_rows_sha256: str | None


@dataclass(frozen=True, slots=True)
class MechanicalReconciliation:
    run_id: str
    scope_key: str
    target_schema: str
    source_snapshot_sha256: str
    plan_sha256: str
    importer_version: str
    dry_run: bool
    status: str
    tables: tuple[TableReconciliation, ...]
    rows: tuple[RowReconciliation, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _snapshot_sha256(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MechanicalHistoryApplyError(f"cannot hash planned source snapshot: {exc}") from exc
    return digest.hexdigest()


def _normalise_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise MechanicalHistoryApplyError("target returned an invalid timestamp") from exc
    else:
        raise MechanicalHistoryApplyError("target returned a non-timestamp value")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MechanicalHistoryApplyError("target returned a timezone-naive timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _normalise(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "jsonb":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise MechanicalHistoryApplyError("invalid JSON value during reconciliation") from exc
        return _canonical_json(value)
    if kind == "timestamptz":
        return _normalise_timestamp(value)
    if kind == "boolean":
        if type(value) is not bool:
            raise MechanicalHistoryApplyError("target returned a non-boolean value")
        return value
    if kind == "bigint":
        if type(value) is not int:
            raise MechanicalHistoryApplyError("target returned a non-integer value")
        return value
    if kind == "double":
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise MechanicalHistoryApplyError("target returned an invalid floating-point value")
        return float(value)
    if kind == "text":
        if not isinstance(value, str):
            raise MechanicalHistoryApplyError("target returned a non-text value")
        return value
    raise AssertionError(kind)


def _row_mapping(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return {column: row[column] for column in columns}
    return dict(zip(columns, row, strict=True))


def _fetchone(executor: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return executor.execute(sql, params or None).fetchone()


def _validate_ledger(executor: Any, schema_sql: str) -> None:
    rows = executor.execute(
        f"SELECT version, checksum FROM {schema_sql}.schema_migrations_v2 ORDER BY version"
    ).fetchall()
    present = {
        int(row["version"] if isinstance(row, Mapping) else row[0]):
        str(row["checksum"] if isinstance(row, Mapping) else row[1])
        for row in rows
    }
    expected = {record.version: record.checksum for record in migration_records()}
    if present != expected or max(present, default=0) != USER_MODEL_SCHEMA_VERSION:
        raise MechanicalHistoryApplyError(
            f"target schema migration ledger is not exactly v{USER_MODEL_SCHEMA_VERSION}"
        )


def _validate_plan(plan: MechanicalMigrationPlan, *, allow_quarantine: bool) -> None:
    if not isinstance(plan.scope_key, str) or not plan.scope_key.strip():
        raise MechanicalHistoryApplyError("plan scope_key must be non-empty")
    plan_body = {
        "format": "mechanical_history/langchao-m0-v1",
        "scope_key": plan.scope_key,
        "include_rebuildable": plan.include_rebuildable,
        "integrity_check": plan.integrity_check,
        "tables": [asdict(audit) for audit in plan.tables],
        "rows": [
            {"table": row.table, "scope_key": row.scope_key, "values": dict(row.values),
             "row_sha256": row.row_sha256}
            for row in plan.rows
        ],
        "quarantine": [
            {"table": item.table, "scope_key": item.scope_key, "reason": item.reason,
             "detail": item.detail, "source_row_sha256": item.source_row_sha256}
            for item in plan.quarantine
        ],
    }
    if plan.plan_sha256 != _sha(plan_body):
        raise MechanicalHistoryApplyError("plan_sha256 does not match the plan payload")
    if plan.quarantine and not allow_quarantine:
        raise MechanicalHistoryApplyError(
            f"plan contains {len(plan.quarantine)} quarantined row(s); apply is refused"
        )
    seen: set[tuple[str, str]] = set()
    previous_order = -1
    for row in plan.rows:
        if row.table not in PRIMARY_KEYS:
            raise MechanicalHistoryApplyError(f"unapproved destination table: {row.table!r}")
        if row.scope_key != plan.scope_key:
            raise MechanicalHistoryApplyError("planned row scope does not match plan scope")
        order = IMPORT_ORDER.index(row.table)
        if order < previous_order:
            raise MechanicalHistoryApplyError("plan rows are not in mechanical import order")
        previous_order = order
        spec = _TABLES[row.table]
        values = row.as_dict()
        if tuple(values) != spec.column_names:
            raise MechanicalHistoryApplyError(f"column contract mismatch for {row.table}")
        pk = str(values[PRIMARY_KEYS[row.table]])
        identity = (row.table, pk)
        if identity in seen:
            raise MechanicalHistoryApplyError(f"duplicate planned primary key in {row.table}")
        seen.add(identity)


def _lock_key(scope_key: str, target_schema: str) -> int:
    digest = hashlib.sha256(f"{target_schema}\0{scope_key}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big", signed=False) ^ ADVISORY_LOCK_NAMESPACE
    return value if value < (1 << 63) else value - (1 << 64)


def _tx_context(executor: Any):
    transaction = getattr(executor, "transaction", None)
    return transaction() if callable(transaction) else nullcontext()


def _select_existing(executor: Any, schema_sql: str, row: PlannedMechanicalRow) -> dict[str, Any] | None:
    spec = _TABLES[row.table]
    pk_column = PRIMARY_KEYS[row.table]
    columns_sql = ", ".join(f'"{column}"' for column in spec.column_names)
    result = _fetchone(
        executor,
        f'SELECT {columns_sql} FROM {schema_sql}."{row.table}" WHERE "{pk_column}" = %s',
        (row.as_dict()[pk_column],),
    )
    return None if result is None else _row_mapping(result, spec.column_names)


def _normalised_values(row: PlannedMechanicalRow, values: Mapping[str, Any]) -> dict[str, Any]:
    return {column: _normalise(_KINDS[row.table][column], values[column]) for column in _TABLES[row.table].column_names}


def _differences(planned: Mapping[str, Any], existing: Mapping[str, Any]) -> tuple[ColumnDifference, ...]:
    return tuple(
        ColumnDifference(column, _sha(planned[column]), _sha(existing[column]))
        for column in planned
        if planned[column] != existing[column]
    )


def _insert(executor: Any, schema_sql: str, row: PlannedMechanicalRow) -> None:
    spec = _TABLES[row.table]
    values = row.as_dict()
    columns_sql = ", ".join(f'"{column}"' for column in spec.column_names)
    placeholders = ", ".join(
        "%s::jsonb" if _KINDS[row.table][column] == "jsonb" else "%s"
        for column in spec.column_names
    )
    params = tuple(values[column] for column in spec.column_names)
    executor.execute(
        f'INSERT INTO {schema_sql}."{row.table}" ({columns_sql}) VALUES ({placeholders})',
        params,
    )


def reconcile_mechanical_plan(
    plan: MechanicalMigrationPlan,
    connection: Any,
    dry_run: bool = True,
    importer_version: str = DEFAULT_IMPORTER_VERSION,
    *,
    target_schema: str = "companion_runtime",
    allow_quarantine: bool = False,
) -> MechanicalReconciliation:
    """Reconcile and optionally atomically apply one immutable mechanical plan."""
    if not isinstance(importer_version, str) or not importer_version.strip():
        raise ValueError("importer_version must be non-empty")
    schema_sql = quote_schema_name(target_schema)
    _validate_plan(plan, allow_quarantine=allow_quarantine)
    source_hash = _snapshot_sha256(plan.source_path)
    executor = getattr(connection, "raw", connection)
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    row_results: list[RowReconciliation] = []
    inserted_rows: list[PlannedMechanicalRow] = []
    grouped: dict[str, list[PlannedMechanicalRow]] = {table: [] for table in IMPORT_ORDER}
    for row in plan.rows:
        grouped[row.table].append(row)

    try:
        with _tx_context(executor):
            current = _fetchone(executor, "SELECT current_schema() AS schema")
            current_name = current["schema"] if isinstance(current, Mapping) else current[0]
            if str(current_name) != target_schema:
                raise MechanicalHistoryApplyError(
                    f"connection current_schema {current_name!r} does not match target_schema {target_schema!r}"
                )
            _validate_ledger(executor, schema_sql)
            executor.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(plan.scope_key, target_schema),))
            prior_audit = _fetchone(
                executor,
                f"SELECT run_id FROM {schema_sql}.mechanical_history_import_audits_v1 "
                "WHERE scope_key = %s AND source_snapshot_sha256 = %s "
                "AND plan_sha256 = %s AND importer_version = %s",
                (plan.scope_key, source_hash, plan.plan_sha256, importer_version),
            )
            if prior_audit is not None:
                run_id = str(prior_audit["run_id"] if isinstance(prior_audit, Mapping) else prior_audit[0])

            hard_conflict = False
            for table in IMPORT_ORDER:
                for row in grouped[table]:
                    planned = _normalised_values(row, row.as_dict())
                    existing_raw = _select_existing(executor, schema_sql, row)
                    pk = str(planned[PRIMARY_KEYS[table]])
                    if existing_raw is None:
                        row_results.append(RowReconciliation(table, pk, "insert"))
                        inserted_rows.append(row)
                    else:
                        existing = _normalised_values(row, existing_raw)
                        diffs = _differences(planned, existing)
                        if diffs:
                            hard_conflict = True
                            row_results.append(RowReconciliation(table, pk, "conflict", diffs))
                        else:
                            row_results.append(RowReconciliation(table, pk, "exact_duplicate"))
            if hard_conflict:
                report = _build_report(
                    run_id, plan, target_schema, source_hash, importer_version, dry_run,
                    "failed", row_results, {},
                )
                raise MechanicalHistoryApplyError(
                    "mechanical history hard conflict; transaction rolled back", report=report
                )

            if not dry_run:
                for row in inserted_rows:
                    _insert(executor, schema_sql, row)

            target_hashes: dict[str, str] = {}
            for table in IMPORT_ORDER:
                selected = grouped[table]
                if not selected:
                    continue
                hashes: list[str] = []
                for row in selected:
                    target = _select_existing(executor, schema_sql, row)
                    # During dry-run absent rows are hashed from their would-be target form.
                    if target is None and dry_run:
                        target = row.as_dict()
                    if target is None:
                        raise MechanicalHistoryApplyError("insert verification could not find selected primary key")
                    normalised = _normalised_values(row, target)
                    digest = _sha(normalised)
                    if digest != row.row_sha256:
                        raise MechanicalHistoryApplyError(
                            f"post-apply canonical hash mismatch for {table} primary key"
                        )
                    hashes.append(digest)
                target_hashes[table] = _sha(hashes)

            report = _build_report(
                run_id, plan, target_schema, source_hash, importer_version, dry_run,
                "dry_run" if dry_run else "applied", row_results, target_hashes,
            )
            if not dry_run and prior_audit is None:
                executor.execute(
                    f"INSERT INTO {schema_sql}.mechanical_history_import_audits_v1 "
                    "(run_id, scope_key, source_snapshot_sha256, plan_sha256, importer_version, "
                    "started_at, completed_at, status, reconciliation) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'applied', %s::jsonb)",
                    (
                        run_id, plan.scope_key, source_hash, plan.plan_sha256,
                        importer_version, started_at, datetime.now(timezone.utc),
                        _canonical_json(report.to_dict()),
                    ),
                )
                for item in plan.quarantine:
                    executor.execute(
                        f"INSERT INTO {schema_sql}.mechanical_history_import_quarantine_v1 "
                        "(quarantine_id, run_id, source_table, source_pk, reason, payload_sha256) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            str(uuid.uuid4()), run_id, item.table,
                            None if item.source_ordinal is None else str(item.source_ordinal),
                            item.reason, item.source_row_sha256,
                        ),
                    )
            return report
    except MechanicalHistoryApplyError:
        raise
    except Exception as exc:
        raise MechanicalHistoryApplyError(
            f"mechanical history apply failed; transaction rolled back: {type(exc).__name__}: {exc}"
        ) from exc


def _build_report(
    run_id: str,
    plan: MechanicalMigrationPlan,
    target_schema: str,
    source_hash: str,
    importer_version: str,
    dry_run: bool,
    status: str,
    rows: list[RowReconciliation],
    target_hashes: Mapping[str, str],
) -> MechanicalReconciliation:
    tables: list[TableReconciliation] = []
    for table in IMPORT_ORDER:
        selected = [row for row in plan.rows if row.table == table]
        outcomes = [row for row in rows if row.table == table]
        if not selected and not outcomes:
            continue
        pks = [str(row.as_dict()[PRIMARY_KEYS[table]]) for row in selected]
        tables.append(TableReconciliation(
            table=table,
            inserted=sum(row.outcome == "insert" for row in outcomes),
            exact_duplicate=sum(row.outcome == "exact_duplicate" for row in outcomes),
            conflict=sum(row.outcome == "conflict" for row in outcomes),
            selected_pk_sha256=_sha(sorted(pks)),
            target_rows_sha256=target_hashes.get(table),
        ))
    return MechanicalReconciliation(
        run_id=run_id,
        scope_key=plan.scope_key,
        target_schema=target_schema,
        source_snapshot_sha256=source_hash,
        plan_sha256=plan.plan_sha256,
        importer_version=importer_version,
        dry_run=dry_run,
        status=status,
        tables=tuple(tables),
        rows=tuple(rows),
    )


def apply_mechanical_plan(
    plan: MechanicalMigrationPlan,
    connection: Any,
    importer_version: str = DEFAULT_IMPORTER_VERSION,
    *,
    target_schema: str = "companion_runtime",
    allow_quarantine: bool = False,
) -> MechanicalReconciliation:
    """Atomically apply ``plan``; any conflict or error rolls back every table and audit."""
    return reconcile_mechanical_plan(
        plan,
        connection,
        dry_run=False,
        importer_version=importer_version,
        target_schema=target_schema,
        allow_quarantine=allow_quarantine,
    )


__all__ = [
    "DEFAULT_IMPORTER_VERSION",
    "PRIMARY_KEYS",
    "ColumnDifference",
    "MechanicalHistoryApplyError",
    "MechanicalReconciliation",
    "RowReconciliation",
    "TableReconciliation",
    "apply_mechanical_plan",
    "reconcile_mechanical_plan",
]
