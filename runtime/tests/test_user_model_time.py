"""Time dynamics (§28) and baseline normalisation (§29) for the user model.

Two audited defects are pinned down here.

* **§28** - precision only relaxed when a *new observation* was stored, so a long
  silence never made the old beliefs more uncertain. :meth:`tick_drift` integrates
  the elapsed seconds instead.
* **§29** - reply speed was compared against nothing at all (an 8-hour replier and a
  10-minute replier scored the same), so "this user normally replies in 8 hours but
  replied in 2 today" taught nothing. The model now keeps a per-user reply-delay
  baseline and scores each delay as a z-score against it.

The tests are written against the public surface wherever one exists
(``predict``, ``relative_delay_signal``, ``reply_delay_baseline_view``,
``numeric_view``); the two places that reach into privates (``_precision``,
``_target_rewards``) do so to state a property that has no public equivalent.
"""

from __future__ import annotations

import json

import pytest

from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import UserModelProjection
from companion_runtime.user_model import (
    REPLY_DELAY_BASELINE_MIN_SAMPLES,
    BehaviourReaction,
    UserInteractionModel,
)

from conftest import BASE_TIME, build_config

MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0

TEN_MINUTES = 10 * MINUTE
TWO_HOURS = 2 * HOUR
EIGHT_HOURS = 8 * HOUR

ACTION = {"type": "contact", "proactive": True}
CONTEXT = {"busy_probability": 0.0, "hours_since_contact": 12.0}


def make_model(config: RuntimeConfig | None = None) -> tuple[UserInteractionModel, Database]:
    """Build a model over an in-memory database."""
    db = Database(":memory:")
    db.migrate()
    return UserInteractionModel(UserModelProjection(db), config or build_config()), db


def learn_replies(
    model: UserInteractionModel,
    db: Database,
    *,
    delay_seconds: float,
    count: int = 1,
) -> None:
    """Fold ``count`` observed replies with one fixed delay into ``model``.

    Everything except the delay is held constant, so any difference between two
    models built this way comes from the reply-speed term alone.
    """
    with db.transaction() as conn:
        for _ in range(count):
            model.observe(
                conn,
                action=ACTION,
                context=CONTEXT,
                reaction=BehaviourReaction(
                    replied=True,
                    reply_delay_seconds=delay_seconds,
                    reply_length=30,
                    continued_topic=True,
                    asked_back=True,
                ),
                now=BASE_TIME,
                observed_at=BASE_TIME,
                semantic_confidence=1.0,
            )


def precision_of(model: UserInteractionModel) -> dict[str, list[float]]:
    """Return a copy of the diagonal precision, for exact before/after checks."""
    return {target: list(values) for target, values in model._precision.items()}


# --------------------------------------------------------------------------------------
# §28: drift is a function of elapsed time
# --------------------------------------------------------------------------------------


def test_a_long_silence_makes_old_beliefs_less_certain() -> None:
    """A month of no interaction relaxes confidence; the learned mean survives."""
    model, db = make_model()
    try:
        learn_replies(model, db, delay_seconds=TEN_MINUTES, count=12)
        fresh = model.predict(action=ACTION, context=CONTEXT)

        with db.transaction() as conn:
            assert model.tick_drift(30 * DAY, connection=conn) is True

        stale = model.predict(action=ACTION, context=CONTEXT)
        # Uncertain again - this is exactly what used to never happen. The margin
        # matters: the old per-observation nudge does move precision, but by ~1e-7,
        # which a bare ``>`` would happily accept as "the belief aged".
        assert stale.uncertainty > fresh.uncertainty + 0.05
        # A one-sided drift: the mean is not moved by the passage of time...
        assert stale.reply_probability == pytest.approx(fresh.reply_probability)
        # ...only the confidence in it, which is what the conservative bound reads.
        assert model.conservative_bound(stale) < model.conservative_bound(fresh) - 0.005
        # Observation counts are historical facts, not beliefs: they do not decay.
        assert model.observations == 12
        assert model.effective_count == pytest.approx(fresh.effective_count)
    finally:
        db.close()


