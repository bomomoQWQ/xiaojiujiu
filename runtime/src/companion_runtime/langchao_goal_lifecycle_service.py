"""Production application boundary for finite-goal terminal lifecycle.

The service deliberately receives exact immutable DTOs and narrow persistence/matter
collaborators.  It cannot create dispatch claims or outbox rows.  Completion and
cancellation events are applied through the pure lifecycle rules, then published as
new active revisions in the caller's transaction.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from .langchao_goal_lifecycle import (
    GoalTerminalTransition,
    complete_finite_goal,
    drop_finite_goal,
    reopen_finite_goal,
)
from .langchao_types import ActionCandidateContract, GoalContract, OutcomeToken


@dataclass(frozen=True, slots=True, kw_only=True)
class GoalLifecycleEvent:
    """An explicit terminal event; elapsed time and summaries are intentionally absent."""

    kind: str
    occurred_at: datetime
    actual_outcomes: tuple[OutcomeToken, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"completed", "cancelled"}:
            raise ValueError("kind must be 'completed' or 'cancelled'")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.kind == "cancelled" and self.actual_outcomes:
            raise ValueError("cancelled events cannot carry completion outcomes")


class LangchaoGoalLifecycleService:
    """Persist explicit completion/cancellation transitions without dispatch powers."""

    def __init__(
        self,
        *,
        contract_repository: Any,
        matter_transition: Callable[[str, str, datetime], Any] | None = None,
    ) -> None:
        self.contracts = contract_repository
        self._matter_transition = matter_transition

    def _transaction(self) -> Any:
        transaction = getattr(self.contracts, "transaction", None)
        return transaction() if callable(transaction) else nullcontext()

    @staticmethod
    def _activate(get_active: Any, activate: Any, identity_name: str, identity: str, revision: int) -> None:
        row = get_active(**{identity_name: identity})
        pointer = 0 if row is None else int(row["pointer_version"] if isinstance(row, dict) else row.pointer_version)
        if not activate(**{identity_name: identity, "revision": revision, "expected_pointer_version": pointer}):
            raise RuntimeError("lifecycle active-pointer changed during terminal transition")

    def apply_terminal_event(
        self,
        event: GoalLifecycleEvent,
        *,
        goal: GoalContract,
        candidates: Iterable[ActionCandidateContract] = (),
    ) -> GoalTerminalTransition:
        """Apply one explicit event; no event means no lifecycle inference."""

        candidate_tuple = tuple(candidates)
        transition = (
            complete_finite_goal(
                goal, actual_outcomes=event.actual_outcomes,
                candidates=candidate_tuple, at=event.occurred_at,
            )
            if event.kind == "completed"
            else drop_finite_goal(goal, candidates=candidate_tuple, at=event.occurred_at)
        )
        if transition.matter_id is not None and self._matter_transition is None:
            raise RuntimeError("terminal goal owns a matter but no matter transition is configured")

        # This is one semantic transition.  A failed candidate write, pointer CAS, or
        # unfinished-matter update must not leave the goal half closed.
        with self._transaction():
            self.contracts.put_goal_revision(transition.goal)
            self._activate(
                self.contracts.get_active_goal, self.contracts.activate_goal,
                "goal_id", transition.goal.goal_id, transition.goal.revision,
            )
            for candidate in transition.candidates:
                self.contracts.put_candidate_revision(candidate)
                self._activate(
                    self.contracts.get_active_candidate, self.contracts.activate_candidate,
                    "candidate_id", candidate.candidate_id, candidate.semantic_revision,
                )
            if transition.matter_id is not None and transition.matter_status is not None:
                assert self._matter_transition is not None
                self._matter_transition(
                    transition.matter_id, transition.matter_status.value, event.occurred_at,
                )
        return transition

    def reopen(
        self,
        closed: GoalContract,
        *,
        goal_id: str,
        episode_id: str,
        episode_source_refs: tuple[str, ...],
        episode_basis_refs: tuple[str, ...],
        at: datetime,
    ) -> GoalContract:
        """Publish a clean episode only when the pure provenance guard accepts it."""

        reopened = reopen_finite_goal(
            closed, goal_id=goal_id, episode_id=episode_id,
            episode_source_refs=episode_source_refs,
            episode_basis_refs=episode_basis_refs, at=at,
        )
        with self._transaction():
            self.contracts.put_goal_revision(reopened)
            self._activate(
                self.contracts.get_active_goal, self.contracts.activate_goal,
                "goal_id", reopened.goal_id, reopened.revision,
            )
        return reopened


__all__ = ["GoalLifecycleEvent", "LangchaoGoalLifecycleService"]
