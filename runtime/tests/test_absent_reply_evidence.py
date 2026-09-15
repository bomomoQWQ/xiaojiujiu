"""The evidence a character gets from being ignored (design §22.3).

Only the positive half of the feedback loop had a producer: a reply is attributed -
and consumed - when the user speaks next. A message that was delivered and then never
answered left no trace at all, so ``no_reply_weight`` was unreachable in production
and the user model could not learn that it was being ignored, whatever the user did.
These tests pin the producer: when, how strongly, and exactly once.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime import action as action_module
from companion_runtime import user_model as user_model_module
from companion_runtime.runtime import Runtime
from companion_runtime.typing import AttemptState, CandidateIntent, CandidateStatus, OutboxKind, new_id

from conftest import BASE_TIME

TEXT = "面试怎么样啦？"


def _delivered_attempt(runtime: Runtime, *, intent: str = "询问面试结果") -> str:
    """Commit, render and deliver one proactive message; return the attempt id."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=intent,
        goal="表达关心",
        sources=["unfinished:unf_absent"],
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
    runtime.reducer.complete_render(outbox_id=render_row.outbox_id, text=TEXT, now=BASE_TIME)
    send_row = [
        item
        for item in runtime.projections.outbox.list_items(status=None, limit=50)
        if item.kind == OutboxKind.SEND.value
    ][0]
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1, kinds=["send"])
    runtime.reducer.mark_delivered(outbox_id=send_row.outbox_id, now=BASE_TIME)
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    return attempt_id


def _hours(runtime: Runtime, hours: float) -> None:
    """Advance the Runtime's clock by ``hours`` through the public tick."""
    runtime.lazy_tick(BASE_TIME + timedelta(hours=hours))


def test_a_delivered_message_that_is_never_answered_becomes_weak_evidence(
    runtime: Runtime,
) -> None:
    """Silence is recorded, weakly, and it is not a rejection.

    The weight is ``no_reply_weight`` damped by ``1 - P(busy)`` (design §22.3): six
    hours without an answer says something, and almost nothing when the user is
    probably busy.
    """
    attempt_id = _delivered_attempt(runtime)
    horizon = runtime.config.user_model.silence_after_hours

    _hours(runtime, horizon + 1.0)

    observation = runtime.projections.user_model.observation_for_attempt(attempt_id)
    assert observation is not None, "an unanswered delivered message must leave evidence"
    outcome = observation["outcome_json"]
    assert outcome["replied"] is False
    assert float(outcome["reply_delay_seconds"]) >= horizon * 3600.0
    # The evidence is the weak one: no_reply_weight, damped by how busy the user
    # probably was, and by the recency/semantic factors every observation carries.
    expected = user_model_module.compute_weight(
        user_model_module.BehaviourReaction(
            replied=False,
            reply_delay_seconds=float(outcome["reply_delay_seconds"]),
            busy_probability=float(outcome["busy_probability"]),
        ),
        config=runtime.config.user_model,
        observed_at=BASE_TIME + timedelta(hours=horizon + 1.0),
        now=BASE_TIME + timedelta(hours=horizon + 1.0),
    )
    assert observation["weight"] == pytest.approx(expected.total, rel=1e-6)
    assert observation["weight"] <= runtime.config.user_model.no_reply_weight + 1e-9, (
        "a silence must never weigh more than the weakest positive evidence"
    )
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.RESOLVED.value
    assert runtime.events.count("interaction_observation") >= 1


def test_silence_evidence_waits_for_the_horizon(runtime: Runtime) -> None:
    """Nothing is recorded while an answer is still plausible."""
    attempt_id = _delivered_attempt(runtime)

    _hours(runtime, runtime.config.user_model.silence_after_hours - 1.0)

    assert runtime.projections.user_model.observation_for_attempt(attempt_id) is None
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value


def test_silence_evidence_is_recorded_exactly_once(runtime: Runtime) -> None:
    """A long silence is one piece of evidence, not one per tick."""
    attempt_id = _delivered_attempt(runtime)
    horizon = runtime.config.user_model.silence_after_hours

    _hours(runtime, horizon + 1.0)
    first = runtime.projections.user_model.observation_for_attempt(attempt_id)
    assert first is not None
    for step in range(2, 12):
        _hours(runtime, horizon + step)
    again = runtime.projections.user_model.observation_for_attempt(attempt_id)
    assert again is not None and again["observation_id"] == first["observation_id"]

    observations = [
        item
        for item in runtime.projections.user_model.list_observations(limit=200)
        if item.get("attempt_id") == attempt_id
    ]
    assert len(observations) == 1


def test_a_reply_before_the_horizon_is_what_gets_recorded(runtime: Runtime) -> None:
    """A real answer must not be shadowed by a pending silence verdict."""
    attempt_id = _delivered_attempt(runtime)
    horizon = runtime.config.user_model.silence_after_hours

    _hours(runtime, horizon / 2)
    runtime.process_user_message(
        content="还在等消息，有点紧张。", timestamp=BASE_TIME + timedelta(hours=horizon / 2 + 1)
    )

    observation = runtime.projections.user_model.observation_for_attempt(attempt_id)
    assert observation is not None
    assert observation["outcome_json"]["replied"] is True, "the answer is the evidence"

    # ... and no second, contradicting observation appears later.
    _hours(runtime, horizon * 3)
    observations = [
        item
        for item in runtime.projections.user_model.list_observations(limit=200)
        if item.get("attempt_id") == attempt_id
    ]
    assert len(observations) == 1


def test_the_model_actually_receives_the_silence_evidence(runtime: Runtime) -> None:
    """Recording it is not enough: the learned parameters must move.

    A model that only ever sees replies keeps predicting that people answer, which is
    how a character ends up nagging. The check is on the model, not on the row.
    """
    attempt_id = _delivered_attempt(runtime)
    horizon = runtime.config.user_model.silence_after_hours
    # The same question in the same situation, before and after the silence: the
    # features must not change, or the comparison measures the context, not learning.
    action = {"type": "follow_up", "proactive": True, "question": True}
    context = runtime._situation_context(BASE_TIME)
    before = runtime.user_model.predict(action=action, context=context)

    _hours(runtime, horizon + 1.0)

    after = runtime.user_model.predict(action=action, context=context)
    assert after.observation_count > before.observation_count
    assert after.reply_probability < before.reply_probability, (
        "being ignored must make the character expect answers a little less"
    )
    assert runtime.projections.user_model.observation_for_attempt(attempt_id) is not None


def test_silence_never_closes_something_that_was_never_delivered(runtime: Runtime) -> None:
    """Only a *delivered* message can be ignored; an undelivered one is a different fault."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="还没渲染出来的话",
        goal="表达关心",
        sources=["unfinished:unf_absent"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        attempt_id, _ = runtime._commit_attempt(conn, chosen=candidate, state=state, now=BASE_TIME)

    _hours(runtime, runtime.config.user_model.silence_after_hours * 3)

    assert runtime.projections.user_model.observation_for_attempt(attempt_id) is None
    assert runtime.projections.attempts.get(attempt_id).state != AttemptState.RESOLVED.value
