"""Executable contract checks for the T01-T16 acceptance mapping manifest.

These checks deliberately validate only capabilities that already exist: the checked-in
mapping is complete and its direct pure-kernel controls still execute.  Semantic review
items remain review metadata; this module does not fake them with string matching or
empty ``pass`` tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from companion_runtime.langchao_engine import LangchaoParameters, advance_langchao
from companion_runtime.langchao_reward import (
    AttentionProfile,
    OutcomeForecast,
    ValueProfile,
    compile_candidate_reward,
)
from companion_runtime.langchao_types import (
    LangchaoState,
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    RewardContract,
    SettlementType,
)
from companion_runtime.user_model_v2_features import V2_FEATURE_NAMES, encode_features_v2
from companion_runtime.user_model_v2_labels import SettlementContextV2, settle_target_label
from companion_runtime.user_model_v2_types import DeliveryBasis, InteractionExposureV2, LabelStatus, Target

ROOT = Path(__file__).parents[1]
MANIFEST_PATH = ROOT / "audit" / "scenarios" / "T01-T16.json"
NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
DIRECTIONS = tuple(MotivationDirection)


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _feature(name: str, encoded: object) -> tuple[float, bool]:
    index = V2_FEATURE_NAMES.index(name)
    return encoded.values[index], encoded.missing_mask[index]  # type: ignore[attr-defined]


def _profiles() -> tuple[ValueProfile, AttentionProfile]:
    weights = tuple((direction, 1.0) for direction in DIRECTIONS)
    return (
        ValueProfile(version="values.fixture.v1", direction_weights=weights, total_weight=8.0),
        AttentionProfile(version="attention.fixture.v1", direction_weights=weights),
    )


def _unknown_reward() -> tuple[RewardContract, OutcomeForecast]:
    token = OutcomeToken(
        token_id="reply:t01",
        scope_key="fixture:user:t01",
        goal_id="goal:t01",
        episode_id="episode:t01",
        outcome_key="reply",
        settlement_type=SettlementType.EXPECTED,
        status=OutcomeStatus.UNEXECUTED,
        base_amount=-4.0,
        direction_weights=((MotivationDirection.CARE, 1.0),),
        evidence_version="fixture.v1",
        idempotency_key="fixture:t01:reply",
    )
    contract = RewardContract(
        reward_contract_id="reward:t01",
        scope_key="fixture:user:t01",
        goal_id="goal:t01",
        episode_id="episode:t01",
        template_key="contact.v1",
        unit="utility",
        outcome_tokens=(token,),
        total_cap=4.0,
        overlap_group="t01",
        created_at=NOW,
        updated_at=NOW,
    )
    forecast = OutcomeForecast(
        token_id=token.token_id,
        probability=None,
        support="no observed reply forecast",
        status="unknown",
        source_version="fixture.v1",
    )
    return contract, forecast


def _state() -> LangchaoState:
    return LangchaoState(
        scope_key="fixture:user:t11",
        decision_round_id="round:t11",
        working_set=("contact", "research"),
        readiness=(("contact", 0.0), ("research", 0.0)),
        attraction=(("contact", 0.5), ("research", 0.1)),
        attention=tuple((direction, 1.0) for direction in DIRECTIONS),
        advanced_at=NOW,
        based_on_state_version=0,
        event_cursor="fixture:0",
        goal_snapshot_version="goals:fixture",
        reward_snapshot_version="rewards:fixture",
        candidate_snapshot_version="candidates:fixture",
        prediction_snapshot_version="predictions:fixture",
        value_profile_version="values:fixture",
        attention_version="attention:fixture",
        parameter_version="langchao.parameters.v1",
        permission_version="permission:fixture",
    )


def test_t01_t16_manifest_has_reviewable_nonempty_contracts() -> None:
    payload = _manifest()
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list)
    assert [item["id"] for item in scenarios] == [f"T{index:02d}" for index in range(1, 17)]
    required = {
        "fixture_data",
        "entrypoints",
        "mechanical_assertions",
        "semantic_review",
        "oracle",
        "positive_control",
        "current_coverage_tests",
        "coverage",
        "gap",
        "evidence",
    }
    for item in scenarios:
        assert required <= item.keys(), item["id"]
        assert item["entrypoints"] and item["mechanical_assertions"] and item["oracle"]
        assert item["positive_control"] and item["gap"]
        assert item["coverage"] in {"covered", "partial", "gap"}
        review = item["semantic_review"]
        assert isinstance(review["required"], bool)
        if review["required"]:
            assert review["rubric"] and review["artifact"]


def test_t01_busy_observation_and_unknown_are_mechanically_distinct() -> None:
    action = {"type": "contact", "proactive": True}
    busy = encode_features_v2(action, {"busy_probability": 0.8})
    unknown = encode_features_v2(action, {})
    assert _feature("busy", busy) == (pytest.approx(0.8), False)
    assert _feature("busy", unknown) == (0.0, True)

    contract, forecast = _unknown_reward()
    values, attention = _profiles()
    compiled = compile_candidate_reward(
        candidate_id="candidate:t01",
        reward_contract=contract,
        forecasts=(forecast,),
        value_profile=values,
        attention_profile=attention,
        utility_scale=1.0,
    )
    assert compiled.unknown_outcomes == (forecast.token_id,)
    assert compiled.outcome_expected_values == ((forecast.token_id, None),)
    assert compiled.total_utility == 0.0


def test_t10_partial_window_is_censored_and_full_window_is_observed_negative() -> None:
    end = NOW + timedelta(hours=6)
    exposure = InteractionExposureV2(
        exposure_id="exp:t10",
        scope_key="fixture:user:t10",
        occurred_at=NOW,
        window_started_at=NOW,
        window_ends_at=end,
        horizon_seconds=6 * 60 * 60,
        delivery_basis=DeliveryBasis.DELIVERED,
        created_at=NOW,
        updated_at=NOW,
        source_event_ids=("delivery:t10",),
    )
    partial = settle_target_label(
        exposure,
        Target.REPLY,
        (),
        SettlementContextV2(as_of=NOW + timedelta(hours=2), observation_complete=False),
    )
    complete = settle_target_label(
        exposure,
        Target.REPLY,
        (),
        SettlementContextV2(as_of=end, observation_complete=True),
    )
    assert partial.status is LabelStatus.CENSORED and partial.value is None
    assert complete.status is LabelStatus.OBSERVED_NEGATIVE and complete.value is False


def test_t11_budget_defer_keeps_both_goals_and_positive_control_can_decide() -> None:
    parameters = LangchaoParameters(
        leak=0.2,
        competition_gain=1.0,
        decision_threshold=0.6,
        time_scale_seconds=10.0,
        max_step_seconds=0.1,
        crossing_tolerance=1e-7,
        tie_tolerance=1e-6,
    )
    deferred = advance_langchao(
        _state(),
        until=NOW + timedelta(seconds=100),
        parameters=parameters,
        decision_budget_seconds=1.0,
        tie_break_order=("contact", "research"),
    )
    assert deferred.decision is None
    assert deferred.defer_reason == "decision_budget_exhausted"
    assert set(dict(deferred.state.readiness)) == {"contact", "research"}

    decided = advance_langchao(
        _state(),
        until=NOW + timedelta(seconds=100),
        parameters=parameters,
        tie_break_order=("contact", "research"),
    )
    assert decided.decision == "contact"
    assert decided.decision_at is not None
