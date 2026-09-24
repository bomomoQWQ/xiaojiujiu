"""Tests for the protocol-v1 compatibility layer (``companion_runtime.api_v1``).

The layer exists to make the thin AstrBot adapter work against this Runtime, so
these tests are written from both sides of the wire: they drive the FastAPI app
with ``TestClient`` over an in-memory database, and - when the plugin checkout is
present - they also parse every response with the adapter's own wire dataclasses.
Nothing here needs a network, a provider or a running sidecar.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from companion_runtime.api import create_app
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    AttemptState,
    CandidateIntent,
    EventType,
    OutboxStatus,
    new_id,
)
from companion_runtime.utility import parse_datetime, utcnow

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

#: Adapter identity used by every request in this module.
ADAPTER = "test-adapter"

#: Session a fake AstrBot deployment would report (``unified_msg_origin``).
SESSION = "webchat:FriendMessage:user-1"

#: Plugin checkout holding the authoritative wire dataclasses.
PLUGIN_CORE = (
    Path(__file__).resolve().parents[2] / "astrbot_plugin_companion_runtime" / "companion_runtime"
)

#: Package name the plugin's own ``companion_runtime`` is imported under, so it
#: cannot collide with the Runtime package under test.
PLUGIN_PACKAGE = "plugin_companion_runtime_under_test"


def plugin_protocol() -> Any:
    """Return the AstrBot adapter's protocol module.

    Returns:
        The plugin's ``companion_runtime.protocol`` module, imported under a
        private package name so the Runtime's identically named package is
        untouched. Tests are skipped when the plugin checkout is absent.
    """
    if not (PLUGIN_CORE / "protocol.py").exists():
        pytest.skip("AstrBot plugin checkout is not available")
    if PLUGIN_PACKAGE not in sys.modules:
        package = types.ModuleType(PLUGIN_PACKAGE)
        package.__path__ = [str(PLUGIN_CORE)]  # type: ignore[attr-defined]
        sys.modules[PLUGIN_PACKAGE] = package
    return importlib.import_module(f"{PLUGIN_PACKAGE}.protocol")


@pytest.fixture()
def client(runtime: Runtime):
    """A TestClient bound to a fresh Runtime with the v1 router included."""
    app = create_app(runtime, runtime.config)
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def commit_attempt(runtime: Runtime, *, intent: str = "询问面试结果") -> tuple[str, str]:
    """Commit a proactive attempt and return ``(attempt_id, render_outbox_id)``.

    This mirrors the v0 delivery tests: committing is a Runtime decision, so the
    fixture creates one directly through the private commit helper instead of
    waiting for the motivational game to reach for it.
    """
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=intent,
        goal="表达关心",
        sources=["unfinished:unf_test"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        attempt_id, outbox_id = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=utcnow()
        )
    return attempt_id, outbox_id


def user_event(text: str, event_id: str, *, session: str = SESSION) -> dict[str, Any]:
    """Build one ``EventRecord.to_wire()`` mapping for a user message."""
    return {
        "event_id": event_id,
        "kind": "user_message",
        "session": session,
        "text": text,
        "occurred_at": utcnow().isoformat(),
        "platform": "webchat",
        "message_type": "private",
        "sender_id": "user-1",
        "sender_name": "User",
        "self_id": "bot-1",
        "group_id": "",
        "message_id": "msg-1",
        "wake": True,
        "preempts_proactive": True,
    }


def post_events(client: TestClient, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Post an event envelope and return the decoded response body."""
    response = client.post(
        "/v1/events",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "sent_at": utcnow().isoformat(),
            "events": events,
        },
    )
    assert response.status_code == 200
    return response.json()


def lease(client: TestClient, **overrides: Any) -> dict[str, Any]:
    """Lease actions and return the decoded response body."""
    body: dict[str, Any] = {
        "protocol_version": "1",
        "adapter_id": ADAPTER,
        "capabilities": ["render", "send"],
        "max_actions": 2,
        "lease_ttl_ms": 30_000,
    }
    body.update(overrides)
    response = client.post("/v1/outbox/lease", json=body)
    assert response.status_code == 200
    return response.json()


