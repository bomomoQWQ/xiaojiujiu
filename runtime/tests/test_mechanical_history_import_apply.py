from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from companion_runtime.mechanical_history_import import (
    MechanicalMigrationPlan,
    MechanicalQuarantineRecord,
    PlannedMechanicalRow,
)
from companion_runtime.mechanical_history_import_apply import (
    MechanicalHistoryApplyError,
    apply_mechanical_plan,
    reconcile_mechanical_plan,
)
from companion_runtime.user_model_v2_migrations import migration_records


def _sha(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def _raw_values(**overrides):
    values = {
        "event_id": "event-1",
        "seq": 1,
        "event_type": "message",
        "timestamp": "2025-01-01T00:00:00+00:00",
        "actor": "user",
        "conversation_id": None,
        "content": "sensitive " + "x" * 200,
        "metadata_json": '{"a":1,"b":true}',
        "source_event_ids": '["source-1"]',
        "runtime_version": 7,
        "created_at": "2025-01-01T00:00:01+00:00",
    }
    values.update(overrides)
    return tuple(values.items())


def _plan(tmp_path: Path, *rows: PlannedMechanicalRow, quarantine=()):
    source = tmp_path / "snapshot.db"
    source.write_bytes(b"immutable source")
    rows = tuple(rows)
    quarantine = tuple(quarantine)
    body = {
        "format": "mechanical_history/langchao-m0-v1",
        "scope_key": "scope:one",
        "include_rebuildable": False,
        "integrity_check": "ok",
        "tables": [],
        "rows": [
            {"table": row.table, "scope_key": row.scope_key, "values": dict(row.values),
             "row_sha256": row.row_sha256}
            for row in rows
        ],
        "quarantine": [
            {"table": item.table, "scope_key": item.scope_key, "reason": item.reason,
             "detail": item.detail, "source_row_sha256": item.source_row_sha256}
            for item in quarantine
        ],
    }
    return MechanicalMigrationPlan(
        source_path=str(source), scope_key="scope:one", sqlite_read_only=True,
        integrity_check="ok", include_rebuildable=False, tables=(), rows=rows,
        quarantine=quarantine, plan_sha256=_sha(body),
    )


def _row(**overrides):
    values = _raw_values(**overrides)
    return PlannedMechanicalRow("raw_events", "scope:one", 0, values, _sha(dict(values)))


class Result:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.pending = {table: dict(rows) for table, rows in self.connection.tables.items()}
        self.connection.pending_audits = list(self.connection.audits)

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.connection.tables = self.connection.pending
            self.connection.audits = self.connection.pending_audits
        self.connection.pending = None
        return False


class FakeConnection:
    def __init__(self, *, current_schema="target", existing=None, fail_insert_at=None):
        self.current_schema = current_schema
        self.tables = {"raw_events": dict(existing or {})}
        self.audits = []
        self.pending = None
        self.pending_audits = None
        self.calls = []
        self.insert_count = 0
        self.fail_insert_at = fail_insert_at

    def transaction(self):
        return Transaction(self)

    def execute(self, sql, params=None):
        params = tuple(params or ())
        self.calls.append((" ".join(sql.split()), params))
        if sql.startswith("SELECT current_schema"):
            return Result({"schema": self.current_schema})
        if "schema_migrations_v2 ORDER BY version" in sql:
            return Result(rows=[{"version": r.version, "checksum": r.checksum} for r in migration_records()])
        if "pg_advisory_xact_lock" in sql:
            return Result({"pg_advisory_xact_lock": None})
        if "SELECT run_id FROM" in sql:
            for audit in self.pending_audits:
                if audit[1:5] == list(params):
                    return Result({"run_id": audit[0]})
            return Result()
        if 'SELECT "event_id", "seq"' in sql:
            row = self.pending["raw_events"].get(str(params[0]))
            return Result(row)
        if 'INSERT INTO "target"."raw_events"' in sql:
            self.insert_count += 1
            if self.fail_insert_at == self.insert_count:
                raise RuntimeError("injected insert failure")
            columns = [
                "event_id", "seq", "event_type", "timestamp", "actor", "conversation_id",
                "content", "metadata_json", "source_event_ids", "runtime_version", "created_at",
            ]
            data = dict(zip(columns, params, strict=True))
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
            data["created_at"] = datetime.fromisoformat(data["created_at"])
            data["metadata_json"] = json.loads(data["metadata_json"])
            data["source_event_ids"] = json.loads(data["source_event_ids"])
            self.pending["raw_events"][data["event_id"]] = data
            return Result()
        if "mechanical_history_import_audits_v1" in sql and sql.lstrip().startswith("INSERT"):
            self.pending_audits.append(list(params))
            return Result()
        raise AssertionError(sql)


def test_dry_run_has_zero_writes_and_reports_insert(tmp_path):
    connection = FakeConnection()
    report = reconcile_mechanical_plan(_plan(tmp_path, _row()), connection, target_schema="target")
    assert report.dry_run and report.status == "dry_run"
    assert report.tables[0].inserted == 1
    assert connection.tables["raw_events"] == {}
    assert connection.audits == []
    assert not any(sql.startswith("INSERT") for sql, _ in connection.calls)


def test_apply_insert_then_second_apply_is_all_duplicate_and_one_audit(tmp_path):
    connection = FakeConnection()
    plan = _plan(tmp_path, _row())
    first = apply_mechanical_plan(plan, connection, target_schema="target")
    second = apply_mechanical_plan(plan, connection, target_schema="target")
    assert first.tables[0].inserted == 1
    assert second.tables[0].inserted == 0
    assert second.tables[0].exact_duplicate == 1
    assert len(connection.tables["raw_events"]) == 1
    assert len(connection.audits) == 1
    assert any("pg_advisory_xact_lock" in sql for sql, _ in connection.calls)
    inserts = [sql for sql, _ in connection.calls if 'INSERT INTO "target"."raw_events"' in sql]
    assert len(inserts) == 1
    assert "ON CONFLICT" not in inserts[0]


def test_json_utc_boolean_style_normalisation_yields_exact_duplicate(tmp_path):
    row = _row()
    existing = dict(row.values)
    existing["timestamp"] = datetime(2025, 1, 1, 0, tzinfo=timezone.utc)
    existing["created_at"] = datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    existing["metadata_json"] = {"b": True, "a": 1}
    existing["source_event_ids"] = ["source-1"]
    connection = FakeConnection(existing={"event-1": existing})
    report = reconcile_mechanical_plan(_plan(tmp_path, row), connection, target_schema="target")
    assert report.tables[0].exact_duplicate == 1


def test_conflict_reports_only_column_hashes_and_rolls_back_everything(tmp_path):
    existing = dict(_row().values)
    existing["content"] = "other secret body"
    existing["timestamp"] = datetime.fromisoformat(existing["timestamp"])
    existing["created_at"] = datetime.fromisoformat(existing["created_at"])
    existing["metadata_json"] = json.loads(existing["metadata_json"])
    existing["source_event_ids"] = json.loads(existing["source_event_ids"])
    connection = FakeConnection(existing={"event-1": existing})
    with pytest.raises(MechanicalHistoryApplyError, match="hard conflict") as caught:
        apply_mechanical_plan(_plan(tmp_path, _row()), connection, target_schema="target")
    assert connection.audits == []
    assert caught.value.report.rows[0].differences[0].column == "content"
    assert "other secret body" not in repr(caught.value.report)


def test_insert_exception_rolls_back_prior_rows_and_audit(tmp_path):
    rows = (_row(event_id="one"), replace(_row(event_id="two"), source_ordinal=1))
    connection = FakeConnection(fail_insert_at=2)
    with pytest.raises(MechanicalHistoryApplyError, match="transaction rolled back"):
        apply_mechanical_plan(_plan(tmp_path, *rows), connection, target_schema="target")
    assert connection.tables["raw_events"] == {}
    assert connection.audits == []


def test_schema_injection_and_current_schema_mismatch_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="simple PostgreSQL identifier"):
        reconcile_mechanical_plan(_plan(tmp_path, _row()), FakeConnection(), target_schema='x";drop')
    with pytest.raises(MechanicalHistoryApplyError, match="does not match"):
        reconcile_mechanical_plan(_plan(tmp_path, _row()), FakeConnection(current_schema="other"), target_schema="target")


