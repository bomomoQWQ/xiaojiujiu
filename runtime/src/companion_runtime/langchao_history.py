"""Pure settled-history conditioning for future Langchao reward forecasts.

Realized outcomes are observations, never bonus terms.  This module turns only
settled binary history into a versioned probability estimate and substitutes that
estimate for the corresponding *future expected* token forecast.  Unknown,
censored and unattributable observations carry no sample weight.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from .langchao_reward import OutcomeForecast
from .langchao_shadow import ShadowCandidateInput
from .langchao_types import OutcomeStatus, SettlementType

LANGCHAO_HISTORY_ESTIMATOR_VERSION = "langchao.settled-history.beta.v1"
_SETTLED_KEYS = frozenset({"reply", "continuation", "negative"})


@dataclass(frozen=True, slots=True, kw_only=True)
class SettledOutcomeObservation:
    """One immutable binary observation from an actual/correction OutcomeToken."""

    observation_id: str
    template_key: str
    outcome_key: str
    observed: bool | None

    def __post_init__(self) -> None:
        for name in ("observation_id", "template_key", "outcome_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.observed is not None and not isinstance(self.observed, bool):
            raise TypeError("observed must be bool or None")


def estimate_settled_probability(
    history: Iterable[SettledOutcomeObservation], *, template_key: str,
    outcome_key: str, prior_probability: float | None,
    prior_strength: float = 2.0,
) -> tuple[float | None, int]:
    """Return a Beta posterior mean and effective settled sample count.

    The explicit forecast is the prior mean.  With no usable settled observations the
    forecast is returned unchanged, making unknown/censored history a strict no-op.
    Duplicate observation ids are collapsed defensively for replay/restart safety.
    """

    if prior_probability is not None and not 0.0 <= prior_probability <= 1.0:
        raise ValueError("prior_probability must be between zero and one")
    if prior_strength < 0.0:
        raise ValueError("prior_strength must be non-negative")
    seen: set[str] = set()
    positive = 0
    total = 0
    for item in history:
        if not isinstance(item, SettledOutcomeObservation):
            raise TypeError("history must contain SettledOutcomeObservation values")
        if item.observation_id in seen:
            continue
        seen.add(item.observation_id)
        if item.template_key != template_key or item.outcome_key != outcome_key or item.observed is None:
            continue
        total += 1
        positive += int(item.observed)
    if total == 0:
        return prior_probability, 0
    if prior_probability is None or prior_strength == 0.0:
        return positive / total, total
    return ((prior_probability * prior_strength) + positive) / (prior_strength + total), total


def condition_forecasts_from_history(
    item: ShadowCandidateInput,
    history: Iterable[SettledOutcomeObservation],
    *, prior_strength: float = 2.0,
) -> ShadowCandidateInput:
    """Condition a candidate's expected forecasts without adding realized amounts."""

    observations = tuple(history)
    token_keys = {
        token.token_id: token.outcome_key
        for token in item.reward.outcome_tokens
        if token.settlement_type is SettlementType.EXPECTED
    }
    conditioned: list[OutcomeForecast] = []
    for forecast in item.forecasts:
        outcome_key = token_keys[forecast.token_id]
        if outcome_key not in _SETTLED_KEYS:
            conditioned.append(forecast)
            continue
        probability, samples = estimate_settled_probability(
            observations, template_key=item.reward.template_key,
            outcome_key=outcome_key, prior_probability=forecast.probability,
            prior_strength=prior_strength,
        )
        if samples == 0:
            conditioned.append(forecast)
            continue
        conditioned.append(replace(
            forecast,
            probability=probability,
            support=f"settled_history:{samples}",
            status="conditioned",
            source_version=LANGCHAO_HISTORY_ESTIMATOR_VERSION,
        ))
    return replace(
        item,
        forecasts=tuple(conditioned),
        source_refs=tuple(dict.fromkeys((*item.source_refs, *(obs.observation_id for obs in observations if obs.observed is not None)))),
    )


def observations_from_rows(rows: Iterable[Mapping[str, object]]) -> tuple[SettledOutcomeObservation, ...]:
    """Decode repository rows while treating incomplete statuses as non-observations."""

    result: list[SettledOutcomeObservation] = []
    for row in rows:
        status = OutcomeStatus(str(row["status"]))
        settlement = SettlementType(str(row["settlement_type"]))
        if settlement not in {SettlementType.ACTUAL, SettlementType.CORRECTION}:
            continue
        observed: bool | None
        if status is OutcomeStatus.CONFIRMED:
            observed = True
        elif status is OutcomeStatus.NOT_OBSERVED:
            observed = False
        elif status is OutcomeStatus.CORRECTED:
            observed = float(row["base_amount"]) != 0.0
        else:
            observed = None
        result.append(SettledOutcomeObservation(
            observation_id=f"{row['token_id']}:{row['revision']}",
            template_key=str(row["template_key"]),
            outcome_key=str(row["outcome_key"]),
            observed=observed,
        ))
    return tuple(result)


__all__ = [
    "LANGCHAO_HISTORY_ESTIMATOR_VERSION",
    "SettledOutcomeObservation",
    "condition_forecasts_from_history",
    "estimate_settled_probability",
    "observations_from_rows",
]