def report_body(
    action: dict[str, Any],
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
    attempt_id: str = "",
) -> dict[str, Any]:
    """Build an ``ActionReport.to_wire()`` body for a leased action."""
    body: dict[str, Any] = {
        "protocol_version": "1",
        "adapter_id": ADAPTER,
        "action_id": action["action_id"],
        "lease_id": action["lease_id"],
        "action_type": action["action_type"],
        "status": status,
        "attempt_id": attempt_id or action["attempt_id"],
        "session": action["session"],
        "reported_at": utcnow().isoformat(),
    }
    if result is not None:
        body["result"] = result
    if error:
        body["error"] = error
    return body


def report(client: TestClient, action: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Post an action result and return the decoded response body."""
    response = client.post(
        f"/v1/outbox/{action['action_id']}/result", json=report_body(action, **kwargs)
    )
    assert response.status_code == 200
    return response.json()


def render_ok(client: TestClient, action: dict[str, Any], text: str) -> dict[str, Any]:
    """Report a successful render and return the response body."""
    return report(client, action, status="ok", result={"text": text, "chars": len(text)})


# --------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------


def test_v1_user_message_runs_the_v02_semantic_path(client: TestClient, runtime: Runtime) -> None:
    """A v1 user_message settles (or deliberately refuses to settle) like v0.2."""
    resolved = post_events(client, [user_event("谢谢你，今天陪我聊了这么久。", "evt_resolved")])
    assert resolved["protocol_version"] == "1"
    assert resolved["accepted"] == 1
    assert resolved["duplicates"] == 0
    assert resolved["rejected"] == 0
    assert resolved["runtime_version"] >= 1

    outcome = resolved["outcomes"][0]
    assert outcome["semantic_status"] == "resolved"
    assert outcome["appraisal_source"] == "coarse_rule"
    assert outcome["event"]["event_id"] == "evt_resolved"
    # The adapter's session is the conversation scope, and the whole record is
    # preserved in the raw event metadata.
    assert outcome["event"]["conversation_id"] == SESSION
    assert outcome["event"]["metadata"]["adapter"]["platform"] == "webchat"
    assert outcome["event"]["metadata"]["adapter"]["preempts_proactive"] is True
    assert runtime.projections.semantics.get("evt_resolved")["semantic_status"] == "resolved"

    unresolved = post_events(client, [user_event("算了，也没什么。", "evt_unresolved")])
    assert unresolved["accepted"] == 1
    deferred = unresolved["outcomes"][0]
    assert deferred["semantic_status"] == "unresolved"
    assert deferred["appraisal_source"] == "deferred"
    assert deferred["potential_relevance"]
    assert runtime.projections.semantics.get("evt_unresolved")["semantic_status"] == "unresolved"


def test_v1_event_id_is_an_idempotency_key(client: TestClient, runtime: Runtime) -> None:
    """Re-delivering the same event_id appends nothing and reprocesses nothing."""
    record = user_event("明天下午面试，结束告诉你结果。", "evt_once")
    first = post_events(client, [record])
    assert first["accepted"] == 1
    assert runtime.events.count(EventType.USER_MESSAGE.value) == 1
    version_after_first = runtime.state().version

    repeated = post_events(client, [record])
    assert repeated["accepted"] == 0
    assert repeated["duplicates"] == 1
    assert repeated["outcomes"][0] == {"duplicate": True, "event_id": "evt_once"}
    # No second raw event and no second foreground pass (the version is the
    # proof: process_user_message would have bumped it).
    assert runtime.events.count(EventType.USER_MESSAGE.value) == 1
    assert runtime.state().version == version_after_first

    mixed = post_events(client, [record, user_event("算了，也没什么。", "evt_new")])
    assert mixed["accepted"] == 1
    assert mixed["duplicates"] == 1
    assert runtime.events.count(EventType.USER_MESSAGE.value) == 2


def test_v1_assistant_message_is_append_only(client: TestClient, runtime: Runtime) -> None:
    """An assistant_message is a fact: it is recorded, never interpreted."""
    body = post_events(
        client,
        [
            {
                "event_id": "evt_bot",
                "kind": "assistant_message",
                "session": SESSION,
                "text": "在忙吗？",
                "occurred_at": utcnow().isoformat(),
                "platform": "webchat",
            }
        ],
    )
    assert body["accepted"] == 1
    assert body["outcomes"][0]["event"]["event_type"] == "assistant_message"
    assert runtime.events.count(EventType.ASSISTANT_MESSAGE.value) == 1
    assert runtime.projections.semantics.get("evt_bot") is None


def test_v1_events_without_a_session_fall_back_to_the_configured_conversation(
    client: TestClient, runtime: Runtime
) -> None:
    """A record with no session still lands in the Runtime's conversation."""
    body = post_events(client, [user_event("在吗", "evt_no_session", session="")])
    assert body["accepted"] == 1
    assert body["outcomes"][0]["event"]["conversation_id"] == runtime.config.conversation_id


def test_v1_events_reject_an_unsupported_kind(
    client: TestClient, runtime: Runtime
) -> None:
    """An unknown kind is counted and explained, and never fails the batch."""
    body = post_events(
        client,
        [
            {"event_id": "evt_weird", "kind": "telepathy", "session": SESSION, "text": "?"},
            user_event("在吗", "evt_ok"),
        ],
    )
    assert body["rejected"] == 1
    assert body["accepted"] == 1
    assert body["outcomes"][0]["skipped"] is True
    assert "unsupported_kind" in body["outcomes"][0]["reason"]
    assert runtime.events.exists("evt_ok") is True
    assert runtime.events.exists("evt_weird") is False


# --------------------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------------------


def test_v1_context_returns_a_non_empty_parseable_snapshot(
    client: TestClient, runtime: Runtime
) -> None:
    """POST /v1/context answers the four fields ContextSnapshot.from_wire reads."""
    response = client.post(
        "/v1/context",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "session": SESSION,
            "trigger": "llm_request",
            "platform": "webchat",
            "last_event_id": "evt_1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["text"], str)
    assert body["text"].strip()
    assert body["version"] == str(runtime.state().version)
    assert isinstance(body["sections"], dict)
    assert body["sections"]
    assert isinstance(body["ttl_ms"], int)
    assert body["ttl_ms"] > 0
    assert body["last_event_id"] == "evt_1"

    protocol = plugin_protocol()
    snapshot = protocol.ContextSnapshot.from_wire(body)
    assert snapshot is not None
    assert snapshot.is_empty() is False
    assert snapshot.text == body["text"]
    assert snapshot.version == body["version"]
    assert snapshot.ttl_ms == body["ttl_ms"]
    assert snapshot.render().strip() == body["text"].strip()
    # The README's envelope shape carries the same snapshot.
    nested = protocol.ContextSnapshot.from_wire(body["context"])
    assert nested is not None
    assert nested.text == snapshot.text


# --------------------------------------------------------------------------------------
# lease -> render -> send -> result
# --------------------------------------------------------------------------------------


def test_v1_lease_render_send_lifecycle(client: TestClient, runtime: Runtime) -> None:
    """Rent a render action, then the send action, and advance the attempt."""
    attempt_id, render_outbox_id = commit_attempt(runtime)

    first = lease(client)
    assert first["count"] == 1
    assert first["items"] == first["actions"]
    action = first["items"][0]
    assert action["action_id"] == render_outbox_id
    assert action["action_type"] == "render"
    assert action["attempt_id"] == attempt_id
    assert action["lease_id"]
    assert action["deadline_at"]
    assert action["lease_ttl_ms"] > 0
    # The Runtime is single-conversation, so the lease reports the row's
    # conversation identity (see the api_v1 module docstring).
    assert action["session"] == runtime.config.conversation_id
    assert action["payload"]["prompt"].strip()
    assert action["payload"]["intent"] == "询问面试结果"

    protocol = plugin_protocol()
    leased = protocol.LeasedAction.from_wire(action)
    assert leased is not None
    assert leased.action_id == render_outbox_id
    assert leased.lease_id == action["lease_id"]
    assert leased.action_type == protocol.ACTION_RENDER
    assert leased.session

    rendered = render_ok(client, action, "面试怎么样啦？")
    assert rendered["ok"] is True
    assert not rendered.get("duplicate")
    assert rendered["attempt_state"] == AttemptState.READY_TO_SEND.value

    # committed != sent: rendering is not delivery, and no send was recorded.
    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.READY_TO_SEND.value
    assert attempt.state != AttemptState.SENT.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 0

    send_row_id = rendered["send_outbox_id"]
    second = lease(client)
    assert second["count"] == 1
    send_action = second["items"][0]
    assert send_action["action_id"] == send_row_id
    assert send_action["action_type"] == "send"
    assert send_action["payload"]["text"] == "面试怎么样啦？"

    digest = hashlib.sha256("面试怎么样啦？".encode("utf-8")).hexdigest()
    decision = client.post(
        f"/v1/actions/{send_row_id}/authorize",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "lease_id": send_action["lease_id"],
            "session": send_action["session"],
            "attempt_id": attempt_id,
            "text_preview": "面试怎么样啦？",
            "text_sha256": digest,
        },
    )
    assert decision.status_code == 200
    verdict = decision.json()
    assert verdict["authorized"] is True
    assert verdict["reason"] == "permitted"
    assert verdict["text"] == ""
    assert protocol.AuthorizeDecision.from_wire(verdict).authorized is True
    assert protocol.AuthorizeDecision.from_wire(verdict["authorization"]).authorized is True

    # Still not sent: an authorization is permission, not delivery.
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.READY_TO_SEND.value

    sent = report(client, send_action, status="ok", result={"sent": True, "chars": 7})
    assert sent["ok"] is True
    assert sent["delivered"] is True
    assert sent["attempt_state"] == AttemptState.SENT.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 1
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.DELIVERED.value
    parsed_report = protocol.ActionReport(
        adapter_id=ADAPTER,
        action_id=send_row_id,
        lease_id=send_action["lease_id"],
        status="ok",
        action_type="send",
    )
    assert parsed_report.summary().startswith("send:")


def test_v1_send_report_without_sent_is_not_a_delivery(
    client: TestClient, runtime: Runtime
) -> None:
    """status=ok with result.sent=false is a failure, never a silent send."""
    attempt_id, _render_outbox_id = commit_attempt(runtime)
    render_ok(client, lease(client)["items"][0], "面试怎么样啦？")
    send_action = lease(client)["items"][0]

    reported = report(
        client,
        send_action,
        status="ok",
        result={"sent": False, "reason": "platform_not_found"},
    )
    assert reported["ok"] is True
    assert reported["delivered"] is False
    assert reported["attempt_state"] == AttemptState.FAILED.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 0


def test_v1_render_failure_and_skipped_are_terminal(client: TestClient, runtime: Runtime) -> None:
    """A failed or skipped render ends the attempt instead of re-dispatching it."""
    attempt_id, _render_outbox_id = commit_attempt(runtime)
    action = lease(client)["items"][0]

    failed = report(client, action, status="failed", error="render timed out after 60s")
    assert failed["ok"] is True
    assert failed["attempt_state"] == AttemptState.FAILED.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value
    row = runtime.projections.outbox.get(action["action_id"])
    assert row.status == OutboxStatus.FAILED.value

    attempt_id, outbox_id = commit_attempt(runtime, intent="再问一次")
    action = lease(client)["items"][0]
    assert action["action_id"] == outbox_id
    skipped = report(client, action, status="skipped", error="missing_session")
    assert skipped["ok"] is True
    assert skipped["attempt_state"] == AttemptState.FAILED.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value


def test_v1_result_is_idempotent(client: TestClient, runtime: Runtime) -> None:
    """A repeated report advances nothing a second time."""
    _attempt_id, _render_outbox_id = commit_attempt(runtime)
    action = lease(client)["items"][0]

    first = render_ok(client, action, "面试怎么样啦？")
    assert first["ok"] is True
    assert not first.get("duplicate")
    version_after_first = runtime.state().version
    pending_after_first = len(
        runtime.projections.outbox.list_items(status=OutboxStatus.PENDING.value, limit=50)
    )
    assert pending_after_first == 1

    second = render_ok(client, action, "面试怎么样啦？")
    assert second["ok"] is True
    assert second["duplicate"] is True
    assert second["attempt_state"] == first["attempt_state"]
    assert runtime.state().version == version_after_first
    assert (
        len(runtime.projections.outbox.list_items(status=OutboxStatus.PENDING.value, limit=50))
        == pending_after_first
    )

    third = report(client, action, status="failed", error="late duplicate")
    assert third["ok"] is True
    assert third["duplicate"] is True
    assert third["attempt_state"] == AttemptState.READY_TO_SEND.value


def test_v1_result_for_an_unknown_action_is_reported_not_raised(
    client: TestClient, runtime: Runtime
) -> None:
    """An unknown action_id answers ok=false at HTTP 200."""
    response = client.post(
        "/v1/outbox/obx_missing/result",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "action_id": "obx_missing",
            "lease_id": "test-adapter:obx_missing:1",
            "action_type": "render",
            "status": "ok",
            "attempt_id": "",
            "session": SESSION,
            "reported_at": utcnow().isoformat(),
            "result": {"text": "hi"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "unknown_action"


# --------------------------------------------------------------------------------------
# authorize
# --------------------------------------------------------------------------------------


def test_v1_authorize_denies_a_send_under_a_boundary(
    client: TestClient, runtime: Runtime
) -> None:
    """A hard boundary in force stops the irreversible send.

    The boundary is declared *before* the intention exists, so this exercises the
    authorization gate itself. A boundary that arrives while a message is already
    rendered is a different story: ingest re-coordinates that attempt immediately
    and cancels its send row (covered by the Runtime lifecycle tests), so there
    would be nothing left to lease and therefore nothing to authorize.
    """
    boundary = post_events(client, [user_event("永远别联系我", "evt_boundary")])
    assert boundary["accepted"] == 1
    assert runtime.projections.boundaries.active(utcnow())

    attempt_id, _render_outbox_id = commit_attempt(runtime)
    render_ok(client, lease(client)["items"][0], "面试怎么样啦？")

    send_action = lease(client)["items"][0]
    assert send_action["action_type"] == "send"
    decision = client.post(
        f"/v1/actions/{send_action['action_id']}/authorize",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "lease_id": send_action["lease_id"],
            "session": send_action["session"],
            "attempt_id": attempt_id,
            "text_preview": "面试怎么样啦？",
            "text_sha256": hashlib.sha256("面试怎么样啦？".encode("utf-8")).hexdigest(),
        },
    )
    assert decision.status_code == 200
    verdict = decision.json()
    assert verdict["authorized"] is False
    assert verdict["reason"] == "boundary_blocks_proactive"
    assert verdict["blocking_boundary_ids"]
    assert verdict["text"] == ""

    # A denial is not a delivery either.
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.READY_TO_SEND.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 0

    rejected = report(
        client,
        send_action,
        status="rejected",
        error=verdict["reason"],
        result={"authorized": False},
    )
    assert rejected["ok"] is True
    assert rejected["attempt_state"] == AttemptState.FAILED.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 0


def test_v1_authorize_fails_closed(client: TestClient, runtime: Runtime, monkeypatch) -> None:
    """Missing rows, stale hashes and internal errors all deny."""
    unknown = client.post(
        "/v1/actions/obx_missing/authorize",
        json={"protocol_version": "1", "adapter_id": ADAPTER, "lease_id": "x"},
    )
    assert unknown.status_code == 200
    assert unknown.json()["authorized"] is False
    assert unknown.json()["reason"] == "unknown_action"

    _attempt_id, _render_outbox_id = commit_attempt(runtime)
    render_ok(client, lease(client)["items"][0], "面试怎么样啦？")
    send_action = lease(client)["items"][0]

    tampered = client.post(
        f"/v1/actions/{send_action['action_id']}/authorize",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "lease_id": send_action["lease_id"],
            "attempt_id": send_action["attempt_id"],
            "text_preview": "面筋怎么样啦？",
            "text_sha256": hashlib.sha256("面筋怎么样啦？".encode("utf-8")).hexdigest(),
        },
    )
    assert tampered.status_code == 200
    assert tampered.json()["authorized"] is False
    assert tampered.json()["reason"] == "text_sha256_mismatch"

    from companion_runtime import api_v1

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        """Raise, to prove the authorize path fails closed."""
        raise RuntimeError("authorizer unavailable")

    # A body that cannot be judged must deny: never 500, never a silent send.
    monkeypatch.setattr(api_v1, "authorize", explode)
    broken = client.post(
        f"/v1/actions/{send_action['action_id']}/authorize",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "lease_id": send_action["lease_id"],
            "attempt_id": send_action["attempt_id"],
            "text_preview": "面试怎么样啦？",
        },
    )
    assert broken.status_code == 200
    assert broken.json()["authorized"] is False
    assert broken.json()["reason"] == "authorize_error:RuntimeError"


