"""PF-011 regression: finite completion/closure and legitimate new episodes.

T09 here means the original finite-matter scenario, not the later acceptance-manifest
number that was reused for actual-action attribution.  T27 is the put-down/old-summary/
new-evidence scenario from ``audit/scenarios/T17-T32.json``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.langchao_goal_lifecycle import (
    GoalLifecycleError,
    MatterTerminalStatus,
    complete_finite_goal,
    drop_finite_goal,
    reopen_finite_goal,
)
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
    RetirementReason,
    SettlementType,
)

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(seconds=30)


def goal(*, status: GoalStatus = GoalStatus.ACTIONABLE) -> GoalContract:
    return GoalContract(
        goal_id="goal:answer:1", scope_key="fixture:user:pf011", episode_id="episode:request:1",
        semantic_key="finite:answer:opening-hours", kind=GoalKind.FINITE,
        ownership=GoalOwnership.USER_REQUEST, desired_change="answer delivered completely",
        status=status, evidence_refs=("event:user-request:1",), excluded_outcomes=("retention",),
        completion_outcome_keys=("answer_completed",),
        allowed_candidate_kinds=(CandidateKind.EXTERNAL_MESSAGE,), matter_id="unfinished:request:1",
        reward_contract_id="reward:answer:1", created_at=NOW, updated_at=NOW,
    )


def candidate() -> ActionCandidateContract:
    return ActionCandidateContract(
        candidate_id="candidate:answer:1", scope_key="fixture:user:pf011",
        semantic_key="answer:opening-hours", goal_refs=("goal:answer:1",),
        kind=CandidateKind.EXTERNAL_MESSAGE, action_template="answer.v1",
        input_refs=("event:user-request:1",), reward_contract_ref="reward:answer:1",
        expected_outcome_token_ids=("expected:answer:1",), capability_refs=("external_message",),
        permission_ref="permission:1", precondition_refs=(), invalidation_refs=(), envelope=(),
        state=CandidateState.COMPETITIVE, available_from=NOW, expires_at=None,
        resource_budget=0.0, based_on_state_version=1, created_at=NOW, updated_at=NOW,
    )


def outcome(*, settlement=SettlementType.ACTUAL, status=OutcomeStatus.CONFIRMED,
            evidence=("render:complete-answer", "event:user-close:1")) -> OutcomeToken:
    return OutcomeToken(
        token_id="actual:answer:1", scope_key="fixture:user:pf011", goal_id="goal:answer:1",
        episode_id="episode:request:1", outcome_key="answer_completed",
        settlement_type=settlement, status=status, base_amount=1.0,
        direction_weights=((MotivationDirection.COMMITMENT, 1.0),), evidence_version="pf011.v1",
        idempotency_key="actual:answer:1", evidence_refs=evidence,
    )


def test_t09_actual_completion_closes_goal_retires_candidate_and_resolves_matter() -> None:
    """Positive control: complete delivery + explicit close is terminal even after 30s."""
    result = complete_finite_goal(
        goal(), actual_outcomes=(outcome(),), candidates=(candidate(),), at=LATER,
    )

    assert result.goal.status is GoalStatus.COMPLETED
    assert result.goal.completion_evidence_refs == (
        "actual:answer:1", "render:complete-answer", "event:user-close:1",
    )
    assert result.goal.updated_at == LATER  # duration is metadata, never a completion veto
    assert result.candidates[0].state is CandidateState.RETIRED
    assert result.candidates[0].retirement_reason is RetirementReason.COMPLETED
    assert result.matter_id == "unfinished:request:1"
    assert result.matter_status is MatterTerminalStatus.RESOLVED
    assert result.completion_tokens == (outcome(),)


@pytest.mark.parametrize(
    "bad",
    [
        outcome(settlement=SettlementType.EXPECTED, status=OutcomeStatus.UNEXECUTED),
        outcome(status=OutcomeStatus.CENSORED),
        outcome(evidence=()),
    ],
)
def test_t09_negative_control_plan_failure_or_evidenceless_token_cannot_complete(bad: OutcomeToken) -> None:
    with pytest.raises(GoalLifecycleError, match="actual confirmed completion evidence"):
        complete_finite_goal(goal(), actual_outcomes=(bad,), candidates=(candidate(),), at=LATER)


def test_t09_contract_cannot_claim_completed_without_actual_evidence_refs() -> None:
    with pytest.raises(ValueError, match="actual completion evidence"):
        goal(status=GoalStatus.COMPLETED)


def test_t27_put_down_retires_and_cancels_without_completion_benefit() -> None:
    result = drop_finite_goal(goal(), candidates=(candidate(),), at=LATER)

    assert result.goal.status is GoalStatus.DROPPED
    assert result.goal.completion_evidence_refs == ()
    assert result.completion_tokens == ()
    assert result.candidates[0].retirement_reason is RetirementReason.DROPPED
    assert result.matter_status is MatterTerminalStatus.CANCELLED
    with pytest.raises(GoalLifecycleError, match="closed goal cannot be completed"):
        complete_finite_goal(result.goal, actual_outcomes=(outcome(),), at=LATER)


def test_t27_old_summary_or_same_source_does_not_reopen_closed_episode() -> None:
    closed = complete_finite_goal(goal(), actual_outcomes=(outcome(),), at=LATER).goal

    with pytest.raises(GoalLifecycleError, match="old or same-source"):
        reopen_finite_goal(
            closed, goal_id="goal:answer:2", episode_id="episode:request:2",
            episode_source_refs=("event:user-request:1",),
            episode_basis_refs=("summary:paraphrase:1",), at=LATER,
        )
    with pytest.raises(GoalLifecycleError, match="new episode source and basis"):
        reopen_finite_goal(
            closed, goal_id="goal:answer:2", episode_id="episode:request:2",
            episode_source_refs=("summary:paraphrase:1",), episode_basis_refs=(), at=LATER,
        )
    with pytest.raises(GoalLifecycleError, match="new goal and episode"):
        reopen_finite_goal(
            closed, goal_id=closed.goal_id, episode_id="episode:request:2",
            episode_source_refs=("event:user-request:2",),
            episode_basis_refs=("user-explicit-reopen:2",), at=LATER,
        )


def test_t27_positive_control_genuinely_new_evidence_creates_clean_new_episode() -> None:
    closed = drop_finite_goal(goal(), candidates=(candidate(),), at=LATER).goal
    reopened = reopen_finite_goal(
        closed, goal_id="goal:answer:2", episode_id="episode:request:2",
        episode_source_refs=("event:user-request:2",),
        episode_basis_refs=("user-explicit-reopen:2",), at=LATER + timedelta(hours=6),
    )

    assert reopened.status is GoalStatus.ACTIONABLE
    assert reopened.goal_id != closed.goal_id and reopened.episode_id != closed.episode_id
    assert reopened.parent_goal_id == closed.goal_id
    assert reopened.reward_contract_id is None  # no inherited reward/full-budget claim
    assert reopened.completion_evidence_refs == ()
    assert reopened.revision == 1
    assert set(reopened.evidence_refs) == {"event:user-request:2", "user-explicit-reopen:2"}
