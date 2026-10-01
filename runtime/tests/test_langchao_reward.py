"""Tests for the isolated M2 deterministic 「浪潮」 reward compiler."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from companion_runtime.langchao_reward import (
    AttentionProfile,
    CandidateCostTerm,
    OutcomeForecast,
    ValueProfile,
    compile_candidate_reward,
)
from companion_runtime.langchao_types import (
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    RewardContract,
    SettlementType,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
DIRECTIONS = tuple(MotivationDirection)


def weights(**changes: float) -> tuple[tuple[MotivationDirection, float], ...]:
    return tuple((direction, changes.get(direction.value, 1.0)) for direction in DIRECTIONS)


def value_profile(**changes: float) -> ValueProfile:
    values = weights(**changes)
    return ValueProfile(version="values.v1", direction_weights=values, total_weight=sum(v for _, v in values))


def attention(**changes: float) -> AttentionProfile:
    return AttentionProfile(version="attention.v1", direction_weights=weights(**changes))


def token(
    token_id: str,
    *,
    outcome_key: str | None = None,
    base_amount: float = 2.0,
    direction_weights: tuple[tuple[MotivationDirection, float], ...] = ((MotivationDirection.EXPRESSION, 1.0),),
    settlement_type: SettlementType = SettlementType.EXPECTED,
    status: OutcomeStatus | None = None,
    idempotency_key: str | None = None,
) -> OutcomeToken:
    actual_status = status or (
        OutcomeStatus.UNEXECUTED if settlement_type is SettlementType.EXPECTED else OutcomeStatus.CONFIRMED
    )
    return OutcomeToken(
        token_id=token_id,
        scope_key="user:42",
        goal_id="goal_1",
        episode_id="episode_1",
        outcome_key=outcome_key or token_id,
        settlement_type=settlement_type,
        status=actual_status,
        base_amount=base_amount,
        direction_weights=direction_weights,
        evidence_version="evidence.v1",
        idempotency_key=idempotency_key or f"idem:{token_id}",
    )


def contract(*tokens: OutcomeToken) -> RewardContract:
    return RewardContract(
        reward_contract_id="reward_1",
        scope_key="user:42",
        goal_id="goal_1",
        episode_id="episode_1",
        template_key="deliver_expression.v1",
        unit="utility",
        outcome_tokens=tokens,
        total_cap=100.0,
        overlap_group="expression:episode_1",
        created_at=NOW,
        updated_at=NOW,
    )


def forecast(token_id: str, probability: float | None, *, support: str = "predictor") -> OutcomeForecast:
    return OutcomeForecast(
        token_id=token_id,
        probability=probability,
        support=support,
        status="known" if probability is not None else "unknown",
        source_version="prediction.v1",
    )


def compile(
    reward: RewardContract,
    forecasts: tuple[OutcomeForecast, ...],
    **changes: object,
):
    arguments: dict[str, object] = {
        "candidate_id": "candidate_1",
        "reward_contract": reward,
        "forecasts": forecasts,
        "value_profile": value_profile(),
        "attention_profile": attention(),
        "utility_scale": 10.0,
    }
    arguments.update(changes)
    return compile_candidate_reward(**arguments)  # type: ignore[arg-type]


def test_hand_calculation_and_shared_eight_dimensions_are_not_eight_bonuses() -> None:
    item = token(
        "delivery",
        base_amount=4.0,
        direction_weights=((MotivationDirection.EXPRESSION, 0.5), (MotivationDirection.CARE, 0.25)),
    )
    profile = value_profile(expression=2.0, care=3.0)
    focus = attention(expression=0.5, care=2.0)
    result = compile(contract(item), (forecast("delivery", 0.5),), value_profile=profile, attention_profile=focus)

    assert dict(result.outcome_expected_values) == {"delivery": 2.0}
    assert dict(result.direction_totals)[MotivationDirection.EXPRESSION] == 1.0
    assert dict(result.direction_totals)[MotivationDirection.CARE] == 0.5
    assert dict(result.weighted_terms)[MotivationDirection.EXPRESSION] == 1.0
    assert dict(result.weighted_terms)[MotivationDirection.CARE] == 3.0
    assert result.total_utility == 4.0


def test_expression_delivery_uses_explicit_template_policy_not_reply_probability() -> None:
    delivery = token("delivery", outcome_key="expression_delivered")
    reply = token("reply", outcome_key="user_replied", base_amount=5.0)
    result = compile(
        contract(delivery, reply),
        (forecast("delivery", None), forecast("reply", 0.0)),
        template_probability_policy=(("delivery", 1.0),),
        template_policy_version="delivery-policy.v1",
    )
    assert dict(result.outcome_expected_values) == {"delivery": 2.0, "reply": 0.0}
    assert result.total_utility == 2.0
    assert result.unknown_outcomes == ()


def test_unknown_user_reply_contributes_nothing_and_is_not_a_negative_example() -> None:
    delivery = token("delivery")
    reply = token("reply", base_amount=-8.0)
    result = compile(
        contract(delivery, reply),
        (forecast("delivery", 1.0), forecast("reply", None)),
    )
    assert dict(result.outcome_expected_values)["reply"] is None
    assert result.unknown_outcomes == ("reply",)
    assert result.total_utility == 2.0


def test_negative_base_amount_and_cost_are_applied_once() -> None:
    loss = token("loss", base_amount=-4.0, direction_weights=((MotivationDirection.CARE, 0.5),))
    cost = CandidateCostTerm(kind="compute", amount=1.25, evidence_refs=("budget:1",))
    result = compile(contract(loss), (forecast("loss", 0.5),), costs=(cost,))
    assert result.total_cost == 1.25
    assert result.total_utility == -2.25
    assert result.attraction == -0.225


def test_actual_and_correction_tokens_are_excluded_from_expected_ledger() -> None:
    expected = token("expected")
    actual = token("actual", settlement_type=SettlementType.ACTUAL)
    correction = OutcomeToken(
        token_id="correction", scope_key="user:42", goal_id="goal_1", episode_id="episode_1",
        outcome_key="fix", settlement_type=SettlementType.CORRECTION, status=OutcomeStatus.CORRECTED,
        base_amount=-1.0, direction_weights=((MotivationDirection.REPAIR, 1.0),),
        evidence_version="evidence.v1", idempotency_key="idem:correction", corrects_token_id="actual",
    )
    result = compile(contract(actual, correction, expected), (forecast("expected", 0.5),))
    assert result.outcome_expected_values == (("expected", 1.0),)
    assert result.total_utility == 1.0


def test_personality_and_attention_weights_price_value_without_changing_probability() -> None:
    item = token("delivery", base_amount=3.0)
    first = compile(contract(item), (forecast("delivery", 0.25),))
    second = compile(
        contract(item),
        (forecast("delivery", 0.25),),
        value_profile=value_profile(expression=4.0),
        attention_profile=attention(expression=2.0),
    )
    assert first.outcome_expected_values == second.outcome_expected_values == (("delivery", 0.75),)
    assert second.total_utility == first.total_utility * 8.0


def test_utility_scale_only_scales_attraction() -> None:
    item = token("delivery", base_amount=3.0)
    result = compile(contract(item), (forecast("delivery", 1.0),), utility_scale=2.5)
    assert result.total_utility == 3.0
    assert result.attraction == 1.2


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True])
def test_nan_inf_and_bool_are_rejected_everywhere_numeric(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        OutcomeForecast(token_id="x", probability=bad, support="s", status="known", source_version="v1")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        CandidateCostTerm(kind="compute", amount=bad)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        ValueProfile(version="v1", direction_weights=weights(expression=bad), total_weight=8.0)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        compile(contract(token("x")), (forecast("x", 1.0),), utility_scale=bad)


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_probability_must_be_in_unit_interval(bad: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        forecast("x", bad)


def test_duplicate_missing_and_extra_forecasts_are_rejected() -> None:
    reward = contract(token("a"), token("b"))
    with pytest.raises(ValueError, match="duplicate"):
        compile(reward, (forecast("a", 1.0), forecast("a", 0.5)))
    with pytest.raises(ValueError, match="exactly cover"):
        compile(reward, (forecast("a", 1.0),))
    with pytest.raises(ValueError, match="exactly cover"):
        compile(reward, (forecast("a", 1.0), forecast("b", 1.0), forecast("actual", 1.0)))


def test_duplicate_token_and_idempotency_are_rejected_by_contract_boundary() -> None:
    with pytest.raises(ValueError, match="duplicate token_id"):
        contract(token("a"), token("a"))
    with pytest.raises(ValueError, match="duplicate idempotency"):
        contract(token("a"), token("b", idempotency_key="idem:a"))


def test_profiles_require_exactly_eight_nonnegative_fixed_scale_entries() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ValueProfile(version="v1", direction_weights=weights()[:-1], total_weight=7.0)
    with pytest.raises(ValueError, match="non-negative"):
        value_profile(rest=-1.0)
    with pytest.raises(ValueError, match="sum"):
        ValueProfile(version="v1", direction_weights=weights(), total_weight=7.0)
    with pytest.raises(ValueError, match="greater than zero"):
        ValueProfile(version="v1", direction_weights=tuple((d, 0.0) for d in DIRECTIONS), total_weight=0.0)
    all_one = attention()
    assert all(value == 1.0 for _, value in all_one.direction_weights)


def test_template_policy_must_be_explicit_and_only_fills_unknown() -> None:
    item = token("delivery")
    known = compile(
        contract(item),
        (forecast("delivery", 0.25),),
        template_probability_policy=(("delivery", 1.0),),
        template_policy_version="policy.v1",
    )
    assert known.outcome_expected_values == (("delivery", 0.5),)
    with pytest.raises(ValueError, match="required"):
        compile(
            contract(item), (forecast("delivery", None),),
            template_probability_policy=(("delivery", 1.0),),
        )


def test_rhetorical_forecast_fields_do_not_affect_arithmetic() -> None:
    item = token("delivery")
    first = compile(contract(item), (forecast("delivery", 0.4, support="careful rationale"),))
    second_forecast = OutcomeForecast(
        token_id="delivery", probability=0.4, support="persuasive flourish",
        status="highly confident prose", source_version="prediction.v1",
    )
    second = compile(contract(item), (second_forecast,))
    assert first.total_utility == second.total_utility
    assert first.direction_totals == second.direction_totals


def test_to_dict_is_json_safe_frozen_and_unrounded() -> None:
    amount = 0.123456789123
    result = compile(
        contract(token("delivery", base_amount=amount)),
        (forecast("delivery", 1.0),),
        source_refs=("candidate:1",),
    )
    payload = result.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["outcome_expected_values"]["delivery"] == amount
    assert payload["source_refs"] == ["candidate:1"]
    with pytest.raises(FrozenInstanceError):
        result.total_utility = 0.0  # type: ignore[misc]


def test_input_order_does_not_affect_compilation_result() -> None:
    a = token("a", base_amount=1.0, direction_weights=((MotivationDirection.CARE, 1.0),))
    b = token("b", base_amount=2.0, direction_weights=((MotivationDirection.EXPRESSION, 1.0),))
    cost_a = CandidateCostTerm(kind="compute", amount=0.25)
    cost_b = CandidateCostTerm(kind="latency", amount=0.5)
    first = compile(contract(a, b), (forecast("a", 0.5), forecast("b", 0.25)), costs=(cost_a, cost_b))
    second = compile(contract(b, a), (forecast("b", 0.25), forecast("a", 0.5)), costs=(cost_b, cost_a))
    assert first.to_dict() == second.to_dict()
