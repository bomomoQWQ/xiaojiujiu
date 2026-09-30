"""Isolated, pure decision-v2 user-utility calculation.

This module deliberately has no dependency on the legacy motivation game, Runtime,
hazard scheduling, or semantic providers.  It consumes the explicit probability intervals
from :mod:`companion_runtime.user_model_v2_types` and returns an auditable value object.

The conservative user utility is::

    U_user = vR * pR_lower + vC * pR_lower * pC_lower - cN * pN_upper

The acceptance target is intentionally not consumed: its current contract is conditional on
an explicit-feedback subset and therefore is not a population satisfaction probability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .user_model_v2_types import SupportStatus, Target, TargetPredictionV2

_COLD_SUPPORT = frozenset(
    {SupportStatus.PRIOR_ONLY, SupportStatus.SPARSE, SupportStatus.UNAVAILABLE}
)


def _non_negative(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


@dataclass(frozen=True, slots=True, kw_only=True)
class UserUtilityCoefficientsV2:
    """Frozen, non-negative coefficients for observable user outcomes.

    Set ``v_continue=0`` for a one-shot notification that does not seek a continuing
    conversation.  Low continuation probability then contributes neither benefit nor cost.
    """

    v_reply: float
    v_continue: float
    c_negative: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "v_reply", _non_negative("v_reply", self.v_reply))
        object.__setattr__(self, "v_continue", _non_negative("v_continue", self.v_continue))
        object.__setattr__(self, "c_negative", _non_negative("c_negative", self.c_negative))


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidatePolicyV2:
    """Facts used by the explicit cold-start/support-status consumer.

    The defaults are deliberately not exploration-safe: callers must positively describe a
    candidate as low-pressure, low-frequency and easy to ignore.  Risk facts always win.
    """

    low_pressure: bool = False
    low_frequency: bool = False
    easy_to_ignore: bool = False
    continuous_follow_up: bool = False
    sensitive: bool = False

    def __post_init__(self) -> None:
        for name in (
            "low_pressure",
            "low_frequency",
            "easy_to_ignore",
            "continuous_follow_up",
            "sensitive",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True, kw_only=True)
class UtilityDecompositionV2:
    """Every arithmetic term used to form conservative user utility."""

    reply_benefit: float
    continuation_benefit: float
    negative_cost: float
    total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "reply_benefit": self.reply_benefit,
            "continuation_benefit": self.continuation_benefit,
            "negative_cost": self.negative_cost,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class UsedBoundsV2:
    """Exact prediction bounds read by the formula (no inferred complements)."""

    p_reply_lower: float
    p_continue_given_reply_lower: float
    p_negative_upper: float

    def to_dict(self) -> dict[str, float]:
        return {
            "p_reply_lower": self.p_reply_lower,
            "p_continue_given_reply_lower": self.p_continue_given_reply_lower,
            "p_negative_upper": self.p_negative_upper,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class UserUtilityDecisionV2:
    """Pure decision result with policy gates and an auditable calculation."""

    utility: float
    blocked: bool
    eligible: bool
    decomposition: UtilityDecompositionV2
    used_bounds: UsedBoundsV2
    reasons: tuple[str, ...]
    support: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "utility": self.utility,
            "blocked": self.blocked,
            "eligible": self.eligible,
            "decomposition": self.decomposition.to_dict(),
            "used_bounds": self.used_bounds.to_dict(),
            "reasons": list(self.reasons),
            "support": dict(self.support),
        }


def _require_target(prediction: TargetPredictionV2, expected: Target, name: str) -> None:
    if not isinstance(prediction, TargetPredictionV2):
        raise TypeError(f"{name} must be a TargetPredictionV2")
    if prediction.target is not expected:
        raise ValueError(f"{name} must predict target {expected.value!r}")


def user_utility(
    *,
    reply: TargetPredictionV2,
    continuation: TargetPredictionV2,
    negative: TargetPredictionV2,
    coefficients: UserUtilityCoefficientsV2,
    candidate: CandidatePolicyV2 | None = None,
    boundary_blocked: bool = False,
    boundary_reasons: tuple[str, ...] = (),
    acceptance: TargetPredictionV2 | None = None,
) -> UserUtilityDecisionV2:
    """Calculate conservative v2 user utility and apply its local policy gates.

    Args:
        reply: Fixed-window response prediction; its lower bound is consumed.
        continuation: ``P(continue | reply)`` prediction; its lower bound is consumed.
        negative: Unconditional fixed-window negative-event prediction; its upper bound is
            consumed directly (it is *not* multiplied by response probability).
        coefficients: Non-negative outcome values/cost.
        candidate: Candidate properties used when any target has ``prior_only`` or ``sparse``
            support.  Such a candidate is allowed only when explicitly low-pressure,
            low-frequency, easy to ignore, non-sensitive, and not a continuous follow-up.
        boundary_blocked: Direct hard-boundary verdict.  It cannot be offset by utility.
        boundary_reasons: Auditable boundary reason identifiers/text.
        acceptance: Disabled by contract.  Passing it is rejected instead of silently using
            a conditional-feedback probability as population satisfaction.
    """

    _require_target(reply, Target.REPLY, "reply")
    _require_target(continuation, Target.CONTINUE, "continuation")
    _require_target(negative, Target.NEGATIVE, "negative")
    if not isinstance(coefficients, UserUtilityCoefficientsV2):
        raise TypeError("coefficients must be UserUtilityCoefficientsV2")
    if candidate is not None and not isinstance(candidate, CandidatePolicyV2):
        raise TypeError("candidate must be CandidatePolicyV2 or None")
    if not isinstance(boundary_blocked, bool):
        raise TypeError("boundary_blocked must be a bool")
    if not isinstance(boundary_reasons, tuple) or any(
        not isinstance(reason, str) or not reason.strip() for reason in boundary_reasons
    ):
        raise TypeError("boundary_reasons must be a tuple of non-empty strings")
    if acceptance is not None:
        if not isinstance(acceptance, TargetPredictionV2):
            raise TypeError("acceptance must be a TargetPredictionV2 or None")
        raise ValueError(
            "acceptance consumption is disabled: P(acceptance | explicit-feedback subset) "
            "is not population satisfaction"
        )

    scopes = {reply.scope_key, continuation.scope_key, negative.scope_key}
    if len(scopes) != 1:
        raise ValueError("all predictions must have the same scope_key")

    # An unavailable head has no numeric interval. Fail closed for benefits and
    # conservatively charge the full negative-event range until evidence exists.
    used = UsedBoundsV2(
        p_reply_lower=0.0 if reply.lower is None else float(reply.lower),
        p_continue_given_reply_lower=(
            0.0 if continuation.lower is None else float(continuation.lower)
        ),
        p_negative_upper=1.0 if negative.upper is None else float(negative.upper),
    )
    reply_benefit = coefficients.v_reply * used.p_reply_lower
    continuation_benefit = (
        coefficients.v_continue
        * used.p_reply_lower
        * used.p_continue_given_reply_lower
    )
    negative_cost = coefficients.c_negative * used.p_negative_upper
    total = reply_benefit + continuation_benefit - negative_cost
    decomposition = UtilityDecompositionV2(
        reply_benefit=reply_benefit,
        continuation_benefit=continuation_benefit,
        negative_cost=negative_cost,
        total=total,
    )

    predictions = (reply, continuation, negative)
    support = tuple((item.target.value, item.support.value) for item in predictions)
    reasons: list[str] = []
    blocked = boundary_blocked
    if boundary_blocked:
        reasons.append("hard_boundary_blocked")
        reasons.extend(f"boundary:{reason}" for reason in boundary_reasons)

    cold_targets = tuple(
        item.target.value for item in predictions if item.support in _COLD_SUPPORT
    )
    if cold_targets:
        reasons.append("limited_support:" + ",".join(cold_targets))
        facts = candidate or CandidatePolicyV2()
        if facts.continuous_follow_up:
            blocked = True
            reasons.append("limited_support_continuous_follow_up_blocked")
        if facts.sensitive:
            blocked = True
            reasons.append("limited_support_sensitive_candidate_blocked")
        if not facts.low_pressure:
            blocked = True
            reasons.append("limited_support_high_or_unspecified_pressure_blocked")
        if not facts.low_frequency:
            blocked = True
            reasons.append("limited_support_high_or_unspecified_frequency_blocked")
        if not facts.easy_to_ignore:
            blocked = True
            reasons.append("limited_support_not_ignorable_blocked")
        if not blocked:
            reasons.append("limited_support_safe_exploration_allowed")
    else:
        reasons.append("informative_or_stale_support_conservative_bounds_used")

    return UserUtilityDecisionV2(
        utility=total,
        blocked=blocked,
        eligible=not blocked,
        decomposition=decomposition,
        used_bounds=used,
        reasons=tuple(reasons),
        support=support,
    )


# Descriptive alias for consumers that prefer an explicit verb.
calculate_user_utility = user_utility


__all__ = [
    "CandidatePolicyV2",
    "UsedBoundsV2",
    "UserUtilityCoefficientsV2",
    "UserUtilityDecisionV2",
    "UtilityDecompositionV2",
    "calculate_user_utility",
    "user_utility",
]
