"""Acting-layer independence: the ingest path must never need a generative model.

Architecture patch v0.2 splits the system into two time scales and states the
consequence plainly: the host main LLM performs the current turn, so the Runtime
must not block that turn on a second semantic model.

These tests pin that property as a *structural* guarantee rather than a timing
hope. They are the regression net for the failure mode this patch exists to fix:
some future change quietly reintroducing a model call into
``process_user_message`` and making the whole system depend on it again.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest import mock

from companion_runtime.runtime import Runtime

from conftest import BASE_TIME, build_config

#: Explicitly settleable events, one per coarse category the rule table covers.
SETTLEABLE = [
    ("谢谢你，我今天真的被你安慰到了。", "+"),
    ("面试过啦！", "+"),
    ("我今天很难过。", "-"),
    ("家里出事了，我很难受。", "-"),
    ("我今晚想自己待着", "-"),
]

#: The canonical ambiguous event from patch v0.2 section 11.
AMBIGUOUS = [
    "算了，也没什么。",
    "随便吧，都行。",
    "可能吧，我也不知道。",
    "还好啦。",
]


class TestIngestNeverCallsAModel:
    """No provider, no network, no exceptions - the ingest path is self-contained."""

    def test_ingest_works_with_no_provider_configured(self) -> None:
        runtime = Runtime(config=build_config())
        try:
            for text, _direction in SETTLEABLE:
                outcome = runtime.process_user_message(content=text, timestamp=BASE_TIME)
                assert outcome.semantic_status == "resolved"
                assert outcome.appraisal_source == "coarse_rule"
        finally:
            runtime.close()

    def test_no_outbound_socket_is_opened_during_ingest(self) -> None:
        """A hard proof, not a timing measurement: nothing may dial out."""
        runtime = Runtime(config=build_config())
        try:
            with mock.patch("socket.socket.connect", side_effect=AssertionError("network call")):
                for text, _direction in SETTLEABLE + [(t, "0") for t in AMBIGUOUS]:
                    runtime.process_user_message(content=text, timestamp=BASE_TIME)
        finally:
            runtime.close()

    def test_ambiguous_events_are_deferred_not_guessed(self) -> None:
        runtime = Runtime(config=build_config())
        try:
            for text in AMBIGUOUS:
                outcome = runtime.process_user_message(content=text, timestamp=BASE_TIME)
                assert outcome.semantic_status == "unresolved", text
                assert outcome.appraisal_source == "deferred", text
                # Deferring must not manufacture an emotional after-effect.
                assert outcome.emotion_event_ids == [], text
        finally:
            runtime.close()

    def test_raw_event_survives_being_unresolved(self) -> None:
        """The whole point of deferring: the evidence is never lost."""
        runtime = Runtime(config=build_config())
        try:
            outcome = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            stored = runtime.events.get(outcome.event.event_id)
            assert stored is not None
            assert stored.content == "算了，也没什么。"
            unresolved = runtime.projections.semantics.list_unresolved()
            assert [item["event_id"] for item in unresolved] == [outcome.event.event_id]
        finally:
            runtime.close()

    def test_unresolved_events_accumulate_for_a_later_refresh(self) -> None:
        runtime = Runtime(config=build_config())
        try:
            for index, text in enumerate(AMBIGUOUS):
                runtime.process_user_message(
                    content=text, timestamp=BASE_TIME + timedelta(minutes=index)
                )
            assert runtime.projections.semantics.unresolved_count() == len(AMBIGUOUS)
            stats = runtime.projections.semantics.stats()
            assert stats["unresolved"] == len(AMBIGUOUS)
            assert set(stats["by_relevance"]) <= {"low", "medium", "high"}
        finally:
            runtime.close()

    def test_settlement_can_be_switched_off_entirely(self) -> None:
        """With settlement off, every event defers - still no model is needed."""
        config = build_config()
        config.semantic.settle_on_ingest = False
        runtime = Runtime(config=config)
        try:
            outcome = runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            assert outcome.semantic_status == "unresolved"
        finally:
            runtime.close()


class TestSettlementIsCoarse:
    """A settlement must not claim precision the rules cannot justify."""

    def test_direction_matches_the_anchor(self) -> None:
        runtime = Runtime(config=build_config())
        try:
            for text, direction in SETTLEABLE:
                outcome = runtime.process_user_message(content=text, timestamp=BASE_TIME)
                record = runtime.projections.semantics.get(outcome.event.event_id)
                assert record is not None
                assert record["direction"] == direction, text
                assert record["intensity_band"] in {"negligible", "low", "medium", "medium_high", "high"}
                # The rule layer never names an emotion; that is the deep refresh's job.
                assert record["settlement_source"]
        finally:
            runtime.close()

    def test_empty_and_non_conversational_events_defer(self) -> None:
        from companion_runtime.semantic import classify_event

        assert classify_event("   ") is None
        assert classify_event("谢谢你", event_type="tool_result") is None
        assert classify_event("谢谢你", actor="system") is None


class TestDeferralIsRecordedHonestly:
    """Operators must be able to see how much the Runtime has left unread."""

    def test_health_reports_the_unresolved_backlog(self) -> None:
        from fastapi.testclient import TestClient

        from companion_runtime.api import create_app

        runtime = Runtime(config=build_config())
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            client = TestClient(create_app(runtime, runtime.config))
            body = client.get("/health").json()
            assert body["semantics"]["unresolved"] == 1
        finally:
            runtime.close()


class TestTwoTimeScaleContract:
    """The bundle handed to the main LLM is background, not a scripted reaction."""

    def test_context_block_is_marked_as_background(self) -> None:
        from companion_runtime import context as context_module

        runtime = Runtime(config=build_config())
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            block = context_module.render_block(bundle)
            assert "背景" in block
            # The block must state its own subordination to the current turn.
            assert "不是这一句该怎么回" in block
        finally:
            runtime.close()

    def test_unresolved_events_do_not_leak_into_the_block_as_facts(self) -> None:
        """The deferral must be visible as a deferral, not as an interpretation.

        The old second assertion scanned the inference rows for the literal "失望",
        which no situation row can ever contain: after a deferred message there is no
        inference row at all, and the only inference writers store
        ``关系信号：<signal>``. It therefore stayed green even if the deferral path
        started writing inferences. The consequences are asserted directly, with a
        settled control so the assertions cannot pass because nothing is ever written.
        """
        from companion_runtime import context as context_module

        runtime = Runtime(config=build_config())
        try:
            outcome = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            facts = " ".join(bundle.situation.get("facts", []))
            assert "算了" in facts, "the raw fact is still recorded"
            # ...but nothing was interpreted: no inference row, no emotion, no
            # emotional after-effect.
            assert (
                [
                    item
                    for item in runtime.projections.situation.list_active()
                    if item["kind"] == "inference"
                ]
                == []
            ), "a deferred event must not be presented as an understood state"
            assert runtime.projections.emotion.list_active() == []
            assert outcome.emotion_event_ids == []

            # Control: the settled path writes both, so the assertions above are not
            # passing because these projections are simply never written.
            settled_relation = runtime.process_user_message(
                content="我今晚想自己待着", timestamp=BASE_TIME + timedelta(minutes=1)
            )
            assert [
                item
                for item in runtime.projections.situation.list_active()
                if item["kind"] == "inference"
            ], "a settled relation signal must produce an inference"
            settled_distress = runtime.process_user_message(
                content="我今天很难过。", timestamp=BASE_TIME + timedelta(minutes=2)
            )
            assert settled_relation.event.event_id != settled_distress.event.event_id
            assert runtime.projections.emotion.list_active(), (
                "a settled distress event must produce an emotion"
            )
        finally:
            runtime.close()


def _unused(_: Any) -> None:
    """Keep the datetime import meaningful for type checkers."""
    assert isinstance(BASE_TIME, datetime)
