"""Production-wiring contracts for exact finite-goal terminal events.

These tests intentionally exercise the boundary from persisted ACTUAL outcomes to the
lifecycle service.  A summary is never terminal evidence, and replay/late delivery must
be harmless because the producer reloads the exact active episode.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from companion_runtime.composition_v2 import build_v2_composition
from companion_runtime.config import RuntimeConfig
from companion_runtime.langchao_goal_terminal_producer import (
    GoalCancellationFact,
    GoalTerminalEventProducer,
)
from companion_runtime.langchao_types import GoalStatus
from companion_runtime.runtime_v2 import V2RuntimeCoordinator
from companion_runtime.user_model_v2_schema import USER_MODEL_SCHEMA_VERSION
from test_pf011_goal_lifecycle import LATER, candidate, goal, outcome


class RecordingCoordinator:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.calls = []
        self.failure = failure

    def produce_goal_terminal_event(self, *, evidence, goal, candidates=()):
        self.calls.append((evidence, goal, candidates))
        if self.failure is not None:
            raise self.failure
        return evidence["kind"]


def test_actual_outcome_routes_exact_completed_event_and_replay_or_delay_is_noop() -> None:
    current = goal()
    coordinator = RecordingCoordinator()
    producer = GoalTerminalEventProducer(
        coordinator=coordinator,
        load_goal=lambda scope, goal_id, episode_id: current,
        load_candidates=lambda _goal: (candidate(),),
    )

    assert producer.from_actual_outcomes((outcome(),), occurred_at=LATER) == ("completed",)
    evidence, loaded_goal, candidates = coordinator.calls[0]
    assert evidence["kind"] == "completed"
    assert evidence["actual_outcomes"] == (outcome(),)
    assert evidence["evidence_refs"] == (
        outcome().token_id,
        *outcome().evidence_refs,
    )
    assert loaded_goal is current and candidates == (candidate(),)

    # A replay after the active pointer has moved to a terminal revision is harmless.
    current = replace(
        current,
        status=GoalStatus.COMPLETED,
        completion_evidence_refs=evidence["evidence_refs"],
        updated_at=LATER,
        revision=current.revision + 1,
    )
    assert producer.from_actual_outcomes((outcome(),), occurred_at=LATER) == ()

    # A delayed fact for an older instant cannot close a newer active revision.
    current = replace(goal(), updated_at=LATER, revision=goal().revision + 1)
    assert producer.from_actual_outcomes(
        (outcome(),), occurred_at=LATER - timedelta(microseconds=1)
    ) == ()
    assert len(coordinator.calls) == 1


def test_explicit_cancellation_is_the_only_non_outcome_terminal_entrypoint() -> None:
    current = goal()
    coordinator = RecordingCoordinator()
    producer = GoalTerminalEventProducer(
        coordinator=coordinator,
        load_goal=lambda *_coordinates: current,
    )
    fact = GoalCancellationFact(
        event_id="event:user-cancel:1",
        scope_key=current.scope_key,
        goal_id=current.goal_id,
        episode_id=current.episode_id,
        occurred_at=LATER,
    )

    assert producer.from_cancellation(fact) == "cancelled"
    evidence = coordinator.calls[0][0]
    assert evidence == {
        "kind": "cancelled",
        "occurred_at": LATER,
        "scope_key": current.scope_key,
        "goal_id": current.goal_id,
        "episode_id": current.episode_id,
        "evidence_refs": (fact.event_id,),
    }


def test_summary_payload_cannot_enter_producer_or_coordinator_terminal_boundary() -> None:
    coordinator = RecordingCoordinator()
    producer = GoalTerminalEventProducer(
        coordinator=coordinator,
        load_goal=lambda *_coordinates: goal(),
    )
    with pytest.raises(TypeError, match="OutcomeToken"):
        producer.from_actual_outcomes(
            ({"kind": "summary", "summary": "all done"},),  # type: ignore[arg-type]
            occurred_at=LATER,
        )
    assert coordinator.calls == []

    runtime = object.__new__(V2RuntimeCoordinator)
    runtime.goal_lifecycle_service = SimpleNamespace(
        apply_terminal_event=lambda *_args, **_kwargs: pytest.fail(
            "summary reached lifecycle service"
        )
    )
    assert runtime.produce_goal_terminal_event(
        evidence={"kind": "summary", "occurred_at": LATER, "summary": "all done"},
        goal=goal(),
    ) is None


class FakeConnection:
    def execute(self, _sql, _params=()):
        return SimpleNamespace(rowcount=1)


class FakeDatabase:
    dialect = "postgres"

    def __init__(self) -> None:
        self.connection = FakeConnection()

    def migrate(self):
        return USER_MODEL_SCHEMA_VERSION

    def _connection(self):
        return self.connection

    def close(self):
        pass


class FakeServiceRepository:
    def __init__(self, connection, repository) -> None:
        self.connection = connection
        self.repository = repository


class FakePredictionRepository:
    def __init__(self, repository) -> None:
        self.repository = repository

    def get_active_parameter_snapshot(self, *, scope_key, target):
        return None


class FakeAuthorityRepository:
    def __init__(self, connection, *, scope_key) -> None:
        self.active = None

    def get_active(self):
        return self.active

    def bootstrap(self):
        self.active = {
            "engine_key": "runtime_v2",
            "mode": "live",
            "may_dispatch": True,
            "revision": 1,
        }
        return self.active


class FakeLegacyBridge:
    runtime = SimpleNamespace(reducer=SimpleNamespace(set_witness_reader=lambda _reader: None))


class TransactionalContracts:
    """Shared fake whose transaction exposes partial writes if wiring is non-atomic."""

    instance = None

    def __init__(self, connection, *, scope_key) -> None:
        type(self).instance = self
        self.scope_key = scope_key
        self.active_goal = goal()
        self.active_candidates = (candidate(),)
        self.goal_revisions = {}
        self.candidate_revisions = {}
        self.goal_pointer = None
        self.candidate_pointer = None

    @contextmanager
    def transaction(self):
        before = deepcopy(self.__dict__)
        try:
            yield
        except BaseException:
            self.__dict__.clear()
            self.__dict__.update(before)
            raise

    def load_active_goal(self, scope_key, goal_id, episode_id):
        active = self.active_goal
        if (scope_key, goal_id, episode_id) != (
            active.scope_key,
            active.goal_id,
            active.episode_id,
        ):
            return None
        return active

    def load_active_candidates_for_goal(self, _goal):
        return self.active_candidates

    def put_goal_revision(self, dto):
        self.goal_revisions[(dto.goal_id, dto.revision)] = dto

    def put_candidate_revision(self, dto):
        self.candidate_revisions[(dto.candidate_id, dto.semantic_revision)] = dto

    def get_active_goal(self, *, goal_id):
        return None if self.goal_pointer is None else {"pointer_version": self.goal_pointer[1]}

    def get_active_candidate(self, *, candidate_id):
        return None if self.candidate_pointer is None else {"pointer_version": self.candidate_pointer[1]}

    def activate_goal(self, *, goal_id, revision, expected_pointer_version):
        self.goal_pointer = (revision, expected_pointer_version + 1)
        self.active_goal = self.goal_revisions[(goal_id, revision)]
        return True

    def activate_candidate(self, *, candidate_id, revision, expected_pointer_version):
        self.candidate_pointer = (revision, expected_pointer_version + 1)
        return True


class ActualOutcomeSettler:
    """Stand-in for the real ledger writer; composition must consume its return."""

    def __init__(self, repository) -> None:
        self.repository = repository
        self.terminal_producer = None

    def settle_labels(self, labels):
        result = (outcome(),)
        if self.terminal_producer is not None:
            self.terminal_producer.from_actual_outcomes(result, occurred_at=LATER)
        return result


def _live_composition(monkeypatch):
    import companion_runtime.langchao_live_wiring as live_wiring
    import companion_runtime.langchao_repository as repository_module
    import companion_runtime.langchao_user_outcomes as outcome_module

    monkeypatch.setattr(
        live_wiring,
        "build_langchao_live_runner",
        lambda **_kwargs: SimpleNamespace(repository=object()),
    )
    monkeypatch.setattr(repository_module, "LangchaoRepository", TransactionalContracts)
    monkeypatch.setattr(outcome_module, "LangchaoUserOutcomeSettler", ActualOutcomeSettler)
    config = RuntimeConfig()
    config.storage.dsn = "postgresql://runtime:secret@db/runtime"
    config.langchao.live_enabled = True
    config.langchao.live_scope_allowlist = [goal().scope_key]
    return build_v2_composition(
        config,
        scope_key=goal().scope_key,
        legacy_bridge=FakeLegacyBridge(),  # type: ignore[arg-type]
        runtime_repository=SimpleNamespace(),  # type: ignore[arg-type]
        database=FakeDatabase(),
        service_repository_factory=FakeServiceRepository,
        prediction_repository_factory=FakePredictionRepository,
        authority_repository_factory=FakeAuthorityRepository,
    )


def test_live_composition_feeds_settled_actual_outcomes_to_terminal_producer(monkeypatch) -> None:
    composition = _live_composition(monkeypatch)
    contracts = TransactionalContracts.instance
    assert contracts is not None

    returned = composition.user_model_service.outcome_observer.settle_labels(
        (SimpleNamespace(updated_at=LATER),)
    )

    assert returned == (outcome(),)
    assert contracts.active_goal.status is GoalStatus.COMPLETED
    assert contracts.goal_pointer == (goal().revision + 1, 1)
    assert contracts.candidate_pointer == (candidate().semantic_revision + 1, 1)


def test_terminal_producer_failure_propagates_so_outer_settlement_can_roll_back(monkeypatch) -> None:
    composition = _live_composition(monkeypatch)
    contracts = TransactionalContracts.instance
    assert contracts is not None

    original = composition.coordinator.produce_goal_terminal_event
    composition.coordinator.produce_goal_terminal_event = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("injected producer failure")
    )
    before = deepcopy(contracts.__dict__)
    with pytest.raises(RuntimeError, match="injected producer failure"):
        composition.user_model_service.outcome_observer.settle_labels(
            (SimpleNamespace(updated_at=LATER),)
        )
    # The producer must not swallow failures.  In the real path the user-model
    # settlement_transaction is the outer owner and rolls label/outcome/goal back.
    assert contracts.__dict__ == before
    composition.coordinator.produce_goal_terminal_event = original
