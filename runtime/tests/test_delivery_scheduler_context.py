"""Tests for the delivery service, the scheduler and context assembly."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from companion_runtime import context as context_module
from companion_runtime import scheduler as scheduler_module
from companion_runtime.authorize import AuthorizeRequest, authorize, validate_text
from companion_runtime.config import RuntimeConfig
from companion_runtime.delivery import (
    DeliveryService,
    EchoRenderer,
    NullTransport,
    build_render_payload,
    is_sendable_state,
)
from companion_runtime.runtime import Runtime
from companion_runtime.typing import AttemptState, EventType, OutboxKind, OutboxStatus

from conftest import BASE_TIME, Harness, build_config


class RecordingRenderer:
    """Renderer that records its payloads and returns a scripted message."""

    def __init__(self, text: str = "面试怎么样啦？") -> None:
        """Store the scripted text."""
        self.text = text
        self.payloads: list[dict] = []

    def render(self, payload):
        """Record the payload and return the scripted text."""
        self.payloads.append(dict(payload))
        return self.text


class ExplodingRenderer:
    """Renderer that fails, to exercise the failure path."""

    def render(self, payload):
        """Raise unconditionally."""
        raise RuntimeError("main llm unavailable")


class EmptyRenderer:
    """Renderer that returns nothing, which must never be sent."""

    def render(self, payload):
        """Return an empty string."""
        return "   "


class RejectingTransport:
    """Transport that rejects every message."""

    def send(self, text, conversation_id):
        """Report a failure."""
        return {"ok": False, "error": "channel closed"}


class ExplodingTransport:
    """Transport that raises."""

    def send(self, text, conversation_id):
        """Raise unconditionally."""
        raise RuntimeError("network down")


# --------------------------------------------------------------------------------------
# render / send cycle
# --------------------------------------------------------------------------------------


def _queue_render(harness: Harness, *, intent: str = "询问面试结果"):
    """Commit an attempt and return its identifiers."""
    from companion_runtime.typing import CandidateIntent, new_id

    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=intent,
        goal="表达关心",
        sources=["unfinished:unf_1"],
    )
    with harness.runtime.db.transaction() as conn:
        harness.runtime.projections.candidates.upsert(conn, candidate)
        state = harness.runtime.projections.runtime.ensure()
        attempt_id, outbox_id = harness.runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=harness.start
        )
    return attempt_id, outbox_id, candidate


def test_render_then_send_completes_the_attempt(harness: Harness) -> None:
    """The render row produces a send row, which delivers and resolves."""
    attempt_id, _outbox_id, _candidate = _queue_render(harness)
    renderer = RecordingRenderer()
    harness.service._renderer = renderer

    first = harness.service.cycle(now=harness.start)
    assert first.rendered == 1
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.READY_TO_SEND.value
    assert attempt.rendered_text == "面试怎么样啦？"
    assert renderer.payloads and renderer.payloads[0]["intent"] == "询问面试结果"

    second = harness.service.cycle(now=harness.start + timedelta(seconds=1))
    assert second.sent == 1
    assert harness.transport.sent and harness.transport.sent[0]["text"] == "面试怎么样啦？"
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.SENT.value
    assert harness.runtime.projections.outbox.stats().get("delivered") == 2


def test_render_failure_fails_the_attempt(harness: Harness) -> None:
    """A broken renderer fails the attempt instead of leaving it in flight."""
    attempt_id, _outbox_id, _candidate = _queue_render(harness)
    harness.service._renderer = ExplodingRenderer()
    report = harness.service.cycle(now=harness.start)
    assert report.failed == 1
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.FAILED.value
    assert "renderer_error" in (attempt.failure_reason or "")


def test_empty_render_is_never_sent(harness: Harness) -> None:
    """An empty message cannot become a delivery."""
    attempt_id, _outbox_id, _candidate = _queue_render(harness)
    harness.service._renderer = EmptyRenderer()
    harness.service.cycle(now=harness.start)
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.FAILED.value
    assert attempt.rendered_text is None
    assert harness.transport.sent == []


def test_transport_rejection_requeues_the_send(harness: Harness) -> None:
    """A channel failure is retried rather than silently dropped."""
    attempt_id, _outbox_id, _candidate = _queue_render(harness)
    harness.service._renderer = RecordingRenderer()
    harness.service._transport = RejectingTransport()
    harness.service.cycle(now=harness.start)
    report = harness.service.cycle(now=harness.start + timedelta(seconds=1))
    assert report.failed == 1
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.READY_TO_SEND.value
    pending = harness.runtime.projections.outbox.list_items(status=OutboxStatus.PENDING.value)
    assert pending, "the send row must go back to the queue"


def test_transport_exception_is_recorded(harness: Harness) -> None:
    """An exception from the transport is caught and recorded."""
    attempt_id, _outbox_id, _candidate = _queue_render(harness)
    harness.service._renderer = RecordingRenderer()
    harness.service._transport = ExplodingTransport()
    harness.service.cycle(now=harness.start)
    report = harness.service.cycle(now=harness.start + timedelta(seconds=1))
    assert report.failed == 1
    assert harness.runtime.projections.attempts.get(attempt_id).state != AttemptState.SENT.value


def test_delivery_is_blocked_when_a_boundary_arrives_mid_flight(harness: Harness) -> None:
    """A boundary that arrives after rendering stops the send and withdraws it.

    The state is asserted exactly, not as "aborted *or* still ready to send": a
    boundary is a hard constraint that reaches the attempt at ingest (the
    foreground path re-coordinates everything that has not left the Runtime yet),
    so the only correct outcome is a withdrawn intention. Accepting
    ``ready_to_send`` here would also accept the defect this test exists to catch -
    a declared boundary being ignored and the message going out anyway.
    """
    attempt_id, _outbox_id, _candidate = _queue_render(harness)
    harness.service._renderer = RecordingRenderer()
    harness.service.cycle(now=harness.start)
    assert (
        harness.runtime.projections.attempts.get(attempt_id).state
        == AttemptState.READY_TO_SEND.value
    )

    # The user declares a boundary before the send row is claimed.
    harness.runtime.process_user_message(
        content="永远别联系我", timestamp=harness.start + timedelta(seconds=2)
    )
    report = harness.service.cycle(now=harness.start + timedelta(seconds=3))
    assert report.sent == 0
    assert harness.transport.sent == []
    # The user message re-coordinated the attempt, so the intention is withdrawn
    # and its row is stale, not failed: nothing about the delivery machinery broke.
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.ABORTED.value
    assert attempt.reconcile_action == "abort"


def test_delivery_skips_a_resolved_attempt(harness: Harness) -> None:
    """An attempt resolved while queued is skipped, not retried."""
    attempt_id, _outbox_id, _candidate = _queue_render(harness)
    harness.service._renderer = RecordingRenderer()
    harness.service.cycle(now=harness.start)

    event = harness.runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="面试过啦，结果是过了",
        timestamp=harness.start + timedelta(seconds=2),
    )
    harness.runtime.reducer.reconcile_attempt(
        attempt_id=attempt_id,
        new_events=[harness.runtime.events.get(event.event_id)],
        now=harness.start + timedelta(seconds=2),
    )
    report = harness.service.cycle(now=harness.start + timedelta(seconds=3))
    assert report.sent == 0
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.ABORTED.value


def test_pending_count_and_stats(harness: Harness) -> None:
    """Queue introspection helpers work."""
    _queue_render(harness)
    assert harness.service.pending_count() == 1
    assert harness.service.stats().get("pending") == 1


def test_echo_renderer_is_short_and_non_empty() -> None:
    """The degradation-Level-0 renderer always produces something sendable."""
    renderer = EchoRenderer()
    assert renderer.render({"intent": "询问面试结果"})
    assert renderer.render({})
    templated = EchoRenderer(template="{intent}")
    assert templated.render({"intent": "hi"}) == "hi"


def test_sendable_state_helper() -> None:
    """Only a rendered attempt may be sent."""
    assert is_sendable_state(AttemptState.READY_TO_SEND.value)
    assert not is_sendable_state(AttemptState.COMMITTED.value)
    assert not is_sendable_state(AttemptState.SENT.value)


def test_build_render_payload_merges_context() -> None:
    """The renderer receives the intent plus the ephemeral context."""
    from companion_runtime.typing import OutboxItem

    item = OutboxItem(
        outbox_id="obx_1",
        kind=OutboxKind.RENDER.value,
        payload={"attempt_id": "att_1", "intent": "hi"},
    )
    merged = build_render_payload(item, {"psychological": {"experience": "x"}})
    assert merged["intent"] == "hi"
    assert merged["runtime_context"]["psychological"]["experience"] == "x"


# --------------------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------------------


def test_authorize_permits_a_normal_proactive_action(runtime: Runtime) -> None:
    """With no boundary in force, proactive contact is allowed."""
    verdict = authorize(
        AuthorizeRequest(action="proactive_contact", is_proactive=True, now=BASE_TIME),
        projections=runtime.projections,
        config=runtime.config,
        now=BASE_TIME,
    )
    assert verdict.allowed is True
    assert verdict.reason == "permitted"


def test_authorize_denies_proactive_under_a_boundary(runtime: Runtime) -> None:
    """A hard boundary produces a denial with the blocking identifier."""
    runtime.process_user_message(content="永远别联系我", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))
    verdict = authorize(
        AuthorizeRequest(action="proactive_contact", is_proactive=True, now=BASE_TIME),
        projections=runtime.projections,
        config=runtime.config,
        state=runtime.state(),
        now=BASE_TIME,
    )
    assert verdict.allowed is False
    assert verdict.reason == "boundary_blocks_proactive"
    assert verdict.blocking_boundary_ids


def test_authorize_still_permits_replies_under_a_no_proactive_boundary(
    runtime: Runtime,
) -> None:
    """The user writing first re-opens the ability to answer."""
    runtime.process_user_message(content="今天不要主动联系我。", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))
    verdict = authorize(
        AuthorizeRequest(action="reply", is_proactive=False, now=BASE_TIME),
        projections=runtime.projections,
        config=runtime.config,
        state=runtime.state(),
        now=BASE_TIME,
    )
    assert verdict.allowed is True
    assert verdict.allow_reply is True


def test_authorize_denies_an_unrendered_attempt(runtime: Runtime) -> None:
    """An attempt that was never rendered cannot be sent."""
    from companion_runtime.typing import CandidateIntent, new_id

    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="contact",
        intent="hi",
        sources=["internal_approach_drive"],
    )
    with runtime.db.transaction() as conn:
        state = runtime.state()
        attempt_id, _ = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=BASE_TIME
        )
    verdict = authorize(
        AuthorizeRequest(action="send", attempt_id=attempt_id, is_proactive=True, now=BASE_TIME),
        projections=runtime.projections,
        config=runtime.config,
        now=BASE_TIME,
    )
    assert verdict.allowed is False
    assert verdict.reason == "attempt_not_rendered"


def test_authorize_reports_internal_state_leakage(runtime: Runtime) -> None:
    """Advisory validation catches prompt/state leakage in outgoing text."""
    verdict = authorize(
        AuthorizeRequest(
            action="send",
            text="my pressure = 0.8 and restraint = 0.3",
            is_proactive=True,
            now=BASE_TIME,
        ),
        projections=runtime.projections,
        config=runtime.config,
        now=BASE_TIME,
    )
    assert verdict.allowed is True
    assert any("internal_state_leak" in item for item in verdict.constraints)


def test_validate_text_flags_length_and_leakage() -> None:
    """The validator reports rather than silently rewriting."""
    assert validate_text("普通消息", config=RuntimeConfig()) == []
    assert "text_too_long" in validate_text("x" * 3000, config=RuntimeConfig())
    assert any(
        "valience" in item or "valence" in item
        for item in validate_text("valence", config=RuntimeConfig())
    )


def test_authorize_unknown_attempt(runtime: Runtime) -> None:
    """An unknown attempt is denied with a clear reason."""
    verdict = authorize(
        AuthorizeRequest(action="send", attempt_id="att_missing", now=BASE_TIME),
        projections=runtime.projections,
        config=runtime.config,
        now=BASE_TIME,
    )
    assert verdict.allowed is False
    assert verdict.reason == "unknown_attempt"


# --------------------------------------------------------------------------------------
# context assembly
# --------------------------------------------------------------------------------------


def test_context_bundle_is_marked_ephemeral(runtime: Runtime) -> None:
    """Invariant 10: the injected context can never become history."""
    runtime.process_user_message(content="面试有点紧张，明天下午面试", timestamp=BASE_TIME)
    bundle = context_module.build(runtime=runtime, now=BASE_TIME)
    assert bundle.ephemeral is True
    assert bundle.to_dict()["ephemeral"] is True
    context_module.assert_ephemeral(bundle)
    bundle.ephemeral = False
    with pytest.raises(AssertionError):
        context_module.assert_ephemeral(bundle)


def test_context_separates_facts_from_inferences(runtime: Runtime) -> None:
    """The working situation keeps observation and interpretation apart."""
    runtime.process_user_message(content="我今晚想自己待着", timestamp=BASE_TIME)
    situation = context_module.build_situation(runtime.projections, now=BASE_TIME)
    assert situation["facts"]
    assert all(fact.startswith("用户说：") for fact in situation["facts"])
    for inference in situation["inferences"]:
        assert 0.0 <= inference["confidence"] <= 1.0


def test_render_block_contains_the_documented_sections(runtime: Runtime) -> None:
    """The block has the documented sections, a priority note and a discard note.

    Patch v0.2 reframed the psychological section from "how this turn should feel"
    to "the long-term weather you carry in", so the block must also state its own
    subordination to the current user message.
    """
    runtime.process_user_message(content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME)
    bundle = context_module.build(runtime=runtime, now=BASE_TIME)
    block = context_module.render_block(bundle)
    assert context_module.SECTION_PSYCH in block
    assert context_module.SECTION_SITUATION in block
    assert context_module.SECTION_TIME in block
    assert context_module.PRIORITY_PREAMBLE in block
    assert "当前用户原话" in block
    assert "长期状态" in block
    assert "临时背景" in block


def test_render_block_never_contains_raw_numbers_for_emotion(runtime: Runtime) -> None:
    """Psychological context is language, not floats."""
    runtime.process_user_message(content="家里出事了，我很难受", timestamp=BASE_TIME)
    block = context_module.render_block(context_module.build(runtime=runtime, now=BASE_TIME))
    for needle in ("valence", "arousal", "mood_valence", "approach_impulse"):
        assert needle not in block


def test_time_context_reports_elapsed_hours(runtime: Runtime) -> None:
    """Time continuity is explicit, so the model never has to guess."""
    runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
    later = BASE_TIME + timedelta(hours=8)
    bundle = context_module.build(runtime=runtime, now=later)
    assert bundle.time_context["hours_since_last_user_message"] == pytest.approx(8.0, abs=0.01)


def test_intent_description_includes_the_lead_time(runtime: Runtime) -> None:
    """The "I was about to say something" cue is available without being forced."""
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
        state = runtime.state()
        attempt_id, _ = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=BASE_TIME
        )
    bundle = context_module.build(runtime=runtime, now=BASE_TIME + timedelta(seconds=3))
    assert bundle.intent is not None
    assert bundle.intent["committed_not_yet_sent"] is True
    assert bundle.intent["lead_seconds_before_user_message"] == pytest.approx(3.0, abs=0.01)
    assert "秒" in context_module.render_block(bundle)


def test_select_memories_returns_activation_pool(runtime: Runtime) -> None:
    """Only activated memories are injected, not the whole database.

    Asserting an empty list on an empty database proved nothing: ``select_memories``
    could have been deleted outright. So the pool is populated first - one episodic
    memory is put on the character's mind, another one is stored but never activated -
    and the assertion is that exactly the activated one comes back, from the
    activation source.
    """
    from datetime import timedelta

    from companion_runtime.typing import ActivatedMemory, Memory

    with runtime.db.transaction() as conn:
        runtime.projections.memory.upsert_memory(
            conn,
            Memory(
                memory_id="mem_on_mind",
                kind="episodic",
                summary="用户以前提过面试",
                importance=0.9,
                confidence=0.8,
                created_at=BASE_TIME - timedelta(days=3),
            ),
        )
        runtime.projections.memory.upsert_activation(
            conn, ActivatedMemory(memory_id="mem_on_mind", activation=0.7)
        )
        # Stored, but not in the working set: it must stay out of the prompt.
        runtime.projections.memory.upsert_memory(
            conn,
            Memory(
                memory_id="mem_cold",
                kind="episodic",
                summary="用户以前提过咖啡",
                importance=0.9,
                confidence=0.8,
                created_at=BASE_TIME - timedelta(days=3),
            ),
        )

    selected = context_module.select_memories(runtime.projections, limit=4)
    assert [item["memory_id"] for item in selected] == ["mem_on_mind"]
    assert selected[0]["selection"] == "activation"
    assert selected[0]["activation"] == pytest.approx(0.7, abs=0.001)


def test_build_context_is_json_serialisable(runtime: Runtime) -> None:
    """The bundle can be returned by the HTTP API."""
    import json

    runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
    json.dumps(context_module.build(runtime=runtime, now=BASE_TIME).to_dict())


# --------------------------------------------------------------------------------------
# scheduler
# --------------------------------------------------------------------------------------


def test_collect_signals_gathers_every_anchor(runtime: Runtime) -> None:
    """Every subsystem contributes its wake-up anchor."""
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    signals = scheduler_module.collect_signals(runtime=runtime, now=BASE_TIME)
    assert signals.unfinished_wake_at is not None
    assert signals.foreground_pause_until is not None
    assert signals.now == BASE_TIME


def test_plan_takes_the_earliest_anchor(runtime: Runtime) -> None:
    """``t_next = min(t_hazard, t_unfinished, t_boundary, t_cooldown, t_candidate)``."""
    runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    signals = scheduler_module.collect_signals(runtime=runtime, now=BASE_TIME)
    planned = scheduler_module.plan(signals, config=runtime.config)
    assert planned.next_wake_at > BASE_TIME
    assert planned.delay_seconds >= runtime.config.scheduler.min_interval_seconds
    assert planned.delay_seconds <= runtime.config.scheduler.max_interval_seconds
    # The foreground pause is the nearest anchor here.
    assert "foreground_pause" in planned.reasons or "unfinished" in planned.reasons


def test_plan_respects_the_configured_bounds(runtime: Runtime) -> None:
    """A very distant anchor is clamped to the maximum interval."""
    signals = scheduler_module.collect_signals(
        runtime=runtime, now=BASE_TIME, hazard_wake_at=BASE_TIME + timedelta(days=30)
    )
    planned = scheduler_module.plan(signals, config=runtime.config)
    assert planned.delay_seconds <= runtime.config.scheduler.max_interval_seconds


def test_plan_uses_a_boundary_anchor(runtime: Runtime) -> None:
    """A hard boundary expiry is a legitimate endogenous wake-up reason."""
    runtime.process_user_message(content="今天不要主动联系我。", timestamp=BASE_TIME)
    signals = scheduler_module.collect_signals(runtime=runtime, now=BASE_TIME)
    assert signals.boundary_wake_at is not None
    assert signals.boundary_proactive_allowed is False


def test_quiet_hours_suppress_dispatch(runtime: Runtime) -> None:
    """Quiet hours are honoured by the dispatch gate."""
    runtime.config.scheduler.quiet_hours_start = 0
    runtime.config.scheduler.quiet_hours_end = 23
    signals = scheduler_module.collect_signals(runtime=runtime, now=BASE_TIME)
    allowed, reason = scheduler_module.should_dispatch(
        signals=signals, attempt_states=[], config=runtime.config
    )
    assert allowed is False
    assert reason in {"quiet_hours", "foreground_pause"}


def test_dispatch_blocked_while_an_attempt_is_in_flight(runtime: Runtime) -> None:
    """The Runtime never starts a second outreach while one is in flight."""
    signals = scheduler_module.collect_signals(runtime=runtime, now=BASE_TIME)
    allowed, reason = scheduler_module.should_dispatch(
        signals=signals, attempt_states=[AttemptState.COMMITTED.value], config=runtime.config
    )
    assert allowed is False
    assert reason == "attempt_in_flight"


def test_dispatch_allowed_when_nothing_blocks(runtime: Runtime) -> None:
    """With no pause, no boundary and no in-flight attempt, dispatch is allowed."""
    later = BASE_TIME + timedelta(hours=2)
    runtime.lazy_tick(later)
    signals = scheduler_module.collect_signals(runtime=runtime, now=later)
    signals.foreground_pause_until = None
    signals.boundary_proactive_allowed = True
    allowed, reason = scheduler_module.should_dispatch(
        signals=signals, attempt_states=[], config=runtime.config
    )
    assert allowed is True
    assert reason == "allowed"


def test_summary_payload_is_serialisable(runtime: Runtime) -> None:
    """The schedule endpoint payload is JSON-safe."""
    import json

    signals = scheduler_module.collect_signals(runtime=runtime, now=BASE_TIME)
    planned = scheduler_module.plan(signals, config=runtime.config)
    json.dumps(scheduler_module.next_wake_summary(signals, planned))


def test_interval_jitter_spreads_wake_ups(runtime: Runtime) -> None:
    """Two instances do not beat in perfect sync."""
    signals = scheduler_module.collect_signals(runtime=runtime, now=BASE_TIME)
    a = scheduler_module.plan(signals, config=runtime.config, rng=random.Random(1))
    b = scheduler_module.plan(signals, config=runtime.config, rng=random.Random(2))
    assert a.delay_seconds != b.delay_seconds
