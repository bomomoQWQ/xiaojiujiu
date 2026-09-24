"""Tests for the two motivational-layer gaps: the conservative quantile (§47) and the
outcome terms of ``V_user`` (§46).

Both were *declared but unread*. ``config.utility.downside_quantile`` had no reader at
all and the decision path used a hardcoded ``z = 0.12`` heuristic instead of the user
model's own lower bound, so "be careful while you are unsure" was a constant rather
than the model's uncertainty. And the user-side value rewarded a good outcome while
having no term at all for a reply that lands badly or for neutral filler.

The predictions in the first three tests come from a real
:class:`UserInteractionModel` trained through :meth:`UserInteractionModel.observe` on
an in-memory database: no test here writes ``_theta``/``_precision`` or invents an
uncertainty by hand.
"""

from __future__ import annotations

import random
from datetime import datetime

import pytest

from companion_runtime import motivation
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import UserModelProjection
from companion_runtime.typing import CandidateIntent, RuntimeState
from companion_runtime.user_model import (
    BehaviourReaction,
    Prediction,
    UserInteractionModel,
)

BASE_TIME = datetime.fromisoformat("2026-03-01T09:00:00+00:00")

#: The behaviour under test: an unprompted contact.
ACTION = {"type": "contact", "proactive": True}

#: A situation the model has *learned* is boundary-adjacent: the user has touched a
#: boundary before. It keeps ``boundary_risk`` above
#: ``utility.conservative_risk_threshold`` whatever the evidence count, which is what
#: puts a candidate on the conservative-quantile path.
CONTEXT = {"busy_probability": 0.2, "hours_since_contact": 30.0, "ever_boundary": True}

#: A reply that is warm and continues the exchange, but touches a boundary - the
#: "risky but usually fine" behaviour whose estimate starts out thin.
RISKY_YET_WARM = BehaviourReaction(
    replied=True,
    reply_length=40,
    continued_topic=True,
    asked_back=True,
    boundary_touched=True,
)


def _trained(
    reactions: list[BehaviourReaction], config: RuntimeConfig | None = None
) -> tuple[UserInteractionModel, Database]:
    """Return a model that has actually observed ``reactions`` in :data:`CONTEXT`."""
    settings = config or RuntimeConfig()
    db = Database(":memory:")
    db.migrate()
    model = UserInteractionModel(UserModelProjection(db), settings)
    if reactions:
        with db.transaction() as conn:
            for reaction in reactions:
                model.observe(
                    conn,
                    action=ACTION,
                    context=CONTEXT,
                    reaction=reaction,
                    now=BASE_TIME,
                    observed_at=BASE_TIME,
                    semantic_confidence=1.0,
                )
    return model, db


def _candidate(
    *, internal_need: float = 0.5, unfinished_relevance: float = 0.0
) -> CandidateIntent:
    """Build one contact candidate."""
    return CandidateIntent(
        candidate_id="cnd_1",
        type="contact",
        intent="想联系用户",
        target="relationship",
        sources=["internal_approach_drive"],
        internal_need=internal_need,
        unfinished_relevance=unfinished_relevance,
    )


def _state(*, impulse: float = 0.6, restraint: float = 0.2, pressure: float = 0.69) -> RuntimeState:
    """Build a runtime state whose silence utility sits near the calibrated middle."""
    state = RuntimeState()
    state.approach_impulse = impulse
    state.restraint = restraint
    state.pressure = pressure
    return state


def _decide(
    *,
    config: RuntimeConfig,
    prediction: Prediction,
    state: RuntimeState | None = None,
    candidate: CandidateIntent | None = None,
    elapsed_seconds: float = 3600.0,
    rng_seed: int = 1,
) -> motivation.MotivationResult:
    """Run one decision round with compact arguments."""
    item = candidate or _candidate()
    return motivation.decide(
        motivation.MotivationInputs(
            state=state or _state(),
            candidates=[item],
            predictions={item.candidate_id: prediction},
            boundary_allow_proactive=True,
            boundary_risk_baseline=0.0,
            recent_contacts=0,
            hours_since_contact=30.0,
            cooldown_active=False,
            now=BASE_TIME,
            elapsed_seconds=elapsed_seconds,
        ),
        config=config,
        rng=random.Random(rng_seed),
    )


