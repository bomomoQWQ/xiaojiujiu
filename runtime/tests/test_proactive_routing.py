"""A proactive message must be delivered to the conversation it was formed in.

This is the end-to-end version of a defect found while wiring the AstrBot plugin:
the outbox row used the *process default* conversation instead of the session the
intention came from. Everything passed in tests, but in a real deployment the
plugin received ``session="default"``, could not resolve it to a platform
address, and the unprompted message silently never arrived.

The fix derives the conversation from the candidate's own source events, so a
multi-session deployment routes each proactive action back to where it belongs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from companion_runtime.runtime import Runtime
from companion_runtime.typing import EventType

from conftest import BASE_TIME, build_config

#: Two distinct sessions, shaped like AstrBot's ``platform:type:id`` origins.
SESSION_A = "aiocqhttp:FriendMessage:10001"
SESSION_B = "aiocqhttp:GroupMessage:20002"


def _runtime(**overrides: Any) -> Runtime:
    """Build an in-memory Runtime on the simulated timeline.

    The RNG is seeded. These tests exercise *routing* - which session a leased
    action reports - and an endogenous round that refused to act is a missing
    precondition, not a routing result: the motivational layer draws its hazard
    from real entropy, so an unseeded Runtime makes the scenario decline to
    commit roughly once in 160 rounds and the test fails for a reason unrelated
    to its subject. The seed matches the one ``conftest`` uses.
    """
    config = build_config()
    config.semantic.deep_refresh_enabled = False
    config.conversation_id = "default"
    for key, value in overrides.items():
        setattr(config, key, value)
    return Runtime(config=config, seed=1234, created_at=BASE_TIME)


class TestProactiveRouting:
    """The outage-prone path: an unprompted message must find its session."""

    def test_the_committed_action_carries_the_originating_session(self) -> None:
        runtime = _runtime()
        try:
            runtime.process_user_message(
                content="明天下午面试，结束告诉你结果。",
                conversation_id=SESSION_A,
                timestamp=BASE_TIME,
            )
            matter = runtime.projections.unfinished.list_open()[0]
            outcome = runtime.endogenous_round(
                now=matter.waiting_until + timedelta(hours=1), force=True
            )
            assert outcome.attempt_id is not None, (
                f"the round should have committed: {outcome.decision}"
            )
            attempt = runtime.projections.attempts.get(outcome.attempt_id)
            assert attempt is not None and attempt.outbox_id
            item = runtime.projections.outbox.get(attempt.outbox_id)
            assert item is not None
            assert item.conversation_id == SESSION_A
        finally:
            runtime.close()

    def test_two_sessions_keep_their_own_outbound_targets(self) -> None:
        """Each session's intention must route back to that session, not to one."""
        runtime = _runtime()
        try:
            runtime.process_user_message(
                content="明天下午面试，结束告诉你结果。",
                conversation_id=SESSION_A,
                timestamp=BASE_TIME,
            )
            runtime.process_user_message(
                content="我下周要考试，考完跟你说。",
                conversation_id=SESSION_B,
                timestamp=BASE_TIME + timedelta(minutes=1),
            )
            matters = runtime.projections.unfinished.list_open()
            assert len(matters) == 2, [matter.title for matter in matters]
            deadlines = [matter.waiting_until for matter in matters if matter.waiting_until]
            due = (max(deadlines) if deadlines else BASE_TIME) + timedelta(hours=1)
            runtime.endogenous_round(now=due, force=True)

            routed = set()
            for attempt in runtime.projections.attempts.list_all(limit=20):
                if not attempt.outbox_id:
                    continue
                item = runtime.projections.outbox.get(attempt.outbox_id)
                if item is not None:
                    routed.add(item.conversation_id)
            assert routed, "at least one proactive action should be queued"
            assert routed <= {SESSION_A, SESSION_B}
            assert "default" not in routed, "the process default must never be used"
        finally:
            runtime.close()

    def test_the_commit_event_is_recorded_in_the_right_conversation(self) -> None:
        """The immutable history must agree with where the action was sent."""
        runtime = _runtime()
        try:
            runtime.process_user_message(
                content="明天下午面试，结束告诉你结果。",
                conversation_id=SESSION_A,
                timestamp=BASE_TIME,
            )
            matter = runtime.projections.unfinished.list_open()[0]
            runtime.endogenous_round(now=matter.waiting_until + timedelta(hours=1), force=True)
            events = runtime.events.read(
                __import__("companion_runtime.eventlog", fromlist=["EventQuery"]).EventQuery(
                    event_types=[EventType.PROACTIVE_COMMITTED.value], limit=5
                )
            )
            assert events
            assert events[0].conversation_id == SESSION_A
        finally:
            runtime.close()

    def test_it_falls_back_to_the_configured_default(self) -> None:
        """A candidate with no resolvable sources must not break the commit."""
        runtime = _runtime()
        try:
            runtime.process_user_message(
                content="在吗",
                conversation_id="",
                timestamp=BASE_TIME,
            )
            # No candidate is expected here; the point is that a commit with an
            # unusable conversation id degrades to the default instead of raising.
            outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(hours=2), force=True)
            assert outcome is not None
            assert runtime.config.conversation_id == "default"
        finally:
            runtime.close()

    def test_an_event_without_a_conversation_uses_the_default(self) -> None:
        from companion_runtime.typing import CandidateIntent

        runtime = _runtime()
        try:
            candidate = CandidateIntent(
                candidate_id="cnd_orphan",
                type="contact",
                goal="g",
                intent="i",
                sources=[],
            )
            assert runtime._conversation_for(candidate, now=BASE_TIME) == "default"
        finally:
            runtime.close()

    def test_a_dangling_source_id_does_not_break_the_commit(self) -> None:
        """Grounding normally prevents this; a bad id must still not raise."""
        from companion_runtime.typing import CandidateIntent

        runtime = _runtime()
        try:
            candidate = CandidateIntent(
                candidate_id="cnd_dangling",
                type="contact",
                goal="g",
                intent="i",
                sources=["evt_does_not_exist"],
            )
            assert runtime._conversation_for(candidate, now=BASE_TIME) == "default"
        finally:
            runtime.close()


