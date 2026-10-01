"""Contract tests for the isolated M1 「浪潮」 DTOs."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.langchao_types import (
    LANGCHAO_CANDIDATE_CONTRACT_VERSION,
    LANGCHAO_GOAL_CONTRACT_VERSION,
    LANGCHAO_OUTCOME_TOKEN_VERSION,
    LANGCHAO_REWARD_CONTRACT_VERSION,
    LANGCHAO_STATE_VERSION,
    ActionCandidateContract,
    CandidateKind,
    CandidateState,
    GoalContract,
    GoalKind,
    GoalOwnership,
    GoalStatus,
    LangchaoState,
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    RetirementReason,
    RewardContract,
    SettlementType,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=2)


def goal(**changes: object) -> GoalContract:
    values: dict[str, object] = {
        "goal_id": "goal_1", "scope_key": "user:42", "episode_id": "episode_1",
        "semantic_key": "expression:verified-note", "kind": GoalKind.FINITE,
        "ownership": GoalOwnership.SELF_WISH, "desired_change": "share the verified note",
        "status": GoalStatus.ACTIONABLE, "evidence_refs": ("note_1",),
        "excluded_outcomes": ("user_must_reply",),
        "completion_outcome_keys": ("expression_delivered",),
        "allowed_candidate_kinds": (CandidateKind.EXTERNAL_MESSAGE,),
        "reward_contract_id": "reward_1", "created_at": NOW, "updated_at": NOW,
    }
    values.update(changes)
    return GoalContract(**values)  # type: ignore[arg-type]


def token(**changes: object) -> OutcomeToken:
    values: dict[str, object] = {
        "token_id": "token_1", "scope_key": "user:42", "goal_id": "goal_1",
        "episode_id": "episode_1", "outcome_key": "expression_delivered",
        "settlement_type": SettlementType.EXPECTED, "status": OutcomeStatus.UNEXECUTED,
        "base_amount": 0.123456789, "direction_weights": ((MotivationDirection.EXPRESSION, 0.75),),
        "evidence_version": "evidence.v1", "idempotency_key": "user:42:episode_1:expression:v1",
        "evidence_refs": ("note_1",),
    }
    values.update(changes)
    return OutcomeToken(**values)  # type: ignore[arg-type]


def reward(**changes: object) -> RewardContract:
    values: dict[str, object] = {
        "reward_contract_id": "reward_1", "scope_key": "user:42", "goal_id": "goal_1",
        "episode_id": "episode_1", "template_key": "deliver_expression.v1", "unit": "utility",
        "outcome_tokens": (token(),), "total_cap": 1.23456789, "overlap_group": "expression:episode_1",
        "created_at": NOW, "updated_at": NOW,
    }
    values.update(changes)
    return RewardContract(**values)  # type: ignore[arg-type]


def candidate(**changes: object) -> ActionCandidateContract:
    values: dict[str, object] = {
        "candidate_id": "candidate_1", "scope_key": "user:42",
        "semantic_key": "goal_1:episode_1:share:note_1", "goal_refs": ("goal_1",),
        "kind": CandidateKind.EXTERNAL_MESSAGE, "action_template": "share_existing_content.v1",
        "input_refs": ("note_1",), "reward_contract_ref": "reward_1",
        "expected_outcome_token_ids": ("token_1",), "capability_refs": ("send_text",),
        "permission_ref": "permission_1", "precondition_refs": ("note_still_valid",),
        "invalidation_refs": ("note_deleted",),
        "envelope": (("request_reply", False), ("claim_scope", "referenced_only"), ("max_parts", 1)),
        "state": CandidateState.COMPETITIVE, "available_from": NOW, "expires_at": LATER,
        "resource_budget": 0.333333333333, "based_on_state_version": 7,
        "created_at": NOW, "updated_at": NOW,
    }
    values.update(changes)
    return ActionCandidateContract(**values)  # type: ignore[arg-type]


def state(**changes: object) -> LangchaoState:
    values: dict[str, object] = {
        "scope_key": "user:42", "decision_round_id": "round_1",
        "working_set": ("candidate_1", "candidate_2"),
        "readiness": (("candidate_1", 0.125), ("candidate_2", 0.75)),
        "attraction": (("candidate_2", -0.3333333333), ("candidate_1", 1.23456789)),
        "attention": tuple((direction, 1.0) for direction in MotivationDirection),
        "advanced_at": NOW, "based_on_state_version": 7, "event_cursor": "event_99",
        "goal_snapshot_version": "goals:4", "reward_snapshot_version": "rewards:3",
        "candidate_snapshot_version": "candidates:8", "prediction_snapshot_version": "predictions:5",
        "value_profile_version": "values:2", "attention_version": "attention:1",
        "parameter_version": "parameters:1", "permission_version": "permissions:6",
    }
    values.update(changes)
    return LangchaoState(**values)  # type: ignore[arg-type]


def test_enum_order_and_values_are_stable() -> None:
    assert [item.value for item in GoalKind] == ["continuous_need", "finite", "open_activity"]
    assert [item.value for item in GoalOwnership] == [
        "user_request", "self_commitment", "shared_arrangement", "self_wish", "self_interest"
    ]
    assert [item.value for item in GoalStatus] == [
        "adopted", "actionable", "waiting", "paused", "completed", "dropped", "invalidated"
    ]
    assert [item.value for item in MotivationDirection] == [
        "approach", "expression", "exploration", "care", "commitment", "repair", "autonomy", "rest"
    ]
    assert [item.value for item in OutcomeStatus] == [
        "unexecuted", "pending", "confirmed", "not_observed", "censored", "unattributable", "corrected"
    ]
    assert [item.value for item in SettlementType] == ["expected", "actual", "correction"]
    assert [item.value for item in CandidateKind] == [
        "external_message", "internal_process", "defer_or_rest"
    ]
    assert [item.value for item in CandidateState] == [
        "proposed", "validated", "competitive", "dormant", "reserved", "retired"
    ]
    assert [item.value for item in RetirementReason] == [
        "completed", "invalidated", "superseded", "dropped", "window_closed"
    ]


def test_valid_contracts_are_explicitly_json_safe_without_rounding() -> None:
    records = (goal(), token(), reward(), candidate(), state())
    payloads = [record.to_dict() for record in records]
    for payload in payloads:
        assert json.loads(json.dumps(payload)) == payload
    assert payloads[1]["base_amount"] == 0.123456789
    assert payloads[2]["total_cap"] == 1.23456789
    assert payloads[3]["resource_budget"] == 0.333333333333
    assert payloads[4]["attraction"]["candidate_1"] == 1.23456789
    assert payloads[0]["contract_version"] == LANGCHAO_GOAL_CONTRACT_VERSION
    assert payloads[1]["token_version"] == LANGCHAO_OUTCOME_TOKEN_VERSION
    assert payloads[2]["contract_version"] == LANGCHAO_REWARD_CONTRACT_VERSION
    assert payloads[3]["contract_version"] == LANGCHAO_CANDIDATE_CONTRACT_VERSION
    assert payloads[4]["state_version"] == LANGCHAO_STATE_VERSION


def test_records_are_frozen_and_nested_collections_require_tuples() -> None:
    item = goal()
    with pytest.raises(FrozenInstanceError):
        item.status = GoalStatus.COMPLETED  # type: ignore[misc]
    with pytest.raises(TypeError, match="tuple"):
        goal(evidence_refs=["note_1"])
    with pytest.raises(TypeError, match="tuple"):
        reward(outcome_tokens=[token()])
    with pytest.raises(TypeError, match="tuple"):
        candidate(envelope=[("request_reply", False)])
    with pytest.raises(TypeError, match="tuple"):
        state(readiness=[("candidate_1", 0.1), ("candidate_2", 0.2)])


@pytest.mark.parametrize("factory,field", [
    (goal, "contract_version"), (token, "token_version"), (reward, "contract_version"),
    (candidate, "contract_version"), (state, "state_version"),
])
def test_wrong_versions_are_rejected(factory: object, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        factory(**{field: "latest"})  # type: ignore[operator]


@pytest.mark.parametrize("bad_time", [NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=8)))])
def test_naive_and_non_utc_datetimes_are_rejected(bad_time: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        goal(created_at=bad_time)
    with pytest.raises(ValueError, match="UTC"):
        candidate(available_from=bad_time)
    with pytest.raises(ValueError, match="UTC"):
        state(advanced_at=bad_time)


def test_goal_state_dependencies_and_identity_invariants() -> None:
    with pytest.raises(ValueError, match="waiting goals require"):
        goal(status=GoalStatus.WAITING)
    assert goal(status=GoalStatus.WAITING, wait_for_refs=("attempt_1",)).wait_for_refs == ("attempt_1",)
    with pytest.raises(ValueError, match="only waiting"):
        goal(wait_for_refs=("attempt_1",))
    with pytest.raises(ValueError, match="paused goals require"):
        goal(status=GoalStatus.PAUSED)
    assert goal(status=GoalStatus.PAUSED, resume_condition_refs=("new_material",)).status is GoalStatus.PAUSED
    with pytest.raises(ValueError, match="only paused"):
        goal(resume_condition_refs=("new_material",))
    with pytest.raises(ValueError, match="must not equal"):
        goal(parent_goal_id="goal_1")
    with pytest.raises(TypeError, match="GoalKind"):
        goal(kind="finite")
    with pytest.raises(ValueError, match="duplicates"):
        goal(completion_outcome_keys=("delivered", "delivered"))


def test_outcome_state_dependencies_windows_and_numbers() -> None:
    pending = token(status=OutcomeStatus.PENDING, observation_started_at=NOW, observation_ends_at=LATER)
    assert pending.status is OutcomeStatus.PENDING
    with pytest.raises(ValueError, match="pending outcomes require"):
        token(status=OutcomeStatus.PENDING)
    with pytest.raises(ValueError, match="both present"):
        token(observation_started_at=NOW)
    with pytest.raises(ValueError, match="must be after"):
        token(observation_started_at=NOW, observation_ends_at=NOW)
    correction = token(
        token_id="token_2", settlement_type=SettlementType.CORRECTION,
        status=OutcomeStatus.CORRECTED, corrects_token_id="token_1",
    )
    assert correction.corrects_token_id == "token_1"
    with pytest.raises(ValueError, match="correction settlements require"):
        token(settlement_type=SettlementType.CORRECTION)
    with pytest.raises(ValueError, match="require correction"):
        token(status=OutcomeStatus.CORRECTED)
    with pytest.raises(TypeError, match="number"):
        token(base_amount=True)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            token(base_amount=value)
    with pytest.raises(ValueError, match="duplicate keys"):
        token(direction_weights=((MotivationDirection.CARE, 0.5), (MotivationDirection.CARE, 0.6)))
    with pytest.raises(TypeError, match="MotivationDirection"):
        token(direction_weights=(("care", 0.5),))


def test_reward_requires_matching_unique_tokens_and_valid_budget() -> None:
    with pytest.raises(ValueError, match="duplicate token_id"):
        reward(outcome_tokens=(token(), token()))
    with pytest.raises(ValueError, match="match reward"):
        reward(outcome_tokens=(token(scope_key="other"),))
    with pytest.raises(TypeError, match="number"):
        reward(total_cap=True)
    with pytest.raises(ValueError, match="non-negative"):
        reward(total_cap=-0.1)
    with pytest.raises(ValueError, match="must not precede"):
        reward(updated_at=NOW - timedelta(seconds=1))


def test_candidate_envelope_and_state_dependencies_are_strict() -> None:
    with pytest.raises(ValueError, match="envelope keys"):
        candidate(envelope=(("x", 1), ("x", 2)))
    with pytest.raises(TypeError, match="JSON scalars"):
        candidate(envelope=(("nested", ("not", "scalar")),))
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            candidate(envelope=(("score", value),))
    assert candidate(envelope=(("enabled", True), ("attempts", 2))).to_dict()["envelope"] == {
        "enabled": True, "attempts": 2
    }
    with pytest.raises(ValueError, match="retired candidates require"):
        candidate(state=CandidateState.RETIRED)
    retired = candidate(state=CandidateState.RETIRED, retirement_reason=RetirementReason.COMPLETED)
    assert retired.retirement_reason is RetirementReason.COMPLETED
    with pytest.raises(ValueError, match="only retired"):
        candidate(retirement_reason=RetirementReason.DROPPED)
    with pytest.raises(TypeError, match="integer"):
        candidate(attempt_budget=True)
    with pytest.raises(ValueError, match="after available"):
        candidate(expires_at=NOW)


def test_state_requires_exact_unique_working_set_keys_and_eight_attention_items() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        state(readiness=(("candidate_1", 0.1),))
    with pytest.raises(ValueError, match="duplicate keys"):
        state(attraction=(("candidate_1", 0.1), ("candidate_1", 0.2)))
    with pytest.raises(ValueError, match="duplicates"):
        state(working_set=("candidate_1", "candidate_1"))
    with pytest.raises(ValueError, match="exactly one"):
        state(attention=((MotivationDirection.APPROACH, 1.0),))
    duplicate_attention = tuple((direction, 1.0) for direction in MotivationDirection) + (
        (MotivationDirection.APPROACH, 1.0),
    )
    with pytest.raises(ValueError, match="duplicate keys"):
        state(attention=duplicate_attention)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        state(readiness=(("candidate_1", 1.01), ("candidate_2", 0.2)))
    with pytest.raises(TypeError, match="number"):
        state(attraction=(("candidate_1", True), ("candidate_2", 0.2)))
    with pytest.raises(ValueError, match="finite"):
        state(attraction=(("candidate_1", float("nan")), ("candidate_2", 0.2)))
    with pytest.raises(TypeError, match="integer"):
        state(based_on_state_version=False)


def test_replace_revalidates_contracts() -> None:
    with pytest.raises(ValueError, match="retired candidates require"):
        replace(candidate(), state=CandidateState.RETIRED)
    with pytest.raises(ValueError, match="exactly match"):
        replace(state(), working_set=("candidate_1",))
