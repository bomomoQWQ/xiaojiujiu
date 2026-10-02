from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from companion_runtime.langchao_history import (
    SettledOutcomeObservation,
    condition_forecasts_from_history,
    estimate_settled_probability,
)
from companion_runtime.langchao_reward import (
    AttentionProfile,
    OutcomeForecast,
    ValueProfile,
    compile_candidate_reward,
)
from companion_runtime.langchao_shadow import ShadowCandidateInput
from companion_runtime.langchao_types import (
    ActionCandidateContract,
    CandidateKind,
    CandidateState,
    GoalContract,
    GoalKind,
    GoalOwnership,
    GoalStatus,
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    RewardContract,
    SettlementType,
)

NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "langchao_history_closed_loop.json").read_text())


def _weights():
    return tuple((direction, 1.0) for direction in MotivationDirection)


def _input(outcome_key: str, amount: float) -> ShadowCandidateInput:
    token = OutcomeToken(
        token_id=f"token:{outcome_key}", scope_key="scope", goal_id="goal",
        episode_id="episode", outcome_key=outcome_key,
        settlement_type=SettlementType.EXPECTED, status=OutcomeStatus.UNEXECUTED,
        base_amount=amount, direction_weights=((MotivationDirection.APPROACH, 1.0),),
        evidence_version="fixture.v1", idempotency_key=f"expected:{outcome_key}",
    )
    goal = GoalContract(
        goal_id="goal", scope_key="scope", episode_id="episode", semantic_key="contact",
        kind=GoalKind.CONTINUOUS_NEED, ownership=GoalOwnership.SELF_WISH,
        desired_change="relationship_continuity", status=GoalStatus.ACTIONABLE,
        evidence_refs=("fixture",), excluded_outcomes=(), completion_outcome_keys=(outcome_key,),
        allowed_candidate_kinds=(CandidateKind.EXTERNAL_MESSAGE,), reward_contract_id="reward",
        created_at=NOW, updated_at=NOW, revision=2,
    )
    reward = RewardContract(
        reward_contract_id="reward", scope_key="scope", goal_id="goal", episode_id="episode",
        template_key="contact.v1", unit="utility", outcome_tokens=(token,), total_cap=abs(amount),
        overlap_group="contact", created_at=NOW, updated_at=NOW,
    )
    candidate = ActionCandidateContract(
        candidate_id="candidate", scope_key="scope", semantic_key="contact", goal_refs=("goal",),
        kind=CandidateKind.EXTERNAL_MESSAGE, action_template="contact.v1", input_refs=("fixture",),
        reward_contract_ref="reward", expected_outcome_token_ids=(token.token_id,),
        capability_refs=("external_message",), permission_ref="permission", precondition_refs=(),
        invalidation_refs=(), envelope=(("subject_ref", "relationship-continuity"),),
        state=CandidateState.COMPETITIVE, available_from=NOW, expires_at=None,
        resource_budget=0.0, based_on_state_version=1, created_at=NOW, updated_at=NOW,
    )
    forecast = OutcomeForecast(
        token_id=token.token_id, probability=FIXTURE["prior_probability"], support="fixture",
        status="available", source_version="fixture.prediction.v1",
    )
    return ShadowCandidateInput(goal=goal, reward=reward, candidate=candidate, forecasts=(forecast,))


def _attraction(item: ShadowCandidateInput) -> float:
    return compile_candidate_reward(
        candidate_id=item.candidate.candidate_id, reward_contract=item.reward,
        forecasts=item.forecasts,
        value_profile=ValueProfile(version="value", direction_weights=_weights(), total_weight=8.0),
        attention_profile=AttentionProfile(version="attention", direction_weights=_weights()),
        utility_scale=1.0,
    ).attraction


@pytest.mark.parametrize(
    ("outcome_key", "amount", "fixture_key"),
    (("reply", 1.0, "reply_positive"), ("negative", -1.5, "negative_positive")),
)
def test_frozen_settled_history_moves_next_round_attraction_in_expected_direction(
    outcome_key: str, amount: float, fixture_key: str,
) -> None:
    item = _input(outcome_key, amount)
    history = (SettledOutcomeObservation(
        observation_id="actual:1", template_key="contact.v1",
        outcome_key=outcome_key, observed=True,
    ),)
    conditioned = condition_forecasts_from_history(
        item, history, prior_strength=FIXTURE["prior_strength"]
    )
    assert conditioned.forecasts[0].probability == pytest.approx(
        FIXTURE[fixture_key]["expected_probability"]
    )
    baseline, after = _attraction(item), _attraction(conditioned)
    if FIXTURE[fixture_key]["expected_attraction_direction"] == "up":
        assert after > baseline
    else:
        assert after < baseline
    # The actual amount was not added as a bonus: attraction remains expected amount * posterior.
    assert after == pytest.approx(amount * FIXTURE[fixture_key]["expected_probability"])


def test_unknown_and_censored_history_are_strict_noops() -> None:
    item = _input("reply", 1.0)
    history = tuple(
        SettledOutcomeObservation(
            observation_id=f"incomplete:{index}", template_key="contact.v1",
            outcome_key="reply", observed=value,
        )
        for index, value in enumerate(FIXTURE["unknown_censored"]["history"])
    )
    conditioned = condition_forecasts_from_history(item, history)
    assert conditioned == item
    assert _attraction(conditioned) == _attraction(item)


def test_duplicate_and_restart_replay_do_not_repeat_history_weight() -> None:
    observation = SettledOutcomeObservation(
        observation_id="actual:stable:1", template_key="contact.v1",
        outcome_key="reply", observed=True,
    )
    one = estimate_settled_probability(
        (observation,), template_key="contact.v1", outcome_key="reply", prior_probability=0.5,
    )
    duplicate = estimate_settled_probability(
        (observation, observation), template_key="contact.v1", outcome_key="reply", prior_probability=0.5,
    )
    reconstructed_after_restart = SettledOutcomeObservation(
        observation_id=observation.observation_id, template_key=observation.template_key,
        outcome_key=observation.outcome_key, observed=observation.observed,
    )
    replay = estimate_settled_probability(
        (observation, reconstructed_after_restart), template_key="contact.v1",
        outcome_key="reply", prior_probability=0.5,
    )
    assert one == duplicate == replay == (pytest.approx(2 / 3), 1)
