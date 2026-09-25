"""The render prompt must know what she just said (2026-09-25).

A proactive render is a one-shot ``llm_generate`` with no transcript, so the model cannot
look up its own last lines. Measured on the beta: a render 45 seconds after a reply repeated
the same two questions (「几点回」/「外套穿厚的」), and that decision's own sheet showed
``repeat_cost = 0.0`` - the count-level penalty never fires at her cadence, and the
material-level guard (``memory_callback_cooldown_hours``) only covers memory openers. The
case in between had no mechanism at all.

These tests pin both halves: which lines the Runtime collects (proactive sends *and* the
host-reported replies, never the user's own words) and that the render prompt carries them.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from companion_runtime.api_v1 import RENDER_ALREADY_SAID_HEADER, _render_payload
from companion_runtime.runtime import Runtime
from companion_runtime.typing import AttemptState, CandidateIntent, OutboxKind, new_id

from conftest import BASE_TIME

SESSION = "webchat:FriendMessage:user-1"
PROACTIVE_LINE = "几点回，跟谁。"
REPLY_LINE = "外套穿厚的，今天降温。"
USER_LINE = "我今晚不回来了"


@pytest.fixture()
def client(runtime: Runtime):
    """A TestClient bound to the same Runtime, with the v1 router included.

    ``test_api_v1`` keeps the same fixture locally; it is redefined here so this file does
    not depend on another test module's internals.
    """
    from companion_runtime.api import create_app

    app = create_app(runtime, runtime.config)
    with TestClient(app) as test_client:
        yield test_client


def _delivered(runtime: Runtime, text: str) -> str:
    """Commit, render and deliver one proactive message with ``text``; return attempt id."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="询问今晚安排",
        goal="想知道他在哪",
        sources=["unfinished:unf_repeat"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        attempt_id, _outbox_id = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=BASE_TIME
        )
    render_row = [
        item
        for item in runtime.projections.outbox.list_items(status=None, limit=50)
        if item.kind == OutboxKind.RENDER.value
    ][0]
    runtime.reducer.complete_render(outbox_id=render_row.outbox_id, text=text, now=BASE_TIME)
    send_row = [
        item
        for item in runtime.projections.outbox.list_items(status=None, limit=50)
        if item.kind == OutboxKind.SEND.value
    ][0]
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1, kinds=["send"])
    runtime.reducer.mark_delivered(outbox_id=send_row.outbox_id, now=BASE_TIME)
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    return attempt_id


def _report(client: TestClient, *, kind: str, text: str, event_id: str) -> None:
    """Report one event the way the adapter does."""
    response = client.post(
        "/v1/events",
        json={
            "protocol_version": "1",
            "adapter_id": "test-adapter",
            "sent_at": BASE_TIME.isoformat(),
            "events": [
                {
                    "event_id": event_id,
                    "kind": kind,
                    "session": SESSION,
                    "text": text,
                    "occurred_at": BASE_TIME.isoformat(),
                    "platform": "webchat",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def _reply(client: TestClient, text: str, event_id: str) -> None:
    _report(client, kind="assistant_message", text=text, event_id=event_id)


def test_both_halves_of_what_she_said_are_collected(
    client: TestClient, runtime: Runtime
) -> None:
    """Proactive lines come from sent attempts, replies from assistant_message events."""
    _delivered(runtime, PROACTIVE_LINE)
    _reply(client, REPLY_LINE, "evt_reply")
    _report(client, kind="user_message", text=USER_LINE, event_id="evt_user")

    lines = runtime.recent_outgoing_lines()

    assert PROACTIVE_LINE in lines
    assert REPLY_LINE in lines
    assert USER_LINE not in lines, "用户的话不是她说过的话"


def test_nothing_collected_before_she_has_spoken(runtime: Runtime) -> None:
    assert runtime.recent_outgoing_lines() == []


def test_the_list_is_newest_last_and_bounded(client: TestClient, runtime: Runtime) -> None:
    for index in range(8):
        _reply(client, "第%d句" % index, "evt_%d" % index)
    lines = runtime.recent_outgoing_lines(limit=3)
    assert lines == ["第5句", "第6句", "第7句"]


def test_duplicates_are_collapsed(client: TestClient, runtime: Runtime) -> None:
    _reply(client, REPLY_LINE, "evt_a")
    _reply(client, REPLY_LINE, "evt_b")
    assert runtime.recent_outgoing_lines().count(REPLY_LINE) == 1


def test_limit_one_keeps_only_the_newest(client: TestClient, runtime: Runtime) -> None:
    _reply(client, "早", "evt_early")
    _reply(client, "晚", "evt_late")
    assert runtime.recent_outgoing_lines(limit=1) == ["晚"]


def _commit(runtime: Runtime) -> Any:
    """Commit one proactive attempt and return its (unrendered) render outbox row."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="问一句",
        goal="关心",
        sources=["unfinished:unf_x"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        runtime._commit_attempt(conn, chosen=candidate, state=state, now=BASE_TIME)
    return [
        item
        for item in runtime.projections.outbox.list_items(status=None, limit=50)
        if item.kind == OutboxKind.RENDER.value
    ][-1]


def _prompt_for(runtime: Runtime) -> str:
    row = _commit(runtime)
    built = _render_payload(runtime, runtime.config, row, dict(row.payload or {}), BASE_TIME)
    return str(built["prompt"])


def _already_said_block(prompt: str) -> list[str]:
    """Just the lines of the 「别再说一遍」 block.

    The prompt legitimately quotes the user's own words elsewhere (context, the message being
    followed up), so asserting on the whole prompt cannot tell whether the *block* leaked them.
    """
    lines = prompt.splitlines()
    if RENDER_ALREADY_SAID_HEADER not in lines:
        return []
    block: list[str] = []
    for line in lines[lines.index(RENDER_ALREADY_SAID_HEADER) + 1 :]:
        if not line.startswith("  · "):
            break
        block.append(line[len("  · ") :])
    return block


def test_the_render_prompt_carries_her_recent_lines(
    client: TestClient, runtime: Runtime
) -> None:
    """This is the fix: a render is told what it must not say again."""
    _reply(client, REPLY_LINE, "evt_reply")
    _report(client, kind="user_message", text=USER_LINE, event_id="evt_user")
    _delivered(runtime, PROACTIVE_LINE)

    block = _already_said_block(_prompt_for(runtime))

    assert REPLY_LINE in block, "host 报上来的回复也算她说过的话"
    assert PROACTIVE_LINE in block
    assert USER_LINE not in block, "用户的话不是她说过的话"
    assert len(block) == 2, "只列她说过的：一条回复 + 一条主动"


def test_the_prompt_has_no_block_when_she_has_not_spoken(runtime: Runtime) -> None:
    assert _already_said_block(_prompt_for(runtime)) == []
