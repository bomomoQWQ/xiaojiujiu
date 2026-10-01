"""「浪潮」M2 确定性收益编译器。

本模块是纯计算层：不读取时钟、不使用随机数，不连接 Runtime、数据库或发送链路。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, TypeAlias

from .langchao_types import MotivationDirection, OutcomeToken, RewardContract, SettlementType

LANGCHAO_REWARD_COMPILER_VERSION = "langchao.reward-compiler.v1"

DirectionValues: TypeAlias = tuple[tuple[MotivationDirection, float], ...]
ExpectedOutcomeValues: TypeAlias = tuple[tuple[str, float | None], ...]
TemplateProbabilityPolicy: TypeAlias = tuple[tuple[str, float], ...]


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_number(name: str, value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _require_strings(name: str, values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    for value in values:
        _require_text(f"{name} item", value)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


def _validated_direction_values(
    name: str,
    values: DirectionValues,
    *,
    nonnegative: bool,
) -> dict[MotivationDirection, float]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple of pairs")
    result: dict[MotivationDirection, float] = {}
    for item in values:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"{name} entries must be pairs")
        direction, raw_value = item
        if not isinstance(direction, MotivationDirection):
            raise TypeError(f"{name} keys must be MotivationDirection values")
        if direction in result:
            raise ValueError(f"{name} must not contain duplicate directions")
        value = _require_number(f"{name} value", raw_value)
        if nonnegative and value < 0:
            raise ValueError(f"{name} values must be non-negative")
        result[direction] = value
    if len(result) != len(MotivationDirection) or set(result) != set(MotivationDirection):
        raise ValueError(f"{name} must contain exactly one entry per MotivationDirection")
    return result


def _ordered(values: dict[MotivationDirection, float]) -> DirectionValues:
    return tuple((direction, values[direction]) for direction in MotivationDirection)


@dataclass(frozen=True, slots=True, kw_only=True)
class OutcomeForecast:
    """一个预期结果的显式概率；未知必须以 ``probability=None`` 表示。"""

    token_id: str
    probability: float | None
    support: str
    status: str
    source_version: str

    def __post_init__(self) -> None:
        for name in ("token_id", "support", "status", "source_version"):
            _require_text(name, getattr(self, name))
        if self.probability is not None:
            probability = _require_number("probability", self.probability)
            if not 0.0 <= probability <= 1.0:
                raise ValueError("probability must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "probability": self.probability,
            "support": self.support,
            "status": self.status,
            "source_version": self.source_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateCostTerm:
    """候选的单项成本；硬边界不应被编码成成本。"""

    kind: str
    amount: float
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("kind", self.kind)
        amount = _require_number("amount", self.amount)
        if amount < 0:
            raise ValueError("amount must be non-negative")
        _require_strings("evidence_refs", self.evidence_refs)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "amount": self.amount, "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueProfile:
    """稳定的人格定价权重；八维权重之和必须等于显式固定总尺度。"""

    version: str
    direction_weights: DirectionValues
    total_weight: float

    def __post_init__(self) -> None:
        _require_text("version", self.version)
        weights = _validated_direction_values("direction_weights", self.direction_weights, nonnegative=True)
        total_weight = _require_number("total_weight", self.total_weight)
        if total_weight <= 0:
            raise ValueError("total_weight must be greater than zero")
        if not math.isclose(sum(weights.values()), total_weight, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("direction_weights must sum to total_weight")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "direction_weights": {direction.value: value for direction, value in self.direction_weights},
            "total_weight": self.total_weight,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionProfile:
    """当前八维注意力定价；首版通常全部为 1。"""

    version: str
    direction_weights: DirectionValues

    def __post_init__(self) -> None:
        _require_text("version", self.version)
        _validated_direction_values("direction_weights", self.direction_weights, nonnegative=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "direction_weights": {direction.value: value for direction, value in self.direction_weights},
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RewardCompilation:
    """完整、无舍入、可复算的候选收益分解。"""

    candidate_id: str
    reward_contract_id: str
    outcome_expected_values: ExpectedOutcomeValues
    unknown_outcomes: tuple[str, ...]
    direction_totals: DirectionValues
    weighted_terms: DirectionValues
    costs: tuple[CandidateCostTerm, ...]
    total_cost: float
    total_utility: float
    utility_scale: float
    attraction: float
    reward_contract_version: str
    compiler_version: str
    value_profile_version: str
    attention_version: str
    template_policy_version: str | None
    forecast_source_versions: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "reward_contract_id": self.reward_contract_id,
            "outcome_expected_values": dict(self.outcome_expected_values),
            "unknown_outcomes": list(self.unknown_outcomes),
            "direction_totals": {key.value: value for key, value in self.direction_totals},
            "weighted_terms": {key.value: value for key, value in self.weighted_terms},
            "costs": [item.to_dict() for item in self.costs],
            "total_cost": self.total_cost,
            "total_utility": self.total_utility,
            "utility_scale": self.utility_scale,
            "attraction": self.attraction,
            "reward_contract_version": self.reward_contract_version,
            "compiler_version": self.compiler_version,
            "value_profile_version": self.value_profile_version,
            "attention_version": self.attention_version,
            "template_policy_version": self.template_policy_version,
            "forecast_source_versions": list(self.forecast_source_versions),
            "source_refs": list(self.source_refs),
        }


def compile_candidate_reward(
    *,
    candidate_id: str,
    reward_contract: RewardContract,
    forecasts: tuple[OutcomeForecast, ...],
    value_profile: ValueProfile,
    attention_profile: AttentionProfile,
    utility_scale: float,
    costs: tuple[CandidateCostTerm, ...] = (),
    template_probability_policy: TemplateProbabilityPolicy = (),
    template_policy_version: str | None = None,
    source_refs: tuple[str, ...] = (),
) -> RewardCompilation:
    """Compile one candidate's expected utility using only explicit inputs.

    A template probability is consulted only when the corresponding forecast is
    explicitly unknown.  Thus locally observable delivery/internal-work outcomes
    may receive a declared deterministic value or prior, while user-dependent
    unknown outcomes remain ``None`` and contribute no numeric value.
    """

    _require_text("candidate_id", candidate_id)
    if not isinstance(reward_contract, RewardContract):
        raise TypeError("reward_contract must be a RewardContract")
    if not isinstance(value_profile, ValueProfile):
        raise TypeError("value_profile must be a ValueProfile")
    if not isinstance(attention_profile, AttentionProfile):
        raise TypeError("attention_profile must be an AttentionProfile")
    scale = _require_number("utility_scale", utility_scale)
    if scale <= 0:
        raise ValueError("utility_scale must be greater than zero")
    _require_strings("source_refs", source_refs)

    if not isinstance(forecasts, tuple):
        raise TypeError("forecasts must be a tuple")
    if any(not isinstance(item, OutcomeForecast) for item in forecasts):
        raise TypeError("forecasts must contain only OutcomeForecast values")
    forecast_ids = tuple(item.token_id for item in forecasts)
    if len(set(forecast_ids)) != len(forecast_ids):
        raise ValueError("forecasts must not contain duplicate token_id values")

    if not isinstance(costs, tuple):
        raise TypeError("costs must be a tuple")
    if any(not isinstance(item, CandidateCostTerm) for item in costs):
        raise TypeError("costs must contain only CandidateCostTerm values")

    tokens = reward_contract.outcome_tokens
    token_ids = tuple(item.token_id for item in tokens)
    idempotency_keys = tuple(item.idempotency_key for item in tokens)
    if len(set(token_ids)) != len(token_ids):
        raise ValueError("outcome tokens must not contain duplicate token_id values")
    if len(set(idempotency_keys)) != len(idempotency_keys):
        raise ValueError("outcome tokens must not contain duplicate idempotency_key values")
    expected_tokens = tuple(item for item in tokens if item.settlement_type is SettlementType.EXPECTED)
    expected_ids = {item.token_id for item in expected_tokens}
    if set(forecast_ids) != expected_ids:
        raise ValueError("forecasts must exactly cover expected outcome token ids")
    forecasts_by_id = {item.token_id: item for item in forecasts}

    if not isinstance(template_probability_policy, tuple):
        raise TypeError("template_probability_policy must be a tuple of pairs")
    policy: dict[str, float] = {}
    for item in template_probability_policy:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("template_probability_policy entries must be pairs")
        token_id, raw_probability = item
        _require_text("template policy token_id", token_id)
        if token_id in policy:
            raise ValueError("template_probability_policy must not contain duplicate token ids")
        probability = _require_number("template probability", raw_probability)
        if not 0.0 <= probability <= 1.0:
            raise ValueError("template probability must be between 0 and 1")
        if token_id not in expected_ids:
            raise ValueError("template_probability_policy may only name expected outcome tokens")
        policy[token_id] = probability
    if policy:
        if template_policy_version is None:
            raise ValueError("template_policy_version is required when template policy is present")
        _require_text("template_policy_version", template_policy_version)
    elif template_policy_version is not None:
        raise ValueError("template_policy_version requires a template probability policy")

    value_weights = _validated_direction_values(
        "value_profile.direction_weights", value_profile.direction_weights, nonnegative=True
    )
    attention_weights = _validated_direction_values(
        "attention_profile.direction_weights", attention_profile.direction_weights, nonnegative=True
    )
    direction_totals = {direction: 0.0 for direction in MotivationDirection}
    outcome_values: list[tuple[str, float | None]] = []
    unknown: list[str] = []

    for token in sorted(expected_tokens, key=lambda item: item.token_id):
        forecast = forecasts_by_id[token.token_id]
        probability = forecast.probability
        if probability is None:
            probability = policy.get(token.token_id)
        if probability is None:
            outcome_values.append((token.token_id, None))
            unknown.append(token.token_id)
            continue
        expected_base = token.base_amount * probability
        outcome_values.append((token.token_id, expected_base))
        for direction, direction_weight in token.direction_weights:
            direction_totals[direction] += expected_base * direction_weight

    weighted = {
        direction: value_weights[direction] * attention_weights[direction] * direction_totals[direction]
        for direction in MotivationDirection
    }
    ordered_costs = tuple(sorted(costs, key=lambda item: (item.kind, item.amount, item.evidence_refs)))
    total_cost = sum(item.amount for item in ordered_costs)
    total_utility = sum(weighted.values()) - total_cost
    attraction = total_utility / scale

    return RewardCompilation(
        candidate_id=candidate_id,
        reward_contract_id=reward_contract.reward_contract_id,
        outcome_expected_values=tuple(outcome_values),
        unknown_outcomes=tuple(unknown),
        direction_totals=_ordered(direction_totals),
        weighted_terms=_ordered(weighted),
        costs=ordered_costs,
        total_cost=total_cost,
        total_utility=total_utility,
        utility_scale=scale,
        attraction=attraction,
        reward_contract_version=reward_contract.contract_version,
        compiler_version=LANGCHAO_REWARD_COMPILER_VERSION,
        value_profile_version=value_profile.version,
        attention_version=attention_profile.version,
        template_policy_version=template_policy_version,
        forecast_source_versions=tuple(sorted({item.source_version for item in forecasts})),
        source_refs=source_refs,
    )


__all__ = [
    "LANGCHAO_REWARD_COMPILER_VERSION",
    "AttentionProfile",
    "CandidateCostTerm",
    "OutcomeForecast",
    "RewardCompilation",
    "TemplateProbabilityPolicy",
    "ValueProfile",
    "compile_candidate_reward",
]