def _mean_priced(config: RuntimeConfig, prediction: Prediction, state: RuntimeState) -> float:
    """Return ``V_user`` for the same candidate priced at the predicted mean."""
    return motivation.candidate_utility(
        candidate=_candidate(),
        state=state,
        prediction=prediction,
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=30.0,
        conservative_reply=None,
    ).user


# --------------------------------------------------------------------------------------
# Gap 1 (§47): the conservative quantile is wired into the decision
# --------------------------------------------------------------------------------------


def test_a_thinly_observed_candidate_is_penalised_more_than_a_confident_one() -> None:
    """The downside bound bites a thin estimate far harder than a confident one.

    Both predictions are real model output for the same action and situation; the only
    difference is how much evidence the model has (two observed replies versus twenty).
    Both are boundary-risky, so the decision path prices *both* at their lower bound -
    the risky one simply has a much wider bound, because the width is the model's own
    uncertainty and not a constant.
    """
    config = RuntimeConfig()
    state = _state()
    thin_model, thin_db = _trained([RISKY_YET_WARM] * 2, config)
    confident_model, confident_db = _trained([RISKY_YET_WARM] * 20, config)
    try:
        thin_prediction = thin_model.predict(action=ACTION, context=CONTEXT)
        confident_prediction = confident_model.predict(action=ACTION, context=CONTEXT)
        threshold = config.utility.conservative_risk_threshold

        # Both candidates are on the conservative path, and the estimates differ in
        # exactly the quantity the bound consumes.
        assert thin_prediction.boundary_risk > threshold
        assert confident_prediction.boundary_risk > threshold
        assert thin_prediction.uncertainty > confident_prediction.uncertainty + 0.2

        thin_bound = motivation.conservative_bound(
            thin_prediction, quantile=config.utility.downside_quantile
        )
        confident_bound = motivation.conservative_bound(
            confident_prediction, quantile=config.utility.downside_quantile
        )
        thin_share = thin_bound / thin_prediction.reply_probability
        confident_share = confident_bound / confident_prediction.reply_probability
        assert thin_share < 0.80
        assert confident_share > 0.95

        thin = _decide(config=config, prediction=thin_prediction, state=state)
        confident = _decide(config=config, prediction=confident_prediction, state=state)
        thin_breakdown = thin.assessments[0].breakdown
        confident_breakdown = confident.assessments[0].breakdown

        # Penalised more, and the penalty is the bound: the user-side value is the
        # mean-priced value scaled by exactly the bound-to-mean ratio.
        assert thin_breakdown.total < confident_breakdown.total
        assert thin_breakdown.user == pytest.approx(
            _mean_priced(config, thin_prediction, state) * thin_share
        )
        assert confident_breakdown.user == pytest.approx(
            _mean_priced(config, confident_prediction, state) * confident_share
        )
    finally:
        thin_db.close()
        confident_db.close()


def test_the_decision_consumes_the_models_own_lower_bound() -> None:
    """The number the game uses is the user model's own ``conservative_bound``.

    The game recomputes the model's closed form from ``Prediction.uncertainty`` (it is
    handed predictions, not the live model), so this test pins the two numbers
    together: at ``downside_quantile = 0.05`` - the tail probability
    ``config.user_model.conservative_z`` stands for - they agree. A hardcoded heuristic
    width cannot satisfy this.
    """
    config = RuntimeConfig()
    model, db = _trained([RISKY_YET_WARM] * 2, config)
    try:
        prediction = model.predict(action=ACTION, context=CONTEXT)
        game_bound = motivation.conservative_bound(
            prediction, quantile=config.utility.downside_quantile
        )
        model_bound = model.conservative_bound(prediction)
        assert game_bound == pytest.approx(model_bound, abs=1e-3)
        assert game_bound < prediction.reply_probability

        # ...and that number is what the decision is priced at. ``rel=1e-3`` rather
        # than the default: the model approximates the 95% quantile as 1.645 and the
        # game uses the exact 1.6449, so the two numbers differ in the fifth decimal
        # (a hardcoded heuristic is nowhere near this).
        state = _state()
        breakdown = _decide(
            config=config, prediction=prediction, state=state
        ).assessments[0].breakdown
        mean_user = _mean_priced(config, prediction, state)
        assert breakdown.user == pytest.approx(
            mean_user * model_bound / prediction.reply_probability, rel=1e-3
        )
        assert breakdown.user < mean_user
    finally:
        db.close()


