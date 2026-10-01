"""Pure planner for the M0 SQLite mechanical-history import for 「浪潮」.

The planner opens a stopped legacy database read-only, validates its schema and rows,
and returns an immutable, deterministic description of work.  It deliberately has no
PostgreSQL dependency and performs no writes to either database.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Mapping


class MechanicalHistoryImportError(ValueError):
    """The source cannot safely be used to construct a mechanical import plan."""


@dataclass(frozen=True, slots=True)
class TableSpec:
    """Auditable source/destination contract for one isomorphic table."""

    name: str
    columns: tuple[tuple[str, str, bool], ...]
    rebuildable: bool = False

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column for column, _kind, _nullable in self.columns)


@dataclass(frozen=True, slots=True)
class SourceTableAudit:
    table: str
    required: bool
    present: bool
    source_columns: tuple[str, ...]
    missing_columns: tuple[str, ...]
    row_count: int
    planned_count: int
    quarantined_count: int
    canonical_rows_sha256: str


@dataclass(frozen=True, slots=True)
class PlannedMechanicalRow:
    table: str
    scope_key: str
    source_ordinal: int
    values: tuple[tuple[str, Any], ...]
    row_sha256: str

    def as_dict(self) -> dict[str, Any]:
        """Return the destination-column mapping in schema order."""
        return dict(self.values)


@dataclass(frozen=True, slots=True)
class MechanicalQuarantineRecord:
    table: str
    scope_key: str
    source_ordinal: int | None
    reason: str
    detail: str
    source_row_sha256: str


@dataclass(frozen=True, slots=True)
class MechanicalMigrationPlan:
    source_path: str
    scope_key: str
    sqlite_read_only: bool
    integrity_check: str
    include_rebuildable: bool
    tables: tuple[SourceTableAudit, ...]
    rows: tuple[PlannedMechanicalRow, ...]
    quarantine: tuple[MechanicalQuarantineRecord, ...]
    plan_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Kinds describe the native PostgreSQL destination type.  The column contracts are
# intentionally explicit rather than inferred from SQL text, making schema drift fail
# closed and reviewable.
def _column(name: str, kind: str, nullable: bool = False) -> tuple[str, str, bool]:
    return (name, kind, nullable)


TABLE_SPECS: Final[tuple[TableSpec, ...]] = (
    TableSpec("raw_events", (
        _column("event_id", "text"), _column("seq", "bigint", True),
        _column("event_type", "text"), _column("timestamp", "timestamptz"),
        _column("actor", "text"), _column("conversation_id", "text", True),
        _column("content", "text", True), _column("metadata_json", "jsonb"),
        _column("source_event_ids", "jsonb"), _column("runtime_version", "bigint"),
        _column("created_at", "timestamptz"),
    )),
    TableSpec("event_semantics", (
        _column("event_id", "text"), _column("semantic_status", "text"),
        _column("direction", "text", True), _column("intensity_band", "text", True),
        _column("confidence", "double", True), _column("settlement_source", "text", True),
        _column("evidence", "text", True), _column("potential_relevance", "text"),
        _column("unresolved_reason", "text", True), _column("settled_at", "timestamptz", True),
        _column("deep_refresh_id", "text", True), _column("version", "bigint"),
        _column("created_at", "timestamptz"), _column("updated_at", "timestamptz"),
    )),
    TableSpec("interpretation_versions", (
        _column("interpretation_id", "text"), _column("target_kind", "text"),
        _column("target_id", "text"), _column("interpretation_version", "bigint"),
        _column("supersedes_id", "text", True), _column("content", "text"),
        _column("confidence", "double"), _column("source_version", "bigint"),
        _column("source_event_ids", "jsonb"), _column("created_at", "timestamptz"),
    )),
    TableSpec("memories", (
        _column("memory_id", "text"), _column("kind", "text"), _column("summary", "text"),
        _column("structured_json", "jsonb"), _column("topics_json", "jsonb"),
        _column("importance", "double"), _column("confidence", "double"),
        _column("status", "text"), _column("source_event_ids", "jsonb"),
        _column("created_at", "timestamptz"), _column("updated_at", "timestamptz"),
        _column("archived_at", "timestamptz", True),
    )),
    TableSpec("memory_candidates", (
        _column("candidate_id", "text"), _column("summary", "text"),
        _column("kind", "text"), _column("source_event_ids", "jsonb"),
        _column("value", "double"), _column("status", "text"),
        _column("created_at", "timestamptz"), _column("updated_at", "timestamptz"),
        _column("consolidated_memory_id", "text", True), _column("topics_json", "jsonb"),
        _column("confidence", "double"), _column("structured_json", "jsonb"),
    )),
    TableSpec("unfinished_matters", (
        _column("unfinished_id", "text"), _column("title", "text"),
        _column("source_event_ids", "jsonb"), _column("status", "text"),
        _column("waiting_until", "timestamptz", True), _column("priority", "double"),
        _column("mute_until", "timestamptz", True), _column("expire_at", "timestamptz", True),
        _column("resolution_conditions", "jsonb"), _column("created_at", "timestamptz"),
        _column("updated_at", "timestamptz"), _column("resolution_note", "text", True),
    )),
    TableSpec("boundaries", (
        _column("boundary_id", "text"), _column("type", "text"), _column("scope", "text"),
        _column("allow_reply", "boolean"), _column("allow_proactive", "boolean"),
        _column("starts_at", "timestamptz", True), _column("expires_at", "timestamptz", True),
        _column("revocable_by", "text"), _column("source_event_id", "text", True),
        _column("revoked_at", "timestamptz", True), _column("note", "text", True),
        _column("subject", "text", True), _column("created_at", "timestamptz"),
    )),
    TableSpec("activated_memories", (
        _column("memory_id", "text"), _column("activation", "double"),
        _column("last_recalled_at", "timestamptz", True), _column("recall_count", "bigint"),
        _column("reason", "text", True), _column("updated_at", "timestamptz"),
    ), rebuildable=True),
    TableSpec("working_situation_items", (
        _column("item_id", "text"), _column("kind", "text"), _column("content", "text"),
        _column("confidence", "double"), _column("salience", "double"),
        _column("source_kind", "text"), _column("source_id", "text", True),
        _column("created_at", "timestamptz"), _column("updated_at", "timestamptz"),
        _column("expires_at", "timestamptz", True), _column("status", "text"),
    ), rebuildable=True),
)

IMPORT_ORDER: Final[tuple[str, ...]] = tuple(spec.name for spec in TABLE_SPECS)
REQUIRED_TABLES: Final[tuple[str, ...]] = tuple(
    spec.name for spec in TABLE_SPECS if not spec.rebuildable
)
REBUILDABLE_TABLES: Final[tuple[str, ...]] = tuple(
    spec.name for spec in TABLE_SPECS if spec.rebuildable
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def _strict_json(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected SQLite TEXT containing JSON")

    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON number {constant}")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
        return _canonical_json(parsed)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected non-empty timestamp TEXT")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _is_id_column(column: str) -> bool:
    return column == "id" or column.endswith("_id")


def _convert(column: str, kind: str, nullable: bool, value: Any) -> Any:
    if value is None:
        if nullable:
            return None
        raise ValueError("NULL in non-null destination column")
    if kind == "text":
        # SQLite affinity may surface an historical numeric ID as an integer.  IDs must
        # nevertheless remain opaque TEXT and are never parsed or regenerated.
        if _is_id_column(column):
            return str(value)
        if not isinstance(value, str):
            raise ValueError("expected TEXT")
        return value
    if kind == "jsonb":
        return _strict_json(value)
    if kind == "timestamptz":
        return _timestamp(value)
    if kind == "boolean":
        if type(value) is not int or value not in (0, 1):
            raise ValueError("boolean must be SQLite INTEGER 0 or 1")
        return bool(value)
    if kind == "bigint":
        if type(value) is not int:
            raise ValueError("expected SQLite INTEGER")
        if value < -(1 << 63) or value > (1 << 63) - 1:
            raise ValueError("integer outside signed bigint range")
        return value
    if kind == "double":
        if type(value) not in (int, float):
            raise ValueError("expected SQLite INTEGER or REAL")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("number must be finite")
        return converted
    raise AssertionError(f"unknown destination kind: {kind}")


def _source_row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Make arbitrary SQLite values hashable without accepting them as valid data."""
    payload: dict[str, Any] = {}
    for key in sorted(row):
        value = row[key]
        if isinstance(value, bytes):
            payload[key] = {"sqlite_blob_hex": value.hex()}
        elif isinstance(value, float) and not math.isfinite(value):
            payload[key] = {"sqlite_non_finite": repr(value)}
        else:
            payload[key] = value
    return payload