class TestV1LeaseReportsTheSession:
    """The adapter learns the target session only from the lease response."""

    def test_the_lease_payload_exposes_the_right_session(self, monkeypatch) -> None:
        """The leased render action names the session the intention came from.

        The lease endpoint is driven by the *machine* clock (it ticks and claims
        with ``utcnow()``), while everything this scenario sets up lives on the
        simulated timeline: the committed row carries ``available_at`` equal to the
        round's instant, ``matter.waiting_until + 1h`` - about ``2026-03-02T11:00Z``.
        A claim only sees rows whose ``available_at`` has passed, so as written this
        test silently required the machine clock to be *after* that instant: on a
        clock just before it the lease came back empty (the reported "no action
        after the endogenous round") and on a clock just after it the suite looked
        healthy. Pinning the endpoint's clock to the simulated timeline removes the
        dependency on when or where the suite runs, and keeps the real v1 handler
        under test.
        """
        from fastapi.testclient import TestClient

        from companion_runtime import api_v1
        from companion_runtime.api import create_app

        runtime = _runtime()
        try:
            client = TestClient(create_app(runtime, runtime.config))
            client.post(
                "/v1/events",
                json={
                    "protocol_version": "1",
                    "adapter_id": "test-adapter",
                    "events": [
                        {
                            "event_id": "evt_route_1",
                            "kind": "user_message",
                            "session": SESSION_A,
                            "text": "明天下午面试，结束告诉你结果。",
                            "occurred_at": BASE_TIME.isoformat(),
                        }
                    ],
                },
            )
            matter = runtime.projections.unfinished.list_open()[0]
            round_at = matter.waiting_until + timedelta(hours=1)
            runtime.endogenous_round(now=round_at, force=True)

            # One minute after the round committed, on the simulated timeline.
            monkeypatch.setattr(api_v1, "utcnow", lambda: round_at + timedelta(minutes=1))
            leased = client.post(
                "/v1/outbox/lease",
                json={
                    "protocol_version": "1",
                    "adapter_id": "test-adapter",
                    "capabilities": ["render"],
                    "max_actions": 1,
                },
            ).json()
            items = leased.get("items") or []
            assert items, "a render action should be leasable"
            assert items[0]["session"] == SESSION_A
        finally:
            runtime.close()

    @pytest.mark.parametrize("session", [SESSION_A, SESSION_B])
    def test_each_session_round_trips(self, session: str) -> None:
        runtime = _runtime()
        try:
            runtime.process_user_message(
                content="明天下午面试，结束告诉你结果。",
                conversation_id=session,
                timestamp=BASE_TIME,
            )
            matter = runtime.projections.unfinished.list_open()[0]
            outcome = runtime.endogenous_round(
                now=matter.waiting_until + timedelta(hours=1), force=True
            )
            committed = [
                item
                for item in runtime.projections.attempts.list_all(limit=20)
                if item.outbox_id
            ]
            # The precondition is asserted explicitly, with the decision attached:
            # a bare ``StopIteration`` here says "no attempt" without saying whether
            # the round declined, found no candidate or never ran at all.
            assert committed, f"the round must commit an intention: {outcome.decision}"
            attempt = committed[0]
            item = runtime.projections.outbox.get(attempt.outbox_id)
            assert item is not None and item.conversation_id == session
        finally:
            runtime.close()