def test_downside_quantile_changes_the_decision() -> None:
    """Changing the configured tail probability changes the decision.

    This is what "wired" means: with the old hardcoded heuristic the field had no
    reader at all, so both configurations below produced byte-identical decisions. The
    silence utility is read from the result instead of being hardcoded, so the test
    states the relation ("a stricter quantile cannot beat silence, a looser one can")
    rather than a magic threshold.
    """
    config = RuntimeConfig()
    model, db = _trained([RISKY_YET_WARM] * 2, config)
    try:
        prediction = model.predict(action=ACTION, context=CONTEXT)
        assert prediction.boundary_risk > config.utility.conservative_risk_threshold
        state = _state()

        # The mechanism: the configured quantile *is* the width of the bound.
        assert motivation.conservative_bound(
            prediction, quantile=0.01
        ) < motivation.conservative_bound(prediction, quantile=0.50)

        totals: list[float] = []
        for quantile in (0.01, 0.05, 0.20, 0.50):
            config.utility.downside_quantile = quantile
            result = _decide(config=config, prediction=prediction, state=state)
            totals.append(result.assessments[0].breakdown.total)
        # A wider tail -> a higher (less pessimistic) bound -> never a lower utility.
        assert totals == sorted(totals)
        assert totals[0] < totals[-1] - 0.01

        config.utility.downside_quantile = 0.01
        strict = _decide(config=config, prediction=prediction, state=state)
        config.utility.downside_quantile = 0.50
        loose = _decide(
            config=config, prediction=prediction, state=state, elapsed_seconds=86400.0
        )

        silence = strict.outcome.silence_utility
        assert strict.assessments[0].breakdown.total < silence
        assert loose.assessments[0].breakdown.total > silence
        assert strict.outcome.acted is False
        assert strict.outcome.reason == "no_candidate_beats_silence"
        assert loose.outcome.advantage > strict.outcome.advantage
        assert loose.outcome.acted is True
        assert loose.outcome.reason == "hazard_triggered"
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# Gap 2 (§46): the neutral and negative outcome terms of V_user
# --------------------------------------------------------------------------------------


def _outcome_prediction(
    positive: float, continues: float, *, uncertainty: float = 0.3
) -> Prediction:
    """Build a prediction whose reply probability and risk are held fixed."""
    return Prediction(
        reply_probability=0.8,
        positive_probability=positive,
        continue_probability=continues,
        boundary_risk=0.1,
        uncertainty=uncertainty,
    )


def test_a_negative_outcome_is_worth_less_than_a_neutral_one() -> None:
    """A predicted bad landing is penalised, not merely unrewarded.

    The two predictions are constructed so that the *pre-existing* good-outcome reward
    ``0.55 P_pos + 0.45 P_cont`` is exactly ``0.55`` for both, and the reply
    probability is the same: the old formula scored them identically (asserted below),
    so every difference measured here is produced by the new neutral/negative terms.
    One puts the whole non-positive mass into "answers but stays lukewarm" (neutral
    filler); the other splits it into warm-or-bad, with a substantial bad-landing mass.
    """
    config = RuntimeConfig()
    state = _state()
    # 0.55 * 0.1818... + 0.45 * 1.0 == 0.55 and 0.55 * 0.5909... + 0.45 * 0.5 == 0.55.
    neutral = _outcome_prediction(0.1 / 0.55, 1.0)
    negative = _outcome_prediction(0.325 / 0.55, 0.5)

    good_neutral, neutral_mass, bad_neutral = motivation.user_outcome_probabilities(neutral)
    _, neutral_mass_neg, bad_negative = motivation.user_outcome_probabilities(negative)
    assert neutral_mass > 0.8 and bad_neutral == pytest.approx(0.0)
    assert bad_negative > 0.2 and neutral_mass_neg < 0.25

    neutral_value = motivation.candidate_utility(
        candidate=_candidate(),
        state=state,
        prediction=neutral,
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=30.0,
    )
    negative_value = motivation.candidate_utility(
        candidate=_candidate(),
        state=state,
        prediction=negative,
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=30.0,
    )

    # What the old reward-only formula would have said for both predictions.
    old_formula = config.utility.user_gain * neutral.reply_probability * (
        0.55 * good_neutral + 0.45 * neutral.continue_probability
    )
    assert old_formula == pytest.approx(
        config.utility.user_gain
        * negative.reply_probability
        * (0.55 * negative.positive_probability + 0.45 * negative.continue_probability)
    )
    # The neutral term is present and subtracts from the good-outcome reward...
    assert neutral_value.user < old_formula - 0.05
    # ...and a real bad landing costs strictly more than filler does.
    assert negative_value.user < neutral_value.user - 0.05
    assert negative_value.total < neutral_value.total


