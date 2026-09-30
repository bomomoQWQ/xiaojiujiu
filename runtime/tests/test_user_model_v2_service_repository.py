"""Contract tests for the PostgreSQL service/domain adapter."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.user_model_v2_service import PreparedExposureV2
from companion_runtime.user_model_v2_service_repository import (
    PostgresUserModelV2ServiceRepository,
)
from companion_runtime.user_model_v2_types import LabelStatus, Target

from test_user_model_v2_service import HORIZONS, SCOPE, FakeRepository, prepare

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, *, row=None, rows=()):
        self.row = row
        self.rows = tuple(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class ScriptedConnection:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
        self.transactions = 0

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return self.results.pop(0) if self.results else Cursor()

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


class LowLevelRepository:
    def __init__(self, *, active_row=None):
        self.active_row = active_row
        self.exposures = []
        self.revisions = []

    def insert_exposure(self, **kwargs):
        self.exposures.append(kwargs)
        return kwargs["exposure_id"]

    def insert_label_revision_and_activate(self, **kwargs):
        self.revisions.append(kwargs)
        return True

    def get_active_label(self, **kwargs):
        self.active_query = kwargs
        return self.active_row


def _prepared() -> PreparedExposureV2:
    return prepare(FakeRepository())


def test_put_prepared_exposure_serializes_snapshot_and_four_pending_labels_once() -> None:
    prepared = _prepared()
    connection = ScriptedConnection()
    low = LowLevelRepository()

    class Adapter(PostgresUserModelV2ServiceRepository):
        reads = iter((None, prepared))

        def get_prepared_exposure(self, **kwargs):
            return next(self.reads)

    adapter = Adapter(connection, low)  # type: ignore[arg-type]
    assert adapter.put_prepared_exposure(prepared=prepared, idempotency_key="delivery-1") == prepared
    assert connection.transactions == 1
    assert len(low.exposures) == 1
    assert len(low.revisions) == 4
    assert tuple(call["target_name"] for call in low.revisions) == tuple(t.value for t in Target)
    assert all(call["target_value"]["status"] == "pending" for call in low.revisions)
    update_sql, update_params = connection.calls[1]
    assert "WHERE scope_key = %s AND exposure_id = %s AND idempotency_key = %s" in update_sql
    assert update_params[-3:] == (SCOPE, "exp-1", "delivery-1")
    assert '"feature_fingerprint"' in update_params[1]


def test_get_active_label_converts_domain_payload_and_is_scope_filtered() -> None:
    label = _prepared().labels[0]
    row = {**label.to_dict(), "target_label_id": label.label_id, "target_value": label.to_dict(), "pointer_version": 1}
    low = LowLevelRepository(active_row=row)
    adapter = PostgresUserModelV2ServiceRepository(ScriptedConnection(), low)  # type: ignore[arg-type]
    restored = adapter.get_active_label(
        scope_key=SCOPE, exposure_id=label.exposure_id, target=Target.REPLY
    )
    assert restored == (label, 1)
    assert low.active_query == {
        "scope_key": SCOPE,
        "exposure_id": label.exposure_id,
        "target_name": "reply",
    }


def test_compare_and_swap_delegates_to_existing_repository_with_full_label_payload() -> None:
    pending = _prepared().labels[0]
    observed = replace(
        pending,
        status=LabelStatus.OBSERVED_POSITIVE,
        value=True,
        observed_at=NOW + timedelta(seconds=10),
        updated_at=NOW + timedelta(seconds=10),
    )
    low = LowLevelRepository()
    adapter = PostgresUserModelV2ServiceRepository(ScriptedConnection(), low)  # type: ignore[arg-type]
    assert adapter.compare_and_swap_active_label(
        label=observed,
        revision=2,
        expected_revision=1,
        idempotency_key="settle-2",
    )
    call = low.revisions[0]
    assert call["expected_pointer_version"] == 1
    assert call["target_value"]["status"] == "observed_positive"
    assert call["idempotency_key"] == "settle-2"
    with pytest.raises(ValueError, match="exactly"):
        adapter.compare_and_swap_active_label(
            label=observed, revision=3, expected_revision=1, idempotency_key="bad"
        )


def test_list_training_records_joins_active_pointer_and_restores_feature_snapshot() -> None:
    prepared = _prepared()
    pending = prepared.labels[0]
    observed = replace(
        pending,
        status=LabelStatus.OBSERVED_POSITIVE,
        value=True,
        observed_at=NOW + timedelta(seconds=10),
        updated_at=NOW + timedelta(seconds=10),
    )
    row = {
        "target_value": observed.to_dict(),
        "exposure_id": prepared.exposure.exposure_id,
        "action": prepared.features.to_dict()["action_json"],
        "context": prepared.features.to_dict()["context_json"],
        "feature_snapshot": prepared.features.to_dict(),
        "exposure_weight": 0.75,
    }
    connection = ScriptedConnection([Cursor(rows=(row,))])
    adapter = PostgresUserModelV2ServiceRepository(connection, LowLevelRepository())  # type: ignore[arg-type]
    records = tuple(adapter.list_active_training_records(scope_key=SCOPE, target=Target.REPLY))
    assert len(records) == 1
    assert records[0].label == observed
    assert records[0].features == prepared.features
    assert records[0].exposure_weight == 0.75
    sql, params = connection.calls[0]
    assert "a.scope_key = %s AND a.target_name = %s" in sql
    assert "e.scope_key = a.scope_key" in sql and "l.scope_key = a.scope_key" in sql
    assert params == (SCOPE, "reply")
