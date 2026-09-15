"""Regression tests for the Runtime API / reducer reliability fixes.

Every test here was written against a specific failure that was reproduced
first, and each one is phrased as the invariant the fix has to preserve rather
than as a description of the implementation:

* ``POST /rendered`` must find the render row for its attempt whatever the size
  of the queue, must always end in a queued send, and must report explicitly
  when an attempt cannot take the render;
* the commit's own cooldown must not block the render step of the attempt it
  belongs to, while still blocking a *new* proactive decision;
* ``complete_render`` must be idempotent from the recorded effect, inside one
  transaction, and must never fail an attempt that already sent its message;
* concurrent reports must apply at most once (render *and* delivery);
* a duplicate ``event_id`` (v0 and v1) and a duplicate ``task_id`` must converge
  instead of raising, appending a second copy, or applying twice;
* native ``ack``/``nack`` must validate the lease owner when the caller supplies
  one, and keep working for callers that do not.
"""

from __future__ import annotations

import importlib
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from companion_runtime import action as action_module
from companion_runtime.api import create_app
from companion_runtime.authorize import AuthorizeRequest, authorize
from companion_runtime.projections import OutboxProjection
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    AttemptState,
    CandidateIntent,
    EventType,
    OutboxItem,
    OutboxKind,
    OutboxStatus,
    new_id,
)
from companion_runtime.utility import utcnow

from conftest import BASE_TIME

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

ADAPTER = "reliability-adapter"

#: Text used by every render report in this module.
TEXT = "面试怎么样啦？"

#: Plugin checkout holding the authoritative wire dataclasses (skipped when absent).
PLUGIN_CORE = (
    Path(__file__).resolve().parents[2] / "astrbot_plugin_companion_runtime" / "companion_runtime"
)

#: Package name the plugin's own ``companion_runtime`` is imported under here.
PLUGIN_PACKAGE = "reliability_plugin_companion_runtime"


def plugin_protocol() -> Any:
    """Return the AstrBot adapter's protocol module, skipping when it is absent."""
    if not (PLUGIN_CORE / "protocol.py").exists():
        pytest.skip("AstrBot plugin checkout is not available")
    if PLUGIN_PACKAGE not in sys.modules:
        package = types.ModuleType(PLUGIN_PACKAGE)
        package.__path__ = [str(PLUGIN_CORE)]  # type: ignore[attr-defined]
        sys.modules[PLUGIN_PACKAGE] = package
    return importlib.import_module(f"{PLUGIN_PACKAGE}.protocol")


@pytest.fixture()
def client(runtime: Runtime):
    """A TestClient over both the v0 and the v1 surface."""
    app = create_app(runtime, runtime.config)
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def commit_attempt(runtime: Runtime, *, intent: str = "询问面试结果") -> tuple[str, str]:
    """Commit an attempt through the Runtime's own path, with a render row."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=intent,
        goal="表达关心",
        sources=["unfinished:unf_reliability"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        return runtime._commit_attempt(conn, chosen=candidate, state=state, now=BASE_TIME)


def commit_attempt_without_a_render_row(runtime: Runtime) -> str:
    """Commit an attempt that was never queued for rendering."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="直接路径",
        goal="表达关心",
        sources=["unfinished:unf_reliability"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        attempt = action_module.create_proposal(
            candidate=candidate, based_on_version=runtime.state().version, now=BASE_TIME
        )
        action_module.commit(runtime.projections.attempts, conn, attempt, now=BASE_TIME)
    return attempt.attempt_id


def enqueue_noise(runtime: Runtime, count: int) -> None:
    """Fill the queue with unrelated, newer rows.

    They are newer on purpose: the outbox listing is ``ORDER BY created_at DESC``,
    so these are exactly the rows that used to push the real one off the page.
    """
    with runtime.db.transaction() as conn:
        for index in range(count):
            runtime.projections.outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id=new_id("outbox"),
                    kind=OutboxKind.SEND.value,
                    payload={"attempt_id": f"att_noise_{index}", "text": "noise"},
                    created_at=BASE_TIME + timedelta(seconds=index + 1),
                    available_at=BASE_TIME + timedelta(seconds=index + 1),
                ),
            )


def rows_for(runtime: Runtime, attempt_id: str, kind: str | None = None) -> list[OutboxItem]:
    """Return the outbox rows that reference ``attempt_id`` (all of them)."""
    return runtime.projections.outbox.find_for_attempt(attempt_id, kind=kind)


def report_body(action: dict[str, Any], *, status: str, **extra: Any) -> dict[str, Any]:
    """Build a v1 ``ActionReport.to_wire()`` body for a leased action."""
    body: dict[str, Any] = {
        "protocol_version": "1",
        "adapter_id": ADAPTER,
        "action_id": action["action_id"],
        "lease_id": action["lease_id"],
        "action_type": action["action_type"],
        "status": status,
        "attempt_id": action["attempt_id"],
        "session": action["session"],
    }
    body.update(extra)
    return body


# --------------------------------------------------------------------------------------
# /rendered: row lookup, direct path, authorization
# --------------------------------------------------------------------------------------


def test_find_for_attempt_is_not_a_page_of_the_queue(runtime: Runtime) -> None:
    """The lookup is targeted and exact, however busy the queue is."""
    attempt_id, render_row_id = commit_attempt(runtime)
    enqueue_noise(runtime, 150)

    found = rows_for(runtime, attempt_id, kind=OutboxKind.RENDER.value)
    assert [item.outbox_id for item in found] == [render_row_id]

    # A prefix is not a match: the SQL pre-filter is a substring test, so the
    # verification in Python is what makes the lookup exact.
    assert rows_for(runtime, attempt_id[:-1]) == []
    assert rows_for(runtime, "") == []

    # A live row outranks a settled one for the same attempt, because a
    # re-coordination can leave an older row behind.
    with runtime.db.transaction() as conn:
        old = runtime.projections.outbox.get(render_row_id)
        assert old is not None
        runtime.projections.outbox.nack(conn, render_row_id, error="x", terminal=True)
        fresh = OutboxItem(
            outbox_id=new_id("outbox"),
            kind=OutboxKind.RENDER.value,
            payload={"attempt_id": attempt_id},
            created_at=BASE_TIME,
            available_at=BASE_TIME,
        )
        runtime.projections.outbox.enqueue(conn, fresh)
    assert rows_for(runtime, attempt_id, kind=OutboxKind.RENDER.value)[0].outbox_id == fresh.outbox_id


