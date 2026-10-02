from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.langchao_attention_recipe import (
    B2_ATTENTION_VERSION,
    B2_RECIPE_VERSION,
    B3_ATTENTION_VERSION,
    B3_RECIPE_VERSION,
    BASELINE_ATTENTION_VERSION,
    BASELINE_RECIPE_VERSION,
    LANGCHAO_ATTENTION_RECIPE_AUDIT_VERSION,
    AttentionRecipeCandidate,
    ExplicitAttentionSignals,
    compile_attention_recipe,
)
from companion_runtime.langchao_engine import LangchaoParameters, advance_langchao
from companion_runtime.langchao_types import LangchaoState, MotivationDirection

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def parameters(**overrides) -> LangchaoParameters:
    values = {
        "leak": 0.2,
        "competition_gain": 0.0,
        "decision_threshold": 0.6,
        "time_scale_seconds": 10.0,
        "max_step_seconds": 0.05,
        "crossing_tolerance": 1e-7,
        "tie_tolerance": 1e-6,
    }
    values.update(overrides)
    return LangchaoParameters(**values)


def candidate(candidate_id, direction, urgency=0.0, freshness=0.0, resource=0.0):
    return AttentionRecipeCandidate(
        candidate_id=candidate_id,
        direction=direction,
        signals=ExplicitAttentionSignals(
            goal_urgency=urgency,
            source_freshness=freshness,
            resource_availability=resource,
        ),
    )


def state(attraction=(0.9, 0.9), readiness=(0.0, 0.0)) -> LangchaoState:
    ids = ("care", "rest")
    return LangchaoState(
        scope_key="scope",
        decision_round_id="round",
        working_set=ids,
        readiness=tuple(zip(ids, readiness)),
        attraction=tuple(zip(ids, attraction)),
        attention=tuple((direction, 1.0) for direction in MotivationDirection),
        advanced_at=NOW,
        based_on_state_version=0,
        event_cursor="cursor",
        goal_snapshot_version="goals",
        reward_snapshot_version="rewards",
        candidate_snapshot_version="candidates",
        prediction_snapshot_version="predictions",
        value_profile_version="values",
        attention_version="attention",
        parameter_version="langchao.parameters.v1",
        permission_version="permissions",
    )


def test_off_and_b2_preserve_all_one_no_competition_baseline() -> None:
    inputs = (candidate("a", MotivationDirection.CARE, 1, 1, 1),)
    off = compile_attention_recipe(
        selection="off", candidates=inputs, baseline_parameters=parameters(competition_gain=8)
    )
    b2 = compile_attention_recipe(
        selection="b2", candidates=inputs, baseline_parameters=parameters(competition_gain=8)
    )
    assert off.recipe_version == BASELINE_RECIPE_VERSION
    assert off.attention_profile.version == BASELINE_ATTENTION_VERSION
    assert b2.recipe_version == B2_RECIPE_VERSION
    assert b2.attention_profile.version == B2_ATTENTION_VERSION
    for plan in (off, b2):
        assert set(dict(plan.attention_profile.direction_weights).values()) == {1.0}
        assert plan.parameters.competition_gain == 0.0
        assert plan.competition_edges == ()


def test_b3_positive_control_uses_only_explicit_signals_and_full_edges() -> None:
    inputs = (
        candidate("care", MotivationDirection.CARE, 1.0, 1.0, 1.0),
        candidate("rest", MotivationDirection.REST, 0.0, 0.0, 0.0),
        candidate("explore", MotivationDirection.EXPLORATION, 0.5, 0.5, 0.5),
    )
    plan = compile_attention_recipe(
        selection="B3", candidates=inputs, baseline_parameters=parameters()
    )
    weights = dict(plan.attention_profile.direction_weights)
    assert plan.recipe_version == B3_RECIPE_VERSION
    assert plan.attention_profile.version == B3_ATTENTION_VERSION
    assert plan.parameters.competition_gain == 1.0
    assert weights[MotivationDirection.CARE] == pytest.approx(2.0)
    assert weights[MotivationDirection.EXPLORATION] == pytest.approx(1.125)
    assert weights[MotivationDirection.REST] == pytest.approx(0.25)
    assert len(plan.competition_edges) == 6
    assert {edge.weight for edge in plan.competition_edges} == {0.5}
    assert plan.audit_dict()["audit_version"] == LANGCHAO_ATTENTION_RECIPE_AUDIT_VERSION
    assert set(plan.audit_dict()["candidates"][0]) == {"candidate_id", "direction", "signals"}


