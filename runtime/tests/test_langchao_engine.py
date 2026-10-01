"""Tests for the pure, isolated 「浪潮」 numerical kernel."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from companion_runtime.langchao_engine import (
    LANGCHAO_PARAMETERS_VERSION,
    AdvanceResult,
    CompetitionEdge,
    LangchaoParameters,
    advance_langchao,
)
from companion_runtime.langchao_types import LangchaoState, MotivationDirection

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def state(
    *,
    working_set: tuple[str, ...] = ("a",),
    readiness: tuple[tuple[str, float], ...] | None = None,
    attraction: tuple[tuple[str, float], ...] | None = None,
    advanced_at: datetime = NOW,
) -> LangchaoState:
    readiness = readiness or tuple((item, 0.0) for item in working_set)
    attraction = attraction or tuple((item, 0.5) for item in working_set)
    return LangchaoState(
        scope_key="user:42", decision_round_id="round:1", working_set=working_set,
        readiness=readiness, attraction=attraction,
        attention=tuple((direction, 1.0) for direction in MotivationDirection),
        advanced_at=advanced_at, based_on_state_version=7, event_cursor="event:9",
        goal_snapshot_version="goals:1", reward_snapshot_version="rewards:1",
        candidate_snapshot_version="candidates:1", prediction_snapshot_version="predictions:1",
        value_profile_version="values:1", attention_version="attention:1",
        parameter_version="old-parameters:1", permission_version="permissions:1",
    )


def parameters(**changes: object) -> LangchaoParameters:
    values: dict[str, object] = {
        "leak": 0.2, "competition_gain": 1.0, "decision_threshold": 0.6,
        "time_scale_seconds": 10.0, "max_step_seconds": 0.5,
        "crossing_tolerance": 1e-7, "tie_tolerance": 1e-6,
    }
    values.update(changes)
    return LangchaoParameters(**values)  # type: ignore[arg-type]


def scores(result: AdvanceResult) -> dict[str, float]:
    return dict(result.state.readiness)


def test_single_candidate_matches_closed_form_and_documented_crossing_time() -> None:
    item = state(readiness=(("a", 0.1),), attraction=(("a", 0.5),))
    config = parameters(time_scale_seconds=10.0, decision_threshold=0.6, max_step_seconds=100.0)
    equilibrium = 0.5 / 0.7
    expected_time = 10.0 / 0.7 * math.log((equilibrium - 0.1) / (equilibrium - 0.6))

    before_decision = advance_langchao(
        item, until=NOW + timedelta(seconds=expected_time - 0.01), parameters=config,
    )
    assert before_decision.decision is None
    assert scores(before_decision)["a"] == pytest.approx(
        equilibrium + (0.1 - equilibrium) * math.exp(-0.7 * (expected_time - 0.01) / 10.0)
    )

    decided = advance_langchao(item, until=NOW + timedelta(seconds=100), parameters=config)
    assert decided.decision == "a"
    assert (decided.decision_at - NOW).total_seconds() == pytest.approx(expected_time, abs=2e-6)  # type: ignore[union-attr]
    assert scores(decided)["a"] == pytest.approx(0.6, abs=2e-7)


def test_weak_support_never_crosses_when_equilibrium_is_at_or_below_threshold() -> None:
    result = advance_langchao(
        state(attraction=(("a", 0.1),)), until=NOW + timedelta(days=30),
        parameters=parameters(decision_threshold=0.6, max_step_seconds=100_000.0),
    )
    assert result.decision is None
    assert scores(result)["a"] == pytest.approx(1.0 / 3.0)


def test_symmetric_competition_stalls_near_documented_value() -> None:
    edges = (CompetitionEdge(left="a", right="b", weight=0.5), CompetitionEdge(left="b", right="a", weight=0.5))
    result = advance_langchao(
        state(working_set=("a", "b")), until=NOW + timedelta(seconds=1000),
        parameters=parameters(max_step_seconds=0.02), competition_edges=edges,
    )
    assert result.decision is None
    assert scores(result)["a"] == pytest.approx(0.520656, abs=2e-5)
    assert scores(result)["b"] == pytest.approx(0.520656, abs=2e-5)


def test_working_set_edge_and_pair_order_do_not_change_result() -> None:
    first = state(
        working_set=("a", "b", "c"), readiness=(("a", 0.1), ("b", 0.2), ("c", 0.3)),
        attraction=(("a", 0.25), ("b", 0.4), ("c", -0.1)),
    )
    second = state(
        working_set=("c", "a", "b"), readiness=(("b", 0.2), ("c", 0.3), ("a", 0.1)),
        attraction=(("c", -0.1), ("a", 0.25), ("b", 0.4)),
    )
    edges = (
        CompetitionEdge(left="a", right="b", weight=0.7),
        CompetitionEdge(left="a", right="c", weight=0.2),
        CompetitionEdge(left="b", right="a", weight=0.3),
    )
    config = parameters(decision_threshold=0.99, max_step_seconds=0.2)
    one = advance_langchao(first, until=NOW + timedelta(seconds=8), parameters=config, competition_edges=edges)
    two = advance_langchao(second, until=NOW + timedelta(seconds=8), parameters=config, competition_edges=tuple(reversed(edges)))
    assert scores(one) == pytest.approx(scores(two), abs=1e-15)


def test_each_small_step_uses_one_synchronous_before_snapshot() -> None:
    item = state(
        working_set=("a", "b"), readiness=(("a", 0.2), ("b", 0.8)),
        attraction=(("a", 0.5), ("b", 0.5)),
    )
    config = parameters(decision_threshold=1.0, max_step_seconds=1.0, time_scale_seconds=1.0)
    edges = (CompetitionEdge(left="a", right="b", weight=1.0), CompetitionEdge(left="b", right="a", weight=1.0))
    result = advance_langchao(item, until=NOW + timedelta(seconds=1), parameters=config, competition_edges=edges)
    expected_a = 0.5 / 1.5 + (0.2 - 0.5 / 1.5) * math.exp(-1.5)
    expected_b = 0.5 / 0.9 + (0.8 - 0.5 / 0.9) * math.exp(-0.9)
    assert scores(result) == pytest.approx({"a": expected_a, "b": expected_b})
    assert dict(result.steps[0].readiness_before) == {"a": 0.2, "b": 0.8}


def test_smaller_max_steps_converge_under_competition() -> None:
    item = state(working_set=("a", "b"), readiness=(("a", 0.1), ("b", 0.7)))
    edges = (CompetitionEdge(left="a", right="b", weight=0.5), CompetitionEdge(left="b", right="a", weight=0.5))
    coarse = advance_langchao(item, until=NOW + timedelta(seconds=20), parameters=parameters(decision_threshold=0.99, max_step_seconds=1.0), competition_edges=edges)
    medium = advance_langchao(item, until=NOW + timedelta(seconds=20), parameters=parameters(decision_threshold=0.99, max_step_seconds=0.1), competition_edges=edges)
    fine = advance_langchao(item, until=NOW + timedelta(seconds=20), parameters=parameters(decision_threshold=0.99, max_step_seconds=0.01), competition_edges=edges)
    coarse_error = max(abs(scores(coarse)[key] - scores(fine)[key]) for key in ("a", "b"))
    medium_error = max(abs(scores(medium)[key] - scores(fine)[key]) for key in ("a", "b"))
    assert medium_error < coarse_error
    assert medium_error < 0.001


def test_first_crossing_stops_early_with_requested_precision() -> None:
    config = parameters(max_step_seconds=100.0, crossing_tolerance=1e-5)
    result = advance_langchao(state(), until=NOW + timedelta(seconds=100), parameters=config)
    assert result.state.advanced_at == result.decision_at
    assert result.state.advanced_at < NOW + timedelta(seconds=100)
    assert len(result.steps) == 1
    assert result.steps[0].first_crossing_candidates == ("a",)
    assert abs(scores(result)["a"] - config.decision_threshold) < 1e-6


def test_explicit_complete_unique_tie_rule_selects_independently_of_working_order() -> None:
    config = parameters(max_step_seconds=100.0, tie_tolerance=1e-5)
    one = advance_langchao(
        state(working_set=("a", "b")), until=NOW + timedelta(seconds=100), parameters=config,
        tie_break_order=("b", "a"),
    )
    two = advance_langchao(
        state(working_set=("b", "a")), until=NOW + timedelta(seconds=100), parameters=config,
        tie_break_order=("b", "a"),
    )
    assert one.decision == two.decision == "b"
    assert one.decision_at == two.decision_at
    assert set(one.steps[-1].first_crossing_candidates) == {"a", "b"}
    with pytest.raises(ValueError, match="cover"):
        advance_langchao(state(working_set=("a", "b")), until=NOW + timedelta(seconds=100), parameters=config)
    with pytest.raises(ValueError, match="unique"):
        advance_langchao(
            state(working_set=("a", "b")), until=NOW + timedelta(seconds=100), parameters=config,
            tie_break_order=("a", "a"),
        )


def test_decision_budget_defers_without_forcing_a_choice() -> None:
    result = advance_langchao(
        state(), until=NOW + timedelta(seconds=100), parameters=parameters(), decision_budget_seconds=1.0,
    )
    assert result.decision is None
    assert result.defer_reason == "decision_budget_exhausted"
    assert result.state.advanced_at == NOW + timedelta(seconds=1)
    assert scores(result)["a"] < 0.6


def test_negative_attraction_causes_readiness_to_fall() -> None:
    result = advance_langchao(
        state(readiness=(("a", 0.8),), attraction=(("a", -0.4),)),
        until=NOW + timedelta(seconds=5), parameters=parameters(decision_threshold=0.9),
    )
    assert 0 <= scores(result)["a"] < 0.8


def test_zero_elapsed_time_preserves_values_but_versions_new_state() -> None:
    original = state(readiness=(("a", 0.25),))
    result = advance_langchao(original, until=NOW, parameters=parameters())
    assert result.steps == ()
    assert result.state.readiness == original.readiness
    assert result.state.advanced_at == original.advanced_at
    assert result.state.revision == original.revision + 1
    assert result.state.parameter_version == LANGCHAO_PARAMETERS_VERSION
    assert original.revision == 1
    assert original.parameter_version == "old-parameters:1"


def test_new_candidate_receives_only_time_after_callers_advanced_at() -> None:
    joined_at = NOW + timedelta(minutes=15)
    result = advance_langchao(
        state(working_set=("old", "new"), advanced_at=joined_at),
        until=joined_at + timedelta(seconds=1), parameters=parameters(decision_threshold=0.99),
    )
    expected = 0.5 / 0.7 * (1 - math.exp(-0.7 / 10.0))
    assert scores(result)["new"] == pytest.approx(expected)


@pytest.mark.parametrize("bad_until", [NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=8)))])
def test_until_requires_utc(bad_until: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        advance_langchao(state(), until=bad_until, parameters=parameters())


def test_backward_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not precede"):
        advance_langchao(state(), until=NOW - timedelta(microseconds=1), parameters=parameters())


def test_parameters_edges_and_budget_reject_nonfinite_or_invalid_values() -> None:
    for field in (
        "leak", "competition_gain", "decision_threshold", "time_scale_seconds",
        "max_step_seconds", "crossing_tolerance", "tie_tolerance",
    ):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="finite"):
                parameters(**{field: bad})
    for changes in (
        {"leak": -1}, {"competition_gain": -1}, {"decision_threshold": 0},
        {"decision_threshold": 1.01}, {"time_scale_seconds": 0}, {"max_step_seconds": 0},
        {"crossing_tolerance": 0}, {"tie_tolerance": -1},
    ):
        with pytest.raises(ValueError):
            parameters(**changes)
    with pytest.raises(ValueError, match="non-negative"):
        CompetitionEdge(left="a", right="b", weight=-0.1)
    with pytest.raises(ValueError, match="differ"):
        CompetitionEdge(left="a", right="a", weight=1.0)
    with pytest.raises(ValueError, match="working set"):
        advance_langchao(
            state(), until=NOW, parameters=parameters(),
            competition_edges=(CompetitionEdge(left="a", right="missing", weight=1.0),),
        )
    with pytest.raises(ValueError, match="duplicate"):
        advance_langchao(
            state(working_set=("a", "b")), until=NOW, parameters=parameters(),
            competition_edges=(
                CompetitionEdge(left="a", right="b", weight=1.0),
                CompetitionEdge(left="a", right="b", weight=2.0),
            ),
        )
    with pytest.raises(ValueError, match="finite"):
        advance_langchao(state(), until=NOW, parameters=parameters(), decision_budget_seconds=float("nan"))
    with pytest.raises(ValueError, match="non-negative"):
        advance_langchao(state(), until=NOW, parameters=parameters(), decision_budget_seconds=-1)


def test_public_records_are_frozen_and_input_is_not_mutated() -> None:
    config = parameters()
    edge = CompetitionEdge(left="a", right="b", weight=0.5)
    with pytest.raises(FrozenInstanceError):
        config.leak = 0.3  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        edge.weight = 0.9  # type: ignore[misc]
    original = state()
    advance_langchao(original, until=NOW + timedelta(seconds=1), parameters=config)
    assert original == state()
    with pytest.raises(ValueError, match="parameter_version"):
        replace(config, parameter_version="latest")


def test_kernel_has_no_random_or_hazard_mechanism() -> None:
    source = Path(__file__).parents[1] / "src" / "companion_runtime" / "langchao_engine.py"
    text = source.read_text(encoding="utf-8").lower()
    forbidden = ("import random", "from random", "hazard")
    assert all(item not in text for item in forbidden)


def test_readiness_remains_bounded_for_extreme_finite_inputs() -> None:
    item = state(
        working_set=("a", "b"), readiness=(("a", 0.0), ("b", 1.0)),
        attraction=(("a", 1e100), ("b", -1e100)),
    )
    result = advance_langchao(
        item, until=NOW + timedelta(seconds=1),
        parameters=parameters(leak=1e50, competition_gain=1e50, decision_threshold=1.0),
        competition_edges=(CompetitionEdge(left="a", right="b", weight=1e50),),
    )
    assert all(0.0 <= value <= 1.0 for value in scores(result).values())
