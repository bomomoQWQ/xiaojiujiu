"""Safety contract for optional runtime-v2 hooks on the unchanged v1 wire."""

from __future__ import annotations

import json
from typing import Any

import pytest

from companion_runtime.api import create_app
from companion_runtime.runtime import Runtime
from companion_runtime.typing import CandidateIntent, new_id
from companion_runtime.utility import utcnow

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


class RecordingCoordinator:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.events: list[tuple[Any, Any]] = []
        self.renders: list[dict[str, Any]] = []
        self.acks: list[Any] = []

    def after_legacy_user_event(self, *, event, legacy_outcome):
        assert legacy_outcome.event.event_id == event["event_id"]
        self.order.append("v2_user")
        self.events.append((event, legacy_outcome))

    def after_legacy_rendered(self, **kwargs):
        self.order.append("v2_render")
        self.renders.append(kwargs)

    def after_legacy_send_ack(self, ack, *, confirmed=True):
        assert confirmed is True
        self.order.append("v2_send")
        self.acks.append(ack)


@pytest.fixture()
def wired(runtime: Runtime):
    order: list[str] = []
    coordinator = RecordingCoordinator(order)
    runtime.v2_coordinator = coordinator
    app = create_app(runtime, runtime.config)
    with TestClient(app) as client:
        yield client, runtime, coordinator, order


def event(event_id: str = "evt-v2") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": "user_message",
        "session": "webchat:user-1",
        "text": "面试结束了",
        "occurred_at": utcnow().isoformat(),
    }


def post_event(client: TestClient, record: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/v1/events", json={"protocol_version": "1", "events": [record]})
    assert response.status_code == 200
    return response.json()


def commit_v2_attempt(runtime: Runtime) -> tuple[str, str]:
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="问面试结果",
        goal="表达关心",
        sources=[],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        attempt_id, render_id = runtime._commit_attempt(
            conn, chosen=candidate, state=runtime.projections.runtime.ensure(), now=utcnow()
        )
        row = runtime.projections.outbox.get(render_id)
        payload = dict(row.payload)
        payload.update({"decision_id": "decision:v2", "action": {"type": "follow_up"}})
        conn.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(payload, ensure_ascii=False), render_id),
        )
    return attempt_id, render_id


def lease(client: TestClient, kind: str) -> dict[str, Any]:
    body = client.post(
        "/v1/outbox/lease",
        json={"adapter_id": "wire-test", "capabilities": [kind], "max_actions": 1},
    ).json()
    assert body["count"] == 1
    return body["items"][0]


def report(client: TestClient, action: dict[str, Any], *, status="ok", result=None) -> dict[str, Any]:
    return client.post(
        f"/v1/outbox/{action['action_id']}/result",
        json={
            "adapter_id": "wire-test",
            "action_id": action["action_id"],
            "lease_id": action["lease_id"],
            "action_type": action["action_type"],
            "status": status,
            "attempt_id": action["attempt_id"],
            "result": result or {},
        },
    ).json()


def test_user_legacy_runs_before_v2_without_double_ingest_and_wire_is_unchanged(wired):
    client, runtime, coordinator, order = wired
    original = runtime.process_user_message

    def legacy(**kwargs):
        order.append("legacy_user")
        return original(**kwargs)

    runtime.process_user_message = legacy
    body = post_event(client, event())
    assert order == ["legacy_user", "v2_user"]
    assert runtime.events.count("user_message") == 1
    assert body["accepted"] == 1 and body["duplicates"] == 0
    assert set(body) == {"accepted", "duplicates", "rejected", "outcomes", "protocol_version", "runtime_version"}
    assert coordinator.events[0][0]["event_id"] == "evt-v2"


def test_render_then_successful_send_hook_and_duplicate_ack_are_idempotent(wired):
    client, runtime, coordinator, order = wired
    commit_v2_attempt(runtime)
    render = lease(client, "render")
    render_reply = report(client, render, result={"text": "想问问面试结果。"})
    assert render_reply["ok"] is True
    assert order == ["v2_render"]

    send = lease(client, "send")
    first = report(client, send, result={"sent": True})
    replay = report(client, send, result={"sent": True})
    assert first["ok"] is True and first["delivered"] is True
    assert replay["ok"] is True and replay["duplicate"] is True
    assert order == ["v2_render", "v2_send"]
    assert len(coordinator.acks) == 1
    assert coordinator.acks[0].decision_id == "decision:v2"


def test_failed_send_creates_no_v2_ack_or_exposure(wired):
    client, runtime, coordinator, order = wired
    commit_v2_attempt(runtime)
    report(client, lease(client, "render"), result={"text": "想问问面试结果。"})
    failed = report(client, lease(client, "send"), status="failed", result={})
    assert failed["ok"] is True and failed["delivered"] is False
    assert order == ["v2_render"]
    assert coordinator.acks == []
