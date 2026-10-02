from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion_runtime.privacy_deletion_api import create_privacy_deletion_router
from companion_runtime.privacy_deletion_repository import (
    ClaimedWork,
    DeletionRequest,
    DeletionStrategy,
    PrivacyDeletionConflict,
    PrivacyDeletionRepository,
    WORK_KINDS,
)
from companion_runtime.privacy_deletion_schema import PRIVACY_DELETION_SCHEMA_V21_STATEMENTS
from companion_runtime.privacy_deletion_service import PrivacyDeletionCoordinator
from companion_runtime.user_model_v2_schema import MIGRATIONS, USER_MODEL_SCHEMA_VERSION

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, row=None, rows=None, rowcount=1):
        self.row = row
        self.rows = rows or ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        response = self.responses.pop(0) if self.responses else None
        return response if isinstance(response, Cursor) else Cursor(row=response)

    @contextmanager
    def transaction(self):
        yield


def request(scope="scope:a", request_id="delete:1"):
    return DeletionRequest(
        scope_key=scope, request_id=request_id, requested_by="user",
        selector_kind="source", selector={"source_kind": "event", "source_id": "event:secret"},
        strategy=DeletionStrategy.TOMBSTONE, requested_at=NOW,
    )


def test_v22_schema_is_versioned_durable_and_never_cascades_or_deletes_accounting_rows():
    assert USER_MODEL_SCHEMA_VERSION >= 22
    assert (21, PRIVACY_DELETION_SCHEMA_V21_STATEMENTS) in MIGRATIONS
    ddl = "\n".join(PRIVACY_DELETION_SCHEMA_V21_STATEMENTS).lower()
    assert "privacy_deletion_requests_v1" in ddl
    assert "privacy_deletion_work_v1" in ddl and "lease_expires_at" in ddl
    assert "privacy_source_tombstones_v1" in ddl
    assert "privacy_reference_invalidations_v1" in ddl
    assert "on delete cascade" not in ddl
    assert "selector_json" in ddl and "selector_digest" in ddl


def test_request_is_scope_bound_idempotent_and_conflicting_replay_fails():
    repo = PrivacyDeletionRepository(Connection([None]), scope_key="scope:a")
    assert repo.create_request(request()) is True
    inserts = [call for call in repo.connection.calls if "INSERT INTO privacy_deletion_work_v1" in call[0]]
    assert len(inserts) == len(WORK_KINDS)
    assert all(call[1][0] == "scope:a" for call in repo.connection.calls)

    existing = {"selector_digest": request().selector_digest, "strategy": "tombstone", "requested_by": "user"}
    assert PrivacyDeletionRepository(Connection([existing]), scope_key="scope:a").create_request(request()) is False
    conflict = {**existing, "strategy": "crypto_erasure"}
    with pytest.raises(PrivacyDeletionConflict):
        PrivacyDeletionRepository(Connection([conflict]), scope_key="scope:a").create_request(request())
    with pytest.raises(ValueError, match="another scope"):
        PrivacyDeletionRepository(Connection(), scope_key="scope:b").create_request(request())