def test_the_outcome_costs_are_discounted_by_the_models_uncertainty() -> None:
    """An imagined bad landing costs less than an observed one.

    The two predictions have *identical* outcome heads and reply probability and
    differ only in ``uncertainty``, so the good-outcome reward is the same for both.
    The neutral/negative costs are charged at ``user_outcome_confidence`` (= ``1 -
    uncertainty``), which is what keeps a cold model's prior from being billed as if it
    were knowledge - and what makes a confident model's bad news count.
    """
    config = RuntimeConfig()
    state = _state()
    observed = _outcome_prediction(0.3, 0.2, uncertainty=0.1)
    imagined = _outcome_prediction(0.3, 0.2, uncertainty=0.8)

    def _value(prediction: Prediction) -> float:
        return motivation.candidate_utility(
            candidate=_candidate(),
            state=state,
            prediction=prediction,
            config=config,
            boundary_risk_baseline=0.0,
            recent_contacts=0,
            hours_since_contact=30.0,
        ).user

    observed_value = _value(observed)
    imagined_value = _value(imagined)
    # A confident model believes its own bad news; a maximally unsure one discounts it.
    assert observed_value < imagined_value
    assert observed_value < 0.0 < imagined_value

    base_reward = 0.55 * observed.positive_probability + 0.45 * observed.continue_probability
    scale = config.utility.user_gain * observed.reply_probability
    observed_cost = base_reward - observed_value / scale
    imagined_cost = base_reward - imagined_value / scale
    assert observed_cost > 0.0
    assert imagined_cost == pytest.approx(
        observed_cost
        * motivation.user_outcome_confidence(imagined)
        / motivation.user_outcome_confidence(observed)
    )


def test_the_three_outcome_probabilities_partition_a_reply() -> None:
    """Good / neutral / bad are a partition of "the user replied", and never negative."""
    prediction = Prediction(
        reply_probability=0.5,
        positive_probability=0.7,
        continue_probability=0.4,
        boundary_risk=0.0,
        uncertainty=0.3,
    )
    good, neutral, bad = motivation.user_outcome_probabilities(prediction)
    assert (good, neutral, bad) == pytest.approx((0.7, 0.3 * 0.4, 0.3 * 0.6))
    assert good + neutral + bad == pytest.approx(1.0)
    assert min(good, neutral, bad) >= 0.0


# --------------------------------------------------------------------------------------
# Cold start
# --------------------------------------------------------------------------------------


def test_cold_start_is_not_priced_at_the_bound_and_is_not_mute() -> None:
    """A brand-new user gets normal contact, not a permanently silent character.

    With no observations the model's uncertainty is high, so the *bound* is
    pessimistic - but the cold-start boundary-risk prior is below
    ``conservative_risk_threshold``, so the game prices the candidate at the mean and
    the character is still allowed to speak once something actually pushes it. This
    test also pins that the pessimistic number is genuinely different, so "not priced
    at the bound" is a real statement rather than a tautology.
    """
    config = RuntimeConfig()
    model, db = _trained([], config)
    try:
        prediction = model.predict(
            action=ACTION, context={"busy_probability": 0.0, "hours_since_contact": 30.0}
        )
        assert prediction.cold_start is True
        assert model.observations == 0
        assert prediction.uncertainty > 0.5
        assert prediction.boundary_risk <= config.utility.conservative_risk_threshold

        bound = motivation.conservative_bound(
            prediction, quantile=config.utility.downside_quantile
        )
        assert bound < prediction.reply_probability - 0.2

        state = _state(impulse=0.95, restraint=0.2, pressure=0.9)
        candidate = _candidate(internal_need=0.95, unfinished_relevance=0.9)
        result = _decide(
            config=config,
            prediction=prediction,
            state=state,
            candidate=candidate,
            elapsed_seconds=86400.0,
            rng_seed=1,
        )
        breakdown = result.assessments[0].breakdown
        # Priced at the mean: the pessimistic bound was *not* applied.
        assert breakdown.user == pytest.approx(_mean_priced(config, prediction, state))
        assert breakdown.user > 0.0
        # ...and the character does eventually speak.
        assert result.outcome.advantage > 0.0
        assert result.outcome.acted is True
        assert result.outcome.reason == "hazard_triggered"
    finally:
        db.close()
