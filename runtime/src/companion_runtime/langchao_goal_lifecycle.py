"""Finite-goal terminal lifecycle and auditable episode reopening.

This module is deliberately pure.  Callers persist the returned immutable revisions
with :class:`LangchaoRepository` and apply the unfinished-matter transition through
the normal projection API.  Completion is evidence-driven; cancellation/drop is a
separate terminal path and therefore cannot mint completion reward.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Iterable

from .langchao_types import (
    ActionCandidateContract,
    CandidateState,
    GoalContract,
    GoalKind,
    GoalStatus,
    OutcomeStatus,
    OutcomeToken,
    RetirementReason,
    SettlementType,
)


class GoalLifecycleError(ValueError):
    """A requested goal transition is not supported by its evidence."""


class MatterTerminalStatus(str, Enum):
    """The matching unfinished-matter transition, if a goal owns one."""

    RESOLVED = "resolved"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class GoalTerminalTransition:
    """One atomic-intent terminal transition for a goal and its candidates."""

    goal: GoalContract
    candidates: tuple[ActionCandidateContract, ...]
    completion_tokens: tuple[OutcomeToken, ...]
    matter_id: str | None
    matter_status: MatterTerminalStatus | None


def _candidate_revisions(
    goal: GoalContract,
    candidates: Iterable[ActionCandidateContract],
    *,
    reason: RetirementReason,
    at: datetime,
) -> tuple[ActionCandidateContract, ...]:
    retired: list[ActionCandidateContract] = []
    for candidate in candidates:
        if candidate.scope_key != goal.scope_key or goal.goal_id not in candidate.goal_refs:
            raise GoalLifecycleError("candidate does not belong to the terminal goal")
        if candidate.state is CandidateState.RETIRED:
            if candidate.retirement_reason is not reason:
                raise GoalLifecycleError("candidate is already retired for a different reason")
            retired.append(candidate)
            continue
        retired.append(replace(
            candidate,
            state=CandidateState.RETIRED,
            retirement_reason=reason,
            updated_at=at,
            semantic_revision=candidate.semantic_revision + 1,
        ))
    return tuple(retired)


def complete_finite_goal(
    goal: GoalContract,
    *,
    actual_outcomes: Iterable[OutcomeToken],
    candidates: Iterable[ActionCandidateContract] = (),
    at: datetime,
) -> GoalTerminalTransition:
    """Complete a finite goal only from confirmed, attributable actual evidence.

    Expected forecasts, delivery plans, conversation duration, and prose summaries do
    not qualify.  At least one configured completion outcome must have a confirmed
    ``ACTUAL`` token for this exact goal and episode, carrying non-empty evidence refs.
    """

    if goal.kind is not GoalKind.FINITE:
        raise GoalLifecycleError("only finite goals have a completion terminal")
    if goal.status is GoalStatus.COMPLETED:
        raise GoalLifecycleError("finite goal is already completed")
    if goal.status in {GoalStatus.DROPPED, GoalStatus.INVALIDATED}:
        raise GoalLifecycleError("a closed goal cannot be completed")

    qualifying = tuple(token for token in actual_outcomes if (
        token.scope_key == goal.scope_key
        and token.goal_id == goal.goal_id
        and token.episode_id == goal.episode_id
        and token.outcome_key in goal.completion_outcome_keys
        and token.settlement_type is SettlementType.ACTUAL
        and token.status is OutcomeStatus.CONFIRMED
        and bool(token.evidence_refs)
    ))
    if not qualifying:
        raise GoalLifecycleError("actual confirmed completion evidence is required")

    evidence = tuple(dict.fromkeys(
        ref for token in qualifying for ref in (token.token_id, *token.evidence_refs)
    ))
    closed = replace(
        goal,
        status=GoalStatus.COMPLETED,
        completion_evidence_refs=evidence,
        wait_for_refs=(),
        resume_condition_refs=(),
        updated_at=at,
        revision=goal.revision + 1,
    )
    return GoalTerminalTransition(
        goal=closed,
        candidates=_candidate_revisions(goal, candidates, reason=RetirementReason.COMPLETED, at=at),
        completion_tokens=qualifying,
        matter_id=goal.matter_id,
        matter_status=MatterTerminalStatus.RESOLVED if goal.matter_id else None,
    )


def drop_finite_goal(
    goal: GoalContract,
    *,
    candidates: Iterable[ActionCandidateContract] = (),
    at: datetime,
) -> GoalTerminalTransition:
    """Cancel/put down a finite goal without claiming any completion outcome."""

    if goal.kind is not GoalKind.FINITE:
        raise GoalLifecycleError("only finite goals are handled by this terminal transition")
    if goal.status in {GoalStatus.COMPLETED, GoalStatus.INVALIDATED}:
        raise GoalLifecycleError("a closed goal cannot be dropped")
    if goal.status is GoalStatus.DROPPED:
        raise GoalLifecycleError("finite goal is already dropped")
    closed = replace(
        goal,
        status=GoalStatus.DROPPED,
        completion_evidence_refs=(),
        wait_for_refs=(),
        resume_condition_refs=(),
        updated_at=at,
        revision=goal.revision + 1,
    )
    return GoalTerminalTransition(
        goal=closed,
        candidates=_candidate_revisions(goal, candidates, reason=RetirementReason.DROPPED, at=at),
        completion_tokens=(),
        matter_id=goal.matter_id,
        matter_status=MatterTerminalStatus.CANCELLED if goal.matter_id else None,
    )


def reopen_finite_goal(
    closed: GoalContract,
    *,
    goal_id: str,
    episode_id: str,
    episode_source_refs: tuple[str, ...],
    episode_basis_refs: tuple[str, ...],
    at: datetime,
) -> GoalContract:
    """Create a new finite-goal episode from genuinely new source provenance.

    A retrieved/paraphrased old summary keeps the old source refs and is rejected.
    The caller must provide both a new episode identity and at least one independent
    source ref plus the explicit evidence that justifies treating it as a new episode
    (for example a new user request/event).  Completion evidence and reward identity
    are never inherited.
    """

    if closed.kind is not GoalKind.FINITE or closed.status not in {GoalStatus.COMPLETED, GoalStatus.DROPPED}:
        raise GoalLifecycleError("only a closed finite goal may start a new episode")
    if goal_id == closed.goal_id or episode_id == closed.episode_id:
        raise GoalLifecycleError("reopening requires new goal and episode identities")
    if not episode_source_refs or not episode_basis_refs:
        raise GoalLifecycleError("new episode source and basis evidence are required")
    if any(not isinstance(ref, str) or not ref.strip() for ref in (*episode_source_refs, *episode_basis_refs)):
        raise GoalLifecycleError("new episode evidence refs must be non-empty strings")
    old_refs = set(closed.evidence_refs) | set(closed.completion_evidence_refs)
    if set(episode_source_refs) & old_refs:
        raise GoalLifecycleError("old or same-source evidence cannot reopen a closed goal")
    if not (set(episode_basis_refs) - old_refs):
        raise GoalLifecycleError("a genuinely new episode basis is required")

    return replace(
        closed,
        goal_id=goal_id,
        episode_id=episode_id,
        status=GoalStatus.ACTIONABLE,
        evidence_refs=tuple(dict.fromkeys((*episode_source_refs, *episode_basis_refs))),
        completion_evidence_refs=(),
        reward_contract_id=None,
        parent_goal_id=closed.goal_id,
        matter_id=None,
        created_at=at,
        updated_at=at,
        revision=1,
    )


__all__ = [
    "GoalLifecycleError",
    "GoalTerminalTransition",
    "MatterTerminalStatus",
    "complete_finite_goal",
    "drop_finite_goal",
    "reopen_finite_goal",
]
