"""Public v2 evidence is scoped, read-only, and safe to expose to black-box tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from companion_runtime.api import create_app
from companion_runtime.api_v2_observability import create_v2_observability_router
from companion_runtime.runtime import Runtime

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class SyntheticRuntimeRepository:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.writes = 0

    def read_blackbox_evidence(self, *, scope_key: str) -> dict[str, Any]:
        self.reads.append(scope_key)
        return {
            "count": 2,
            "nested": {"api_token": "do-not-leak"},
            "dsn": "postgresql://runtime:topsecret@db/runtime",
        }

    def save_decision_audit(self, **_kwargs: Any) -> None:
        self.writes += 1


class SyntheticAuditRepository:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.writes = 0

    def list_decision_audits(self, *, scope_key: str) -> list[dict[str, Any]]:
        self.reads.append(scope_key)
        return [
            {
                "decision_id": "decision:1",
                "authorization": "Bearer abc.def.ghi",
                "details": {"password": "never-return-this"},
            }
        ]


@dataclass
class SyntheticHealth:
    versions: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "versions": self.versions,
            "storage": {"dialect": "postgres", "dsn": "postgresql://u:p@db/name"},
            "jev": {"enabled": True, "provider": "must-not-be-public", "api_key": "bad"},
        }


def make_client(*, enable_run: bool = False, runner=None):
    runtime_repo = SyntheticRuntimeRepository()
    audit_repo = SyntheticAuditRepository()
    app = FastAPI()
    app.include_router(
        create_v2_observability_router(
            scope_key="tenant:allowed",
            runtime_repository=runtime_repo,
            audit_repository=audit_repo,
            health=SyntheticHealth({"runtime": "test", "secret_version": "hidden"}),
            enable_decision_run=enable_run,
            decision_simulation_runner=runner,
        )
    )
    return TestClient(app), runtime_repo, audit_repo


def test_evidence_requires_exact_configured_scope_and_does_not_probe_repositories() -> None:
    client, runtime_repo, audit_repo = make_client()

    assert client.get("/v2/blackbox/evidence").status_code == 422
    assert client.get("/v2/blackbox/evidence", params={"scope": "tenant:other"}).status_code == 403
    assert runtime_repo.reads == []
    assert audit_repo.reads == []

    response = client.get("/v2/blackbox/evidence", params={"scope": "tenant:allowed"})
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "tenant:allowed"
    assert body["read_only"] is True
    assert runtime_repo.reads == ["tenant:allowed"]
    assert audit_repo.reads == ["tenant:allowed"]


def test_public_payload_recursively_redacts_keys_and_inline_credentials() -> None:
    client, _, _ = make_client()
    health = client.get("/v2/health").json()
    evidence = client.get(
        "/v2/blackbox/evidence", params={"scope": "tenant:allowed"}
    ).json()

    assert health["status"] == "ok"
    assert health["jev"] == {"status": "disabled"}
    combined = repr({"health": health, "evidence": evidence})
    assert "topsecret" not in combined
    assert "abc.def.ghi" not in combined
    assert "never-return-this" not in combined
    assert "must-not-be-public" not in combined
    assert "do-not-leak" not in combined
    assert "***redacted***" in combined


def test_production_router_is_read_only_and_has_no_decision_run_route() -> None:
    client, runtime_repo, audit_repo = make_client()

    response = client.post(
        "/v2/decisions/run", json={"scope": "tenant:allowed", "simulate": True}
    )
    assert response.status_code == 404
    assert runtime_repo.writes == 0
    assert audit_repo.writes == 0


def test_simulation_run_requires_both_explicit_enable_and_simulate_flag() -> None:
    calls: list[dict[str, Any]] = []

    def runner(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"acted": False, "client_secret": "runner-secret"}

    client, _, _ = make_client(enable_run=True, runner=runner)
    assert client.post(
        "/v2/decisions/run", json={"scope": "tenant:allowed"}
    ).status_code == 422
    assert client.post(
        "/v2/decisions/run", json={"scope": "tenant:other", "simulate": True}
    ).status_code == 403
    response = client.post(
        "/v2/decisions/run", json={"scope": "tenant:allowed", "simulate": True}
    )
    assert response.status_code == 200
    assert response.json() == {
        "scope": "tenant:allowed",
        "simulated": True,
        "result": {"acted": False, "client_secret": "***redacted***"},
    }
    assert len(calls) == 1


def test_create_app_mounts_privacy_router_only_for_complete_authorized_composition(
    runtime: Runtime,
) -> None:
    class Repository:
        scope_key = "tenant:allowed"

        def __init__(self):
            self.value = None

        def status(self, *, request_id):
            return self.value

    class Coordinator:
        scope_key = "tenant:allowed"

        def __init__(self, repository):
            self.repository = repository

        def request(self, value):
            self.repository.value = {"request_id": value.request_id, "status": "pending"}
            return True

        def run(self, *, request_id):
            return {"request_id": request_id, "completed": True}

    repository = Repository()
    coordinator = Coordinator(repository)
    composition = SimpleNamespace(
        coordinator=SimpleNamespace(
            scope_key="tenant:allowed", repository=SyntheticRuntimeRepository()
        ),
        audit_repository=SyntheticAuditRepository(),
        health=SyntheticHealth({"runtime": "test"}),
        privacy_deletion_repository=repository,
        privacy_deletion_coordinator=coordinator,
        privacy_deletion_authorize=lambda scope, token: (
            scope == "tenant:allowed" and token == "Bearer allowed"
        ),
    )
    client = TestClient(create_app(runtime, runtime.config, v2_composition=composition))
    payload = {
        "scope": "tenant:allowed", "request_id": "delete:1",
        "selector_kind": "source", "selector": {"source_id": "event:1"},
        "strategy": "tombstone",
    }

    assert client.post("/v1/privacy/deletions", json=payload).status_code == 403
    accepted = client.post(
        "/v1/privacy/deletions", json=payload,
        headers={"Authorization": "Bearer allowed"},
    )
    assert accepted.status_code == 202


def test_create_app_rejects_partial_privacy_composition(runtime: Runtime) -> None:
    composition = SimpleNamespace(
        coordinator=SimpleNamespace(
            scope_key="tenant:allowed", repository=SyntheticRuntimeRepository()
        ),
        audit_repository=SyntheticAuditRepository(),
        health=SyntheticHealth({"runtime": "test"}),
        privacy_deletion_repository=SimpleNamespace(scope_key="tenant:allowed"),
    )
    with pytest.raises(ValueError, match="must be complete"):
        create_app(runtime, runtime.config, v2_composition=composition)


def test_create_app_mounts_v2_only_when_composition_is_injected(runtime: Runtime) -> None:
    runtime_repo = SyntheticRuntimeRepository()
    audit_repo = SyntheticAuditRepository()
    composition = SimpleNamespace(
        coordinator=SimpleNamespace(scope_key="tenant:allowed", repository=runtime_repo),
        audit_repository=audit_repo,
        health=SyntheticHealth({"runtime": "test"}),
    )

    legacy_only = TestClient(create_app(runtime, runtime.config))
    assert legacy_only.get("/v2/health").status_code == 404
    assert legacy_only.post("/v1/privacy/deletions", json={}).status_code == 404

    mounted = TestClient(create_app(runtime, runtime.config, v2_composition=composition))
    assert mounted.get("/v2/health").status_code == 200
    assert mounted.post("/v1/privacy/deletions", json={}).status_code == 404
    evidence = mounted.get(
        "/v2/blackbox/evidence", params={"scope": "tenant:allowed"}
    )
    assert evidence.status_code == 200