def test_a_fresh_belief_survives_a_small_tick() -> None:
    """A second of elapsed time must not visibly age a just-learned belief."""
    model, db = make_model()
    try:
        learn_replies(model, db, delay_seconds=TEN_MINUTES, count=12)
        before = model.predict(action=ACTION, context=CONTEXT)

        with db.transaction() as conn:
            assert model.tick_drift(1.0, connection=conn) is True

        after = model.predict(action=ACTION, context=CONTEXT)
        assert after.uncertainty == pytest.approx(before.uncertainty, rel=1e-3)
        assert after.reply_probability == pytest.approx(before.reply_probability, rel=1e-6)
    finally:
        db.close()


def test_tick_drift_is_a_no_op_for_zero_and_negative_dt() -> None:
    """``dt <= 0`` changes nothing at all - not even a write."""
    model, db = make_model()
    try:
        learn_replies(model, db, delay_seconds=TEN_MINUTES, count=6)
        precision_before = precision_of(model)
        theta_before = {target: list(values) for target, values in model._theta.items()}
        stored_before = model.projection.get_params()

        with db.transaction() as conn:
            assert model.tick_drift(0.0, connection=conn) is False
            assert model.tick_drift(-HOUR, connection=conn) is False

        assert precision_of(model) == precision_before
        assert model._theta == theta_before
        # A persisted row would carry a new ``last_updated_at``, so equality here
        # proves the no-op did not write either.
        assert model.projection.get_params() == stored_before
    finally:
        db.close()


def test_drift_depends_on_elapsed_time_not_on_tick_count() -> None:
    """Seven daily ticks leave the same state as one weekly tick."""
    weekly, weekly_db = make_model()
    daily, daily_db = make_model()
    try:
        for model, db in ((weekly, weekly_db), (daily, daily_db)):
            learn_replies(model, db, delay_seconds=TEN_MINUTES, count=6)

        with weekly_db.transaction() as conn:
            assert weekly.tick_drift(7 * DAY, connection=conn) is True
        with daily_db.transaction() as conn:
            for _ in range(7):
                daily.tick_drift(DAY, connection=conn)

        for target in weekly._precision:
            assert daily._precision[target] == pytest.approx(weekly._precision[target])
    finally:
        weekly_db.close()
        daily_db.close()


def test_tick_drift_is_persisted_through_the_projection() -> None:
    """A restart must not resurrect the confidence that the silence removed."""
    config = build_config()
    db = Database(":memory:")
    db.migrate()
    model = UserInteractionModel(UserModelProjection(db), config)
    try:
        learn_replies(model, db, delay_seconds=TEN_MINUTES, count=6)
        with db.transaction() as conn:
            assert model.tick_drift(45 * DAY, connection=conn) is True

        reloaded = UserInteractionModel(UserModelProjection(db), config)
        assert reloaded._precision == model._precision
        assert reloaded.predict(action=ACTION, context=CONTEXT).uncertainty == pytest.approx(
            model.predict(action=ACTION, context=CONTEXT).uncertainty
        )
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# §29: reply speed relative to this user's own baseline
# --------------------------------------------------------------------------------------


