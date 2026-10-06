"""Safety contract for optional runtime-v2 hooks on the unchanged v1 wire."""

from __future__ import annotations

import json
from types import SimpleNamespace
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


def test_failed_send_terminally_notifies_v2_without_claiming_exposure(wired):
    client, runtime, coordinator, order = wired
    commit_v2_attempt(runtime)
    report(client, lease(client, "render"), result={"text": "想问问面试结果。"})
    failed = report(client, lease(client, "send"), status="failed", result={})
    assert failed["ok"] is True and failed["delivered"] is False
    assert order == ["v2_render", "v2_send"]
    assert len(coordinator.acks) == 1
    assert coordinator.acks[0].sent is False


class StrictV2Coordinator(RecordingCoordinator):
    """Stands in for the real coordinator, which only knows its own decisions."""

    def after_legacy_rendered(self, *, decision_id, **kwargs):
        if not str(decision_id).startswith("decision:v2"):
            raise KeyError(f"unknown decision_id: {decision_id}")
        super().after_legacy_rendered(decision_id=decision_id, **kwargs)


class LangchaoRunnerStub:
    """Records the render hook the langchao engine owns."""

    def __init__(self) -> None:
        self.renders: list[dict[str, Any]] = []

    def after_legacy_rendered(self, *, decision_id: str, outbox_id: str, now) -> None:
        self.renders.append({"decision_id": decision_id, "outbox_id": outbox_id})


class AbandonedRoundRunner:
    """A langchao runner that reports an unsettled commit and records a settlement."""

    def __init__(self, *, terminal_ack_kind: str | None = None) -> None:
        self.terminal_ack_kind = terminal_ack_kind
        self.acks: list[Any] = []
        self.repository_asked: list[str] = []
        outer = self

        class _Repository:
            def get(self, round_id: str):
                outer.repository_asked.append(round_id)
                return SimpleNamespace(terminal_ack_kind=outer.terminal_ack_kind)

        self.repository = _Repository()

    def after_legacy_rendered(self, **_kwargs) -> None:
        return None

    def after_legacy_send_ack(self, ack, *, confirmed: bool = True):
        self.acks.append((ack, confirmed))
        return ()


def test_a_render_that_failed_for_good_settles_the_round_it_cannot_deliver(runtime):
    """Failing the render row is not enough: the commit waits for an ack that never comes.

    A round whose render failed terminally left its durable commit unsettled forever -- the
    user received nothing while the books said ``pending``. Sixteen had accumulated and the
    only way out was settling them by hand. The report path must close it as a non-delivery.
    """
    runtime.v2_coordinator = StrictV2Coordinator([])
    runner = AbandonedRoundRunner()
    runtime.langchao_live_runner = runner

    _attempt_id, render_id = commit_v2_attempt(runtime)
    with runtime.db.transaction() as conn:
        row = runtime.projections.outbox.get(render_id)
        payload = dict(row.payload)
        payload.update({"engine": "langchao", "decision_id": "langchao:round-abandoned"})
        conn.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(payload, ensure_ascii=False), render_id),
        )

    app = create_app(runtime, runtime.config)
    with TestClient(app) as client:
        replied = report(client, lease(client, "render"), status="failed",
                         result={"reason": "render timed out"})

    assert replied["ok"] is True, replied
    assert runner.repository_asked == ["langchao:round-abandoned"]
    assert len(runner.acks) == 1, "the round must be closed as a non-delivery"
    ack, confirmed = runner.acks[0]
    assert ack.sent is False and confirmed is True
    assert ack.decision_id == "langchao:round-abandoned"
    assert ack.send_outbox_id == render_id
    assert runtime.projections.outbox.get(render_id).status == "failed"


def test_a_render_committed_by_the_langchao_engine_is_answered_by_that_engine(runtime):
    """The render hook must follow the engine that committed the row.

    A proactive render used to be reported to the v2 coordinator, which has no audit for a
    langchao decision: it raised KeyError, the Runtime answered ``report_error:KeyError``,
    the render was silently dropped and the attempt sat at ``committed`` until the sweeper
    retired it. The message never reached the send stage -- the whole point of the feature.
    """
    order: list[str] = []
    coordinator = StrictV2Coordinator(order)
    runtime.v2_coordinator = coordinator
    runner = LangchaoRunnerStub()
    runtime.langchao_live_runner = runner

    attempt_id, render_id = commit_v2_attempt(runtime)
    with runtime.db.transaction() as conn:
        row = runtime.projections.outbox.get(render_id)
        payload = dict(row.payload)
        payload.update({"engine": "langchao", "decision_id": "langchao:round-1"})
        conn.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(payload, ensure_ascii=False), render_id),
        )

    app = create_app(runtime, runtime.config)
    with TestClient(app) as client:
        reply = report(client, lease(client, "render"), result={"text": "在忙什么呢。"})

    assert reply["ok"] is True, reply
    assert not str(reply.get("reason", "")).startswith("report_error"), reply
    assert [entry["decision_id"] for entry in runner.renders] == ["langchao:round-1"]
    assert coordinator.renders == [], "the langchao render must not be reported to v2"
    assert runtime.projections.attempts.get(attempt_id).state != "committed", (
        "a completed render must advance the attempt"
    )