# --------------------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------------------


def test_v1_heartbeat_extends_a_live_lease(client: TestClient, runtime: Runtime) -> None:
    """A live lease is extended through a direct, single-column update."""
    _attempt_id, render_outbox_id = commit_attempt(runtime)
    action = lease(client)["items"][0]
    before = parse_datetime(action["deadline_at"])
    assert before is not None

    response = client.post(
        f"/v1/outbox/{render_outbox_id}/heartbeat",
        json={
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "lease_id": action["lease_id"],
            "extend_ms": 60_000,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["extended"] is True
    assert body["protocol_version"] == "1"
    after = parse_datetime(body["deadline_at"])
    assert after is not None
    assert after > before
    assert body["lease_ttl_ms"] > 0
    assert runtime.projections.outbox.get(render_outbox_id).lease_expires_at == after


def test_v1_heartbeat_answers_false_instead_of_raising(
    client: TestClient, runtime: Runtime
) -> None:
    """Unknown rows, foreign leases and stale lease ids are ordinary answers."""
    _attempt_id, render_outbox_id = commit_attempt(runtime)
    action = lease(client)["items"][0]

    def beat(action_id: str, **overrides: Any) -> dict[str, Any]:
        """Post a heartbeat and return the decoded body."""
        body: dict[str, Any] = {
            "protocol_version": "1",
            "adapter_id": ADAPTER,
            "lease_id": action["lease_id"],
            "extend_ms": 5_000,
        }
        body.update(overrides)
        response = client.post(f"/v1/outbox/{action_id}/heartbeat", json=body)
        assert response.status_code == 200
        return response.json()

    missing = beat("obx_missing")
    assert missing["ok"] is False
    assert missing["extended"] is False
    assert missing["reason"] == "unknown_action"

    foreign = beat(render_outbox_id, adapter_id="another-adapter")
    assert foreign["ok"] is False
    assert foreign["reason"] == "lease_owner_mismatch"

    stale = beat(render_outbox_id, lease_id="test-adapter:obx_stale:1")
    assert stale["ok"] is False
    assert stale["reason"] == "stale_lease"

    assert beat(render_outbox_id)["ok"] is True


# --------------------------------------------------------------------------------------
# capability gating
# --------------------------------------------------------------------------------------


def test_v1_lease_respects_capabilities(client: TestClient, runtime: Runtime) -> None:
    """An adapter that cannot render is never given a render row."""
    _attempt_id, outbox_id = commit_attempt(runtime)
    assert lease(client, capabilities=["send"])["count"] == 0
    assert runtime.projections.outbox.get(outbox_id).status == OutboxStatus.PENDING.value
    assert lease(client, capabilities=["telepathy"])["count"] == 0

    granted = lease(client, capabilities=["render"], max_actions=1)
    assert granted["count"] == 1
    assert granted["items"][0]["action_type"] == "render"


# --------------------------------------------------------------------------------------
# render prompt styling
# --------------------------------------------------------------------------------------


def test_v1_render_prompt_carries_the_style_contract(client: TestClient, runtime: Runtime) -> None:
    """A render prompt restates her style itself, because no persona reaches that path.

    A render is a one-shot ``llm_generate`` call: AstrBot adds no persona, no tools and
    no datetime reminder to it, and ``Context.llm_generate`` has no persona fallback.
    The system context is already in the transcript by then, so only the style contract
    has to live in the prompt. Measured before the fix: proactive messages ran 60-100
    characters, with list formatting and closing summaries, i.e. nothing like her.
    """
    commit_attempt(runtime)
    payload = lease(client)["items"][0]["payload"]
    prompt = payload["prompt"]

    assert "30 字以内" in prompt
    assert "我不写 Markdown" in prompt
    # The question habit gets its own rule: that is where the failure was worst.
    assert "只问一个" in prompt
    # The style contract must not swallow the render instruction itself.
    assert "只回正文本身" in prompt
    assert payload["intent"] == "询问面试结果"


def test_v1_render_prompt_states_the_clock_with_the_task(
    client: TestClient, runtime: Runtime
) -> None:
    """The clock is a parameter of the performance, so it is stated as an instruction.

    Regression: the clock used to sit inside the injection block, and that block ends
    by telling the reader it describes the state *before* this turn and is **not** how
    to react in it - so the only clock a render ever gets was filed as background, on
    the line right above the sentence that de-authorised it. Measured on the beta: a
    17:53 render still greeted the user with 早呀, and the drafts being rendered were up
    to 6.0h old (median 2.3h), which is exactly the candidate TTL.

    The end-to-end reproduction lives in ``framework/tests/test_proactive_timing.py``;
    this is the cheap unit-level pin so a refactor cannot quietly move it back.
    """
    commit_attempt(runtime)
    payload = lease(client)["items"][0]["payload"]
    prompt = payload["prompt"]

    header_at = prompt.index("【我现在要说的话】")
    clock_at = prompt.index("- 现在是：")
    ruling_at = prompt.index("不是这一句该怎么回")
    assert header_at < clock_at, "the clock must be part of the instruction"
    assert clock_at > ruling_at, "the clock is still inside the de-authorised background"
    assert "- 当前本地时间：" not in prompt, "the block still carries a second clock"

    # Only the clock moved: the time section survives in the block, so "how long since
    # we last spoke" stays where it was - it is cognition, not a performance parameter.
    assert "【时间上的事】" in prompt
    assert "以上都只是我进来之前的状态" in prompt

    # It must be the local wall clock, not an ISO stamp on a line labelled 现在是 - and
    # the draft's own anchor must be stated next to it. "Now" alone cannot tell the render
    # how far a draft written hours ago has drifted; both moments can, and code supplies
    # both, so the drift is checkable instead of guessed.
    clock_stamp = prompt.split("- 现在是：", 1)[1].splitlines()[0].strip()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S", clock_stamp), clock_stamp
    assert any(day in clock_stamp for day in ("周一", "周二", "周三", "周四", "周五", "周六", "周日"))
    written_stamp = prompt.split("- 写于：", 1)[1].splitlines()[0].strip()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S", written_stamp), written_stamp

    # And the rule has to keep both halves: the stale *tone* is recomputed against the
    # clock, a dated *appointment* is not. Dropping the second half would trade a wrong
    # "早安" for a moved commitment, which is worse than the defect being fixed.
    rule = prompt.split("- 时间上：", 1)[1].splitlines()[0]
    assert "我唯一能信的现在" in rule, rule
    assert "重说一遍" in rule, rule
    assert "照原样留着" in rule, rule


def test_v1_render_prompt_names_the_draft_word_that_drifted(
    client: TestClient, runtime: Runtime
) -> None:
    """A draft carrying a relative moment is flagged *by name*, not left to be noticed.

    Prohibiting time in a draft was tried and dropped: the draft's material is the user's
    own sentence, so stripping it destroys the content, and the prompt was never a
    guarantee anyway. What makes it safe is that both moments are stated and the
    discrepancy is pointable - "「明天」is relative to 写于, recompute it" is a checkable
    instruction; "mind the time" is not.
    """
    commit_attempt(runtime, intent="约他明天中午去那家面馆")
    prompt = lease(client)["items"][0]["payload"]["prompt"]

    hint = prompt.split("留意：", 1)[1].splitlines()[0]
    assert "明天" in hint, hint
    assert "写于" in hint, hint

    # A draft with no relative moment gets no such line: the hint has to mean something.
    runtime2 = None
    try:
        from companion_runtime.config import RuntimeConfig
        from companion_runtime.db import Database
        from companion_runtime.projections import Projections
        from companion_runtime.runtime import Runtime as LiveRuntime

        config = RuntimeConfig()
        config.storage.database_path = ":memory:"
        runtime2 = LiveRuntime(config, seed=99, created_at=utcnow())
        with runtime2.db.transaction() as conn:
            from companion_runtime.typing import CandidateIntent, new_id

            candidate = CandidateIntent(
                candidate_id=new_id("candidate"),
                type="follow_up",
                intent="问他那家店还去不去",
                sources=["unfinished:unf_x"],
            )
            runtime2.projections.candidates.upsert(conn, candidate)
            state = runtime2.projections.runtime.ensure()
            runtime2._commit_attempt(conn, chosen=candidate, state=state, now=utcnow())
        from companion_runtime.api import create_app

        clean_client = TestClient(create_app(runtime2, runtime2.config))
        clean = lease(clean_client)["items"][0]["payload"]["prompt"]
        assert "留意：" not in clean, clean
    finally:
        if runtime2 is not None:
            runtime2.close()
