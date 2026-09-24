"""Focused tests for the Runtime lifecycle fixes.

Six defects are covered here, each with the failure it used to cause:

1. the scheduler was never wired into ``companion-runtime serve``, so a standard
   deployment could only ever answer and never initiate;
2. an action attempt stayed in flight forever when its outbox row exhausted its
   attempts or was failed terminally, which also blocked every later round;
3. a user message did not re-coordinate an intention that had not been delivered;
4. a normal reply was neither attributed to the message it answered (the wrong,
   oldest attempt was picked) nor resolved, and could be folded into the user
   model more than once;
5. the daily contact counter was charged twice per delivered message and charged
   a new day's first message against yesterday's total;
6. a delayed timestamp moved the time anchors backwards.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_runtime import action as action_module
from companion_runtime import cli as cli_module
from companion_runtime import scheduler as scheduler_module
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    AttemptState,
    CandidateIntent,
    OutboxStatus,
    new_id,
)

from conftest import BASE_TIME, Harness, build_config


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _queue_render(
    harness: Harness, *, intent: str = "询问面试结果", now: datetime | None = None
) -> tuple[str, str]:
    """Commit an intention and return ``(attempt_id, render_outbox_id)``."""
    stamp = now or harness.start
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=intent,
        goal="表达关心",
        sources=["internal_approach_drive"],
    )
    with harness.runtime.db.transaction() as conn:
        harness.runtime.projections.candidates.upsert(conn, candidate)
        state = harness.runtime.projections.runtime.ensure()
        attempt_id, outbox_id = harness.runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=stamp
        )
    return attempt_id, outbox_id


def _deliver(harness: Harness, *, intent: str = "询问面试结果", now: datetime | None = None) -> str:
    """Commit, render and deliver one message; return the attempt identifier."""
    stamp = now or harness.start
    attempt_id, _render_id = _queue_render(harness, intent=intent, now=stamp)
    harness.service.cycle(now=stamp + timedelta(seconds=1))
    report = harness.service.cycle(now=stamp + timedelta(seconds=2))
    assert report.sent == 1, report.to_dict()
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    return attempt_id


def _claim(harness: Harness, *, now: datetime | None = None, owner: str = "test-worker"):
    """Claim the next outbox row and return it."""
    items = harness.runtime.reducer.claim_outbox(owner=owner, now=now or harness.start, limit=1)
    assert items, "expected a claimable outbox row"
    return items[0]


# --------------------------------------------------------------------------------------
# 6. time anchors never move backwards
# --------------------------------------------------------------------------------------


def test_lazy_tick_keeps_the_clock_monotone(runtime: Runtime) -> None:
    """A delayed tick must not undo a tick that already happened."""
    later = BASE_TIME + timedelta(hours=10)
    runtime.lazy_tick(later)
    assert runtime.state().last_tick_at == later

    report = runtime.lazy_tick(BASE_TIME + timedelta(hours=1))

    assert report.dt_seconds == 0.0
    assert report.changed is False
    assert runtime.state().last_tick_at == later


def test_a_delayed_message_cannot_move_the_exchange_anchors_backwards(
    runtime: Runtime,
) -> None:
    """The absence term reads these anchors, so they only ever advance."""
    newest = BASE_TIME + timedelta(hours=5)
    runtime.process_user_message(content="刚到家", timestamp=newest)
    before = runtime.state()
    pause = before.foreground_pause_until

    runtime.process_user_message(
        content="这条消息迟到了", timestamp=BASE_TIME + timedelta(hours=1)
    )
    after = runtime.state()

    assert after.last_user_message_at == newest
    assert after.last_exchange_at == newest
    assert after.foreground_pause_until == pause


def test_a_delayed_delivery_report_cannot_shorten_the_cooldown(harness: Harness) -> None:
    """A late report about an older moment must not rewind the contact anchors."""
    _deliver(harness, now=harness.start)

    later = harness.start + timedelta(hours=5)
    attempt_id, _render_id = _queue_render(harness, intent="问候近况", now=later)
    harness.service.cycle(now=later + timedelta(seconds=1))
    row = harness.runtime.projections.attempts.get(attempt_id).outbox_id
    claimed = harness.runtime.reducer.claim_outbox(
        owner="late-worker", now=later + timedelta(seconds=2), limit=1
    )
    assert claimed and claimed[0].outbox_id == row
    before = harness.runtime.state()
    anchor = before.last_contact_at
    cooldown = before.cooldown_until
    assert anchor is not None and cooldown is not None

    # The report arrives late, carrying a stamp from hours earlier.
    harness.runtime.reducer.mark_delivered(outbox_id=row, now=harness.start)

    after = harness.runtime.state()
    assert after.last_contact_at == anchor
    assert after.cooldown_until == cooldown
    # The delivery still counts as a contact.
    assert after.contact_count_today == 2


# --------------------------------------------------------------------------------------
# 5. the daily contact counter
# --------------------------------------------------------------------------------------


def test_the_contact_counter_is_charged_once_per_delivered_message(harness: Harness) -> None:
    """Committing is not contacting, and a repeated report is not a second contact."""
    attempt_id, _render_id = _queue_render(harness)
    assert harness.runtime.state().contact_count_today == 0

    harness.service.cycle(now=harness.start + timedelta(seconds=1))
    report = harness.service.cycle(now=harness.start + timedelta(seconds=2))
    assert report.sent == 1
    assert harness.runtime.state().contact_count_today == 1

    send_row = harness.runtime.projections.attempts.get(attempt_id).outbox_id
    duplicate = harness.runtime.reducer.mark_delivered(
        outbox_id=send_row, now=harness.start + timedelta(seconds=3)
    )
    assert duplicate["delivered"] is True
    assert duplicate.get("duplicate") is True
    assert harness.runtime.state().contact_count_today == 1


def test_the_contact_counter_rolls_over_before_it_is_charged(harness: Harness) -> None:
    """A new local day starts at zero, and its first message counts as one."""
    _deliver(harness, now=harness.start)
    assert harness.runtime.state().contact_count_today == 1

    # 25 hours later is always a different local calendar day.
    next_day = harness.start + timedelta(hours=25)
    harness.runtime.lazy_tick(next_day)
    assert harness.runtime.state().contact_count_today == 0
    assert harness.runtime.state().meta.get("contact_day")

    _deliver(harness, intent="新的一天", now=next_day + timedelta(minutes=5))
    assert harness.runtime.state().contact_count_today == 1


# --------------------------------------------------------------------------------------
# 2. attempt/outbox lifecycle closure
# --------------------------------------------------------------------------------------


def test_an_exhausted_lease_closes_the_attempt(harness: Harness) -> None:
    """A hopeless row fails its attempt instead of leaving it in flight forever."""
    harness.config.outbox.max_attempts = 1
    attempt_id, render_id = _queue_render(harness)
    claimed = _claim(harness, owner="dead-worker")
    assert claimed.outbox_id == render_id

    expiry = harness.start + timedelta(seconds=harness.config.outbox.lease_seconds + 1)
    report = harness.runtime.lazy_tick(expiry)

    assert attempt_id in report.expired_attempts
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.FAILED.value
    assert "outbox_failed" in (attempt.failure_reason or "")
    assert harness.runtime.projections.outbox.get(render_id).status == OutboxStatus.FAILED.value

    # The workspace is no longer busy, so the dispatch gate reopens.
    signals = scheduler_module.collect_signals(runtime=harness.runtime, now=expiry)
    allowed, _reason = scheduler_module.should_dispatch(
        signals=signals, attempt_states=[], config=harness.config
    )
    assert allowed is True


def test_a_terminal_row_failure_closes_the_attempt_at_once(harness: Harness) -> None:
    """``nack(terminal=True)`` closes the intention in the same call."""
    attempt_id, render_id = _queue_render(harness)
    _claim(harness)

    assert harness.runtime.reducer.nack_outbox(
        render_id, error="renderer exploded", terminal=True, now=harness.start
    )

    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.FAILED.value
    assert "renderer exploded" in (attempt.failure_reason or "")


def test_a_render_failure_stays_on_the_row_that_reported_it(harness: Harness) -> None:
    """The reported row is ``failed``; only the *other* rows are cancelled.

    The two statuses are not interchangeable: a ``failed`` row keeps the error text
    and counts in ``outbox.stats()["failed"]``, while a ``cancelled`` one means
    "nothing was wrong, this was withdrawn". Closing the attempt first and failing
    the row afterwards lets ``cancel_for_attempt`` swallow the very failure the
    report is about, so this pins the order: report the failure, then clean up.
    """
    attempt_id, render_row = _queue_render(harness)
    _claim(harness, owner="render-worker")

    assert harness.runtime.reducer.fail_render(
        outbox_id=render_row, error="renderer exploded", now=harness.start + timedelta(seconds=1)
    )

    row = harness.runtime.projections.outbox.get(render_row)
    assert row.status == OutboxStatus.FAILED.value
    assert "renderer exploded" in (row.last_error or "")
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.FAILED.value
    assert "renderer exploded" in (attempt.failure_reason or "")
    # No send row was ever queued for the dead intention.
    rows = [
        item
        for item in harness.runtime.projections.outbox.list_items(limit=50)
        if item.payload.get("attempt_id") == attempt_id
    ]
    assert [item.kind for item in rows] == ["render"]


def test_a_failed_send_closes_the_attempt_and_its_rows(harness: Harness) -> None:
    """A delivery failure is terminal for the intention, not just for the row."""
    attempt_id, _render_id = _queue_render(harness)
    harness.service.cycle(now=harness.start + timedelta(seconds=1))
    send_row = harness.runtime.projections.attempts.get(attempt_id).outbox_id
    _claim(harness, now=harness.start + timedelta(seconds=1), owner="tester")

    result = harness.runtime.reducer.mark_delivered(
        outbox_id=send_row, success=False, error="channel closed", now=harness.start + timedelta(seconds=2)
    )

    assert result["delivered"] is False
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.FAILED.value
    assert harness.runtime.projections.outbox.get(send_row).status == OutboxStatus.FAILED.value


def test_a_late_delivery_report_does_not_reopen_a_closed_attempt(harness: Harness) -> None:
    """A report about an intention the Runtime gave up on is recorded, not replayed.

    The message may genuinely have left, but a terminal attempt has no exit: the
    runtime must neither raise nor invent a transition, and must not count a
    contact it cannot attribute.
    """
    from companion_runtime.typing import EventType

    attempt_id, _render_id = _queue_render(harness)
    harness.service.cycle(now=harness.start + timedelta(seconds=1))
    send_row = harness.runtime.projections.attempts.get(attempt_id).outbox_id
    _claim(harness, now=harness.start + timedelta(seconds=2))

    harness.runtime.process_user_message(
        content="以后别主动联系我了", timestamp=harness.start + timedelta(seconds=3)
    )
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.ABORTED.value

    result = harness.runtime.reducer.mark_delivered(
        outbox_id=send_row, success=True, now=harness.start + timedelta(seconds=4)
    )

    assert result["delivered"] is True
    assert result.get("late_report") is True
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.ABORTED.value
    assert harness.runtime.events.count(EventType.PROACTIVE_SENT.value) == 0


def test_an_attempt_with_no_deliverable_row_is_expired(harness: Harness) -> None:
    """Nothing can deliver an intention whose rows are all gone."""
    attempt_id, render_id = _queue_render(harness)
    with harness.runtime.db.transaction() as conn:
        harness.runtime.projections.outbox.cancel(conn, render_id, reason="manual")

    window = harness.config.action.send_expiry_seconds
    report = harness.runtime.lazy_tick(harness.start + timedelta(seconds=window + 1))

    assert attempt_id in report.expired_attempts
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.EXPIRED.value


def test_expire_stale_leaves_a_delivered_attempt_alone(harness: Harness) -> None:
    """A sent message is not "waiting to be sent": expiring it is illegal.

    The attempt state machine allows ``sent -> resolved`` only, so a sweep that
    included sent attempts used to raise an illegal-transition error - and would
    have destroyed the record the user's reply has to be attributed to.
    """
    attempt_id = _deliver(harness, now=harness.start)

    with harness.runtime.db.transaction() as conn:
        expired = action_module.expire_stale(
            harness.runtime.projections.attempts,
            conn,
            config=harness.config,
            now=harness.start + timedelta(days=30),
        )

    assert expired == []
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value


# --------------------------------------------------------------------------------------
# 3. re-coordination on ingest
# --------------------------------------------------------------------------------------


def test_ingest_re_coordinates_an_undelivered_intention(harness: Harness) -> None:
    """The user speaking first must reach the attempt, not only an explicit call."""
    attempt_id, _render_id = _queue_render(harness)
    harness.service.cycle(now=harness.start + timedelta(seconds=1))
    assert (
        harness.runtime.projections.attempts.get(attempt_id).state
        == AttemptState.READY_TO_SEND.value
    )

    outcome = harness.runtime.process_user_message(
        content="我今天有点累，随便聊聊", timestamp=harness.start + timedelta(seconds=5)
    )

    decisions = {item["attempt_id"]: item for item in outcome.reconcile_decisions}
    assert attempt_id in decisions
    assert decisions[attempt_id]["action"] == "rerender"
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.superseded_by_event_ids == [outcome.event.event_id]
    # Reported through the outcome *and* through the HTTP-shaped rendering.
    assert outcome.to_dict()["reconcile_decisions"]


def test_ingest_drops_an_undelivered_intention_when_a_boundary_arrives(harness: Harness) -> None:
    """A hard boundary declared while the message is pending cancels the row."""
    attempt_id, _render_id = _queue_render(harness)
    harness.service.cycle(now=harness.start + timedelta(seconds=1))
    send_row = harness.runtime.projections.attempts.get(attempt_id).outbox_id

    outcome = harness.runtime.process_user_message(
        content="以后别主动联系我了", timestamp=harness.start + timedelta(seconds=5)
    )

    decisions = {item["attempt_id"]: item for item in outcome.reconcile_decisions}
    assert decisions[attempt_id]["action"] == "abort"
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.ABORTED.value
    assert harness.runtime.projections.outbox.get(send_row).status == OutboxStatus.CANCELLED.value

    # Nothing is left to deliver, so the delivery worker has nothing to send.
    report = harness.service.cycle(now=harness.start + timedelta(seconds=6))
    assert report.sent == 0
    assert harness.transport.sent == []


def test_a_boundary_withdraws_an_intention_that_was_never_rendered(harness: Harness) -> None:
    """Withdrawal is a property of ingest, not of the delivery worker's verdict.

    The same boundary must stop an intention at every pre-send stage - committed,
    rendering and ready-to-send - and always end it as *withdrawn* (``aborted``),
    never as ``failed``: nothing about the delivery machinery went wrong, the user
    forbade the contact. A ``failed`` state here would be a lie in the attempt
    history and would pollute the delivery failure statistics.
    """
    attempt_id, render_row = _queue_render(harness)
    assert (
        harness.runtime.projections.attempts.get(attempt_id).state
        == AttemptState.COMMITTED.value
    )

    outcome = harness.runtime.process_user_message(
        content="以后别主动联系我了", timestamp=harness.start + timedelta(seconds=2)
    )

    decisions = {item["attempt_id"]: item for item in outcome.reconcile_decisions}
    assert decisions[attempt_id]["action"] == "abort"
    attempt = harness.runtime.projections.attempts.get(attempt_id)
    assert attempt.state == AttemptState.ABORTED.value
    assert attempt.reconcile_action == "abort"
    assert "boundary" in decisions[attempt_id]["reason"] or "premise" in decisions[attempt_id]["reason"]
    assert harness.runtime.projections.outbox.get(render_row).status == OutboxStatus.CANCELLED.value
    # The render row is gone, so the worker cannot even be handed the intention.
    assert harness.service.cycle(now=harness.start + timedelta(seconds=3)).claimed == 0


def test_a_send_refused_by_the_worker_is_a_failure_not_a_withdrawal(harness: Harness) -> None:
    """The contrast case: a report about a refused send is recorded as a failure.

    A boundary that is already in force when the worker asks for permission is
    reported back as a *failed* action (``mark_delivered(success=False)``), which
    is the documented v1 contract for a rejected report: the row carries the
    refusal and the attempt is closed. The intention itself was already withdrawn
    at ingest, so the attempt keeps its ``aborted`` state and the late report
    cannot rewrite it - see ``test_a_late_delivery_report_does_not_reopen_a_closed_attempt``.
    """
    attempt_id, _render_id = _queue_render(harness)
    harness.service.cycle(now=harness.start + timedelta(seconds=1))
    send_row = harness.runtime.projections.attempts.get(attempt_id).outbox_id
    _claim(harness, now=harness.start + timedelta(seconds=2), owner="boundary-worker")

    harness.runtime.process_user_message(
        content="以后别主动联系我了", timestamp=harness.start + timedelta(seconds=3)
    )
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.ABORTED.value

    result = harness.runtime.reducer.mark_delivered(
        outbox_id=send_row,
        success=False,
        error="boundary_blocks_proactive",
        now=harness.start + timedelta(seconds=4),
    )

    assert result["delivered"] is False
    # The refusal is recorded, and the withdrawal is not rewritten into a failure.
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.ABORTED.value


# --------------------------------------------------------------------------------------
# 4. exactly-once reply attribution
# --------------------------------------------------------------------------------------


def test_a_normal_reply_is_attributed_once_and_resolves_the_message(harness: Harness) -> None:
    """One reply, one observation, one resolution - and no second observation."""
    attempt_id = _deliver(harness, now=harness.start)

    first = harness.runtime.process_user_message(
        content="面试过啦，谢谢你还记得", timestamp=harness.start + timedelta(minutes=5)
    )
    assert first.observation_id is not None
    assert first.attributed_attempt_id == attempt_id
    assert (
        harness.runtime.projections.attempts.get(attempt_id).state
        == AttemptState.RESOLVED.value
    )
    assert harness.runtime.user_model.observations == 1

    second = harness.runtime.process_user_message(
        content="刚刚在忙，没看到", timestamp=harness.start + timedelta(minutes=6)
    )
    assert second.observation_id is None
    assert second.attributed_attempt_id is None
    assert harness.runtime.user_model.observations == 1

    stored = [
        item
        for item in harness.runtime.projections.user_model.list_observations()
        if item.get("attempt_id") == attempt_id
    ]
    assert len(stored) == 1


def test_attribution_targets_the_newest_delivered_message(harness: Harness) -> None:
    """The reply answers the message the user just received."""
    older = _deliver(harness, intent="询问面试结果", now=harness.start)
    newer = _deliver(harness, intent="问问今天怎么样", now=harness.start + timedelta(minutes=10))

    outcome = harness.runtime.process_user_message(
        content="刚看到你的消息", timestamp=harness.start + timedelta(minutes=20)
    )

    assert outcome.attributed_attempt_id == newer
    assert harness.runtime.projections.attempts.get(newer).state == AttemptState.RESOLVED.value
    assert harness.runtime.projections.attempts.get(older).state == AttemptState.SENT.value


def test_an_explicit_reaction_still_resolves_the_delivered_message(harness: Harness) -> None:
    """When the host reports the reaction, the same closure happens."""
    from companion_runtime.user_model import BehaviourReaction

    attempt_id = _deliver(harness, now=harness.start)

    outcome = harness.runtime.process_user_message(
        content="嗯，收到了",
        timestamp=harness.start + timedelta(minutes=2),
        reason=BehaviourReaction(replied=True, reply_length=4),
    )

    assert outcome.attributed_attempt_id == attempt_id
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.RESOLVED.value


def test_a_reply_under_a_boundary_still_attributes_and_resolves(harness: Harness) -> None:
    """Re-coordination covers pre-send attempts; a delivered one is closed here.

    A boundary is a reply - a negative one - so it is recorded with the user
    model's boundary evidence rather than as an ordinary happy answer.
    """
    attempt_id = _deliver(harness, now=harness.start)

    outcome = harness.runtime.process_user_message(
        content="以后别主动联系我了", timestamp=harness.start + timedelta(minutes=3)
    )

    assert outcome.attributed_attempt_id == attempt_id
    assert harness.runtime.projections.attempts.get(attempt_id).state == AttemptState.RESOLVED.value
    stored = [
        item
        for item in harness.runtime.projections.user_model.list_observations()
        if item.get("attempt_id") == attempt_id
    ]
    assert len(stored) == 1
    assert stored[0]["outcome_json"]["boundary_touched"] is True


# --------------------------------------------------------------------------------------
# 1. scheduler wiring
# --------------------------------------------------------------------------------------


def test_scheduler_replans_from_live_runtime_state(harness: Harness) -> None:
    """The loop's delay comes from the Runtime's own anchors."""
    scheduler = scheduler_module.Scheduler(
        config=harness.config, round_callback=lambda: None, runtime=harness.runtime
    )
    planned = scheduler.replan(now=harness.start)

    assert planned.delay_seconds >= harness.config.scheduler.min_interval_seconds
    assert planned.delay_seconds <= harness.config.scheduler.max_interval_seconds
    assert scheduler.status()["plan"] == planned.to_dict()


def test_the_first_wake_of_a_fresh_runtime_is_prompt() -> None:
    """A never-ticked Runtime must not sleep the whole base interval first.

    The base interval is the *maximum* one, so without this a freshly started
    sidecar would not think at all for up to 90 minutes - including after a
    restart, when the elapsed time since creation is exactly what it needs to
    integrate.
    """
    config = build_config()
    runtime = Runtime(config=config)
    try:
        scheduler = scheduler_module.Scheduler(
            config=config, round_callback=lambda: None, runtime=runtime
        )
        planned = scheduler.replan()
        assert "bootstrap" in planned.reasons
        assert planned.delay_seconds == config.scheduler.min_interval_seconds
    finally:
        runtime.close()


def test_scheduler_runs_endogenous_rounds_until_stopped(harness: Harness) -> None:
    """The loop calls the round callback on its own and stops cleanly."""
    harness.config.scheduler.min_interval_seconds = 0.01
    harness.config.scheduler.max_interval_seconds = 0.05
    calls: list[float] = []

    def round_callback() -> SimpleNamespace:
        calls.append(time.monotonic())
        return SimpleNamespace(next_wake_at=datetime.now(tz=BASE_TIME.tzinfo) + timedelta(seconds=0.02))

    scheduler = scheduler_module.Scheduler(
        config=harness.config, round_callback=round_callback, runtime=harness.runtime
    )

    async def scenario() -> dict:
        await scheduler.start()
        await asyncio.sleep(0.25)
        status = scheduler.status()
        await scheduler.stop()
        return status

    status = asyncio.run(scenario())

    assert calls, "the scheduler must run the round on its own"
    assert status["rounds"] >= 1
    assert status["plan"] is not None
    assert scheduler.running is False


def test_scheduler_does_not_start_a_round_while_an_attempt_is_in_flight(
    harness: Harness,
) -> None:
    """The dispatch gate is what stops a second parallel outreach."""
    _queue_render(harness)
    harness.config.scheduler.min_interval_seconds = 0.01
    harness.config.scheduler.max_interval_seconds = 0.05
    calls: list[int] = []
    scheduler = scheduler_module.Scheduler(
        config=harness.config, round_callback=lambda: calls.append(1), runtime=harness.runtime
    )

    async def scenario() -> dict:
        await scheduler.start()
        await asyncio.sleep(0.2)
        status = scheduler.status()
        await scheduler.stop()
        return status

    status = asyncio.run(scenario())

    assert calls == []
    assert status["dispatch_allowed"] is False
    assert status["dispatch_reason"] == "attempt_in_flight"


def test_stop_waits_for_a_round_that_is_already_running(harness: Harness) -> None:
    """Shutdown must not race the round that owns the database connection."""
    import threading

    harness.config.scheduler.min_interval_seconds = 0.01
    harness.config.scheduler.max_interval_seconds = 0.02
    harness.config.scheduler.busy_poll_seconds = 1.0
    started = threading.Event()
    release = threading.Event()

    def round_callback() -> SimpleNamespace:
        started.set()
        release.wait(timeout=5.0)
        return SimpleNamespace(next_wake_at=None)

    scheduler = scheduler_module.Scheduler(
        config=harness.config, round_callback=round_callback, runtime=harness.runtime
    )

    async def scenario() -> tuple[bool, bool]:
        await scheduler.start()
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set(), "the round must have started"
        stopping = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0.05)
        in_flight_during_stop = scheduler.round_in_flight
        release.set()
        await stopping
        return in_flight_during_stop, scheduler.round_in_flight

    in_flight_during_stop, after_stop = asyncio.run(scenario())

    assert in_flight_during_stop is True, "stop must not return mid-round"
    assert after_stop is False


def test_serve_wires_the_endogenous_scheduler(monkeypatch, tmp_path) -> None:
    """``companion-runtime serve`` must think on its own, not only answer.

    The server itself is replaced by a short sleep: what is asserted is that the
    serve lifecycle starts the scheduler, lets it run a real endogenous round, and
    stops it again on shutdown.
    """
    import uvicorn

    config = build_config()
    config.storage.database_path = ":memory:"
    config.scheduler.min_interval_seconds = 0.01
    config.scheduler.max_interval_seconds = 0.05
    config.server.port = 0

    monkeypatch.setattr(cli_module, "_resolve_config", lambda args: config)
    monkeypatch.setattr(cli_module, "configure_logging", lambda level: None)

    rounds: list[int] = []
    original = cli_module.Runtime

    class RecordingRuntime(original):  # type: ignore[misc, valid-type]
        """Runtime double that counts the rounds the scheduler drives."""

        instances: list = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            RecordingRuntime.instances.append(self)

        def endogenous_round(self, **kwargs):
            rounds.append(1)
            return super().endogenous_round(**kwargs)

    monkeypatch.setattr(cli_module, "Runtime", RecordingRuntime)

    async def fake_serve(self) -> None:  # noqa: ANN001 - uvicorn signature
        await asyncio.sleep(0.3)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)

    assert cli_module.cmd_serve(cli_module.build_parser().parse_args(["serve"])) == 0

    assert rounds, "the serve lifecycle must drive endogenous rounds"
    assert RecordingRuntime.instances
    after_shutdown = len(rounds)
    time.sleep(0.15)
    assert len(rounds) == after_shutdown, "the loop must stop with the server"
