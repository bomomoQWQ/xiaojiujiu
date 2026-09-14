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
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from .utility import clamp, sigmoid, softmax, softplus, utcnow

LOGGER = logging.getLogger("companion_runtime.motivation")


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

    For boundary-risky candidates the user benefit uses a conservative lower
    quantile instead of the mean, so thin data makes risky behaviour cautious
    automatically.

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
        conservative_reply: Lower quantile of the reply probability.

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
    reply = conservative_reply if conservative_reply is not None else reply_mean
    if prediction.boundary_risk > 0.25 and conservative_reply is not None:
        reply = min(reply, reply_mean)
    user = settings.user_gain * (
        reply * (0.55 * prediction.positive_probability + 0.45 * prediction.continue_probability)
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

    Args:
        state: Runtime state to mutate.
        config: Runtime configuration.
        now: Contact time.
    """
    settings = config.drive
    state.approach_impulse = clamp(state.approach_impulse * (1.0 - settings.impulse_release))
    state.pressure = clamp(state.pressure * (1.0 - settings.pressure_release))
    state.restraint = clamp(state.restraint + settings.restraint_boost)
    state.cooldown_until = now + timedelta(seconds=settings.cooldown_seconds)
    state.last_contact_at = now


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
            conservative = _conservative_reply(candidate, prediction)

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


def _conservative_reply(
    candidate: CandidateIntent, prediction: Prediction, *, z: float = 0.12
) -> float:
    """Return a conservative reply bound for a risky candidate.

    The bound is an approximate lower quantile: it widens with model uncertainty
    and with how boundary-sensitive the behaviour class is, so thin data makes
    risky behaviour cautious automatically.

    Args:
        candidate: Candidate under evaluation.
        prediction: Prediction to bound.
        z: Base quantile width.

    Returns:
        The lower quantile of the reply probability.
    """
    probability = prediction.reply_probability
    if candidate.type in {"contact", "check_in"}:
        type_factor = 2.0
    elif candidate.type in {"follow_up", "repair"}:
        type_factor = 1.4
    else:
        type_factor = 1.0
    return clamp(probability - z * type_factor * (0.5 + prediction.uncertainty))


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