def test_signal_validation_rejects_nonfinite_out_of_range_and_implicit_fields() -> None:
    for value in (-0.1, 1.1, math.inf, math.nan):
        with pytest.raises(ValueError):
            ExplicitAttentionSignals(goal_urgency=value)
    with pytest.raises(TypeError):
        ExplicitAttentionSignals(source_freshness=True)
    with pytest.raises(TypeError):
        ExplicitAttentionSignals(user_desire=1.0)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="unique"):
        compile_attention_recipe(
            selection="B3",
            candidates=(candidate("x", MotivationDirection.CARE),
                        candidate("x", MotivationDirection.REST)),
            baseline_parameters=parameters(),
        )


def test_b3_extreme_signals_are_finite_bounded_and_order_deterministic() -> None:
    items = (
        candidate("z", MotivationDirection.CARE, 1, 1, 1),
        candidate("a", MotivationDirection.REST, 0, 0, 0),
    )
    first = compile_attention_recipe(
        selection="B3", candidates=items, baseline_parameters=parameters()
    )
    second = compile_attention_recipe(
        selection="B3", candidates=tuple(reversed(items)), baseline_parameters=parameters()
    )
    assert first == second
    weights = dict(first.attention_profile.direction_weights)
    assert all(math.isfinite(value) and 0.25 <= value <= 2.0 for value in weights.values())
    assert all(math.isfinite(edge.weight) and edge.weight > 0 for edge in first.competition_edges)


def test_full_competition_positive_control_reaches_a_decision() -> None:
    plan = compile_attention_recipe(
        selection="B3",
        candidates=(
            candidate("care", MotivationDirection.CARE, 1, 1, 1),
            candidate("rest", MotivationDirection.REST, 0, 0, 0),
        ),
        baseline_parameters=parameters(),
    )
    result = advance_langchao(
        state(attraction=(1.0, 0.15)),
        until=NOW + timedelta(seconds=30),
        parameters=plan.parameters,
        competition_edges=plan.competition_edges,
        decision_budget_seconds=30,
        tie_break_order=("care", "rest"),
    )
    assert result.decision_candidate_id == "care"
    assert result.defer_reason is None


def test_positive_floor_prevents_permanent_starvation_after_competitor_removal() -> None:
    plan = compile_attention_recipe(
        selection="B3",
        candidates=(
            candidate("care", MotivationDirection.CARE, 1, 1, 1),
            candidate("rest", MotivationDirection.REST, 0, 0, 0),
        ),
        baseline_parameters=parameters(decision_threshold=0.5),
    )
    # A low-signal candidate keeps positive attention. Once the competing candidate is
    # absent in a later round, the same kernel can still select it within finite time.
    assert dict(plan.attention_profile.direction_weights)[MotivationDirection.REST] == 0.25
    solo = replace(
        state(attraction=(0.9, 0.9)),
        working_set=("rest",),
        readiness=(("rest", 0.0),),
        attraction=(("rest", 0.35),),
    )
    result = advance_langchao(
        solo,
        until=NOW + timedelta(seconds=60),
        parameters=plan.parameters,
        competition_edges=(),
        decision_budget_seconds=60,
        tie_break_order=("rest",),
    )
    assert result.decision_candidate_id == "rest"
