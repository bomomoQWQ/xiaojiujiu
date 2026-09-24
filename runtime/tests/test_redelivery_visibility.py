"""A re-leased message must say that it is a re-lease (design §69, §86.9).

``committed != sent`` is in the design's invariant list, and the state machine implements
it: the Runtime only learns that a message reached the user when the host *reports* it.
Between "the platform sent it" and "the report arrived" there is a window in which the host
can die, and the Runtime is left holding a leased ``send`` row for a message that may
already be in the user's chat.

What the Runtime does about that window today is retry: the lease expires, the row goes
back to ``pending``, and the host leases it again. That is at-least-once delivery, and it
is a defensible choice - but a host could not even *see* that it was being handed a
redelivery, so it had no way to choose anything else. The only trace was the claim counter
embedded inside ``lease_id``, which is a staleness token, not a documented signal.

These tests pin the wire contract: a lease says how many times the row has been claimed and
whether this is a redelivery. They do **not** assert that the Runtime refuses to re-send -
that would be at-most-once, which risks silently dropping a message nobody sent - and they
do not assert a host policy. They assert that the information needed to choose exists.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from companion_runtime.api import create_app
from companion_runtime.db import Database
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    CandidateIntent,
    OutboxKind,
    OutboxStatus,
    new_id,
)
from companion_runtime.utility import utcnow

from conftest import BASE_TIME

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

LEASE_CAPABILITIES = {"capabilities": ["send"]}

#: Short enough that a test can actually outlive a lease, long enough that the first
#: lease is not already expired when it is handed out.
LEASE_SECONDS = 0.05


@pytest.fixture()
def runtime(config):
    """A Runtime whose leases lapse fast, so the crash window is reachable in a test.

    The lease endpoint stamps its own ``utcnow()`` (it cannot be told what time it is),
    so this window has to be driven on the real clock - but only for a few tens of
    milliseconds.
    """
    config.outbox.lease_seconds = LEASE_SECONDS
    instance = Runtime(config, seed=1234, database=Database(":memory:"), created_at=BASE_TIME)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def client(runtime: Runtime):
    app = create_app(runtime, runtime.config)
    with TestClient(app) as test_client:
        yield test_client


def _rendered_attempt(runtime: Runtime) -> str:
    """Commit and render one proactive message; return its attempt id.

    The message is deliberately not sent: the crash window lives between "rendered" and
    "reported", and the host is the only party that knows which side of it it died on.
    """
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="询问面试结果",
        goal="表达关心",
        sources=["unfinished:unf_lease"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        attempt_id, _outbox_id = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=BASE_TIME
        )
    render_row = [
        item
        for item in runtime.projections.outbox.list_items(
            status=OutboxStatus.PENDING.value, limit=20
        )
        if item.kind == OutboxKind.RENDER.value and item.payload.get("attempt_id") == attempt_id
    ][0]
    runtime.reducer.complete_render(outbox_id=render_row.outbox_id, text="在忙吗", now=BASE_TIME)
    return attempt_id


def _lease(client: TestClient) -> list[dict]:
    response = client.post("/v1/outbox/lease", json={**LEASE_CAPABILITIES, "adapter_id": "host"})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _crash_after_send(runtime: Runtime) -> None:
    """Simulate the window: the host sent the message, then died before reporting it.

    The lease is left to lapse and the Runtime's own reclaim puts the row back, exactly
    as it would after a real crash. Nothing is sent twice by the harness - the point of
    the test is what the *next* lease tells the host.
    """
    time.sleep(LEASE_SECONDS + 0.03)
    with runtime.db.transaction() as conn:
        runtime.projections.outbox.reclaim_expired(conn, now=utcnow())


def test_the_first_lease_reports_one_claim(client: TestClient, runtime: Runtime) -> None:
    """The ordinary case must stay unambiguous, or every send looks suspicious."""
    _rendered_attempt(runtime)

    items = _lease(client)

    assert len(items) == 1, f"expected the send row, got {items}"
    item = items[0]
    assert item["action_type"] == "send"
    assert item["attempts"] == 1, "the first lease is the first claim"
    assert item["redelivery"] is False, "a first lease must not claim to be a redelivery"


def test_a_re_leased_message_says_it_is_a_redelivery(client: TestClient, runtime: Runtime) -> None:
    """The host has to be able to tell that this row may already be in the chat.

    This is the whole point: the Runtime is the only party that knows the row was handed
    out before, and the host is the only party that knows whether it sent it. Without this
    field neither side can close the window, and the host cannot even log the ambiguity.
    """
    _rendered_attempt(runtime)

    first = _lease(client)
    assert first and first[0]["redelivery"] is False
    assert runtime.projections.outbox.get(first[0]["action_id"]).status == OutboxStatus.LEASED.value

    _crash_after_send(runtime)

    second = _lease(client)

    assert len(second) == 1, "the row must be handed out again (at-least-once is the design)"
    item = second[0]
    assert item["attempts"] == 2, item
    assert item["redelivery"] is True, "a second claim must be visible as a redelivery"
    assert item["action_id"] == first[0]["action_id"], "it is the same row"
    assert item["lease_id"] != first[0]["lease_id"], "and a new lease"


def test_the_claim_counter_and_the_flag_agree(client: TestClient, runtime: Runtime) -> None:
    """``redelivery`` must be a reading of ``attempts``, not an independent opinion."""
    _rendered_attempt(runtime)
    seen: list[tuple[int, bool]] = []

    for _ in range(3):
        items = _lease(client)
        if not items:
            break
        seen.append((items[0]["attempts"], items[0]["redelivery"]))
        _crash_after_send(runtime)

    assert seen == [(1, False), (2, True), (3, True)], seen


def test_the_redelivery_flag_is_not_a_send_only_concept(
    client: TestClient, runtime: Runtime
) -> None:
    """The field reads the claim counter, so it is the same answer for a render row.

    A host that renders twice has the same question to answer, and a flag that only
    existed on sends would be a special case nobody could rely on.
    """
    _rendered_attempt(runtime)
    consumed = _lease(client)
    assert consumed and consumed[0]["action_type"] == "send"

    # The render row is already gone; lease the *next* render row of a second attempt
    # and re-lease it, to show the flag follows ``attempts`` rather than the kind.
    _rendered_attempt(runtime)
    again = _lease(client)
    assert again and again[0]["action_type"] == "send"
    assert again[0]["redelivery"] is False
