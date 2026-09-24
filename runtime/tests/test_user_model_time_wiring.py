"""The user model ages with the clock, seen from the Runtime (design §28/§29).

`test_user_model_time.py` pins the model's own behaviour. This file pins the wiring:
the Runtime must actually *call* the drift on its tick, persist it, and let a silence
change what the character predicts - one line missing in `_apply_time_passage` is all
it takes for the whole time-dynamics path to be dead code again.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime.runtime import Runtime
from companion_runtime.user_model import BehaviourReaction, UserInteractionModel

from conftest import BASE_TIME

ACTION = {"type": "follow_up", "proactive": True, "question": True}

#: Enough observations that the model has real confidence to lose: with one
#: observation it is already almost at the prior, and "the beliefs got less certain"
#: would be true but unmeasurable.
OBSERVATIONS = 12


def _learn_about_the_user(runtime: Runtime, *, count: int = OBSERVATIONS) -> None:
    """Give the model ``count`` real observations, so it has confidence to lose."""
    with runtime.db.transaction() as conn:
        for _ in range(count):
            runtime.user_model.observe(
                conn,
                action=ACTION,
                context=runtime._situation_context(BASE_TIME),
                reaction=BehaviourReaction(replied=True, reply_delay_seconds=1800.0),
                now=BASE_TIME,
                observed_at=BASE_TIME,
            )
    runtime.reload_user_model()


def test_a_long_silence_makes_the_character_less_sure_about_the_user(
    runtime: Runtime,
) -> None:
    """The tick must age the beliefs: this is the wiring, not the arithmetic.

    A Runtime whose tick forgets to call ``tick_drift`` looks identical to a working
    one from the model's own tests - only the clock passing through the Runtime shows
    the difference.
    """
    _learn_about_the_user(runtime)
    context = runtime._situation_context(BASE_TIME)
    fresh = runtime.user_model.predict(action=ACTION, context=context)
    fresh_bound = runtime.user_model.conservative_bound(fresh)

    # Four weeks without contact: two half-lives of the accumulated confidence.
    runtime.lazy_tick(BASE_TIME + timedelta(days=28))
    aged = runtime.user_model.predict(action=ACTION, context=context)
    aged_bound = runtime.user_model.conservative_bound(aged)

    assert aged.observation_count == fresh.observation_count, "facts are not forgotten"
    assert aged.uncertainty > fresh.uncertainty + 0.03, (
        f"uncertainty must grow with the silence: {fresh.uncertainty} -> {aged.uncertainty}"
    )
    assert aged_bound < fresh_bound, (
        "the conservative bound must shrink as confidence relaxes"
    )


def test_the_aged_confidence_survives_a_reload(runtime: Runtime) -> None:
    """Drift that is not persisted is drift that never happened.

    The Runtime rebuilds its user model from stored parameters on every reload, so an
    in-memory-only decay would silently reset the moment anything reloads it.
    """
    _learn_about_the_user(runtime)
    runtime.lazy_tick(BASE_TIME + timedelta(days=14))
    context = runtime._situation_context(BASE_TIME)
    aged = runtime.user_model.predict(action=ACTION, context=context)

    reloaded = UserInteractionModel(runtime.projections.user_model, runtime.config)
    again = reloaded.predict(action=ACTION, context=context)

    assert again.uncertainty == pytest.approx(aged.uncertainty)
    assert again.observation_count == aged.observation_count


def test_drift_depends_on_elapsed_time_not_on_how_often_it_ticks(
    runtime: Runtime,
) -> None:
    """Seven small drifts and one big one must land in the same place.

    Otherwise the character's confidence would depend on how often whatever drives
    the clock happens to poll. The comparison model is built from the projection
    *before* the ticks, so it starts from the same state - building it afterwards
    would silently age the beliefs twice.
    """
    _learn_about_the_user(runtime)
    context = runtime._situation_context(BASE_TIME)
    single = UserInteractionModel(runtime.projections.user_model, runtime.config)

    for day in range(7):
        runtime.lazy_tick(BASE_TIME + timedelta(days=day + 1))
    stepped = runtime.user_model.predict(action=ACTION, context=context)

    single.tick_drift(7 * 86400.0)
    one_shot = single.predict(action=ACTION, context=context)

    assert stepped.uncertainty == pytest.approx(one_shot.uncertainty)
    assert stepped.reply_probability == pytest.approx(one_shot.reply_probability)