def test_quarantine_is_rejected_before_database_access(tmp_path):
    quarantine = MechanicalQuarantineRecord("raw_events", "scope:one", 1, "bad", "secret", "b" * 64)
    connection = FakeConnection()
    with pytest.raises(MechanicalHistoryApplyError, match="quarantined"):
        apply_mechanical_plan(_plan(tmp_path, quarantine=(quarantine,)), connection, target_schema="target")
    assert connection.calls == []


def test_table_order_is_validated_before_database_access(tmp_path):
    boundary_values = (
        ("boundary_id", "b"), ("type", "pause"), ("scope", "all"),
        ("allow_reply", True), ("allow_proactive", False), ("starts_at", None),
        ("expires_at", None), ("revocable_by", "user"), ("source_event_id", None),
        ("revoked_at", None), ("note", None), ("subject", None),
        ("created_at", "2025-01-01T00:00:00+00:00"),
    )
    boundary = PlannedMechanicalRow("boundaries", "scope:one", 0, boundary_values, _sha(dict(boundary_values)))
    connection = FakeConnection()
    with pytest.raises(MechanicalHistoryApplyError, match="import order"):
        reconcile_mechanical_plan(_plan(tmp_path, boundary, _row()), connection, target_schema="target")
    assert connection.calls == []