def test_expired_lease_is_reclaimed_for_crash_recovery_and_scope_is_in_every_query():
    claimed = {"work_kind": "source_protection", "selector_kind": "source",
               "selector_json": {"source_id": "event:secret"}, "strategy": "tombstone"}
    conn = Connection([claimed])
    work = PrivacyDeletionRepository(conn, scope_key="scope:a").claim_next(
        request_id="delete:1", worker_id="worker:new", now=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert work and work.work_kind == "source_protection"
    select = conn.calls[0]
    assert "lease_expires_at <= %s" in select[0]
    assert select[1][:2] == ("scope:a", "delete:1")
    assert all("scope_key" in sql for sql, _ in conn.calls)


def test_coordinator_invalidates_dependencies_stops_unsent_and_keeps_sent_audit():
    work = ClaimedWork(scope_key="scope:a", request_id="delete:1",
                       work_kind="goal_candidate_invalidation", selector_kind="source",
                       selector={"source_id": "event:secret"}, strategy=DeletionStrategy.TOMBSTONE)
    conn = Connection([Cursor(rowcount=2), Cursor(rowcount=1)])
    coordinator = PrivacyDeletionCoordinator(
        PrivacyDeletionRepository(conn, scope_key="scope:a"), worker_id="worker", clock=lambda: NOW
    )
    counts = coordinator._execute(work, now=NOW)
    assert counts == {"candidates": 2, "active_goals": 1}
    sql = " ".join(statement for statement, _ in conn.calls)
    assert "candidate_intents SET status='retired'" in sql
    assert "unfinished_matters SET status='cancelled'" in sql

    live = ClaimedWork(scope_key="scope:a", request_id="delete:1", work_kind="live_outbox_stop",
                       selector_kind="source", selector={"source_id": "event:secret"},
                       strategy=DeletionStrategy.TOMBSTONE)
    conn = Connection([Cursor(rowcount=3), Cursor(rowcount=2)])
    coordinator.connection = conn
    assert coordinator._execute(live, now=NOW) == {"outbox_cancelled": 3, "live_commits_stopped": 2}
    assert "status IN ('pending','leased')" in conn.calls[0][0]
    assert "terminal_ack_kind IS NULL" in conn.calls[1][0]
    assert not any("DELETE FROM" in sql for sql, _ in conn.calls)


def test_memory_interpretation_and_outcome_use_invalidation_overlay_not_fk_breaking_delete():
    coordinator = PrivacyDeletionCoordinator(
        PrivacyDeletionRepository(Connection([Cursor(rowcount=1), Cursor(rowcount=2)]), scope_key="scope:a"),
        worker_id="worker", clock=lambda: NOW,
    )
    memory = ClaimedWork(scope_key="scope:a", request_id="delete:1",
                         work_kind="memory_interpretation_invalidation", selector_kind="source",
                         selector={"source_id": "event:secret"}, strategy=DeletionStrategy.PAYLOAD_REDACTION)
    assert coordinator._execute(memory, now=NOW) == {"memories": 1, "interpretations": 2}
    assert "[privacy-deleted]" in coordinator.connection.calls[0][0]
    assert "privacy_reference_invalidations_v1" in coordinator.connection.calls[1][0]

    conn = Connection([Cursor(rowcount=4)])
    coordinator.connection = conn
    outcome = ClaimedWork(scope_key="scope:a", request_id="delete:1",
                          work_kind="outcome_evidence_minimization", selector_kind="source",
                          selector={"source_id": "event:secret"}, strategy=DeletionStrategy.TOMBSTONE)
    assert coordinator._execute(outcome, now=NOW) == {"outcome_tokens_minimized": 4}
    assert "INSERT INTO privacy_reference_invalidations_v1" in conn.calls[0][0]
    assert "UPDATE langchao_outcome_revisions" not in conn.calls[0][0]
    assert "DELETE" not in conn.calls[0][0]


class ApiRepository:
    scope_key = "scope:a"

    def __init__(self):
        self.value = None

    def status(self, *, request_id):
        return self.value


class ApiCoordinator:
    scope_key = "scope:a"

    def __init__(self, repository):
        self.repository = repository
        self.requests = []

    def request(self, value):
        self.requests.append(value)
        self.repository.value = {"request_id": value.request_id, "status": "pending"}
        return True

    def run(self, *, request_id):
        return {"request_id": request_id, "completed": True}


def test_api_is_fail_closed_cross_scope_safe_and_does_not_run_without_explicit_call():
    repository = ApiRepository()
    coordinator = ApiCoordinator(repository)
    app = FastAPI()
    app.include_router(create_privacy_deletion_router(
        scope_key="scope:a", repository=repository, coordinator=coordinator,
        authorize=lambda scope, token: scope == "scope:a" and token == "Bearer allowed",
    ))
    client = TestClient(app)
    payload = {"scope": "scope:a", "request_id": "delete:1", "selector_kind": "source",
               "selector": {"source_id": "event:secret"}, "strategy": "tombstone"}
    assert client.post("/v1/privacy/deletions", json=payload).status_code == 403
    assert not coordinator.requests
    denied = client.post("/v1/privacy/deletions", json={**payload, "scope": "scope:b"},
                         headers={"Authorization": "Bearer allowed"})
    assert denied.status_code == 403
    accepted = client.post("/v1/privacy/deletions", json=payload,
                           headers={"Authorization": "Bearer allowed"})
    assert accepted.status_code == 202 and len(coordinator.requests) == 1
    # Request creation only persists the plan; production mutation needs explicit /run.
    assert accepted.json()["status"] == "pending"
    assert client.post("/v1/privacy/deletions/delete:1/run",
                       headers={"Authorization": "Bearer allowed"}).json()["completed"] is True
