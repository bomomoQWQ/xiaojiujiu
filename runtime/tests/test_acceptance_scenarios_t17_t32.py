"""Executable structural/numeric checks for the T17-T32 audit manifest.

These tests deliberately do not turn semantic review into string matching.  They pin the
parts of T29-T32 that are mechanical today and leave the rest marked ``planned`` in the
manifest.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from companion_runtime.langchao_engine import (
    CompetitionEdge,
    LangchaoParameters,
    advance_langchao,
)
from companion_runtime.langchao_types import LangchaoState, MotivationDirection

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "audit" / "scenarios" / "T17-T32.json"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _scenario(identifier: str) -> dict[str, object]:
    scenarios = _manifest()["scenarios"]
    assert isinstance(scenarios, list)
    return next(item for item in scenarios if item["id"] == identifier)


def _state(
    *,
    working_set: tuple[str, ...] = ("a", "b"),
    readiness: tuple[tuple[str, float], ...] = (("a", 0.1), ("b", 0.7)),
    attraction: tuple[tuple[str, float], ...] = (("a", 0.5), ("b", 0.5)),
) -> LangchaoState:
    return LangchaoState(
        scope_key="audit:t29",
        decision_round_id="audit-round:t29",
        working_set=working_set,
        readiness=readiness,
        attraction=attraction,
        attention=tuple((direction, 1.0) for direction in MotivationDirection),
        advanced_at=NOW,
        based_on_state_version=1,
        event_cursor="event:visible:1",
        goal_snapshot_version="goals:1",
        reward_snapshot_version="rewards:1",
        candidate_snapshot_version="candidates:1",
        prediction_snapshot_version="predictions:1",
        value_profile_version="values:1",
        attention_version="attention:1",
        parameter_version="langchao.parameters.v1",
        permission_version="permission:1",
    )


def _parameters(max_step_seconds: float) -> LangchaoParameters:
    return LangchaoParameters(
        leak=0.2,
        competition_gain=1.0,
        decision_threshold=0.6,
        time_scale_seconds=10.0,
        max_step_seconds=max_step_seconds,
        crossing_tolerance=1e-7,
        tie_tolerance=1e-6,
    )


def _readiness(result) -> dict[str, float]:
    return dict(result.state.readiness)


def test_manifest_has_exact_contiguous_scenarios_and_review_facets() -> None:
    manifest = _manifest()
    scenarios = manifest["scenarios"]
    assert [item["id"] for item in scenarios] == [f"T{number}" for number in range(17, 33)]
    for item in scenarios:
        assert item["phase"]
        assert item["fixture"]["status"] in {"executable", "partial", "planned"}
        assert item["mechanical"]["status"] in {"executable", "partial", "planned"}
        assert item["semantic"]["status"] in {"executable", "partial", "planned"}
        assert item["oracle"]["hard_failures"]
        assert item["positive_controls"]
        assert "gaps" in item


def test_t29_frozen_numeric_tolerance_and_non_vacuous_oracles() -> None:
    scenario = _scenario("T29")
    fixture = scenario["fixture"]
    tolerance = fixture["tolerance"]["absolute_readiness_medium_vs_fine"]
    edges = (
        CompetitionEdge(left="a", right="b", weight=0.5),
        CompetitionEdge(left="b", right="a", weight=0.5),
    )
    runs = {
        step: advance_langchao(
            _state(
                readiness=(("a", 0.1), ("b", 0.2)),
                attraction=(("a", 0.25), ("b", 0.4)),
            ),
            until=NOW + timedelta(seconds=fixture["timeline_seconds"]),
            parameters=_parameters(step),
            competition_edges=edges,
            decision_budget_seconds=fixture["decision_budget_seconds"],
            tie_break_order=tuple(fixture["tie_order"]),
        )
        for step in fixture["step_sizes_seconds"]
    }
    coarse, medium, fine = (runs[step] for step in (1.0, 0.1, 0.01))
    coarse_error = max(abs(_readiness(coarse)[key] - _readiness(fine)[key]) for key in ("a", "b"))
    medium_error = max(abs(_readiness(medium)[key] - _readiness(fine)[key]) for key in ("a", "b"))
    assert all(0.0 <= value <= 1.0 for result in runs.values() for value in _readiness(result).values())
    # The engine uses analytic frozen-coefficient steps. This symmetric fixture may
    # therefore be partition-invariant; refinement must not worsen the reference error.
    assert medium_error <= coarse_error
    assert medium_error < tolerance

    # Non-vacuous positive control: a legal single candidate reaches a decision.
    positive = advance_langchao(
        _state(working_set=("expression",), readiness=(("expression", 0.1),),
               attraction=(("expression", 0.5),)),
        until=NOW + timedelta(seconds=100),
        parameters=_parameters(1.0),
        decision_budget_seconds=100.0,
    )
    assert positive.decision == "expression"

    # A symmetric inhibited pair exposes a real defer/stall outcome rather than a fake winner.
    stalled = advance_langchao(
        _state(readiness=(("a", 0.0), ("b", 0.0))),
        until=NOW + timedelta(seconds=20),
        parameters=_parameters(0.1),
        competition_edges=edges,
        decision_budget_seconds=20.0,
        tie_break_order=("a", "b"),
    )
    assert stalled.decision is None
    assert stalled.defer_reason == "decision_budget_exhausted"


def test_t30_manifest_freezes_four_distinct_ablation_groups() -> None:
    groups = _scenario("T30")["fixture"]["groups"]
    assert set(groups) == {"B0", "B1", "B2", "B3"}
    assert {groups[name]["candidate_supply"] for name in groups} == {"runtime_v2_frozen"}
    assert {groups[name]["semantic_generation"] for name in groups} == {"frozen"}
    assert groups["B0"]["engine"] == groups["B1"]["engine"] == "runtime_v2"
    assert groups["B0"]["tempo"] != groups["B1"]["tempo"]
    assert groups["B2"]["engine"] != groups["B3"]["engine"]
    assert groups["B1"]["tempo"] == groups["B2"]["tempo"] == groups["B3"]["tempo"]


def test_t31_fixture_separates_visible_hidden_and_unobserved_outcomes() -> None:
    partition = _scenario("T31")["fixture"]["information_partition"]
    visible = set(partition["model_visible"])
    evaluation_only = set(partition["evaluation_only"])
    not_observed = set(partition["not_observed"])
    assert visible.isdisjoint(evaluation_only | not_observed)
    assert evaluation_only.isdisjoint(not_observed)
    assert "hidden_world_state" not in visible
    assert "counterfactual_user_satisfaction" in not_observed


def test_t32_all_silent_control_cannot_claim_capability_pass() -> None:
    scenario = _scenario("T32")
    controls = scenario["fixture"]["controls"]
    assertions = scenario["mechanical"]["assertions"]
    failures = scenario["oracle"]["hard_failures"]
    assert set(controls) == {"expression_profile", "exploration_profile", "all_silent_control"}
    assert "总是 defer/no action" in controls["all_silent_control"]
    assert any("capability_pass=false" in assertion for assertion in assertions)
    assert "all_silent_capability_pass" in failures
