from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from companion_runtime.user_model_v2_import import plan_migration
from companion_runtime.user_model_v2_import_apply import apply_migration_plan

from test_user_model_v2_import import _fixture


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.exposure_keys: set[tuple[str, str]] = set()
        self.label_keys: set[tuple[str, str]] = set()
        self.quarantine_keys: set[tuple[str, ...]] = set()
        self.audits: set[tuple[str, ...]] = set()
        self.transactions = 0
        self.calls: list[tuple[str, tuple]] = []

    @contextmanager
    def transaction(self):
        self.transactions += 1
        snapshot = (
            set(self.exposure_keys), set(self.label_keys), set(self.quarantine_keys),
            set(self.audits),
        )
        try:
            yield
        except Exception:
            (self.exposure_keys, self.label_keys, self.quarantine_keys, self.audits) = snapshot
            raise

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if normalized.startswith("SELECT 1 FROM interaction_exposures_v2"):
            return Cursor((1,) if (params[0], params[1]) in self.exposure_keys else None)
        if normalized.startswith("SELECT 1 FROM interaction_target_labels_v2"):
            return Cursor((1,) if (params[0], params[1]) in self.label_keys else None)
        if normalized.startswith("INSERT INTO user_model_migration_quarantine_v2"):
            key = (params[1], params[2], params[3], params[4], params[7])
            if key in self.quarantine_keys:
                return Cursor()
            self.quarantine_keys.add(key)
            return Cursor((params[0],))
        if normalized.startswith("INSERT INTO user_model_migration_audits_v2"):
            self.audits.add((params[1], params[2], params[3]))
            return Cursor()
        return Cursor()


class LowRepository:
    def __init__(self, connection: Connection, fail_exposure: str | None = None):
        self.connection = connection
        self.fail_exposure = fail_exposure
        self.exposure_calls = []
        self.label_calls = []

    def insert_exposure(self, **kwargs):
        if kwargs["exposure_id"] == self.fail_exposure:
            raise RuntimeError("injected failure")
        self.exposure_calls.append(kwargs)
        self.connection.exposure_keys.add((kwargs["scope_key"], kwargs["idempotency_key"]))
        return kwargs["exposure_id"]

    def insert_label_revision_and_activate(self, **kwargs):
        self.label_calls.append(kwargs)
        self.connection.label_keys.add((kwargs["scope_key"], kwargs["idempotency_key"]))
        return True


class ServiceRepository:
    def __init__(self, connection=None, low=None):
        self.connection = connection or Connection()
        self.repository = low or LowRepository(self.connection)


def _plan(tmp_path: Path):
    source = tmp_path / "legacy.db"
    _fixture(source)
    import sqlite3
    with sqlite3.connect(source) as connection:
        connection.executemany(
            "INSERT INTO attempt_events VALUES (?,?,?,?,?,?)",
            [
                ("sent-1", "a1", "committed", "sent", None, "2025-01-01T00:00:00+00:00"),
                ("sent-2", "a2", "committed", "sent", None, "2025-01-01T00:01:00+00:00"),
            ],
        )
        connection.execute(
            "INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?)",
            ("reply-1", "user_message", "2025-01-01T00:02:00+00:00", "user", "chat",
             "yes", '{"attempt_id":"a1"}', "2025-01-01T00:02:00+00:00"),
        )
        connection.execute(
            "INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?)",
            ("reply-bad", "user_message", None, "user", "chat", "?", "{}", None),
        )
        connection.execute(
            "INSERT INTO interaction_observations VALUES (?,?,?,?,?,?)",
            ("obs", "2025-01-01T00:03:00+00:00", "a1", "{}", "{}", '{"positive":false}'),
        )
    return plan_migration({source: "person:one"})


def test_dry_run_reports_hash_rows_candidates_and_rejections_without_touching_repository(tmp_path):
    plan = _plan(tmp_path)
    service = ServiceRepository()
    report = apply_migration_plan(plan, service, scope_key="person:one", dry_run=True)
    assert report.exposure_candidates == 2
    assert report.reliable_reply_candidates == 1
    assert report.rejected_candidates == 1
    assert report.quarantine_candidates == 2  # legacy soft label plus unreliable reply
    assert report.exposures_written == report.labels_written == 0
    assert sum(item.source_rows for item in report.source_tables) == 5
    assert all(len(item.source_sha256) == 64 for item in report.source_tables)
    assert not service.connection.calls


def test_apply_imports_exposures_and_only_reliable_R_label_and_quarantines_rest(tmp_path):
    plan = _plan(tmp_path)
    service = ServiceRepository()
    report = apply_migration_plan(plan, service, scope_key="person:one", dry_run=False, batch_size=1)
    assert (report.exposures_written, report.labels_written, report.quarantine_written) == (2, 1, 2)
    assert len(service.repository.label_calls) == 1
    label = service.repository.label_calls[0]
    assert label["target_name"] == "reply"
    assert label["target_value"]["migration_evidence_class"] == "reliable_R_candidate"
    assert label["evidence"]["reliability"] == "R"
    assert all("active_labels" not in sql for sql, _ in service.connection.calls
               if "quarantine" in sql)


def test_replay_is_idempotent_and_reports_duplicates_without_count_growth(tmp_path):
    plan = _plan(tmp_path)
    service = ServiceRepository()
    first = apply_migration_plan(plan, service, scope_key="person:one", dry_run=False)
    second = apply_migration_plan(plan, service, scope_key="person:one", dry_run=False)
    assert first.exposures_written == 2 and first.labels_written == 1
    assert second.exposures_written == second.labels_written == second.quarantine_written == 0
    assert (second.exposures_duplicate, second.labels_duplicate, second.quarantine_duplicate) == (2, 1, 2)
    assert len(service.connection.exposure_keys) == 2
    assert len(service.connection.label_keys) == 1


def test_scope_is_required_and_must_be_present_in_plan(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(ValueError, match="scope_key"):
        apply_migration_plan(plan, ServiceRepository(), scope_key="")
    with pytest.raises(ValueError, match="not present"):
        apply_migration_plan(plan, ServiceRepository(), scope_key="person:other")


def test_failed_batch_rolls_back_and_does_not_continue_to_labels(tmp_path):
    plan = _plan(tmp_path)
    connection = Connection()
    low = LowRepository(connection, fail_exposure=plan.exposures[1].exposure_id)
    service = ServiceRepository(connection, low)
    with pytest.raises(RuntimeError, match="injected"):
        apply_migration_plan(plan, service, scope_key="person:one", dry_run=False, batch_size=2)
    assert connection.exposure_keys == set()
    assert connection.label_keys == set()
    assert low.label_calls == []


def test_plan_dry_run_flag_does_not_override_explicit_apply_mode(tmp_path):
    plan = replace(_plan(tmp_path), dry_run=True)
    service = ServiceRepository()
    report = apply_migration_plan(plan, service, scope_key="person:one", dry_run=False)
    assert report.exposures_written == 2
