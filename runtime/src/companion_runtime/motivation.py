"""Motivational game: utility, silence, hazard and softmax selection.

This layer answers the only question that matters before the main LLM runs:
*do I actually act, and if so, which of my candidate thoughts wins?*

The decision is deliberately not a fixed threshold. It uses a hazard rate

``lambda(t) = lambda_0 * ln(1 + exp(beta * D(t)))``

with ``D(t) = max_i U_i(t) - U_silence(t)``, and then

``P(act in dt) = 1 - exp(-lambda(t) * dt)``.

Consequences that matter in practice: ``0.799 / 0.801`` no longer flip a
behaviour, the decision does not depend on heartbeat frequency, and rising
pressure makes action naturally more likely while low advantage keeps a little
randomness.

Hard boundaries never participate in the game: a blocked candidate is removed
from the pool before utilities are compared.

Two parts of the design were previously declared but not read here, and both are
now wired into the numbers below:

* a boundary-risky candidate is priced at a *lower quantile* of its predicted reply
  probability, at the tail probability ``config.utility.downside_quantile``, instead
  of at the mean - see :func:`conservative_bound`. The width of that bound is the
  user model's own uncertainty, so "be careful while you are unsure" is the model's
  estimate rather than a fixed constant;
* the user-side value now prices all three outcomes of a reply, not only the good
  one: a reply that is tolerated but lukewarm is *neutral filler* and a reply that
  lands badly and ends the exchange is *negative* - see
  :func:`user_outcome_probabilities` and the two weights below.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import NormalDist
from typing import Any, Mapping, Sequence

from .config import RuntimeConfig
from .typing import (
    CandidateIntent,
    DecisionOutcome,
    EmotionEvent,
    RuntimeState,
    UtilityBreakdown,
)
from .user_model import Prediction
from .utility import (
    clamp,
    local_day_key,
    logit,
    max_datetime,
    sigmoid,
    softmax,
    softplus,
    utcnow,
)

LOGGER = logging.getLogger("companion_runtime.motivation")

#: ``runtime_state.meta`` key holding the local day the contact counter belongs to.
CONTACT_DAY_META_KEY = "contact_day"

# --------------------------------------------------------------------------------------
# Constants of the conservative bound (§47) and of the outcome terms of V_user (§46)
#
# These are module constants rather than ``UtilityConfig`` fields because ``config.py``
# is owned by another change in flight. They are reported so the config owner can
# promote them to real knobs with the same defaults; every one of them is read below.
# --------------------------------------------------------------------------------------

#: The user model blends four per-target standard errors into one 0..1 figure with
#: ``uncertainty = clamp(1 - exp(-0.9 * mean_error))`` (see
#: ``UserInteractionModel.predict``). ``0.9`` is the scale of that blend and is the
#: only number :func:`conservative_bound` needs in order to recover the mean
#: logit-space standard error from a :class:`Prediction`.
#:
#: WARNING: this duplicates a literal that lives inside ``user_model.py``. It is a
#: faithful inverse of the documented mapping, not an independent calibration: if that
#: literal is ever changed, this constant must change with it (or the model should
#: expose the standard error directly). ``test_motivation_bounds.py`` pins the
#: agreement against the model's own ``conservative_bound`` so the drift cannot pass
#: unnoticed.
USER_MODEL_UNCERTAINTY_SCALE = 0.9

#: Weight of the *neutral* outcome in ``V_user``: a reply that is not positive but
#: keeps the exchange going is filler. It is a small cost and not a reward because
#: the user still spends attention on it; ``0.18`` is roughly a fifth of the ``+1.0``
#: a fully positive, continuing reply is worth. It is strictly positive so that
#: "acknowledged politely" scores below "welcome", which the previous
#: reward-only formula could not express at all. Charged at the model's confidence
#: (:func:`user_outcome_confidence`), so a cold prior is not billed at full price.
USER_NEUTRAL_OUTCOME_WEIGHT = 0.18

#: Weight of the *negative* outcome in ``V_user``: a reply that is not positive and
#: does not continue the exchange landed badly. ``1.10`` deliberately makes one bad
#: landing slightly worse than one perfect exchange is good (the good outcome is
#: worth at most ``0.55 + 0.45 = 1.0``): the asymmetry is the point of a downside
#: term, and the magnitude stays explainable in one line - it can cancel a warm
#: prediction, it cannot veto the whole candidate on its own (the boundary,
#: interrupt, repeat and risk costs do that part). Charged at the model's confidence
#: (:func:`user_outcome_confidence`), so a model that has actually watched the user
#: pays nearly full price and a brand-new one is not punished for imagined harm.
USER_NEGATIVE_OUTCOME_WEIGHT = 1.10

#: The tail probability ``UserInteractionModel.conservative_bound`` is calibrated at is
#: ``0.05`` - its ``config.user_model.conservative_z`` of 1.645 is the one-sided 95%
#: normal quantile - and that is why :func:`conservative_bound` reproduces the model's
#: own number at the default ``config.utility.downside_quantile``. The mapping is pinned
#: numerically by ``test_motivation_bounds.py`` rather than by a second constant here,
#: because a constant nobody reads is the defect this section exists to fix.

#: Standard normal distribution used to turn a tail probability into a z-score.
_STANDARD_NORMAL = NormalDist()


@dataclass(slots=True)
class MotivationInputs:
    """Everything the game needs, assembled by the reducer."""

    state: RuntimeState
    candidates: Sequence[CandidateIntent]
    predictions: Mapping[str, Prediction]
    boundary_allow_proactive: bool
    boundary_ids: Sequence[str] = ()
    boundary_risk_baseline: float = 0.0
    active_emotions: Sequence[EmotionEvent] = ()
    recent_contacts: int = 0
    hours_since_contact: float = 24.0
    cooldown_active: bool = False
    now: datetime | None = None
    elapsed_seconds: float = 0.0
    #: When set, the round is executed even under a blocking boundary so the
    #: decision can be inspected. It never makes the action executable.
    force_allow: bool = False


@dataclass(slots=True)
class CandidateAssessment:
    """A candidate together with its utility decomposition and prediction."""

    candidate: CandidateIntent
    breakdown: UtilityBreakdown
    prediction: Prediction

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "candidate": self.candidate.to_dict(),
            "utility": self.breakdown.to_dict(),
            "prediction": self.prediction.to_dict(),
        }


@dataclass(slots=True)
class MotivationResult:
    """The complete result of one motivational round."""

    outcome: DecisionOutcome
    assessments: list[CandidateAssessment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "outcome": self.outcome.to_dict(),
            "assessments": [assessment.to_dict() for assessment in self.assessments],
        }


# --------------------------------------------------------------------------------------
# Utility
# --------------------------------------------------------------------------------------


def silence_utility(
    *,
    state: RuntimeState,
    config: RuntimeConfig,
    boundary_risk: float,
    cooldown_active: bool,
    hours_since_contact: float,
) -> float:
    """Return the utility of staying silent, a *formal* available action.

    ``U_silence = B0 + aR + bB + cC + fI - eP^2``.

    Impulse *raises* the value of silence rather than lowering it: a character that
    wants to reach out but has nothing to say is aware of the pull, and holding back
    is what it is choosing. This is what makes loneliness alone insufficient -- the
    resting candidate gains value from impulse too, but not as much, so a concrete
    reason is required to tip the comparison. High pressure still makes silence
    progressively more unpleasant (the ``P^2`` term), which is what eventually
    breaks a long quiet stretch once something is actually pending.

    Args:
        state: Runtime state.
        config: Runtime configuration.
        boundary_risk: Current boundary risk level.
        cooldown_active: Whether a post-contact cooldown is in effect.
        hours_since_contact: Hours since the last contact.

    Returns:
        The silence utility (unbounded, typically around ``[-1, 1.5]``).
    """
    settings = config.silence
    cooldown_term = 1.0 if cooldown_active else clamp(1.0 - hours_since_contact / 24.0) * 0.5
    value = (
        settings.base
        + settings.restraint_gain * state.restraint
        + settings.boundary_gain * clamp(boundary_risk)
        + settings.cooldown_gain * cooldown_term
        + settings.impulse_gain * state.approach_impulse
        - settings.pressure_penalty * (state.pressure ** 2)
    )
    return value


def normal_quantile_z(quantile: float) -> float:
    """Return the one-sided standard normal quantile ``z`` of a tail probability.

    ``quantile = 0.05`` gives ``1.6449``, which is the value
    ``config.user_model.conservative_z`` (1.645) approximates in the user model - the
    reason the default configuration reproduces the model's own bound.

    The argument is clamped into ``[1e-6, 0.5]``: ``0.5`` is the median, where the
    bound is the mean itself (z = 0, i.e. no caution), and a non-finite or
    out-of-range value falls back to that same "no caution" end rather than raising -
    a bad configuration must not take the decision layer down.

    Args:
        quantile: Tail probability in ``(0, 0.5]``.

    Returns:
        A non-negative z-score.
    """
    if not math.isfinite(quantile):
        return 0.0
    tail = clamp(float(quantile), 1e-6, 0.5)
    return _STANDARD_NORMAL.inv_cdf(1.0 - tail)


def conservative_bound(
    prediction: Prediction,
    *,
    quantile: float,
    target: str = "reply_probability",
) -> float:
    """Return the conservative lower quantile of one predicted probability.

    This is the downside bound of design §47, and it is the number the motivational
    game prices a boundary-risky candidate at. It is the *same closed form* as
    :meth:`UserInteractionModel.conservative_bound`, re-derived from the
    :class:`Prediction` alone:

    ``bound = sigmoid(logit(P) - z(q) * e / 2)``, ``e = -ln(1 - U) / 0.9``

    where ``P`` is the predicted probability of ``target``, ``q`` is the configured
    tail probability (``config.utility.downside_quantile``), ``z(q)`` its one-sided
    normal quantile, ``U = prediction.uncertainty`` and ``0.9`` is the user model's
    uncertainty blend (:data:`USER_MODEL_UNCERTAINTY_SCALE`). The model builds ``U``
    from the mean logit-space standard error as ``U = 1 - exp(-0.9 e)``, so ``e`` is
    recoverable exactly and no information is invented here.

    Mapping, and what it means in practice:

    * at the default ``q = 0.05`` this returns the model's own
      ``conservative_bound`` value to within ~1e-5 (the model hardcodes
      ``conservative_z = 1.645``, this uses the exact 1.6449), so the decision path
      consumes the model's own bound number without needing the live model object -
      :func:`decide` is handed plain predictions, not the model. If the Runtime ever
      hands the live model into the round, this helper should delegate to
      ``UserInteractionModel.conservative_bound`` and rescale its result by
      ``z(q) / z(0.05)`` so that ``downside_quantile`` keeps governing the width;
    * because the width is ``z(q)``, the bound widens as the tail probability falls
      and collapses onto the mean at ``q = 0.5``. Changing
      ``config.utility.downside_quantile`` therefore changes the decision: that knob
      is the reader this quantity was missing;
    * a *thinly observed* user has a large ``U`` and therefore a bound far below the
      mean, while a well-observed one has a bound close to it. Caution is the model's
      uncertainty, not a constant;
    * with **no observations at all** the bound is real but unused in the default
      configuration: a cold-start prediction carries ``U ~ 0.8``, so the bound sits
      far below the mean, yet :func:`decide` only applies it above
      ``config.utility.conservative_risk_threshold`` and the cold-start
      ``boundary_risk`` prior is around ``0.20`` - below that threshold, because the
      model deliberately starts neutral about boundaries rather than suspicious (see
      ``test_cold_start_prediction_is_neutral_and_uncertain``). A brand-new user is
      therefore *not* priced at the pessimistic bound and the character is still
      allowed normal contact; the bound starts to bite once the model has actually
      learned that a boundary exists, or once the situation features themselves say
      the user is busy.

    Args:
        prediction: Prediction to bound.
        quantile: Tail probability; see :func:`normal_quantile_z`.
        target: Which predicted probability to bound.

    Returns:
        The lower quantile, clamped into ``[0, P]`` so it can never exceed the mean.
    """
    probability = clamp(float(getattr(prediction, target)), 0.0, 1.0)
    uncertainty = clamp(float(prediction.uncertainty), 0.0, 1.0 - 1e-9)
    mean_error = -math.log(max(1e-9, 1.0 - uncertainty)) / USER_MODEL_UNCERTAINTY_SCALE
    bounded = sigmoid(logit(probability) - normal_quantile_z(quantile) * mean_error * 0.5)
    return clamp(min(bounded, probability))


def user_outcome_probabilities(prediction: Prediction) -> tuple[float, float, float]:
    """Return ``(P_good, P_neutral, P_bad)`` conditional on the user replying.

    Design §46 asks for ``V_user = sum_y P(y | i) r(y)`` over the outcomes of the
    behaviour, but :class:`Prediction` carries two conditional heads rather than a
    full outcome distribution: ``positive_probability`` ("given a reply, is it
    positive") and ``continue_probability`` ("given a reply, does the exchange
    continue"). The three outcomes are the partition induced by those two:

    * ``P_good = P_pos`` - the reply is positive, whether or not it continues;
    * ``P_neutral = (1 - P_pos) * P_cont`` - the reply is not positive but the
      exchange goes on: polite filler, answered without being welcomed;
    * ``P_bad = (1 - P_pos) * (1 - P_cont)`` - the reply is not positive *and* the
      exchange stops: the message landed badly enough to end it.

    The three sum to 1 by construction, so nothing needs renormalising. The boundary
    dimension is deliberately *not* folded in here: an explicit breach is already
    priced by ``C_boundary``, ``C_interrupt`` and ``C_risk``, and counting it a fourth
    time would make one mistake cost four times.

    Args:
        prediction: Prediction to decompose.

    Returns:
        ``(P_good, P_neutral, P_bad)``, each in ``[0, 1]``.
    """
    positive = clamp(float(prediction.positive_probability))
    continues = clamp(float(prediction.continue_probability))
    not_positive = 1.0 - positive
    return positive, not_positive * continues, not_positive * (1.0 - continues)


def user_outcome_confidence(prediction: Prediction) -> float:
    """Return how much of the predicted outcome distribution should be believed.

    ``confidence = 1 - prediction.uncertainty``: the model's own ``uncertainty`` is a
    calibrated statement about how far its estimate can be trusted, so its complement
    is the weight the two outcome *costs* below are charged at.

    Why the costs are discounted and the good-outcome reward is not: with no
    observations the outcome heads are a prior, not a prediction. Charging a cold
    model the full price of the harm it merely imagines makes the character mute
    exactly when the design (§31) wants cheap, low-risk exploration - the
    ``test_scenario_2d_a_less_restrained_character_does_reach_out`` margin is 0.04
    utility, and a full-strength penalty consumes it. A model that *has* observed the
    user is believed at close to full weight, which is the case the outcome costs
    exist for. Note that this is the same statement as the conservative bound, read in
    the other direction: the bound makes an unreliable estimate of a *good* outcome
    less attractive, and this keeps an unreliable estimate of a *bad* outcome from
    being treated as fact.

    Args:
        prediction: Prediction to weigh.

    Returns:
        A factor in ``[0, 1]``; ``0`` when the model is maximally uncertain.
    """
    return clamp(1.0 - float(prediction.uncertainty))


def candidate_utility(
    *,
    candidate: CandidateIntent,
    state: RuntimeState,
    prediction: Prediction,
    config: RuntimeConfig,
    boundary_risk_baseline: float,
    recent_contacts: int,
    hours_since_contact: float,
    emotion_alignment: float = 0.0,
    blocked: bool = False,
    block_reason: str | None = None,
    conservative_reply: float | None = None,
) -> UtilityBreakdown:
    """Compute one candidate's utility decomposition.

    ``U_i = V_internal + V_user + V_relation - C_boundary - C_interrupt - C_repeat - C_risk``

    with the user-side value priced over all three reply outcomes rather than only the
    good one:

    ``V_user = g_u * P_R * [0.55 P_good + 0.45 P_cont - c * (w_neu P_neutral + w_neg P_bad)]``

    ``P_good/P_neutral/P_bad`` come from :func:`user_outcome_probabilities` and ``c``
    is :func:`user_outcome_confidence`. The first two reward terms are the previous
    formula, unchanged, so the calibrated scale of a good exchange is preserved:
    ``0.45 P_cont`` rewards a continuing exchange on top of a positive one. The two new
    terms are costs with documented weights (:data:`USER_NEUTRAL_OUTCOME_WEIGHT`,
    :data:`USER_NEGATIVE_OUTCOME_WEIGHT`), so a predicted bad landing subtracts value
    instead of merely failing to add any - charged at the model's confidence, because
    an unobserved outcome distribution is a prior rather than a prediction.

    For boundary-risky candidates ``P_R`` is replaced by a conservative lower quantile
    (:func:`conservative_bound`) instead of the mean, so thin data makes risky
    behaviour cautious automatically. The bound is never allowed above the mean.

    Args:
        candidate: Candidate under evaluation.
        state: Runtime state.
        prediction: Predicted user reaction.
        config: Runtime configuration.
        boundary_risk_baseline: Risk implied by past boundaries.
        recent_contacts: Contacts within the repeat window.
        hours_since_contact: Hours since last contact.
        emotion_alignment: How well the candidate matches the current emotion.
        blocked: Whether a hard constraint already removed the candidate.
        block_reason: Reason for blocking.
        conservative_reply: Lower quantile of the reply probability; ``None`` prices
            the candidate at the predicted mean.

    Returns:
        A :class:`UtilityBreakdown`.
    """
    settings = config.utility
    values = state.values

    # A concrete reason to speak - an unfinished matter that is due, or a strong
    # need - is what actually separates "reaching out" from "staying quiet". The
    # internal-need term is scaled down deliberately: the resting "I just want to be
    # in contact" candidate always carries some need, and if that alone were enough
    # the character would speak from loneliness, which is exactly what must not
    # happen.
    urgency = 0.0
    if candidate.unfinished_relevance > 0.0 or candidate.internal_need > 0.0:
        urgency = settings.urgency_gain * max(
            candidate.unfinished_relevance * 1.25, candidate.internal_need * 0.8
        )

    internal = settings.internal_gain * (
        0.45 * candidate.internal_need
        + 0.35 * candidate.unfinished_relevance
        + 0.20 * clamp(emotion_alignment)
    ) + urgency
    reply_mean = prediction.reply_probability
    # The downside bound is a *lower* bound: a caller that hands in something larger
    # than the mean is clamped, never trusted.
    reply = reply_mean if conservative_reply is None else min(float(conservative_reply), reply_mean)
    good, neutral_outcome, bad_outcome = user_outcome_probabilities(prediction)
    confidence = user_outcome_confidence(prediction)
    user = settings.user_gain * reply * (
        0.55 * good
        + 0.45 * prediction.continue_probability
        - confidence
        * (
            USER_NEUTRAL_OUTCOME_WEIGHT * neutral_outcome
            + USER_NEGATIVE_OUTCOME_WEIGHT * bad_outcome
        )
    )
    relation = settings.relation_gain * (
        0.5 * values.relationship_maintenance * clamp(candidate.unfinished_relevance + 0.35)
        + 0.3 * values.user_care * prediction.positive_probability
        + 0.2 * values.stability_commitment * prediction.continue_probability
    )

    boundary_cost = settings.boundary_cost_gain * values.boundary_respect * max(
        prediction.boundary_risk, clamp(boundary_risk_baseline)
    )
    interrupt_cost = settings.interrupt_gain * (
        0.6 * prediction.boundary_risk + 0.4 * (1.0 - prediction.reply_probability)
    )
    tolerance = max(1, settings.repeat_contact_tolerance)
    repeat_pressure = max(0.0, recent_contacts - tolerance + 1) / tolerance
    repeat_cost = settings.repeat_gain * repeat_pressure * (0.6 + 0.4 * prediction.boundary_risk)
    risk_cost = settings.risk_gain * prediction.uncertainty * (0.5 + prediction.boundary_risk)

    total = (
        internal
        + user
        + relation
        - boundary_cost
        - interrupt_cost
        - repeat_cost
        - risk_cost
        - settings.uncertainty_penalty * prediction.uncertainty
    )
    if hours_since_contact > 48.0:
        # Very long silence slightly raises the value of re-establishing contact.
        total += 0.05
    if blocked:
        total = float("-inf")

    breakdown = UtilityBreakdown(
        candidate_id=candidate.candidate_id,
        internal=internal,
        user=user,
        relation=relation,
        boundary_cost=boundary_cost,
        interrupt_cost=interrupt_cost,
        repeat_cost=repeat_cost,
        risk_cost=risk_cost,
        total=total,
        blocked=blocked,
        block_reason=block_reason,
        reply_probability=prediction.reply_probability,
        positive_probability=prediction.positive_probability,
        continue_probability=prediction.continue_probability,
        boundary_risk=prediction.boundary_risk,
        uncertainty=prediction.uncertainty,
    )
    return breakdown


def hazard_rate(advantage: float, *, config: RuntimeConfig) -> float:
    """Return the action hazard ``lambda_0 * softplus(beta * D)``.

    Args:
        advantage: ``max_i U_i - U_silence``.
        config: Runtime configuration.

    Returns:
        A non-negative hazard rate per second.
    """
    settings = config.utility
    return settings.hazard_base * softplus(settings.hazard_beta * advantage, beta=1.0)


def action_probability(hazard: float, delta_t: float) -> float:
    """Return ``1 - exp(-lambda * dt)``, the probability of acting within ``dt``."""
    if delta_t <= 0.0 or hazard <= 0.0:
        return 0.0
    return clamp(1.0 - math.exp(-max(0.0, hazard * delta_t)))


def _exp_neg(value: float) -> float:
    """Return ``exp(-value)`` guarded against underflow."""
    return math.exp(-max(0.0, value))


def precondition_holds(candidate: CandidateIntent, situation_text: str) -> tuple[bool, str | None]:
    """Check a candidate's preconditions against the current situation.

    Preconditions are natural language, so the check is a coarse keyword-presence
    test over the working situation: a precondition counts as satisfied when at
    least one of its content tokens appears there. Missing evidence therefore
    blocks the candidate, which is the safe direction - a thought whose
    preconditions cannot be shown to hold should not become an action.

    Args:
        candidate: Candidate to test.
        situation_text: Current working-situation text.

    Returns:
        ``(holds, failed_condition)``.
    """
    if not candidate.preconditions:
        return True, None
    lowered = situation_text.lower()
    for condition in candidate.preconditions:
        keywords = _condition_tokens(condition)
        if not keywords:
            continue
        if not any(keyword in lowered for keyword in keywords):
            return False, condition
    return True, None


#: Filler words that carry no discriminating power in a precondition.
_CONDITION_STOPWORDS = frozenset(
    {"需要", "必须", "如果", "已经", "当前", "条件", "用户", "the", "a", "is", "if", "must"}
)


def _condition_tokens(condition: str) -> list[str]:
    """Extract discriminating tokens from a natural-language precondition.

    Latin words are used whole; CJK text is decomposed into bigrams (plus single
    characters for very short conditions) so that "需要用户在线" can be matched
    against a situation that mentions "在线" without a word segmenter.
    """
    from .utility import topic_tokens, tokenize

    tokens = set(topic_tokens(condition)) | set(tokenize(condition))
    return [token for token in tokens if token not in _CONDITION_STOPWORDS]


def is_candidate_proactive(candidate: CandidateIntent) -> bool:
    """Return whether a candidate represents an unprompted contact."""
    from .candidate import is_candidate_proactive as _impl

    return _impl(candidate)


# --------------------------------------------------------------------------------------
# Approach / restraint / pressure dynamics
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class DriveInputs:
    """Inputs of the target impulse / restraint computation.

    ``x(t) = [E, O, M, A, C, B, W, U]``: emotion tendency, unfinished matters,
    activated memories, time since contact, recent-contact cooldown, user
    boundary, user busyness and current uncertainty.
    """

    emotion_tendency: float = 0.0
    unfinished: float = 0.0
    memory_activation: float = 0.0
    hours_since_contact: float = 0.0
    recent_contact_ratio: float = 0.0
    boundary_pressure: float = 0.0
    user_busy: float = 0.0
    uncertainty: float = 0.0
    mood_valence: float = 0.0


@dataclass(slots=True)
class DriveTargets:
    """Target values of the approach dynamics."""

    impulse: float = 0.0
    restraint: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-serialisable rendering."""
        return {"impulse": round(self.impulse, 6), "restraint": round(self.restraint, 6)}


def target_drives(inputs: DriveInputs, *, state: RuntimeState, config: RuntimeConfig) -> DriveTargets:
    """Return target impulse and restraint, ``sigmoid(theta^T x + b)``.

    The weights are compiled from the value profile, so the same event yields
    different dynamics for different personalities. The dominant long-run term is
    the absence term: with no exchange at all, impulse rises above restraint and
    the character slowly becomes willing to speak first.

    Args:
        inputs: Feature block.
        state: Runtime state (provides the value profile).
        config: Runtime configuration.

    Returns:
        A :class:`DriveTargets`.
    """
    values = state.values
    saturation = max(1.0, config.drive.absence_saturation_hours)
    absence_term = min(1.0, inputs.hours_since_contact / saturation)

    impulse_logit = (
        -1.60
        + 1.60 * max(0.0, inputs.emotion_tendency)
        + 0.90 * values.user_care * inputs.unfinished
        + 0.60 * inputs.memory_activation
        # The absence term is the long-run driver. It is deliberately strong enough
        # that a long silence visibly raises impulse (so the character reaches out
        # when a concrete reason appears), while the *silent* default is preserved by
        # restraint and the silence utility rising with it: loneliness alone still
        # never crosses the line. See `test_scenario_2*` for both halves.
        + (1.55 + 0.30 * values.relationship_maintenance) * absence_term
        + 0.20 * values.curiosity
        - (0.55 + 0.35 * values.boundary_respect) * inputs.boundary_pressure
        - 0.40 * inputs.user_busy
        - 0.35 * inputs.recent_contact_ratio
        + 0.30 * max(0.0, -inputs.mood_valence)
    )
    restraint_logit = (
        -0.40
        + 0.75 * values.boundary_respect
        + 0.35 * values.stability_commitment
        - 0.25 * values.conflict_directness
        + 0.70 * inputs.boundary_pressure
        + 0.45 * inputs.user_busy
        + 0.50 * inputs.recent_contact_ratio
        + 0.35 * inputs.uncertainty
        - 0.20 * max(0.0, inputs.emotion_tendency)
    )
    return DriveTargets(
        impulse=clamp(sigmoid(impulse_logit)),
        restraint=clamp(sigmoid(restraint_logit)),
    )


def step_drives(
    *,
    state: RuntimeState,
    targets: DriveTargets,
    config: RuntimeConfig,
    dt_seconds: float,
) -> None:
    """Advance impulse, restraint and pressure by ``dt_seconds``.

    ``dI/dt = (I_hat - I) / tau_I``, ``dR/dt = (R_hat - R) / tau_R`` and

    ``dP/dt = kappa_+ (1 - P) S_beta(I - R) - kappa_- P S_beta(R - I)``.

    The analytic first-order step makes a single lazy tick safe even across many
    hours of absence. State is mutated in place.

    Args:
        state: Runtime state to mutate.
        targets: Target impulse and restraint.
        config: Runtime configuration.
        dt_seconds: Elapsed seconds.
    """
    if dt_seconds <= 0.0:
        return
    settings = config.drive
    alpha_i = 1.0 - exp_neg(dt_seconds / max(1.0, settings.tau_impulse_seconds))
    alpha_r = 1.0 - exp_neg(dt_seconds / max(1.0, settings.tau_restraint_seconds))
    state.approach_impulse = clamp(
        state.approach_impulse + (targets.impulse - state.approach_impulse) * alpha_i
    )
    state.restraint = clamp(state.restraint + (targets.restraint - state.restraint) * alpha_r)

    gap = state.approach_impulse - state.restraint
    pressure = state.pressure
    accumulation = settings.kappa_plus * (1.0 - pressure) * softplus(gap, beta=settings.beta)
    dissipation = settings.kappa_minus * pressure * softplus(-gap, beta=settings.beta)
    # Scale by a bounded effective dt so an extremely long absence cannot produce
    # an integration artefact.
    effective_dt = min(dt_seconds, settings.max_pressure_step_seconds)
    state.pressure = clamp(pressure + (accumulation - dissipation) * effective_dt)


def exp_neg(value: float) -> float:
    """Return ``exp(-value)`` clamped at zero for negative inputs."""
    return math.exp(-max(0.0, value))


def _latest(*values: datetime | None) -> datetime | None:
    """Return the most recent of ``values``, ignoring ``None``."""
    present = [value for value in values if value is not None]
    return max(present) if present else None


def release_after_contact(state: RuntimeState, *, config: RuntimeConfig, now: datetime) -> None:
    """Apply the post-contact transition.

    ``I <- (1 - rho_I) I``, ``P <- (1 - rho_P) P`` and ``R <- min(1, R + rho_R)``,
    plus a cooldown so the character does not immediately speak again.

    The two time anchors written here are monotone: a delayed report about a
    contact that happened earlier than the one already recorded must not move
    ``last_contact_at`` or ``cooldown_until`` backwards, or the absence term and
    the cooldown would both be recomputed as if the newer contact never happened.

    Args:
        state: Runtime state to mutate.
        config: Runtime configuration.
        now: Contact time.
    """
    settings = config.drive
    state.approach_impulse = clamp(state.approach_impulse * (1.0 - settings.impulse_release))
    state.pressure = clamp(state.pressure * (1.0 - settings.pressure_release))
    state.restraint = clamp(state.restraint + settings.restraint_boost)
    state.cooldown_until = max_datetime(
        state.cooldown_until, now + timedelta(seconds=settings.cooldown_seconds)
    )
    state.last_contact_at = max_datetime(state.last_contact_at, now)


def rollover_contact_day(state: RuntimeState, *, now: datetime) -> bool:
    """Reset the daily proactive-contact counter when the local day changes.

    The counter is a *daily* budget, so it has to be cleared on the local
    calendar boundary before anything reads or increments it - otherwise the
    first delivery of a new day is added to yesterday's total and the character
    silently stops speaking once the budget appears exhausted.

    Args:
        state: Runtime state to mutate.
        now: Reference time.

    Returns:
        ``True`` when this call crossed into a new day and reset the counter.
    """
    day_key = local_day_key(now)
    if state.meta.get(CONTACT_DAY_META_KEY) == day_key:
        return False
    state.meta = dict(state.meta) | {CONTACT_DAY_META_KEY: day_key}
    state.contact_count_today = 0
    return True


def cooldown_remaining(state: RuntimeState, now: datetime) -> float:
    """Return seconds remaining in the post-contact cooldown (0 when inactive)."""
    if state.cooldown_until is None:
        return 0.0
    return max(0.0, (state.cooldown_until - now).total_seconds())


def empirical_impulse_half_life(state: RuntimeState, config: RuntimeConfig) -> float:
    """Diagnostic helper: seconds for the current impulse gap to halve."""
    tau = config.drive.tau_impulse_seconds
    return tau * math.log(2.0) if tau > 0 else 0.0


# --------------------------------------------------------------------------------------
# The round itself
# --------------------------------------------------------------------------------------


def decide(
    inputs: MotivationInputs,
    *,
    config: RuntimeConfig,
    rng: random.Random | None = None,
    situation_text: str = "",
    emotion_alignment: Mapping[str, float] | None = None,
) -> MotivationResult:
    """Run one motivational round and decide whether to act.

    Candidates whose predicted ``boundary_risk`` exceeds
    ``config.utility.conservative_risk_threshold`` are priced at
    :func:`conservative_bound` at the configured ``config.utility.downside_quantile``
    instead of at their mean reply probability; every other candidate is priced at the
    mean. Hard boundaries are still applied before any of this, as blocks rather than
    as costs.

    Args:
        inputs: Assembled inputs.
        config: Runtime configuration.
        rng: Random source (hazard draw and softmax are stochastic).
        situation_text: Working-situation text used for precondition checks.
        emotion_alignment: Per-candidate emotion alignment in ``[0, 1]``.

    Returns:
        A :class:`MotivationResult` with the outcome and every assessment.
    """
    source = rng or random.Random()
    now = inputs.now or utcnow()
    state = inputs.state
    alignments = dict(emotion_alignment or {})

    cooldown_active = bool(
        state.cooldown_until is not None and state.cooldown_until > now
    ) or inputs.cooldown_active

    silence = silence_utility(
        state=state,
        config=config,
        boundary_risk=inputs.boundary_risk_baseline,
        cooldown_active=cooldown_active,
        hours_since_contact=inputs.hours_since_contact,
    )

    assessments: list[CandidateAssessment] = []
    # ``force_allow`` exists so that tests and diagnostic tooling can *run* a
    # round under a boundary and observe the decision it would have produced. A
    # forced round still refuses to act: the hard gate is not a momentum term,
    # and the returned utilities are the ones that would have applied.
    boundary_denied = not inputs.boundary_allow_proactive
    hard_blocked = boundary_denied

    for candidate in inputs.candidates:
        prediction = inputs.predictions.get(candidate.candidate_id)
        if prediction is None:
            LOGGER.debug("Candidate %s has no prediction; skipping", candidate.candidate_id)
            continue
        blocked = False
        reason: str | None = None
        if hard_blocked and is_candidate_proactive(candidate):
            blocked = True
            reason = "boundary_blocks_proactive"
        else:
            holds, failed = precondition_holds(candidate, situation_text)
            if not holds:
                blocked = True
                reason = f"precondition_failed:{failed}"
            elif cooldown_active:
                blocked = True
                reason = "cooldown_active"
            elif inputs.recent_contacts >= config.drive.max_contacts_per_day:
                blocked = True
                reason = "daily_contact_budget_exhausted"

        conservative = None
        if prediction.boundary_risk > config.utility.conservative_risk_threshold:
            # §47: a boundary-risky candidate is priced at its lower quantile at the
            # configured tail probability, using the user model's own uncertainty.
            conservative = conservative_bound(
                prediction, quantile=config.utility.downside_quantile
            )
            LOGGER.debug(
                "Candidate %s is boundary-risky (%.3f); pricing reply probability at Q%.4f = %.4f",
                candidate.candidate_id,
                prediction.boundary_risk,
                config.utility.downside_quantile,
                conservative,
            )

        breakdown = candidate_utility(
            candidate=candidate,
            state=state,
            prediction=prediction,
            config=config,
            boundary_risk_baseline=inputs.boundary_risk_baseline,
            recent_contacts=inputs.recent_contacts,
            hours_since_contact=inputs.hours_since_contact,
            emotion_alignment=alignments.get(candidate.candidate_id, 0.0),
            blocked=blocked,
            block_reason=reason,
            conservative_reply=conservative,
        )
        assessments.append(
            CandidateAssessment(candidate=candidate, breakdown=breakdown, prediction=prediction)
        )

    eligible = [a for a in assessments if not a.breakdown.blocked and a.breakdown.total > silence]
    best = max((a.breakdown.total for a in assessments if not a.breakdown.blocked), default=float("-inf"))
    advantage = (best - silence) if best != float("-inf") else float("-inf")

    outcome = DecisionOutcome(
        acted=False,
        reason="no_eligible_candidate",
        utilities=[a.breakdown for a in assessments],
        silence_utility=silence,
        advantage=advantage if advantage != float("-inf") else -99.0,
        delta_t=0.0,
        next_wake_at=None,
    )

    if not eligible:
        # Order matters: a hard boundary is reported as the cause even when other
        # reasons would also have sufficed, because it is the binding one.
        if hard_blocked:
            outcome.reason = "blocked_by_boundary"
        elif cooldown_active:
            outcome.reason = "cooldown_active"
        elif all(a.breakdown.blocked for a in assessments) and assessments:
            outcome.reason = "all_candidates_blocked"
        else:
            outcome.reason = "no_candidate_beats_silence"
        outcome.next_wake_at = now + timedelta(seconds=config.utility.max_sleep_seconds)
        return MotivationResult(outcome=outcome, assessments=assessments)

    hazard = hazard_rate(advantage, config=config)
    outcome.hazard = hazard
    # The hazard is a rate per second, so it must be integrated over the time that
    # actually elapsed since the previous tick - not over an arbitrary window.
    outcome.delta_t = max(0.0, float(inputs.elapsed_seconds))
    probability = 1.0 - _exp_neg(hazard * outcome.delta_t)
    outcome.action_probability = probability

    if source.random() > probability:
        outcome.reason = "hazard_not_triggered"
        outcome.next_wake_at = _next_wake(now, config, probability, outcome.delta_t)
        return MotivationResult(outcome=outcome, assessments=assessments)

    scores = [a.breakdown.total for a in eligible]
    weights = softmax(scores, temperature=config.utility.temperature)
    index = _sample_index(weights, source)
    chosen = eligible[index]
    outcome.acted = True
    outcome.reason = "hazard_triggered"
    outcome.chosen_candidate_id = chosen.candidate.candidate_id
    outcome.selected_probability = weights[index]
    outcome.next_wake_at = now + timedelta(hours=6)
    return MotivationResult(outcome=outcome, assessments=assessments)


def _next_wake(
    now: datetime, config: RuntimeConfig, probability: float, delta_t: float
) -> datetime:
    """Return the next endogenous wake-up time implied by the hazard draw."""
    if probability <= 1e-9:
        delay = config.scheduler.max_interval_seconds
    else:
        delay = delta_t / max(probability, 1e-3)
    delay = clamp(delay, config.utility.min_sleep_seconds, config.scheduler.max_interval_seconds)
    return now + timedelta(seconds=delay)


def _sample_index(weights: Sequence[float], rng: random.Random) -> int:
    """Draw an index from a probability vector."""
    threshold = rng.random()
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += weight
        if threshold <= cumulative:
            return index
    return len(weights) - 1


def tie_break_softmax(
    totals: Sequence[float], *, config: RuntimeConfig
) -> list[float]:
    """Return the softmax distribution over candidate utilities (for inspection)."""
    return softmax(list(totals), temperature=config.utility.temperature)


def reproduce_advantage(
    *,
    totals: Sequence[float],
    blocked: Sequence[bool],
    silence: float,
) -> float:
    """Return ``max_i U_i - U_silence`` over the non-blocked candidates."""
    usable = [t for t, b in zip(totals, blocked) if not b]
    if not usable:
        return float("-inf")
    return max(usable) - silence


def probability_of_silence(*, hazard: float, delta_t: float) -> float:
    """Return the probability that the Runtime keeps silent across ``delta_t``."""
    return clamp(_exp_neg(hazard * max(0.0, delta_t)))
