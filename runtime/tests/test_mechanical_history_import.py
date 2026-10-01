from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from companion_runtime.mechanical_history_import import (
    IMPORT_ORDER,
    REBUILDABLE_TABLES,
    REQUIRED_TABLES,
    TABLE_SPECS,
    MechanicalHistoryImportError,
    plan_sqlite_mechanical_import,
)


def _sqlite_type(kind: str) -> str:
    return {
        "text": "TEXT",
        "jsonb": "TEXT",
        "timestamptz": "TEXT",
        "boolean": "INTEGER",
        "bigint": "INTEGER",
        "double": "REAL",
    }[kind]


def _create_source(path: Path, *, omit: str | None = None) -> None:
    with sqlite3.connect(path) as connection:
        for spec in TABLE_SPECS:
            if spec.name == omit:
                continue
            definitions = ", ".join(
                f'"{name}" {_sqlite_type(kind)}' for name, kind, _nullable in spec.columns
            )
            connection.execute(f'CREATE TABLE "{spec.name}" ({definitions})')


def _value(column: str, kind: str, nullable: bool, *, suffix: str = "1") -> object:
    if nullable:
        return None
    if kind == "text":
        return f"text-{suffix}"
    if kind == "jsonb":
        list_columns = {"source_event_ids", "topics_json", "resolution_conditions"}
        return "[]" if column in list_columns else "{}"
    if kind == "timestamptz":
        return "2025-01-01T01:02:03+08:00"
    if kind == "boolean":
        return 1
    if kind == "bigint":
        return 7
    if kind == "double":
        return 0.25
    raise AssertionError(kind)


def _insert_valid(
    connection: sqlite3.Connection,
    table: str,
    *,
    suffix: str = "1",
    **overrides: object,
) -> None:
    spec = next(item for item in TABLE_SPECS if item.name == table)
    columns = spec.column_names
    values = [
        overrides.get(name, _value(name, kind, nullable, suffix=suffix))
        for name, kind, nullable in spec.columns
    ]
    marks = ",".join("?" for _ in values)
    names = ",".join(f'"{name}"' for name in columns)
    connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', values)


def _row(plan: object, table: str) -> dict[str, object]:
    rows = [item for item in plan.rows if item.table == table]  # type: ignore[attr-defined]
    assert len(rows) == 1
    return rows[0].as_dict()


