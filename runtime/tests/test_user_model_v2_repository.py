"""SQL-contract tests for the pure PostgreSQL user-model v2 repository."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from companion_runtime.user_model_v2_repository import (
    UserModelV2Repository,
    canonical_json,
    stable_idempotency_key,
)

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
SCOPE = "user:42/channel:direct"


class FakeCursor:
    def __init__(self, row=None, rowcount=1):
        self.row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.transactions = 0

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))
        row = self.rows.pop(0) if self.rows else None
        return FakeCursor(row)

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


def test_canonical_json_and_derived_idempotency_are_stable_and_scope_bound():
    assert canonical_json({"z": 1, "é": [True], "a": 2}) == '{"a":2,"z":1,"é":[true]}'
    left = stable_idempotency_key("exposure", SCOPE, {"b": 2, "a": 1})
    right = stable_idempotency_key("exposure", SCOPE, {"a": 1, "b": 2})
    assert left == right
    assert left != stable_idempotency_key("exposure", "other", {"a": 1, "b": 2})
    with pytest.raises(ValueError, match="scope_key"):
        stable_idempotency_key("exposure", " ", {})


def test_insert_exposure_uses_pg_placeholders_bound_scope_and_stable_json():
    conn = FakeConnection(rows=[{"exposure_id": "exp-1"}])
    result = UserModelV2Repository(conn).insert_exposure(
        scope_key=SCOPE, exposure_id="exp-1", occurred_at=NOW,
        action={"z": 2, "a": 1}, context={"channel": "direct"}, propensity=0.5,
        idempotency_key="delivery:123",
    )
    sql, params = conn.calls[0]
    assert result == "exp-1"
    assert "?" not in sql and "%s" in sql
    assert "ON CONFLICT (scope_key, idempotency_key)" in sql
    assert params[1:3] == (SCOPE, "delivery:123")
    assert params[4] == '{"a":1,"z":2}'


@pytest.mark.parametrize("method, kwargs, returned", [
    ("insert_label", dict(target_label_id="label-1", exposure_id="exp-1", labelled_at=NOW,
                          target_name="reply", target_value={"status": "positive"},
                          evidence={"event": "e1"}, label_version=1), "label-1"),
    ("insert_parameter_snapshot", dict(parameter_snapshot_id="params-1", effective_at=NOW,
                          parameters={"b": 2, "a": 1}, provenance={}, parameter_version=1), "params-1"),
    ("insert_prediction", dict(prediction_snapshot_id="pred-1", parameter_snapshot_id="params-1",
                          predicted_at=NOW, features={}, predictions={"reply": .5}, confidence=.8,
                          prediction_version=1), "pred-1"),
    ("insert_expectation", dict(expectation_id="expect-1", prediction_snapshot_id="pred-1",
                          expectation={"target": "reply"}, expected_value=.5,
                          expectation_version=1), "expect-1"),
])
def test_all_insert_kinds_bind_scope_and_are_idempotent(method, kwargs, returned):
    conn = FakeConnection(rows=[(returned,)])
    repository = UserModelV2Repository(conn)
    assert getattr(repository, method)(scope_key=SCOPE, **kwargs) == returned
    sql, params = conn.calls[0]
    assert "?" not in sql
    assert "ON CONFLICT (scope_key, idempotency_key)" in sql
    assert SCOPE in params
    assert any(isinstance(value, str) and value.startswith("v2:") for value in params)


def test_label_revision_and_active_pointer_are_one_transaction_and_cas_scoped():
    conn = FakeConnection(rows=[None, {"target_label_id": "old", "pointer_version": 2}, ("new",), None])
    repository = UserModelV2Repository(conn)
    assert repository.insert_label_revision_and_activate(
        scope_key=SCOPE, target_label_id="new", exposure_id="exp-1",
        labelled_at=NOW, target_name="reply", target_value=True,
        evidence={"event": "reply-1"}, expected_pointer_version=2,
    )
    assert conn.transactions == 1
    assert len(conn.calls) == 4
    assert "pg_advisory_xact_lock" in conn.calls[0][0]
    select_sql, select_params = conn.calls[1]
    pointer_sql, pointer_params = conn.calls[3]
    assert "WHERE scope_key = %s AND exposure_id = %s AND target_name = %s" in select_sql
    assert "FOR UPDATE" in select_sql
    assert select_params == (SCOPE, "exp-1", "reply")
    assert "WHERE user_model_active_labels_v2.pointer_version = %s" in pointer_sql
    assert pointer_params[0] == SCOPE
    assert pointer_params[-1] == 2


def test_label_revision_cas_mismatch_writes_nothing():
    conn = FakeConnection(rows=[None, {"target_label_id": "old", "pointer_version": 3}])
    result = UserModelV2Repository(conn).insert_label_revision_and_activate(
        scope_key=SCOPE, target_label_id="new", exposure_id="exp-1",
        labelled_at=NOW, target_name="reply", target_value=True,
        evidence={}, expected_pointer_version=2,
    )
    assert result is False
    assert len(conn.calls) == 2


def test_parameter_pointer_cas_is_locked_transactional_and_scope_safe():
    conn = FakeConnection(rows=[None, {"active_parameters_id": "active-1", "parameter_snapshot_id": "old"}, None, ("active-2",)])
    result = UserModelV2Repository(conn).compare_and_swap_active_parameters(
        scope_key=SCOPE, expected_snapshot_id="old", new_snapshot_id="new",
        activation_version=4, activation_context={"reason": "retrain"},
    )
    assert result is True and conn.transactions == 1
    assert "pg_advisory_xact_lock" in conn.calls[0][0]
    assert "WHERE scope_key = %s AND deactivated_at IS NULL FOR UPDATE" in conn.calls[1][0]
    assert conn.calls[1][1] == (SCOPE,)
    assert "WHERE scope_key = %s AND active_parameters_id = %s" in conn.calls[2][0]
    assert conn.calls[2][1][0] == SCOPE
    assert conn.calls[3][1][0] == SCOPE


def test_every_query_path_requires_scope_and_cannot_omit_scope_filter():
    conn = FakeConnection(rows=[None, None])
    repository = UserModelV2Repository(conn)
    repository.get_active_parameters(scope_key=SCOPE)
    repository.get_active_label(scope_key=SCOPE, exposure_id="exp-1", target_name="reply")
    for sql, params in conn.calls:
        assert "scope_key = %s" in sql
        assert SCOPE in params
        assert SCOPE not in sql  # scope is data, never interpolated into SQL
    with pytest.raises(TypeError):
        repository.get_active_parameters()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="scope_key"):
        repository.get_active_label(scope_key="", exposure_id="exp-1", target_name="reply")