def test_the_same_reply_is_quick_for_a_slow_replier_and_slow_for_a_quick_one() -> None:
    """One absolute delay, two verdicts - the comparison the baseline enables."""
    slow_habit, slow_db = make_model()
    quick_habit, quick_db = make_model()
    try:
        learn_replies(slow_habit, slow_db, delay_seconds=EIGHT_HOURS, count=8)
        learn_replies(quick_habit, quick_db, delay_seconds=TEN_MINUTES, count=8)

        assert slow_habit.reply_delay_baseline_seconds == pytest.approx(EIGHT_HOURS, rel=0.05)
        assert quick_habit.reply_delay_baseline_seconds == pytest.approx(TEN_MINUTES, rel=0.05)
        # Two hours is well inside the slow user's normal rhythm and far outside the
        # quick user's, so the very same 7200 seconds has opposite meanings.
        assert slow_habit.relative_delay_signal(TWO_HOURS) > 0.5
        assert quick_habit.relative_delay_signal(TWO_HOURS) < -0.5

        slow_before = slow_habit.predict(action=ACTION, context=CONTEXT).positive_probability
        quick_before = quick_habit.predict(action=ACTION, context=CONTEXT).positive_probability
        learn_replies(slow_habit, slow_db, delay_seconds=TWO_HOURS, count=1)
        learn_replies(quick_habit, quick_db, delay_seconds=TWO_HOURS, count=1)
        slow_delta = (
            slow_habit.predict(action=ACTION, context=CONTEXT).positive_probability - slow_before
        )
        quick_delta = (
            quick_habit.predict(action=ACTION, context=CONTEXT).positive_probability - quick_before
        )

        # The slow-habit user is rewarded for a reply that is fast *for them*...
        assert slow_delta > 0.005
        # ...and the identical reply is not a reward for the quick-habit user. Without
        # the relative term both models would be indistinguishable here, so the two
        # deltas would be equal.
        assert quick_delta < slow_delta - 0.005
    finally:
        slow_db.close()
        quick_db.close()


def test_a_reply_that_is_slow_for_this_user_earns_no_speed_bonus() -> None:
    """Two identical quick-habit models, differing only in the new reply's delay."""
    fast_arm, fast_db = make_model()
    slow_arm, slow_db = make_model()
    try:
        for model, db in ((fast_arm, fast_db), (slow_arm, slow_db)):
            learn_replies(model, db, delay_seconds=TEN_MINUTES, count=4)

        learn_replies(fast_arm, fast_db, delay_seconds=TEN_MINUTES, count=1)
        learn_replies(slow_arm, slow_db, delay_seconds=EIGHT_HOURS, count=1)

        fast_prediction = fast_arm.predict(action=ACTION, context=CONTEXT)
        slow_prediction = slow_arm.predict(action=ACTION, context=CONTEXT)
        # Identical histories before the last observation: only the relative speed of
        # that last reply can separate the two models.
        assert fast_prediction.positive_probability > slow_prediction.positive_probability
    finally:
        fast_db.close()
        slow_db.close()


def test_a_relative_slow_reply_is_weaker_evidence_not_negative_evidence() -> None:
    """The delay term can cancel a bonus; it can never invert a reply's sign."""
    model, db = make_model()
    try:
        learn_replies(model, db, delay_seconds=TEN_MINUTES, count=4)
        # Topic + asked-back + long reply: 0.5 + 0.12 + 0.08 + 0.10 = 0.80 normally.
        late = BehaviourReaction(
            replied=True,
            reply_delay_seconds=90 * DAY,
            reply_length=30,
            continued_topic=True,
            asked_back=True,
        )
        # 0.80 minus the full relative-delay weight, not below neutral.
        assert model._target_rewards(late)["positive_probability"] == pytest.approx(0.70)
        # With no bonus to cancel, a late reply adds nothing and stays positive.
        plain = BehaviourReaction(replied=True, reply_delay_seconds=90 * DAY, reply_length=8)
        assert model._target_rewards(plain)["positive_probability"] == pytest.approx(0.40)
    finally:
        db.close()