def _audit_hash(rows: Iterable[PlannedMechanicalRow]) -> str:
    return _sha([row.row_sha256 for row in rows])


def plan_sqlite_mechanical_import(
    source_path: str | Path,
    scope_key: str,
    include_rebuildable: bool = False,
    strict: bool = True,
) -> MechanicalMigrationPlan:
    """Plan a deterministic SQLite-to-PostgreSQL mechanical history import.

    Invalid *rows* are always quarantined.  ``strict`` controls structural failures:
    required missing tables/columns raise :class:`MechanicalHistoryImportError`; in
    non-strict mode they are represented in the audit and produce no planned rows.
    Rebuildable tables are outside the plan unless ``include_rebuildable`` is true.
    """
    path = Path(source_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key must be a non-empty string")

    selected = tuple(
        spec for spec in TABLE_SPECS if include_rebuildable or not spec.rebuildable
    )
    connection = sqlite3.connect(_readonly_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    audits: list[SourceTableAudit] = []
    planned: list[PlannedMechanicalRow] = []
    quarantine: list[MechanicalQuarantineRecord] = []
    try:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity = "; ".join(str(row[0]) for row in integrity_rows)
        if len(integrity_rows) != 1 or str(integrity_rows[0][0]).lower() != "ok":
            raise MechanicalHistoryImportError(f"SQLite integrity_check failed: {integrity}")

        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for spec in selected:
            required = not spec.rebuildable
            if spec.name not in existing:
                if strict and required:
                    raise MechanicalHistoryImportError(
                        f"required mechanical_history table is missing: {spec.name}"
                    )
                audits.append(SourceTableAudit(
                    table=spec.name, required=required, present=False, source_columns=(),
                    missing_columns=spec.column_names, row_count=0, planned_count=0,
                    quarantined_count=0, canonical_rows_sha256=_sha([]),
                ))
                continue

            source_columns = tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{spec.name}")').fetchall()
            )
            missing = tuple(column for column in spec.column_names if column not in source_columns)
            if missing and strict and required:
                raise MechanicalHistoryImportError(
                    f"required columns missing from {spec.name}: {', '.join(missing)}"
                )

            source_rows = [
                dict(row) for row in connection.execute(f'SELECT * FROM "{spec.name}"').fetchall()
            ]
            table_planned: list[PlannedMechanicalRow] = []
            table_quarantined = 0
            if missing:
                for ordinal, row in enumerate(source_rows):
                    payload = _source_row_payload(row)
                    quarantine.append(MechanicalQuarantineRecord(
                        table=spec.name, scope_key=scope_key, source_ordinal=ordinal,
                        reason="missing_required_columns", detail=", ".join(missing),
                        source_row_sha256=_sha(payload),
                    ))
                    table_quarantined += 1
            else:
                for ordinal, row in enumerate(source_rows):
                    try:
                        values = tuple(
                            (column, _convert(column, kind, nullable, row[column]))
                            for column, kind, nullable in spec.columns
                        )
                        digest = _sha(dict(values))
                        table_planned.append(PlannedMechanicalRow(
                            table=spec.name, scope_key=scope_key, source_ordinal=ordinal,
                            values=values, row_sha256=digest,
                        ))
                    except (KeyError, TypeError, ValueError, OverflowError) as exc:
                        payload = _source_row_payload(row)
                        quarantine.append(MechanicalQuarantineRecord(
                            table=spec.name, scope_key=scope_key, source_ordinal=ordinal,
                            reason="invalid_row", detail=str(exc),
                            source_row_sha256=_sha(payload),
                        ))
                        table_quarantined += 1

            # Physical SQLite row order is not migration semantics.  Hash and emit rows in
            # canonical order, while retaining source ordinals solely for diagnostics.
            table_planned.sort(key=lambda item: (item.row_sha256, item.source_ordinal))
            planned.extend(table_planned)
            audits.append(SourceTableAudit(
                table=spec.name, required=required, present=True,
                source_columns=source_columns, missing_columns=missing,
                row_count=len(source_rows), planned_count=len(table_planned),
                quarantined_count=table_quarantined,
                canonical_rows_sha256=_audit_hash(table_planned),
            ))
    except sqlite3.DatabaseError as exc:
        raise MechanicalHistoryImportError(f"cannot audit SQLite source: {exc}") from exc
    finally:
        connection.close()

    # Quarantine ordering is also independent of query/storage order.
    quarantine.sort(
        key=lambda item: (IMPORT_ORDER.index(item.table), item.source_row_sha256,
                          -1 if item.source_ordinal is None else item.source_ordinal)
    )
    plan_body = {
        "format": "mechanical_history/langchao-m0-v1",
        "scope_key": scope_key,
        "include_rebuildable": include_rebuildable,
        "integrity_check": integrity,
        "tables": [asdict(audit) for audit in audits],
        "rows": [
            {"table": row.table, "scope_key": row.scope_key, "values": dict(row.values),
             "row_sha256": row.row_sha256}
            for row in planned
        ],
        "quarantine": [
            {"table": item.table, "scope_key": item.scope_key, "reason": item.reason,
             "detail": item.detail, "source_row_sha256": item.source_row_sha256}
            for item in quarantine
        ],
    }
    return MechanicalMigrationPlan(
        source_path=str(path.resolve()), scope_key=scope_key, sqlite_read_only=True,
        integrity_check=integrity, include_rebuildable=include_rebuildable,
        tables=tuple(audits), rows=tuple(planned), quarantine=tuple(quarantine),
        plan_sha256=_sha(plan_body),
    )


__all__ = [
    "IMPORT_ORDER", "REBUILDABLE_TABLES", "REQUIRED_TABLES", "TABLE_SPECS",
    "MechanicalHistoryImportError", "MechanicalMigrationPlan",
    "MechanicalQuarantineRecord", "PlannedMechanicalRow", "SourceTableAudit",
    "TableSpec", "plan_sqlite_mechanical_import",
]
