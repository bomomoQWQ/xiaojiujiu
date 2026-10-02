"""Map authoritative reducer/outcome facts to exact goal terminal events.

This is the production producer in front of ``V2RuntimeCoordinator``.  It accepts
only structured reducer facts: confirmed ACTUAL outcomes or an explicit cancellation
fact.  Summaries, retrieved memories and elapsed-time signals have no representation
in this API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

from .langchao_types import GoalStatus, OutcomeStatus, OutcomeToken, SettlementType


@dataclass(frozen=True, slots=True, kw_only=True)
class GoalCancellationFact:
    """A reducer-confirmed cancellation for one exact goal episode."""

    event_id: str
    scope_key: str
    goal_id: str
    episode_id: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        for name in ("event_id", "scope_key", "goal_id", "episode_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")


class GoalTerminalEventProducer:
    """Produce terminal events after resolving the current exact contract revision.

    ``load_goal`` must return the active :class:`GoalContract` for the supplied exact
    coordinates, or ``None``.  Returning the active revision (rather than accepting a
    caller-provided stale snapshot) is what makes duplicate, delayed and out-of-order
    reducer delivery harmless across process restarts.
    """

    def __init__(
        self,
        *,
        coordinator: Any,
        load_goal: Callable[[str, str, str], Any | None],
        load_candidates: Callable[[Any], Sequence[Any]] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._load_goal = load_goal
        self._load_candidates = load_candidates or (lambda _goal: ())

    def _active(self, scope_key: str, goal_id: str, episode_id: str, occurred_at: datetime) -> Any | None:
        goal = self._load_goal(scope_key, goal_id, episode_id)
        if goal is None:
            return None
        if (
            goal.scope_key != scope_key
            or goal.goal_id != goal_id
            or goal.episode_id != episode_id
        ):
            raise RuntimeError("goal loader returned different scope/goal/episode coordinates")
        if goal.status in {GoalStatus.COMPLETED, GoalStatus.DROPPED, GoalStatus.INVALIDATED}:
            return None
        if occurred_at < goal.updated_at:
            return None
        return goal

    def from_actual_outcomes(
        self, outcomes: Iterable[OutcomeToken], *, occurred_at: datetime
    ) -> tuple[Any, ...]:
        """Complete exact active episodes supported by confirmed ACTUAL evidence."""

        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        grouped: dict[tuple[str, str, str], list[OutcomeToken]] = {}
        for token in outcomes:
            if not isinstance(token, OutcomeToken):
                raise TypeError("outcomes must contain only OutcomeToken values")
            if (
                token.settlement_type is not SettlementType.ACTUAL
                or token.status is not OutcomeStatus.CONFIRMED
                or not token.evidence_refs
            ):
                continue
            grouped.setdefault(
                (token.scope_key, token.goal_id, token.episode_id), []
            ).append(token)

        transitions: list[Any] = []
        for (scope_key, goal_id, episode_id), tokens in grouped.items():
            goal = self._active(scope_key, goal_id, episode_id, occurred_at)
            if goal is None:
                continue
            qualifying = tuple(
                token for token in tokens
                if token.outcome_key in goal.completion_outcome_keys
            )
            if not qualifying:
                continue
            evidence_refs = tuple(dict.fromkeys(
                ref for token in qualifying for ref in (token.token_id, *token.evidence_refs)
            ))
            transitions.append(self.coordinator.produce_goal_terminal_event(
                evidence={
                    "kind": "completed",
                    "occurred_at": occurred_at,
                    "scope_key": scope_key,
                    "goal_id": goal_id,
                    "episode_id": episode_id,
                    "evidence_refs": evidence_refs,
                    "actual_outcomes": qualifying,
                },
                goal=goal,
                candidates=tuple(self._load_candidates(goal)),
            ))
        return tuple(transitions)

    def from_cancellation(self, fact: GoalCancellationFact) -> Any | None:
        """Drop one active episode only from an explicit reducer cancellation fact."""

        goal = self._active(
            fact.scope_key, fact.goal_id, fact.episode_id, fact.occurred_at
        )
        if goal is None:
            return None
        return self.coordinator.produce_goal_terminal_event(
            evidence={
                "kind": "cancelled",
                "occurred_at": fact.occurred_at,
                "scope_key": fact.scope_key,
                "goal_id": fact.goal_id,
                "episode_id": fact.episode_id,
                "evidence_refs": (fact.event_id,),
            },
            goal=goal,
            candidates=tuple(self._load_candidates(goal)),
        )


__all__ = ["GoalCancellationFact", "GoalTerminalEventProducer"]
