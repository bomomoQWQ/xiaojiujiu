from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta

import pytest

from companion_runtime.langchao_goal_lifecycle import GoalLifecycleError
from companion_runtime.langchao_goal_lifecycle_service import (
    GoalLifecycleEvent,
    LangchaoGoalLifecycleService,
)
from companion_runtime.langchao_types import GoalStatus
from companion_runtime.runtime_v2 import V2RuntimeCoordinator
from test_pf011_goal_lifecycle import LATER, candidate, goal, outcome


class Contracts:
    def __init__(self):
        self.goals = {}
        self.candidates = {}
        self.active_goals = {}
        self.active_candidates = {}
        self.outbox = []
        self.claims = []

    def put_goal_revision(self, dto): self.goals[(dto.goal_id, dto.revision)] = dto
    def put_candidate_revision(self, dto): self.candidates[(dto.candidate_id, dto.semantic_revision)] = dto
    def get_active_goal(self, *, goal_id):
        value = self.active_goals.get(goal_id)
        return None if value is None else {"pointer_version": value[1]}
    def get_active_candidate(self, *, candidate_id):
        value = self.active_candidates.get(candidate_id)
        return None if value is None else {"pointer_version": value[1]}
    def activate_goal(self, *, goal_id, revision, expected_pointer_version):
        self.active_goals[goal_id] = (revision, expected_pointer_version + 1); return True
    def activate_candidate(self, *, candidate_id, revision, expected_pointer_version):
        self.active_candidates[candidate_id] = (revision, expected_pointer_version + 1); return True


class TransactionalContracts(Contracts):
    def __init__(self, *, fail_candidate=False):
        super().__init__()
        self.fail_candidate = fail_candidate
        self.transaction_entries = 0

    @contextmanager
    def transaction(self):
        self.transaction_entries += 1
        before = deepcopy((self.goals, self.candidates, self.active_goals, self.active_candidates))
        try:
            yield
        except BaseException:
            self.goals, self.candidates, self.active_goals, self.active_candidates = before
            raise

    def put_candidate_revision(self, dto):
        if self.fail_candidate:
            raise RuntimeError("candidate insert failed")
        super().put_candidate_revision(dto)


def test_explicit_completion_and_cancellation_events_call_lifecycle_without_outbox_or_claim():
    contracts = Contracts()
    matter_events = []
    service = LangchaoGoalLifecycleService(
        contract_repository=contracts,
        matter_transition=lambda matter_id, status, at: matter_events.append((matter_id, status, at)),
    )
    completed = service.apply_terminal_event(
        GoalLifecycleEvent(kind="completed", occurred_at=LATER, actual_outcomes=(outcome(),)),
        goal=goal(), candidates=(candidate(),),
    )
    assert completed.goal.status is GoalStatus.COMPLETED
    assert matter_events[-1][1] == "resolved"

    cancelled = service.apply_terminal_event(
        GoalLifecycleEvent(kind="cancelled", occurred_at=LATER + timedelta(days=90)),
        goal=goal(), candidates=(candidate(),),
    )
    assert cancelled.goal.status is GoalStatus.DROPPED
    assert cancelled.completion_tokens == ()
    assert matter_events[-1][1] == "cancelled"
    assert contracts.outbox == [] and contracts.claims == []


def test_terminal_transition_failure_rolls_back_goal_candidate_pointers_and_matter():
    contracts = TransactionalContracts(fail_candidate=True)
    matter_events = []
    service = LangchaoGoalLifecycleService(
        contract_repository=contracts,
        matter_transition=lambda matter_id, status, at: matter_events.append((matter_id, status, at)),
    )

    with pytest.raises(RuntimeError, match="candidate insert failed"):
        service.apply_terminal_event(
            GoalLifecycleEvent(kind="completed", occurred_at=LATER, actual_outcomes=(outcome(),)),
            goal=goal(), candidates=(candidate(),),
        )

    assert contracts.transaction_entries == 1
    assert contracts.goals == {}
    assert contracts.candidates == {}
    assert contracts.active_goals == {}
    assert contracts.active_candidates == {}
    assert matter_events == []


def test_matter_transition_failure_rolls_back_all_contract_writes():
    contracts = TransactionalContracts()
    service = LangchaoGoalLifecycleService(
        contract_repository=contracts,
        matter_transition=lambda *_args: (_ for _ in ()).throw(RuntimeError("matter update failed")),
    )

    with pytest.raises(RuntimeError, match="matter update failed"):
        service.apply_terminal_event(
            GoalLifecycleEvent(kind="completed", occurred_at=LATER, actual_outcomes=(outcome(),)),
            goal=goal(), candidates=(candidate(),),
        )

    assert contracts.goals == {}
    assert contracts.candidates == {}
    assert contracts.active_goals == {}
    assert contracts.active_candidates == {}


def test_runtime_producer_calls_service_only_for_explicit_terminal_evidence():
    calls = []

    class Service:
        def apply_terminal_event(self, event, *, goal, candidates):
            calls.append((event, goal, candidates))
            return "transition"

    coordinator = object.__new__(V2RuntimeCoordinator)
    coordinator.goal_lifecycle_service = Service()
    current_goal = goal()
    current_candidates = (candidate(),)

    assert coordinator.produce_goal_terminal_event(
        evidence={"kind": "summary", "occurred_at": LATER, "summary": "all done"},
        goal=current_goal, candidates=current_candidates,
    ) is None
    assert calls == []
    assert coordinator.produce_goal_terminal_event(
        evidence={
            "kind": "completed", "occurred_at": LATER,
            "actual_outcomes": (outcome(),), "summary": "ignored",
        },
        goal=current_goal, candidates=current_candidates,
    ) == "transition"
    assert calls[0][0].kind == "completed"
    assert calls[0][0].actual_outcomes == (outcome(),)
    assert calls[0][1:] == (current_goal, current_candidates)


def test_old_summary_cannot_reopen_through_service_and_elapsed_time_has_no_completion_gain():
    contracts = Contracts()
    service = LangchaoGoalLifecycleService(
        contract_repository=contracts, matter_transition=lambda *_args: None,
    )
    closed = service.apply_terminal_event(
        GoalLifecycleEvent(kind="completed", occurred_at=LATER, actual_outcomes=(outcome(),)),
        goal=goal(), candidates=(),
    ).goal
    with pytest.raises(GoalLifecycleError, match="old or same-source"):
        service.reopen(
            closed, goal_id="goal:answer:2", episode_id="episode:request:2",
            episode_source_refs=("event:user-request:1",),
            episode_basis_refs=("summary:old",), at=LATER + timedelta(days=365),
        )
    # Merely waiting a year cannot synthesize a completion event or outcome.
    with pytest.raises(GoalLifecycleError, match="actual confirmed completion evidence"):
        service.apply_terminal_event(
            GoalLifecycleEvent(kind="completed", occurred_at=LATER + timedelta(days=365)),
            goal=goal(),
        )
    assert contracts.outbox == [] and contracts.claims == []