def test_baseline_needs_several_samples_before_it_dominates() -> None:
    """Before that, the absolute reference delay keeps the first observations useful."""
    config = build_config()
    default = config.user_model.default_reply_delay_seconds
    model, db = make_model(config)
    try:
        cold = model.reply_delay_baseline_view()
        assert cold["samples"] == 0
        assert cold["trusted"] is False
        assert cold["mean_seconds"] is None
        assert cold["reference_seconds"] == pytest.approx(default, rel=0.01)

        for expected_samples in range(1, REPLY_DELAY_BASELINE_MIN_SAMPLES):
            learn_replies(model, db, delay_seconds=EIGHT_HOURS, count=1)
            view = model.reply_delay_baseline_view()
            assert view["samples"] == expected_samples
            assert view["trusted"] is False
            # One or two slow replies must not redefine "normal" yet...
            assert view["reference_seconds"] == pytest.approx(default, rel=0.01)
            assert view["mean_seconds"] == pytest.approx(EIGHT_HOURS, rel=0.01)
            # ...so a two-hour reply still reads as "slower than a typical reply".
            assert model.relative_delay_signal(TWO_HOURS) < 0.0

        learn_replies(model, db, delay_seconds=EIGHT_HOURS, count=1)
        trusted = model.reply_delay_baseline_view()
        assert trusted["samples"] == REPLY_DELAY_BASELINE_MIN_SAMPLES
        assert trusted["trusted"] is True
        assert trusted["reference_seconds"] == pytest.approx(EIGHT_HOURS, rel=0.01)
        # Now the user's own habit dominates and the same reply is fast.
        assert model.relative_delay_signal(TWO_HOURS) > 0.0
    finally:
        db.close()


def test_a_missing_reply_does_not_teach_the_delay_baseline() -> None:
    """Silence says nothing about how fast this user answers."""
    config = build_config()
    model, db = make_model(config)
    try:
        with db.transaction() as conn:
            model.observe(
                conn,
                action=ACTION,
                context=CONTEXT,
                reaction=BehaviourReaction(replied=False, reply_delay_seconds=EIGHT_HOURS),
                now=BASE_TIME,
                observed_at=BASE_TIME,
                busy_probability=0.9,
            )
        assert model.reply_delay_baseline_samples == 0
        assert model.reply_delay_baseline_seconds is None
        assert model.reply_delay_baseline_view()["reference_seconds"] == pytest.approx(
            config.user_model.default_reply_delay_seconds, rel=0.01
        )
    finally:
        db.close()


def test_reply_delay_baseline_survives_a_reload() -> None:
    """The baseline is persisted with the other parameters, not relearned."""
    config = build_config()
    db = Database(":memory:")
    db.migrate()
    model = UserInteractionModel(UserModelProjection(db), config)
    try:
        learn_replies(model, db, delay_seconds=EIGHT_HOURS, count=4)
        baseline = model.reply_delay_baseline_seconds
        assert baseline is not None

        reloaded = UserInteractionModel(UserModelProjection(db), config)
        assert reloaded.reply_delay_baseline_samples == 4
        assert reloaded.reply_delay_baseline_seconds == pytest.approx(baseline)
        assert reloaded.relative_delay_signal(TWO_HOURS) == pytest.approx(
            model.relative_delay_signal(TWO_HOURS)
        )
        assert reloaded.numeric_view()["reply_delay_baseline"]["trusted"] is True
    finally:
        db.close()


def test_numeric_view_gains_the_baseline_without_losing_anything() -> None:
    """Additive only: the previous keys keep their shape and stay JSON-safe."""
    model, db = make_model()
    try:
        learn_replies(model, db, delay_seconds=EIGHT_HOURS, count=3)
        view = model.numeric_view()
        for key in (
            "observations",
            "effective_count",
            "class_evidence",
            "behaviour_offsets",
            "reply_probability",
            "positive_probability",
            "continue_probability",
            "boundary_risk",
            "semantic",
        ):
            assert key in view
        baseline = view["reply_delay_baseline"]
        assert baseline["samples"] == 3
        assert baseline["trusted"] is True
        assert baseline["mean_seconds"] == pytest.approx(EIGHT_HOURS, rel=0.01)
        json.dumps(view)
    finally:
        db.close()