def test_rendered_finds_its_row_behind_more_than_a_hundred_others(
    client: TestClient, runtime: Runtime
) -> None:
    """A busy outbox must not route the report down the direct path."""
    attempt_id, render_row_id = commit_attempt(runtime)
    enqueue_noise(runtime, 150)

    response = client.post(
        "/rendered",
        json={"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["path"] == "outbox", "the render row was missed and the direct path was taken"
    assert body["state"] == AttemptState.READY_TO_SEND.value

    # The render row was consumed, and exactly one send row exists for the text.
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.DELIVERED.value
    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1
    assert sends[0].payload["text"] == TEXT
    assert runtime.projections.attempts.get(attempt_id).outbox_id == sends[0].outbox_id


def test_rendered_direct_path_queues_the_send(client: TestClient, runtime: Runtime) -> None:
    """An attempt outside the outbox flow still ends up with a send row."""
    attempt_id = commit_attempt_without_a_render_row(runtime)
    assert rows_for(runtime, attempt_id) == []

    response = client.post(
        "/rendered",
        json={"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["path"] == "direct"
    assert body["applied"] is True

    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1, "the direct path accepted the text without queueing a send"
    assert sends[0].status == OutboxStatus.PENDING.value
    assert sends[0].payload["text"] == TEXT
    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.READY_TO_SEND.value
    assert attempt.outbox_id == sends[0].outbox_id


def test_rendered_direct_path_fails_explicitly_for_an_uncommitted_attempt(
    client: TestClient, runtime: Runtime
) -> None:
    """Nothing was committed, so there is nothing to render: say so."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="还没决定",
        sources=["unfinished:unf_reliability"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        attempt = action_module.create_proposal(
            candidate=candidate, based_on_version=0, now=BASE_TIME
        )
        runtime.projections.attempts.upsert(conn, attempt)

    response = client.post(
        "/rendered",
        json={"attempt_id": attempt.attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()},
    )
    assert response.status_code == 409
    assert "render_not_applicable" in response.json()["detail"]
    # The attempt was not silently advanced and no delivery was queued.
    assert runtime.projections.attempts.get(attempt.attempt_id).state == AttemptState.PROPOSED.value
    assert rows_for(runtime, attempt.attempt_id) == []
    assert client.post("/rendered", json={"attempt_id": "att_missing", "text": "x"}).status_code == 404


def test_rendered_is_not_blocked_by_the_commit_s_own_cooldown(
    client: TestClient, runtime: Runtime
) -> None:
    """The render of an attempt the Runtime just committed must go through.

    Committing starts the cooldown and counts the contact, so asking the *render*
    step for permission under the same budget would deadlock every proactive
    message the Runtime had already decided to send. The budget still applies to
    a fresh decision, which is asserted here as the control.
    """
    attempt_id, _render_row_id = commit_attempt(runtime)
    state = runtime.state()
    assert state.cooldown_until is not None and state.cooldown_until > BASE_TIME

    response = client.post(
        "/rendered",
        json={"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True, body.get("reason")
    assert body["state"] == AttemptState.READY_TO_SEND.value

    # Control: a prospective proactive decision is still refused by the cooldown.
    verdict = authorize(
        AuthorizeRequest(action="proactive_contact", is_proactive=True, now=BASE_TIME),
        projections=runtime.projections,
        config=runtime.config,
        state=runtime.state(),
        now=BASE_TIME,
    )
    assert verdict.allowed is False
    assert verdict.reason == "cooldown_active"


def test_rendered_replay_converges(client: TestClient, runtime: Runtime) -> None:
    """Reporting the same render twice queues one send, not two."""
    attempt_id, _render_row_id = commit_attempt(runtime)
    payload = {"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()}

    first = client.post("/rendered", json=payload).json()
    assert first["applied"] is True
    assert not first["duplicate"]
    transitions_after_first = runtime.projections.attempts.transitions(attempt_id)

    second = client.post("/rendered", json=payload)
    assert second.status_code == 200
    body = second.json()
    assert body["accepted"] is True
    assert body["duplicate"] is True
    assert body["applied"] is False
    assert body["state"] == AttemptState.READY_TO_SEND.value
    assert (
        runtime.projections.attempts.transitions(attempt_id) == transitions_after_first
    ), "a replay must not transition the attempt again"

    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1


def test_rendered_after_delivery_is_refused_without_touching_state(
    client: TestClient, runtime: Runtime
) -> None:
    """Once the message left, a late render report is refused, not re-applied.

    The render gate denies an attempt that is already ``sent``, so the caller is
    told the truth (``accepted=false`` with a reason) and nothing is queued or
    re-attached: a second message must never leave the Runtime because a report
    was redelivered.
    """
    attempt_id, _render_row_id = commit_attempt(runtime)
    report = {"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()}
    assert client.post("/rendered", json=report).json()["accepted"] is True

    claimed = client.post(
        "/outbox/claim", json={"owner": "w", "limit": 1, "kinds": ["send"]}
    ).json()["items"][0]
    delivered = client.post(
        "/delivery", json={"outbox_id": claimed["outbox_id"], "success": True}
    ).json()
    assert delivered["delivered"] is True
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    sent_events = runtime.events.count(EventType.PROACTIVE_SENT.value)

    late = client.post("/rendered", json=report)
    assert late.status_code == 200
    body = late.json()
    assert body["accepted"] is False
    assert body["reason"] == "attempt_already_sent"
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == sent_events
    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1


# --------------------------------------------------------------------------------------
# complete_render: transactional idempotency
# --------------------------------------------------------------------------------------


def test_complete_render_replay_does_not_queue_a_second_send(runtime: Runtime) -> None:
    """A repeated render result is absorbed, not applied twice."""
    attempt_id, render_row_id = commit_attempt(runtime)
    claimed = runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1)
    assert claimed[0].outbox_id == render_row_id

    first = runtime.reducer.complete_render(outbox_id=render_row_id, text=TEXT, now=BASE_TIME)
    assert first.applied is True
    assert first.outbox_id
    version_after_first = runtime.state().version

    second = runtime.reducer.complete_render(outbox_id=render_row_id, text=TEXT, now=BASE_TIME)
    assert second.applied is False
    assert second.duplicate is True
    assert second.reason == "render_already_completed"
    assert second.outbox_id == first.outbox_id
    assert runtime.state().version == version_after_first

    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.READY_TO_SEND.value


def test_complete_render_replay_after_delivery_does_not_fail_the_attempt(
    runtime: Runtime,
) -> None:
    """The dangerous replay: the report arrives after the message was delivered.

    Re-attaching the text is impossible (``sent -> ready_to_send`` is illegal), so
    the only correct answer is the recorded outcome. The previous implementation
    tried to fail the attempt instead, which raised out of the reducer.
    """
    attempt_id, render_row_id = commit_attempt(runtime)
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1)
    first = runtime.reducer.complete_render(outbox_id=render_row_id, text=TEXT, now=BASE_TIME)
    assert first.outbox_id
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1, kinds=["send"])
    delivered = runtime.reducer.mark_delivered(outbox_id=first.outbox_id, now=BASE_TIME)
    assert delivered["delivered"] is True

    replay = runtime.reducer.complete_render(outbox_id=render_row_id, text=TEXT, now=BASE_TIME)
    assert replay.applied is False
    assert replay.duplicate is True
    assert replay.state == AttemptState.SENT.value
    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.SENT.value
    assert attempt.failure_reason is None, "a late report must not mark a delivered message failed"
    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1
    assert sends[0].status == OutboxStatus.DELIVERED.value


def test_rendered_report_of_an_empty_text_fails_the_attempt_once(runtime: Runtime) -> None:
    """A render that cannot be used closes the attempt and the row, once.

    The failure is applied through the reducer's shared terminal-synchronization
    helper, so it is asserted here against ``fail_render`` -- the other caller of
    that helper -- rather than against a private detail: both ways of reporting a
    failed render must leave the same history, and neither may leave a second
    ``proactive_aborted`` record when the report is replayed.
    """
    attempt_id, render_row_id = commit_attempt(runtime)
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1)

    blank = runtime.reducer.complete_render(outbox_id=render_row_id, text="   ", now=BASE_TIME)
    assert blank.applied is True
    assert blank.outbox_id is None
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.FAILED.value
    aborted = runtime.events.count(EventType.PROACTIVE_ABORTED.value)
    assert aborted == 1

    # The row is terminal, so a repeat is a duplicate rather than a second write.
    again = runtime.reducer.complete_render(outbox_id=render_row_id, text="   ", now=BASE_TIME)
    assert again.applied is False
    assert again.duplicate is True
    assert again.reason == "render_row_failed"
    assert runtime.events.count(EventType.PROACTIVE_ABORTED.value) == aborted

    # The other reporting path for the same situation reaches the same state.
    other_attempt_id, other_row_id = commit_attempt(runtime, intent="另一个渲染失败")
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1)
    assert runtime.reducer.fail_render(
        outbox_id=other_row_id, error="renderer_error:boom", now=BASE_TIME
    ) is True
    assert runtime.projections.attempts.get(other_attempt_id).state == AttemptState.FAILED.value
    assert runtime.projections.outbox.get(other_row_id).status == OutboxStatus.FAILED.value
    assert runtime.events.count(EventType.PROACTIVE_ABORTED.value) == aborted + 1


def test_a_late_render_report_cannot_reopen_a_swept_attempt(runtime: Runtime) -> None:
    """The outbox-failure sweep owns the terminal state; a late report must not undo it.

    The sweep (``Reducer.close_settled_outbox_attempts``) fails an attempt whose
    outbox row failed terminally and cancels its siblings. A render report that
    arrives afterwards must report the recorded outcome and change nothing -- not
    even a second ``proactive_aborted`` record, and certainly not a new send row.
    """
    attempt_id, render_row_id = commit_attempt(runtime)
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1)
    assert runtime.reducer.nack_outbox(
        render_row_id, error="renderer_gone", terminal=True, now=BASE_TIME
    ) is True
    runtime.reducer.close_settled_outbox_attempts(now=BASE_TIME)

    swept_state = runtime.projections.attempts.get(attempt_id).state
    assert swept_state == AttemptState.FAILED.value
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.FAILED.value
    aborted = runtime.events.count(EventType.PROACTIVE_ABORTED.value)

    late = runtime.reducer.complete_render(outbox_id=render_row_id, text=TEXT, now=BASE_TIME)
    assert late.applied is False
    assert late.duplicate is True
    assert runtime.projections.attempts.get(attempt_id).state == swept_state
    assert runtime.events.count(EventType.PROACTIVE_ABORTED.value) == aborted
    assert [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value] == []


def test_render_endpoint_reports_a_replay_instead_of_raising(
    client: TestClient, runtime: Runtime
) -> None:
    """POST /render is idempotent too, and still reports the outcome."""
    attempt_id, render_row_id = commit_attempt(runtime)
    first = client.post("/render", json={"outbox_id": render_row_id, "text": TEXT})
    assert first.status_code == 200
    assert first.json()["state"] == AttemptState.READY_TO_SEND.value
    send_row_id = first.json()["outbox_id"]

    replay = client.post("/render", json={"outbox_id": render_row_id, "text": TEXT})
    assert replay.status_code == 200
    body = replay.json()
    assert body["duplicate"] is True
    assert body["applied"] is False
    assert body["outbox_id"] == send_row_id
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.READY_TO_SEND.value
    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1

    # And after delivery the replay still reports the recorded outcome.
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1, kinds=["send"])
    runtime.reducer.mark_delivered(outbox_id=send_row_id, now=BASE_TIME)
    late = client.post("/render", json={"outbox_id": render_row_id, "text": TEXT})
    assert late.status_code == 200
    assert late.json()["duplicate"] is True
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value


# --------------------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------------------


def test_concurrent_render_reports_queue_one_send(
    client: TestClient, runtime: Runtime
) -> None:
    """Two reports at once must not both advance the same attempt."""
    attempt_id, render_row_id = commit_attempt(runtime)
    payload = {"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()}

    def call() -> int:
        return client.post("/rendered", json=payload).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: call(), range(4)))

    assert statuses == [200] * 4
    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1, "concurrent reports queued more than one send"
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.DELIVERED.value


def test_concurrent_v1_result_reports_apply_once(
    client: TestClient, runtime: Runtime
) -> None:
    """The v1 result path decides idempotency inside the transaction."""
    _attempt_id, render_row_id = commit_attempt(runtime)
    action = client.post(
        "/v1/outbox/lease",
        json={"protocol_version": "1", "adapter_id": ADAPTER, "capabilities": ["render"]},
    ).json()["items"][0]
    assert action["action_id"] == render_row_id
    body = report_body(action, status="ok", result={"text": TEXT})

    def call() -> dict[str, Any]:
        response = client.post(f"/v1/outbox/{render_row_id}/result", json=body)
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(), range(4)))

    assert all(item["ok"] for item in results)
    assert sum(1 for item in results if not item.get("duplicate")) == 1
    pending = [
        item
        for item in runtime.projections.outbox.list_items(
            status=OutboxStatus.PENDING.value, limit=100
        )
        if item.kind == OutboxKind.SEND.value
    ]
    assert len(pending) == 1


def test_concurrent_send_reports_count_the_contact_once(
    client: TestClient, runtime: Runtime
) -> None:
    """A duplicated delivery report must not count a second proactive contact."""
    attempt_id, render_row_id = commit_attempt(runtime)
    client.post("/rendered", json={"attempt_id": attempt_id, "text": TEXT})
    send_action = client.post(
        "/v1/outbox/lease",
        json={"protocol_version": "1", "adapter_id": ADAPTER, "capabilities": ["send"]},
    ).json()["items"][0]
    body = report_body(send_action, status="ok", result={"sent": True, "chars": len(TEXT)})

    def call() -> dict[str, Any]:
        response = client.post(
            f"/v1/outbox/{send_action['action_id']}/result", json=body
        )
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(), range(4)))

    assert all(item["ok"] for item in results)
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 1
    assert runtime.events.count(EventType.ASSISTANT_MESSAGE.value) == 1
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value


def test_rendered_survives_a_concurrent_tick(client: TestClient, runtime: Runtime) -> None:
    """A version bump during a render report must not surface as a 500.

    The render handler used to read the state outside a transaction and write it
    back with ``expect_version``, so a tick that landed in between produced a
    ``VersionConflict``. Reads and the write now happen in one transaction.
    """
    attempt_id, _render_row_id = commit_attempt(runtime)
    payload = {"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()}
    failures: list[tuple[str, int, str]] = []

    def render_call() -> None:
        response = client.post("/rendered", json=payload)
        if response.status_code >= 500:
            failures.append(("rendered", response.status_code, response.text[:200]))

    def tick_call(offset: int) -> None:
        response = client.post(
            "/tick", json={"now": (BASE_TIME + timedelta(seconds=offset + 1)).isoformat()}
        )
        if response.status_code >= 500:
            failures.append(("tick", response.status_code, response.text[:200]))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(render_call) for _ in range(2)]
        futures += [pool.submit(tick_call, index) for index in range(6)]
        for future in futures:
            future.result()

    assert not failures, failures
    sends = [item for item in rows_for(runtime, attempt_id) if item.kind == OutboxKind.SEND.value]
    assert len(sends) == 1


# --------------------------------------------------------------------------------------
# duplicate events and proposals
# --------------------------------------------------------------------------------------


def test_duplicate_user_message_event_id_converges(
    client: TestClient, runtime: Runtime
) -> None:
    """A redelivered user_message is the recorded event, not a 500."""
    payload = {
        "event_type": EventType.USER_MESSAGE.value,
        "content": "明天下午面试，结束告诉你结果。",
        "event_id": "evt_dup_message",
        "timestamp": BASE_TIME.isoformat(),
    }
    first = client.post("/events", json=payload)
    assert first.status_code == 200
    assert first.json()["outcome"]["duplicate"] is False
    matters_after_first = len(runtime.projections.unfinished.list_open())
    observations_after_first = len(runtime.projections.user_model.list_observations())

    second = client.post("/events", json=payload)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["duplicate"] is True
    assert body["outcome"]["duplicate"] is True
    assert body["outcome"]["event"]["event_id"] == "evt_dup_message"
    # Nothing was processed a second time: one raw event and no second foreground
    # pass (no boundary event, no second matter, no second observation).
    assert runtime.events.count(EventType.USER_MESSAGE.value) == 1
    assert runtime.events.count(EventType.BOUNDARY_DECLARED.value) == 0
    assert len(runtime.projections.unfinished.list_open()) == matters_after_first
    assert len(runtime.projections.user_model.list_observations()) == observations_after_first


def test_duplicate_raw_event_id_converges(client: TestClient, runtime: Runtime) -> None:
    """The append-only path converges on the same ``event_id`` too."""
    payload = {
        "event_type": EventType.TOOL_RESULT.value,
        "actor": "tool",
        "content": "flight cancelled",
        "event_id": "evt_dup_raw",
    }
    first = client.post("/events", json=payload)
    assert first.status_code == 200
    assert first.json()["kind"] == "event"
    assert "duplicate" not in first.json()

    second = client.post("/events", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True
    assert second.json()["event"]["event_id"] == "evt_dup_raw"
    assert runtime.events.count(EventType.TOOL_RESULT.value) == 1


def test_duplicate_proposal_task_id_applies_once(client: TestClient, runtime: Runtime) -> None:
    """A retried proposal must not be applied twice."""
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="家里出事了，我很难受",
        timestamp=BASE_TIME,
    )
    payload = {
        "task_id": "tsk_retried",
        "task_type": "emotion_eval",
        "based_on_version": runtime.version(),
        "source_event_ids": [event.event_id],
        "payload": {"direction": "-", "impact": 0.8, "activation": 0.7, "confidence": 0.9},
    }
    first = client.post("/proposals", json=payload)
    assert first.status_code == 200
    assert first.json()["applied"] is True
    emotions_after_first = len(runtime.projections.emotion.list_active())
    version_after_first = runtime.state().version

    second = client.post("/proposals", json=payload)
    assert second.status_code == 200
    body = second.json()
    assert body["applied"] is False
    assert body["reason"] == "duplicate_task_id"
    assert body["action"] == "discard"
    assert len(runtime.projections.emotion.list_active()) == emotions_after_first
    assert runtime.state().version == version_after_first


def test_a_new_task_id_is_still_applied(client: TestClient, runtime: Runtime) -> None:
    """The duplicate guard must not become a blanket refusal."""
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="家里出事了，我很难受",
        timestamp=BASE_TIME,
    )
    base = {
        "task_type": "emotion_eval",
        "based_on_version": runtime.version(),
        "source_event_ids": [event.event_id],
        "payload": {"direction": "-", "impact": 0.4, "activation": 0.3, "confidence": 0.9},
    }
    for index in range(2):
        response = client.post("/proposals", json=base | {"task_id": f"tsk_fresh_{index}"})
        assert response.json()["applied"] is True, response.text


# --------------------------------------------------------------------------------------
# native ack / nack ownership
# --------------------------------------------------------------------------------------


def test_ack_and_nack_validate_the_owner_when_supplied(
    client: TestClient, runtime: Runtime
) -> None:
    """A worker cannot complete or release another worker's lease."""
    _attempt_id, render_row_id = commit_attempt(runtime)
    claimed = client.post("/outbox/claim", json={"owner": "worker-a", "limit": 1}).json()
    assert claimed["items"][0]["lease_owner"] == "worker-a"

    foreign_nack = client.post(
        f"/outbox/{render_row_id}/nack", json={"error": "steal", "owner": "worker-b"}
    )
    assert foreign_nack.status_code == 409
    assert "worker-a" in foreign_nack.json()["detail"]
    row = runtime.projections.outbox.get(render_row_id)
    assert row.status == OutboxStatus.LEASED.value
    assert row.attempts == 1

    foreign_ack = client.post(
        f"/outbox/{render_row_id}/ack", json={"owner": "worker-b", "lease_owner": "worker-b"}
    )
    assert foreign_ack.status_code == 409
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.LEASED.value

    # The owner itself can still release and then complete the row.
    assert client.post(
        f"/outbox/{render_row_id}/nack", json={"error": "retry", "owner": "worker-a"}
    ).status_code == 200
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.PENDING.value
    reclaimed = client.post(
        "/outbox/claim", json={"owner": "worker-a", "limit": 1}
    ).json()["items"][0]
    assert reclaimed["outbox_id"] == render_row_id
    assert client.post(
        f"/outbox/{render_row_id}/ack", json={"owner": "worker-a"}
    ).status_code == 200
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.DELIVERED.value


def test_ack_and_nack_without_an_owner_still_work(client: TestClient, runtime: Runtime) -> None:
    """Compatibility: a caller that never learned the owner is not refused."""
    _attempt_id, render_row_id = commit_attempt(runtime)
    claimed = client.post("/outbox/claim", json={"owner": "worker-a", "limit": 1}).json()["items"][0]
    assert claimed["outbox_id"] == render_row_id

    nacked = client.post(f"/outbox/{render_row_id}/nack", json={"error": "no owner"})
    assert nacked.status_code == 200
    assert nacked.json()["status"] == OutboxStatus.PENDING.value

    client.post("/outbox/claim", json={"owner": "worker-b", "limit": 1})
    acked = client.post(f"/outbox/{render_row_id}/ack", json={})
    assert acked.status_code == 200
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.DELIVERED.value
    # Unknown rows are still a conflict, not a silent success.
    assert client.post("/outbox/obx_missing/ack", json={}).status_code == 409


def test_ack_ownership_is_enforced_inside_the_projection(runtime: Runtime) -> None:
    """The guard lives in the statement, so a direct caller gets it too."""
    attempt_id, render_row_id = commit_attempt(runtime)
    outbox: OutboxProjection = runtime.projections.outbox
    runtime.reducer.claim_outbox(owner="worker-a", now=BASE_TIME, limit=1)

    with runtime.db.transaction() as conn:
        assert outbox.nack(
            conn, render_row_id, error="wrong owner", expect_owner="worker-b"
        ) is False
    assert outbox.get(render_row_id).status == OutboxStatus.LEASED.value

    with runtime.db.transaction() as conn:
        assert outbox.ack(conn, render_row_id, BASE_TIME, expect_owner="worker-b") is False
        assert outbox.ack(conn, render_row_id, BASE_TIME, expect_owner="worker-a") is True
    assert outbox.get(render_row_id).status == OutboxStatus.DELIVERED.value
    assert rows_for(runtime, attempt_id, kind=OutboxKind.RENDER.value)[0].status == (
        OutboxStatus.DELIVERED.value
    )


# --------------------------------------------------------------------------------------
# authorize outage reports (status=failed with result.authorize_unavailable=true)
# --------------------------------------------------------------------------------------


def lease_v1(client: TestClient, *capabilities: str) -> dict[str, Any]:
    """Lease one action of the given v1 capabilities."""
    body = {
        "protocol_version": "1",
        "adapter_id": ADAPTER,
        "capabilities": list(capabilities),
        "max_actions": 1,
    }
    response = client.post("/v1/outbox/lease", json=body)
    assert response.status_code == 200
    items = response.json()["items"]
    assert items, "a lease should have been granted"
    return items[0]


def outage_body(action: dict[str, Any], **result: Any) -> dict[str, Any]:
    """Build the report an adapter sends when it could not obtain a verdict."""
    payload: dict[str, Any] = {"authorize_unavailable": True}
    payload.update(result)
    return report_body(
        action,
        status="failed",
        error="authorize_unavailable:RuntimeError: runtime unreachable",
        result=payload,
    )


def ready_to_send(client: TestClient, runtime: Runtime) -> tuple[str, str]:
    """Commit an attempt, attach text through /rendered, and return its ids."""
    attempt_id, _render_row_id = commit_attempt(runtime)
    rendered = client.post(
        "/rendered",
        json={"attempt_id": attempt_id, "text": TEXT, "now": BASE_TIME.isoformat()},
    ).json()
    assert rendered["state"] == AttemptState.READY_TO_SEND.value
    return attempt_id, rendered["outbox_id"]


def test_authorize_outage_report_requeues_instead_of_failing(
    client: TestClient, runtime: Runtime
) -> None:
    """An outage is not a delivery failure: the row goes back, the attempt stands.

    Recording ``mark_delivered(success=False)`` here would fail the attempt and
    the row terminally over a network blip the Runtime never saw, dropping a
    proactive message the character had already decided to send.
    """
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    assert action["action_id"] == send_row_id
    assert runtime.projections.outbox.get(send_row_id).attempts == 1
    version_before = runtime.state().version

    response = client.post(f"/v1/outbox/{send_row_id}/result", json=outage_body(action))
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["retryable"] is True
    assert body["requeued"] is True
    assert body["duplicate"] is False
    assert body["attempt_state"] == AttemptState.READY_TO_SEND.value

    row = runtime.projections.outbox.get(send_row_id)
    assert row.status == OutboxStatus.PENDING.value, "the row must be retryable"
    assert row.lease_owner is None
    # The claim counter is deliberately *not* refunded: it is what keeps a
    # ``lease_id`` unique per claim, so an outage report can still be told apart
    # from a report about a later claim. The requeue has no exhaustion rule, so
    # the rising counter does not make the row any less retryable.
    assert row.attempts == 1
    assert "authorize_unavailable" in (row.last_error or "")

    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.READY_TO_SEND.value
    assert attempt.failure_reason is None
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 0
    assert runtime.events.count(EventType.PROACTIVE_ABORTED.value) == 0
    assert runtime.state().version == version_before, "an outage writes no Runtime state"


def test_send_outage_branch_returns_retryable_and_leaves_the_attempt_alone(
    client: TestClient, runtime: Runtime
) -> None:
    """The send-row branch itself: ``ok=true``, ``retryable=true``, state unchanged.

    The adapter's condition is exactly ``action_type == "send"`` plus
    ``result.authorize_unavailable == true``, so this pins that body: the row is
    handed back, the attempt is not transitioned, and the answer says nothing but
    the truth about both.
    """
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    assert action["action_type"] == "send"

    state_before = runtime.projections.attempts.get(attempt_id)
    transitions_before = runtime.projections.attempts.transitions(attempt_id)

    body = client.post(
        f"/v1/outbox/{send_row_id}/result",
        json=report_body(
            action,
            status="failed",
            error="authorize_unavailable:RuntimeError: runtime unreachable",
            result={"authorize_unavailable": True},
        ),
    ).json()

    assert body["ok"] is True
    assert body["retryable"] is True
    assert body["requeued"] is True
    assert body["duplicate"] is False
    # "state unchanged" is asserted against the attempt itself, not only against
    # the echoed field: no transition, no failure reason, same state, same ids.
    state_after = runtime.projections.attempts.get(attempt_id)
    assert body["attempt_state"] == state_before.state == state_after.state
    assert state_after.state == AttemptState.READY_TO_SEND.value
    assert state_after.failure_reason is None
    assert state_after.rendered_text == state_before.rendered_text
    assert state_after.outbox_id == send_row_id
    assert runtime.projections.attempts.transitions(attempt_id) == transitions_before

    row = runtime.projections.outbox.get(send_row_id)
    assert row.status == OutboxStatus.PENDING.value
    assert row.lease_owner is None
    assert row.acked_at is None, "the row is not acknowledged, it is handed back"

    # And it is genuinely re-leaseable, which is what "retryable" promises.
    again = lease_v1(client, "send")
    assert again["action_id"] == send_row_id
    assert again["lease_id"] != action["lease_id"]


def test_outage_reports_stay_retryable_after_the_attempt_budget_is_spent(
    client: TestClient, runtime: Runtime
) -> None:
    """Repeated outages never fail the row or the attempt, even past max_attempts.

    ``nack`` fails a row whose attempt budget is spent; an outage is not an
    attempt, so every report must still come back retryable -- otherwise a long
    Runtime outage would quietly drop the message, and the sweep that closes
    attempts behind failed rows would close this one too.
    """
    attempt_id, send_row_id = ready_to_send(client, runtime)
    original = runtime.projections.outbox.get(send_row_id)
    assert original is not None
    budget = original.max_attempts

    for _ in range(budget + 2):
        action = lease_v1(client, "send")
        assert action["action_id"] == send_row_id
        body = client.post(f"/v1/outbox/{send_row_id}/result", json=outage_body(action)).json()
        assert body["ok"] is True
        assert body["retryable"] is True
        assert body["attempt_state"] == AttemptState.READY_TO_SEND.value
        assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.PENDING.value

    stored = runtime.projections.outbox.get(send_row_id)
    assert stored is not None
    assert stored.attempts > budget, "the budget really was spent"
    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.READY_TO_SEND.value
    assert attempt.failure_reason is None
    assert runtime.events.count(EventType.PROACTIVE_ABORTED.value) == 0

    # And the message still goes out once the Runtime answers again.
    final = lease_v1(client, "send")
    sent = client.post(
        f"/v1/outbox/{send_row_id}/result",
        json=report_body(final, status="ok", result={"sent": True}),
    ).json()
    assert sent["delivered"] is True
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value


def test_authorize_outage_replay_is_idempotent(client: TestClient, runtime: Runtime) -> None:
    """The same outage report twice converges: one requeue, no second write."""
    _attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    body = outage_body(action)

    first = client.post(f"/v1/outbox/{send_row_id}/result", json=body).json()
    assert first["requeued"] is True
    attempts_after_first = runtime.projections.outbox.get(send_row_id).attempts
    version_after_first = runtime.state().version

    second = client.post(f"/v1/outbox/{send_row_id}/result", json=body)
    assert second.status_code == 200
    replay = second.json()
    assert replay["ok"] is True
    assert replay["duplicate"] is True
    assert replay["requeued"] is False
    row = runtime.projections.outbox.get(send_row_id)
    assert row.status == OutboxStatus.PENDING.value
    assert row.attempts == attempts_after_first, "a replay must not write again"
    assert runtime.state().version == version_after_first


def test_authorize_outage_for_a_stale_claim_is_ignored(
    client: TestClient, runtime: Runtime
) -> None:
    """A report about a lease that was already replaced does not release the new one."""
    _attempt_id, send_row_id = ready_to_send(client, runtime)
    stale = lease_v1(client, "send")
    stale_attempts = runtime.projections.outbox.get(send_row_id).attempts
    assert client.post(
        f"/v1/outbox/{send_row_id}/result", json=outage_body(stale)
    ).json()["requeued"] is True

    current = lease_v1(client, "send")
    assert current["action_id"] == send_row_id
    assert current["lease_id"] != stale["lease_id"], "a re-claim gets a new lease id"
    current_attempts = runtime.projections.outbox.get(send_row_id).attempts
    assert current_attempts == stale_attempts + 1

    replayed = client.post(
        f"/v1/outbox/{send_row_id}/result", json=outage_body(stale)
    ).json()
    assert replayed["ok"] is True
    assert replayed["duplicate"] is True
    assert replayed["requeued"] is False
    row = runtime.projections.outbox.get(send_row_id)
    assert row.status == OutboxStatus.LEASED.value, "the live claim must survive"
    assert row.attempts == current_attempts

    # The current claim can still release itself.
    assert client.post(
        f"/v1/outbox/{send_row_id}/result", json=outage_body(current)
    ).json()["requeued"] is True
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.PENDING.value


def test_authorize_outage_from_another_adapter_is_refused(
    client: TestClient, runtime: Runtime
) -> None:
    """Releasing somebody else's live claim is not this adapter's business."""
    _attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    body = outage_body(action) | {"adapter_id": "different-adapter"}

    response = client.post(f"/v1/outbox/{send_row_id}/result", json=body)
    assert response.status_code == 200
    verdict = response.json()
    assert verdict["ok"] is False
    assert verdict["reason"] == "lease_owner_mismatch"
    row = runtime.projections.outbox.get(send_row_id)
    assert row.status == OutboxStatus.LEASED.value
    assert row.lease_owner == ADAPTER


def test_authorize_outage_can_ask_for_pacing(client: TestClient, runtime: Runtime) -> None:
    """``result.retry_after_ms`` delays the reclaim; the default is immediate.

    Pacing is asserted through the reducer's own claim (which takes an explicit
    clock) rather than through the HTTP lease route, whose clock is the wall clock
    and therefore cannot be moved forward inside a test.
    """
    _attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    before = utcnow()
    body = outage_body(action, retry_after_ms=60_000)

    response = client.post(f"/v1/outbox/{send_row_id}/result", json=body).json()
    assert response["requeued"] is True
    assert response["available_at"]

    row = runtime.projections.outbox.get(send_row_id)
    assert row.status == OutboxStatus.PENDING.value
    assert row.available_at is not None
    assert row.available_at > before + timedelta(seconds=30), "the requested delay was ignored"

    # Not claimable now, claimable once the requested delay has passed.
    assert runtime.reducer.claim_outbox(owner="w", now=before, limit=1) == []
    later = runtime.reducer.claim_outbox(owner="w", now=row.available_at + timedelta(seconds=1), limit=1)
    assert [item.outbox_id for item in later] == [send_row_id]

    # Without a requested delay the row is claimable at once.
    _attempt_id_two, other_row = ready_to_send(client, runtime)
    other_action = lease_v1(client, "send")
    assert other_action["action_id"] == other_row
    immediate = client.post(
        f"/v1/outbox/{other_row}/result", json=outage_body(other_action)
    ).json()
    assert immediate["requeued"] is True
    assert runtime.projections.outbox.get(other_row).status == OutboxStatus.PENDING.value
    assert [
        item.outbox_id
        for item in runtime.reducer.claim_outbox(owner="w", now=utcnow(), limit=5)
    ] == [other_row]


def test_the_message_survives_the_outage(client: TestClient, runtime: Runtime) -> None:
    """Requeue after the outage, then deliver for real."""
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    assert client.post(
        f"/v1/outbox/{send_row_id}/result", json=outage_body(action)
    ).json()["requeued"] is True

    again = lease_v1(client, "send")
    assert again["action_id"] == send_row_id
    sent = client.post(
        f"/v1/outbox/{send_row_id}/result",
        json=report_body(again, status="ok", result={"sent": True, "chars": len(TEXT)}),
    ).json()
    assert sent["ok"] is True
    assert sent["delivered"] is True
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 1


def test_authorize_outage_on_a_delivered_row_converges(
    client: TestClient, runtime: Runtime
) -> None:
    """An outage report that arrives after delivery changes nothing."""
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    assert client.post(
        f"/v1/outbox/{send_row_id}/result",
        json=report_body(action, status="ok", result={"sent": True}),
    ).json()["delivered"] is True
    sent_events = runtime.events.count(EventType.PROACTIVE_SENT.value)

    late = client.post(f"/v1/outbox/{send_row_id}/result", json=outage_body(action))
    assert late.status_code == 200
    body = late.json()
    assert body["ok"] is True
    assert body["duplicate"] is True
    assert body["requeued"] is False
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.DELIVERED.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == sent_events


def test_authorize_outage_on_a_render_row_is_also_non_terminal(
    client: TestClient, runtime: Runtime
) -> None:
    """The rule is "no verdict was given", so it holds for either action kind."""
    attempt_id, render_row_id = commit_attempt(runtime)
    action = lease_v1(client, "render")
    assert action["action_id"] == render_row_id

    body = client.post(
        f"/v1/outbox/{render_row_id}/result", json=outage_body(action)
    ).json()
    assert body["ok"] is True
    assert body["requeued"] is True
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.PENDING.value
    attempt = runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.COMMITTED.value
    assert attempt.failure_reason is None


def test_a_real_execution_failure_stays_terminal(
    client: TestClient, runtime: Runtime
) -> None:
    """Only the explicit outage marker is non-terminal; a real failure is not."""
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")

    failed = client.post(
        f"/v1/outbox/{send_row_id}/result",
        json=report_body(
            action,
            status="failed",
            error="platform_not_found",
            result={"sent": False, "reason": "platform_not_found"},
        ),
    ).json()
    assert failed["ok"] is True
    assert failed["delivered"] is False
    assert failed["attempt_state"] == AttemptState.FAILED.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.FAILED.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 0

    # A render that produced nothing usable stays terminal too.
    other_attempt_id, other_row_id = commit_attempt(runtime, intent="渲染也失败")
    render_action = lease_v1(client, "render")
    assert render_action["action_id"] == other_row_id
    skipped = client.post(
        f"/v1/outbox/{other_row_id}/result",
        json=report_body(render_action, status="skipped", error="missing_session"),
    ).json()
    assert skipped["attempt_state"] == AttemptState.FAILED.value
    assert runtime.projections.attempts.get(other_attempt_id).state == AttemptState.FAILED.value


def test_outage_marker_is_not_confused_with_a_false_flag(
    client: TestClient, runtime: Runtime
) -> None:
    """``authorize_unavailable=false`` is an ordinary terminal failure."""
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")

    body = client.post(
        f"/v1/outbox/{send_row_id}/result",
        json=report_body(
            action,
            status="failed",
            error="platform_not_found",
            result={"authorize_unavailable": False, "reason": "platform_not_found"},
        ),
    ).json()
    assert body.get("requeued") is None
    assert body["attempt_state"] == AttemptState.FAILED.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.FAILED.value


def test_concurrent_outage_reports_requeue_once(client: TestClient, runtime: Runtime) -> None:
    """Two copies of one outage report must not requeue twice."""
    _attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    body = outage_body(action)

    def call() -> dict[str, Any]:
        response = client.post(f"/v1/outbox/{send_row_id}/result", json=body)
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(), range(4)))

    assert all(item["ok"] for item in results)
    assert sum(1 for item in results if item["requeued"]) == 1
    assert all(item["duplicate"] for item in results if not item["requeued"])
    row = runtime.projections.outbox.get(send_row_id)
    assert row.status == OutboxStatus.PENDING.value
    assert row.attempts == 1, "only the claim itself counts"


def test_authorize_outage_after_the_attempt_was_closed_voids_the_row(
    client: TestClient, runtime: Runtime
) -> None:
    """No delivery is left to retry, so the claim is released by cancelling it."""
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")
    # The intention is closed underneath the lease (an unrelated failure path).
    with runtime.db.transaction() as conn:
        attempt = runtime.projections.attempts.get(attempt_id)
        action_module.fail(
            runtime.projections.attempts, conn, attempt, reason="user_spoke_first", now=BASE_TIME
        )
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value

    body = client.post(f"/v1/outbox/{send_row_id}/result", json=outage_body(action)).json()
    assert body["ok"] is True
    assert body["duplicate"] is True
    assert body["requeued"] is False
    assert body["attempt_state"] == AttemptState.FAILED.value
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.CANCELLED.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value


def test_a_contradictory_outage_report_never_causes_a_second_send(
    client: TestClient, runtime: Runtime
) -> None:
    """A body that says the message went out is a delivery, marker or not.

    The marker means "no verdict was obtained", so on its own it requeues. It must
    not override the one thing that is worse to get wrong: a report that the
    message was actually delivered. Requeueing that would send it twice.
    """
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")

    contradictory = report_body(
        action,
        status="ok",
        result={"sent": True, "authorize_unavailable": True},
    )
    body = client.post(f"/v1/outbox/{send_row_id}/result", json=contradictory).json()
    assert body["ok"] is True
    assert body["delivered"] is True
    assert body.get("requeued") is None
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.DELIVERED.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 1

    # And a render body that carries text is applied rather than requeued.
    _other_attempt, render_row_id = commit_attempt(runtime, intent="渲染带文本")
    render_action = lease_v1(client, "render")
    assert render_action["action_id"] == render_row_id
    rendered = client.post(
        f"/v1/outbox/{render_row_id}/result",
        json=report_body(
            render_action,
            status="ok",
            result={"text": TEXT, "authorize_unavailable": True},
        ),
    ).json()
    assert rendered["ok"] is True
    assert rendered["attempt_state"] == AttemptState.READY_TO_SEND.value
    assert rendered.get("requeued") is None
    assert runtime.projections.outbox.get(render_row_id).status == OutboxStatus.DELIVERED.value


def test_the_adapter_s_own_outage_report_is_accepted(client: TestClient, runtime: Runtime) -> None:
    """The marker is read from the adapter's real wire body, not a hand-made one.

    The plugin builds its reports with ``ActionReport.to_wire``; this pins the
    cross-repository contract between that body and the Runtime's answer, so a
    rename on either side fails here instead of silently dropping a proactive
    message in a deployment.
    """
    protocol = plugin_protocol()
    attempt_id, send_row_id = ready_to_send(client, runtime)
    action = lease_v1(client, "send")

    report = protocol.ActionReport(
        adapter_id=ADAPTER,
        action_id=send_row_id,
        lease_id=action["lease_id"],
        status=protocol.STATUS_FAILED,
        action_type="send",
        attempt_id=attempt_id,
        session=action["session"],
        result={"authorize_unavailable": True},
        error="authorize_unavailable:RuntimeError: runtime unreachable",
    )
    body = report.to_wire()
    body["action_id"] = send_row_id
    assert body["status"] == "failed"
    assert body["result"]["authorize_unavailable"] is True

    response = client.post(f"/v1/outbox/{send_row_id}/result", json=body)
    assert response.status_code == 200
    verdict = response.json()
    assert verdict["ok"] is True
    assert verdict["requeued"] is True
    assert runtime.projections.outbox.get(send_row_id).status == OutboxStatus.PENDING.value
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.READY_TO_SEND.value

    # The adapter's retry loop then works over the same real shapes.
    again = lease_v1(client, "send")
    assert again["action_id"] == send_row_id
    sent = protocol.ActionReport(
        adapter_id=ADAPTER,
        action_id=send_row_id,
        lease_id=again["lease_id"],
        status=protocol.STATUS_OK,
        action_type="send",
        attempt_id=attempt_id,
        session=again["session"],
        result={"sent": True, "chars": len(TEXT)},
    ).to_wire()
    sent["action_id"] = send_row_id
    delivered = client.post(f"/v1/outbox/{send_row_id}/result", json=sent).json()
    assert delivered["delivered"] is True
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    assert runtime.events.count(EventType.PROACTIVE_SENT.value) == 1


def plugin_report(
    protocol: Any,
    action: dict[str, Any],
    *,
    status: str,
    attempt_id: str,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a result body with the adapter's own wire dataclass."""
    report = protocol.ActionReport(
        adapter_id=ADAPTER,
        action_id=action["action_id"],
        lease_id=action["lease_id"],
        status=status,
        action_type=action["action_type"],
        attempt_id=attempt_id,
        session=action["session"],
        result=dict(result or {}),
        error=error,
    )
    body = report.to_wire()
    # ``ActionReport.to_wire`` carries the id in the body without the path param
    # the adapter adds in ``action_report_body``; the Runtime reads the path.
    body["action_id"] = action["action_id"]
    return body


#: ``(kind, status, result, error, expected row status, expected attempt state)``.
#:
#: This is the whole result contract in one place, expressed in the states the
#: Runtime must end up in rather than in the calls it makes. Only the row that
#: was reported is asserted for a render that succeeded: that path also queues
#: the send row, which the dedicated tests cover.
OUTCOME_TABLE: list[tuple[str, str, dict[str, Any], str, str, str]] = [
    # --- an outage is not a verdict: the claim goes back, the attempt stands.
    (
        "send",
        "failed",
        {"authorize_unavailable": True},
        "authorize_unavailable:RuntimeError: unreachable",
        OutboxStatus.PENDING.value,
        AttemptState.READY_TO_SEND.value,
    ),
    (
        "render",
        "failed",
        {"authorize_unavailable": True},
        "authorize_unavailable:RuntimeError: unreachable",
        OutboxStatus.PENDING.value,
        AttemptState.COMMITTED.value,
    ),
    # --- a real execution failure stays terminal, with or without the flag.
    (
        "send",
        "failed",
        {"sent": False, "reason": "platform_not_found"},
        "platform_not_found",
        OutboxStatus.FAILED.value,
        AttemptState.FAILED.value,
    ),
    (
        "send",
        "failed",
        {"authorize_unavailable": False, "reason": "platform_not_found"},
        "platform_not_found",
        OutboxStatus.FAILED.value,
        AttemptState.FAILED.value,
    ),
    (
        "send",
        "rejected",
        {"authorized": False},
        "boundary_blocks_proactive",
        OutboxStatus.FAILED.value,
        AttemptState.FAILED.value,
    ),
    (
        "send",
        "skipped",
        {},
        "no_session",
        OutboxStatus.FAILED.value,
        AttemptState.FAILED.value,
    ),
    (
        "send",
        "ok",
        {"sent": False, "reason": "delivery_failed"},
        "delivery_failed",
        OutboxStatus.FAILED.value,
        AttemptState.FAILED.value,
    ),
    (
        "render",
        "failed",
        {},
        "renderer_error:boom",
        OutboxStatus.FAILED.value,
        AttemptState.FAILED.value,
    ),
    (
        "render",
        "skipped",
        {},
        "missing_session",
        OutboxStatus.FAILED.value,
        AttemptState.FAILED.value,
    ),
    # --- the outcome really happened.
    (
        "send",
        "ok",
        {"sent": True, "chars": 7},
        "",
        OutboxStatus.DELIVERED.value,
        AttemptState.SENT.value,
    ),
    (
        "render",
        "ok",
        {"text": TEXT},
        "",
        OutboxStatus.DELIVERED.value,
        AttemptState.READY_TO_SEND.value,
    ),
]


@pytest.mark.parametrize(
    ("kind", "status", "result", "error", "row_status", "attempt_state"),
    OUTCOME_TABLE,
)
def test_result_contract_table(
    client: TestClient,
    runtime: Runtime,
    kind: str,
    status: str,
    result: dict[str, Any],
    error: str,
    row_status: str,
    attempt_state: str,
) -> None:
    """Every reported outcome lands in the documented row/attempt state.

    The bodies are built with the adapter's own ``ActionReport``, so a rename on
    either side of the wire fails here instead of silently changing what a
    deployment does with a proactive message.
    """
    protocol = plugin_protocol()
    if kind == "send":
        attempt_id, row_id = ready_to_send(client, runtime)
    else:
        attempt_id, row_id = commit_attempt(runtime)
    action = lease_v1(client, kind)
    assert action["action_id"] == row_id

    body = plugin_report(
        protocol, action, status=status, attempt_id=attempt_id, result=result, error=error
    )
    is_outage = bool(result.get("authorize_unavailable")) and status != "ok"

    before_version = runtime.state().version
    before_events = (
        runtime.events.count(EventType.PROACTIVE_SENT.value),
        runtime.events.count(EventType.PROACTIVE_ABORTED.value),
    )

    response = client.post(f"/v1/outbox/{row_id}/result", json=body)
    assert response.status_code == 200
    answer = response.json()
    assert answer["ok"] is True, answer

    row = runtime.projections.outbox.get(row_id)
    attempt = runtime.projections.attempts.get(attempt_id)
    assert row.status == row_status, (kind, status, result, row.status)
    assert attempt.state == attempt_state, (kind, status, result, attempt.state)

    if is_outage:
        # The row is retryable and the intention is untouched: nothing was
        # executed, no verdict was given, and no history was written for it.
        assert answer["retryable"] is True
        assert answer["requeued"] is True
        assert answer["duplicate"] is False
        assert attempt.failure_reason is None
        assert row.lease_owner is None
        assert runtime.state().version == before_version, "an outage writes no state"
        assert (
            runtime.events.count(EventType.PROACTIVE_SENT.value),
            runtime.events.count(EventType.PROACTIVE_ABORTED.value),
        ) == before_events, "an outage creates no outcome record"
    else:
        assert row.status != OutboxStatus.PENDING.value, "only an outage requeues"
        assert answer.get("requeued") is None