def test_valid_native_mapping_ids_and_json_canonicalization(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _create_source(source)
    with sqlite3.connect(source) as connection:
        _insert_valid(
            connection,
            "raw_events",
            event_id="00123",
            seq=42,
            metadata_json='{ "z": 1, "a": {"β": true} }',
            source_event_ids='[ "0007", 2 ]',
            timestamp="2025-01-01T08:00:00+08:00",
            created_at="2025-01-01T00:00:01Z",
        )
        _insert_valid(
            connection,
            "boundaries",
            boundary_id="0009",
            allow_reply=0,
            allow_proactive=1,
        )

    plan = plan_sqlite_mechanical_import(source, "person:one")
    raw = _row(plan, "raw_events")
    boundary = _row(plan, "boundaries")

    assert raw["event_id"] == "00123"
    assert isinstance(raw["event_id"], str)
    assert raw["seq"] == 42
    assert raw["metadata_json"] == '{"a":{"β":true},"z":1}'
    assert raw["source_event_ids"] == '["0007",2]'
    assert raw["timestamp"] == "2025-01-01T00:00:00+00:00"
    assert boundary["boundary_id"] == "0009"
    assert boundary["allow_reply"] is False
    assert boundary["allow_proactive"] is True
    assert plan.sqlite_read_only is True
    assert plan.integrity_check == "ok"
    assert not plan.quarantine
    assert tuple(audit.table for audit in plan.tables) == REQUIRED_TABLES


def test_source_is_never_modified_and_same_input_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _create_source(source)
    with sqlite3.connect(source) as connection:
        _insert_valid(connection, "raw_events")
    before = source.read_bytes()

    first = plan_sqlite_mechanical_import(source, "person:stable")
    middle = source.read_bytes()
    second = plan_sqlite_mechanical_import(source, "person:stable")

    assert before == middle == source.read_bytes()
    assert first == second
    assert first.plan_sha256 == second.plan_sha256
    assert len(first.plan_sha256) == 64
    assert len(first.rows[0].row_sha256) == 64
    with pytest.raises(FrozenInstanceError):
        first.rows[0].table = "changed"  # type: ignore[misc]


def test_hash_is_stable_across_physical_row_order(tmp_path: Path) -> None:
    paths = (tmp_path / "one.db", tmp_path / "two.db")
    for path in paths:
        _create_source(path)
    with sqlite3.connect(paths[0]) as connection:
        _insert_valid(connection, "raw_events", suffix="a", event_id="a")
        _insert_valid(connection, "raw_events", suffix="b", event_id="b")
    with sqlite3.connect(paths[1]) as connection:
        _insert_valid(connection, "raw_events", suffix="b", event_id="b")
        _insert_valid(connection, "raw_events", suffix="a", event_id="a")

    first = plan_sqlite_mechanical_import(paths[0], "same-scope")
    second = plan_sqlite_mechanical_import(paths[1], "same-scope")

    assert first.plan_sha256 == second.plan_sha256
    assert [row.row_sha256 for row in first.rows] == [row.row_sha256 for row in second.rows]


@pytest.mark.parametrize(
    ("table", "overrides", "message"),
    [
        ("raw_events", {"metadata_json": "{bad"}, "invalid JSON"),
        ("raw_events", {"timestamp": "2025-01-01T00:00:00"}, "timezone-aware"),
        ("boundaries", {"allow_reply": 2}, "0 or 1"),
        ("memories", {"importance": float("inf")}, "finite"),
    ],
)
def test_bad_values_are_quarantined(
    tmp_path: Path, table: str, overrides: dict[str, object], message: str
) -> None:
    source = tmp_path / f"{table}.db"
    _create_source(source)
    with sqlite3.connect(source) as connection:
        _insert_valid(connection, table, **overrides)

    plan = plan_sqlite_mechanical_import(source, "person:bad")

    assert not [row for row in plan.rows if row.table == table]
    record = next(item for item in plan.quarantine if item.table == table)
    assert record.reason == "invalid_row"
    assert message in record.detail
    audit = next(item for item in plan.tables if item.table == table)
    assert audit.row_count == 1
    assert audit.planned_count == 0
    assert audit.quarantined_count == 1


def test_json_nonstandard_nonfinite_number_is_quarantined(tmp_path: Path) -> None:
    source = tmp_path / "nan-json.db"
    _create_source(source)
    with sqlite3.connect(source) as connection:
        _insert_valid(connection, "raw_events", metadata_json='{"value": NaN}')

    plan = plan_sqlite_mechanical_import(source, "person:bad-json")
    assert "non-finite JSON number" in plan.quarantine[0].detail


def test_missing_required_table_and_column_fail_strict(tmp_path: Path) -> None:
    missing_table = tmp_path / "missing-table.db"
    _create_source(missing_table, omit="memories")
    with pytest.raises(MechanicalHistoryImportError, match="table is missing: memories"):
        plan_sqlite_mechanical_import(missing_table, "person:one")

    missing_column = tmp_path / "missing-column.db"
    _create_source(missing_column)
    with sqlite3.connect(missing_column) as connection:
        connection.execute("ALTER TABLE raw_events DROP COLUMN actor")
    with pytest.raises(
        MechanicalHistoryImportError,
        match="columns missing from raw_events: actor",
    ):
        plan_sqlite_mechanical_import(missing_column, "person:one")


def test_non_strict_audits_structural_gaps(tmp_path: Path) -> None:
    source = tmp_path / "partial.db"
    _create_source(source, omit="memories")
    plan = plan_sqlite_mechanical_import(source, "person:one", strict=False)
    audit = next(item for item in plan.tables if item.table == "memories")
    assert not audit.present
    assert audit.missing_columns


def test_rebuildable_tables_are_opt_in_and_order_is_fixed(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _create_source(source)
    with sqlite3.connect(source) as connection:
        for spec in TABLE_SPECS:
            _insert_valid(connection, spec.name)

    default = plan_sqlite_mechanical_import(source, "person:one")
    included = plan_sqlite_mechanical_import(source, "person:one", include_rebuildable=True)

    default_tables = {row.table for row in default.rows}
    assert not (default_tables & set(REBUILDABLE_TABLES))
    assert tuple(audit.table for audit in default.tables) == REQUIRED_TABLES
    assert tuple(audit.table for audit in included.tables) == IMPORT_ORDER
    assert tuple(row.table for row in included.rows) == IMPORT_ORDER
    assert IMPORT_ORDER == (
        "raw_events",
        "event_semantics",
        "interpretation_versions",
        "memories",
        "memory_candidates",
        "unfinished_matters",
        "boundaries",
        "activated_memories",
        "working_situation_items",
    )
