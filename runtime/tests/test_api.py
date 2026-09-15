"""Tests for the HTTP API surface.

The API is a thin shell, so these tests focus on the contract: request shapes,
status codes, and the fact that every write still goes through the reducer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from companion_runtime.api import create_app
from companion_runtime.runtime import Runtime
from companion_runtime.typing import AttemptState, EventType

from conftest import BASE_TIME, build_config

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(runtime: Runtime):
    """A TestClient bound to a fresh Runtime."""
    app = create_app(runtime, runtime.config)
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------------------
# health and inspection
# --------------------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    """Health reports liveness plus a compact activity summary."""
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "state_version" in payload
    assert "outbox" in payload
    assert payload["runtime_version"]


def test_state_and_config_endpoints(client: TestClient) -> None:
    """The state projection and redacted config are readable."""
    state = client.get("/state").json()
    assert "mood" in state and "drive" in state and "values" in state
    config = client.get("/config").json()
    assert config["runtime_id"] == "companion"
    assert "server" in config


def test_schedule_endpoint(client: TestClient) -> None:
    """The scheduler plan is visible for debugging."""
    response = client.get("/schedule")
    assert response.status_code == 200
    payload = response.json()
    assert "plan" in payload and "dispatch_allowed" in payload


def test_explain_endpoint(client: TestClient) -> None:
    """The psychological explanation is available standalone."""
    response = client.post("/explain", json={"force": True})
    assert response.status_code == 200
    assert response.json()["experience"]


# --------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------


def test_append_user_message_runs_the_foreground_path(client: TestClient) -> None:
    """POST /events with a user_message goes through processing, not raw append."""
    response = client.post(
        "/events",
        json={
            "event_type": EventType.USER_MESSAGE.value,
            "content": "明天下午面试，结束告诉你结果。",
            "timestamp": BASE_TIME.isoformat(),
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "user_message"
    outcome = payload["outcome"]
    assert outcome["unfinished_created"]
    assert outcome["version"] > 0
    assert outcome["event"]["event_type"] == "user_message"


def test_append_raw_event(client: TestClient) -> None:
    """Other event types are appended verbatim."""
    response = client.post(
        "/events",
        json={
            "event_type": EventType.TOOL_RESULT.value,
            "actor": "tool",
            "content": "flight cancelled",
            "metadata": {"success": False},
        },
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "event"


def test_append_event_requires_a_type(client: TestClient) -> None:
    """A malformed request is a 422."""
    assert client.post("/events", json={"content": "hi"}).status_code == 422


def test_read_events_and_one_event(client: TestClient, runtime: Runtime) -> None:
    """Events are readable individually and as a filtered list."""
    created = client.post(
        "/events",
        json={"event_type": EventType.USER_MESSAGE.value, "content": "在吗", "timestamp": BASE_TIME.isoformat()},
    ).json()["outcome"]["event"]
    listing = client.get("/events", params={"limit": 10})
    assert listing.status_code == 200
    assert listing.json()["count"] >= 1
    single = client.get(f"/events/{created['event_id']}")
    assert single.status_code == 200
    assert single.json()["event"]["content"] == "在吗"
    # Nothing has interpreted this event yet...
    assert single.json()["interpretations"] == []
    # ...and once a version exists the endpoint must expose it. Without this half an
    # endpoint that always returned ``[]`` would keep the whole suite green.
    with runtime.db.transaction() as conn:
        runtime.projections.interpretations.add_version(
            conn,
            target_kind="event",
            target_id=created["event_id"],
            content="这句问候后面可能还有话",
            confidence=0.5,
            source_version=runtime.version(),
            source_event_ids=[created["event_id"]],
        )
    after = client.get(f"/events/{created['event_id']}").json()
    assert [item["content"] for item in after["interpretations"]] == [
        "这句问候后面可能还有话"
    ]
    assert after["interpretations"][0]["interpretation_version"] == 1
    assert client.get("/events/evt_missing").status_code == 404


# --------------------------------------------------------------------------------------
# context and render block
# --------------------------------------------------------------------------------------


def test_context_and_render_block(client: TestClient) -> None:
    """The temporary context and its prompt block are exposed."""
    client.post(
        "/events",
        json={"event_type": EventType.USER_MESSAGE.value, "content": "在吗", "timestamp": BASE_TIME.isoformat()},
    )
    bundle = client.get("/context").json()
    assert bundle["ephemeral"] is True
    assert "psychological" in bundle
    block = client.post("/context/render-block", json={}).json()
    assert block["ephemeral"] is True
    assert "临时背景" in block["block"]
    assert "当前用户原话" in block["block"]


# --------------------------------------------------------------------------------------
# endogenous rounds and proposals
# --------------------------------------------------------------------------------------


def test_tick_endpoint_advances_time(client: TestClient, runtime: Runtime) -> None:
    """POST /tick runs lazy_tick explicitly.

    Every write to the state row bumps its version, so ``version >= before`` was
    true even for a tick that advanced zero seconds. The tick report the endpoint
    returns is what shows the three hours really moved the clock.
    """
    before = client.get("/health").json()["state_version"]
    response = client.post("/tick", json={"now": (BASE_TIME + timedelta(hours=3)).isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert body["version"] > before
    assert body["dt_seconds"] == pytest.approx(3 * 3600.0)
    assert body["changed"] is True
    assert runtime.state().last_tick_at == BASE_TIME + timedelta(hours=3)


def test_endogenous_endpoint_returns_a_decision(client: TestClient) -> None:
    """POST /endogenous returns the full motivational round."""
    response = client.post(
        "/endogenous", json={"now": (BASE_TIME + timedelta(hours=2)).isoformat(), "force": True}
    )
    assert response.status_code == 200
    decision = response.json()["decision"]
    assert "outcome" in decision
    assert "assessments" in decision


def test_proposal_endpoint_applies_and_discards(client: TestClient, runtime: Runtime) -> None:
    """Background results are classified through the protocol."""
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="家里出事了，我很难受",
        timestamp=BASE_TIME,
    )
    applied = client.post(
        "/proposals",
        json={
            "task_id": "tsk_api_1",
            "task_type": "emotion_eval",
            "based_on_version": runtime.version(),
            "source_event_ids": [event.event_id],
            "payload": {"direction": "-", "impact": 0.7, "confidence": 0.8},
        },
    )
    assert applied.status_code == 200
    assert applied.json()["action"] == "apply"
    assert applied.json()["applied"] is True

    discarded = client.post(
        "/proposals",
        json={
            "task_id": "tsk_api_2",
            "task_type": "emotion_eval",
            "based_on_version": runtime.version(),
            "source_event_ids": ["evt_missing"],
            "payload": {"impact": 0.9},
        },
    )
    assert discarded.json()["action"] == "discard"
    assert discarded.json()["applied"] is False


def test_register_task_endpoint(client: TestClient) -> None:
    """Task snapshots can be registered before dispatch."""
    response = client.post(
        "/tasks",
        json={"task_id": "tsk_api_3", "task_type": "emotion_explain", "source_event_ids": []},
    )
    assert response.status_code == 200
    assert response.json()["task_id"] == "tsk_api_3"


# --------------------------------------------------------------------------------------
# candidates / memories / user model
# --------------------------------------------------------------------------------------


def test_candidates_endpoint_and_operations(client: TestClient) -> None:
    """The pool is readable and mutable through the pool manager only."""
    created = client.post(
        "/candidates/operations",
        json={
            "operations": [
                {
                    "op": "add",
                    "candidate": {
                        "type": "contact",
                        "intent": "只是想联系",
                        "sources": ["internal_approach_drive"],
                    },
                },
                {"op": "add", "candidate": {"type": "contact", "intent": "groundless"}},
            ]
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert len(body["changes"]) == 1
    assert len(body["rejected"]) == 1
    listing = client.get("/candidates").json()
    assert listing["count"] == 1


def test_memories_endpoint(client: TestClient) -> None:
    """Memory, activation and candidates are readable together."""
    body = client.get("/memories").json()
    assert set(body) == {"memories", "activated", "candidates"}


def test_user_model_endpoints(client: TestClient) -> None:
    """Both views and the prediction endpoint are available."""
    body = client.get("/user-model").json()
    assert "numeric" in body and "semantic" in body
    prediction = client.post(
        "/user-model/predict",
        json={"action": {"type": "contact", "proactive": True}, "context": {"busy_probability": 0.2}},
    )
    assert prediction.status_code == 200
    payload = prediction.json()
    assert 0.0 <= payload["reply_probability"] <= 1.0
    assert payload["conservative_reply_probability"] <= payload["reply_probability"]


# --------------------------------------------------------------------------------------
# unfinished matters and boundaries
# --------------------------------------------------------------------------------------


def test_unfinished_endpoints(client: TestClient) -> None:
    """Matters can be created, listed and resolved over HTTP."""
    created = client.post(
        "/unfinished",
        json={
            "title": "等待面试结果",
            "waiting_until": (BASE_TIME + timedelta(hours=4)).isoformat(),
            "priority": 0.8,
        },
    )
    assert created.status_code == 200
    matter_id = created.json()["matter"]["unfinished_id"]
    listing = client.get("/unfinished").json()
    assert listing["count"] == 1
    resolved = client.post(f"/unfinished/{matter_id}/resolve", json={"note": "told me"})
    assert resolved.status_code == 200
    assert client.post("/unfinished/unf_missing/resolve", json={}).status_code == 404


def test_boundaries_endpoint(client: TestClient) -> None:
    """Boundaries and the permission verdict are visible."""
    runtime_now = datetime.now(timezone.utc)
    client.post(
        "/events",
        json={
            "event_type": EventType.USER_MESSAGE.value,
            "content": "永远别联系我",
            "timestamp": runtime_now.isoformat(),
        },
    )
    body = client.get("/boundaries").json()
    assert body["boundaries"]
    assert len(body["verdict"]["active_boundary_ids"]) == 1
    assert body["verdict"]["allow_proactive"] is False
    assert body["verdict"]["reason"] == "boundary_blocks_proactive"


def test_authorize_endpoint(client: TestClient) -> None:
    """The authorize endpoint answers permission questions."""
    allowed = client.post("/authorize", json={"action": "proactive_contact", "is_proactive": True})
    assert allowed.status_code == 200
    assert allowed.json()["allowed"] is True

    client.post(
        "/events",
        json={
            "event_type": EventType.USER_MESSAGE.value,
            "content": "永远别联系我",
            "timestamp": BASE_TIME.isoformat(),
        },
    )
    client.post("/tick", json={})
    denied = client.post("/authorize", json={"action": "proactive_contact", "is_proactive": True})
    assert denied.json()["allowed"] is False
    assert denied.json()["reason"] == "boundary_blocks_proactive"


# --------------------------------------------------------------------------------------
# outbox / render / delivery flow
# --------------------------------------------------------------------------------------


def _force_attempt(runtime: Runtime) -> tuple[str, str]:
    """Create a committed attempt with a queued render row."""
    from companion_runtime.typing import CandidateIntent, new_id

    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="询问面试结果",
        goal="表达关心",
        sources=["unfinished:unf_1"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        return runtime._commit_attempt(conn, chosen=candidate, state=state, now=BASE_TIME)


def test_outbox_claim_render_and_deliver(client: TestClient, runtime: Runtime) -> None:
    """The whole asynchronous delivery contract works over HTTP."""
    attempt_id, _outbox_id = _force_attempt(runtime)

    listing = client.get("/outbox").json()
    assert listing["stats"]["pending"] == 1

    claimed = client.post(
        "/outbox/claim", json={"owner": "worker-a", "limit": 1, "now": BASE_TIME.isoformat()}
    ).json()
    assert claimed["count"] == 1
    render_row = claimed["items"][0]
    assert render_row["kind"] == "render"
    assert render_row["lease_owner"] == "worker-a"

    # A second claimer gets nothing while the lease is live.
    assert client.post("/outbox/claim", json={"owner": "worker-b"}).json()["count"] == 0

    rendered = client.post(
        "/render", json={"outbox_id": render_row["outbox_id"], "text": "面试结果怎么样啦？"}
    )
    assert rendered.status_code == 200
    assert rendered.json()["state"] == "ready_to_send"
    assert rendered.json()["outbox_id"]

    send_claim = client.post(
        "/outbox/claim", json={"owner": "worker-a", "limit": 1, "kinds": ["send"]}
    ).json()
    assert send_claim["count"] == 1
    send_row = send_claim["items"][0]
    assert send_row["payload"]["text"] == "面试结果怎么样啦？"

    delivered = client.post(
        "/delivery", json={"outbox_id": send_row["outbox_id"], "success": True}
    ).json()
    assert delivered["delivered"] is True
    assert delivered["attempt_id"] == attempt_id

    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == "sent"


def test_outbox_ack_and_nack(client: TestClient, runtime: Runtime) -> None:
    """Lease bookkeeping is exposed, and a nack releases the row at once.

    A negatively acknowledged row is deliberately claimable immediately: the
    common caller pattern is nack-then-retry, and a hidden service-side delay would
    make that retry look like it had been silently dropped. Pacing belongs to the
    caller, which can ask for a delay explicitly when it wants one.
    """
    _force_attempt(runtime)
    claimed = client.post("/outbox/claim", json={"owner": "w", "limit": 1}).json()["items"][0]
    nacked = client.post(
        f"/outbox/{claimed['outbox_id']}/nack", json={"error": "worker crashed"}
    )
    assert nacked.status_code == 200
    assert nacked.json()["status"] == "pending"
    assert nacked.json()["attempts"] == 1

    # Immediately claimable again, by a different worker.
    again = client.post("/outbox/claim", json={"owner": "w2", "limit": 1}).json()["items"][0]
    assert again["outbox_id"] == claimed["outbox_id"]
    assert again["attempts"] == 2
    assert client.post(f"/outbox/{again['outbox_id']}/ack", json={}).status_code == 200
    # Acking an unleased row is a conflict, not a silent success.
    assert client.post(f"/outbox/{again['outbox_id']}/ack", json={}).status_code == 409


def test_outbox_nack_can_request_an_explicit_retry_delay(
    client: TestClient, runtime: Runtime
) -> None:
    """A caller may still ask for throttling instead of immediate reclaim."""
    _force_attempt(runtime)
    claimed = client.post("/outbox/claim", json={"owner": "w", "limit": 1}).json()["items"][0]
    nacked = client.post(
        f"/outbox/{claimed['outbox_id']}/nack",
        json={"error": "slow down", "retry_delay_seconds": 3600},
    )
    assert nacked.status_code == 200
    # Not claimable yet...
    assert client.post("/outbox/claim", json={"owner": "w2", "limit": 1}).json()["count"] == 0
    # ...but claimable once the requested delay has passed.
    later = datetime.now(timezone.utc) + timedelta(seconds=3601)
    reclaimed = client.post(
        "/outbox/claim", json={"owner": "w2", "limit": 1, "now": later.isoformat()}
    ).json()
    assert reclaimed["count"] == 1


def test_render_fail_endpoint(client: TestClient, runtime: Runtime) -> None:
    """A failed render fails the attempt, and the response reports the new state.

    ``render/fail`` returns ``attempt_state`` because a 200 only means "recorded":
    the attempt may already have been terminal, in which case the call is a no-op
    and the caller cannot tell without asking. Reporting the resulting state makes
    the outcome observable either way.
    """
    attempt_id, _outbox_id = _force_attempt(runtime)
    claimed = client.post("/outbox/claim", json={"owner": "w", "limit": 1}).json()["items"][0]
    response = client.post("/render/fail", json={"outbox_id": claimed["outbox_id"], "error": "boom"})
    assert response.status_code == 200

    body = response.json()
    assert body["attempt_state"] == "failed"
    stored = runtime.projections.attempts.get(attempt_id)
    assert stored.state == "failed"
    assert stored.state_enum is AttemptState.FAILED
    # The typed value is exposed for clients that do not want to assume a bare str.
    assert stored.to_dict()["state_value"] == AttemptState.FAILED.value

    # Reporting the same failure twice is idempotent, and still reports the state.
    repeat = client.post("/render/fail", json={"outbox_id": claimed["outbox_id"], "error": "boom"})
    assert repeat.status_code == 200
    assert repeat.json()["attempt_state"] == "failed"


def test_rendered_endpoint_direct_path(client: TestClient, runtime: Runtime) -> None:
    """POST /rendered accepts text for an attempt outside the outbox flow."""
    attempt_id, _outbox_id = _force_attempt(runtime)
    response = client.post(
        "/rendered", json={"attempt_id": attempt_id, "text": "面试怎么样啦？"}
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert client.post("/rendered", json={"attempt_id": "att_missing", "text": "x"}).status_code == 404


def test_observations_endpoint(client: TestClient, runtime: Runtime) -> None:
    """Reactions can be recorded with or without an attempt."""
    recorded = client.post(
        "/observations",
        json={
            "action": {"type": "contact", "proactive": True},
            "context": {"busy_probability": 0.1},
            "reaction": {"replied": True, "reply_length": 20, "explicit_positive": True},
            "now": BASE_TIME.isoformat(),
        },
    )
    assert recorded.status_code == 200
    assert recorded.json()["observation"]["weight"] > 0.0
    listing = client.get("/observations").json()
    assert listing["count"] == 1


def test_reconcile_endpoint(client: TestClient, runtime: Runtime) -> None:
    """In-flight attempts can be re-coordinated explicitly."""
    attempt_id, _outbox_id = _force_attempt(runtime)
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="面试过啦，结果是过了",
        timestamp=BASE_TIME + timedelta(seconds=3),
    )
    response = client.post(
        "/reconcile", json={"attempt_id": attempt_id, "event_ids": [event.event_id]}
    )
    assert response.status_code == 200
    decisions = response.json()["decisions"]
    assert decisions and decisions[0]["action"] == "resolved"


def test_attempts_and_situation_endpoints(client: TestClient, runtime: Runtime) -> None:
    """Attempt history and the working situation are inspectable."""
    attempt_id, _outbox_id = _force_attempt(runtime)
    body = client.get("/attempts").json()
    assert body["count"] == 1
    assert body["attempts"][0]["attempt_id"] == attempt_id
    assert body["attempts"][0]["transitions"]
    situation = client.get("/situation").json()
    assert "facts" in situation and "unfinished" in situation


def test_openapi_schema_is_available(client: TestClient) -> None:
    """The API documents itself, which is how the host integrates."""
    schema = client.get("/openapi.json").json()
    assert "/events" in schema["paths"]
    assert "/context" in schema["paths"]
    assert "/render" in schema["paths"]
    assert "/outbox" in schema["paths"]
    assert "/authorize" in schema["paths"]
    assert "/rendered" in schema["paths"]
    assert "/delivery" in schema["paths"]
    assert "/health" in schema["paths"]


# --------------------------------------------------------------------------------------
# durability endpoints
# --------------------------------------------------------------------------------------


def test_maintenance_endpoints(client: TestClient, tmp_path) -> None:
    """Verify, checkpoint, recovery plan and the full pass are exposed over HTTP."""
    client.post(
        "/events",
        json={
            "event_type": EventType.USER_MESSAGE.value,
            "content": "在吗",
            "timestamp": BASE_TIME.isoformat(),
        },
    )

    verified = client.get("/maintenance/verify")
    assert verified.status_code == 200
    assert verified.json()["integrity"] == "ok"

    checkpointed = client.post("/maintenance/checkpoint", json={"mode": "TRUNCATE"})
    assert checkpointed.status_code == 200
    assert checkpointed.json()["mode"] == "TRUNCATE"

    assert client.post("/maintenance/checkpoint", json={"mode": "NOPE"}).status_code == 422

    plan = client.get("/maintenance/recovery-plan", params={"backup_dir": str(tmp_path)}).json()
    assert "recommended_action" in plan

    # Checkpoint and verify work for an in-memory database too; only the backup
    # step needs a file, so the full pass is exercised separately below.
    passed = client.post("/maintenance/tick", json={})
    assert passed.status_code == 200
    assert passed.json()["verify"]["ok"] is True
    assert passed.json()["backup"] is None


def test_backup_endpoint_rejects_in_memory_database(client: TestClient) -> None:
    """An in-memory database cannot be snapshotted, and says so."""
    response = client.post("/maintenance/backup", json={})
    assert response.status_code == 409
    assert "in-memory" in response.json()["detail"]


def test_backup_endpoint_writes_a_snapshot(tmp_path) -> None:
    """A file-backed Runtime can be snapshotted over HTTP."""
    from companion_runtime.db import Database
    from companion_runtime.runtime import Runtime

    config = build_config()
    config.storage.database_path = str(tmp_path / "http.sqlite3")
    config.storage.raw_log_path = str(tmp_path / "http.jsonl")
    config.storage.mirror_raw_events = False
    runtime = Runtime(config, seed=5, created_at=BASE_TIME)
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        app = create_app(runtime, config)
        with TestClient(app) as local_client:
            target = tmp_path / "snap.sqlite3"
            response = local_client.post(
                "/maintenance/backup", json={"destination": str(target)}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["integrity"] == "ok"
            assert Path(body["path"]).exists()
            # Overwriting requires an explicit flag.
            assert (
                local_client.post(
                    "/maintenance/backup", json={"destination": str(target)}
                ).status_code
                == 409
            )

            # The full maintenance pass snapshots and prunes on a file database.
            full = local_client.post(
                "/maintenance/tick",
                json={"backup_dir": str(tmp_path / "backups"), "keep": 2},
            )
            assert full.status_code == 200
            assert full.json()["backup"]["integrity"] == "ok"
            assert Path(full.json()["backup"]["path"]).exists()
    finally:
        runtime.close()
