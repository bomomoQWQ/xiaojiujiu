"""Versioned, opt-in attention/competition recipe compiler.

This pure module consumes only caller-supplied operational facts: explicit goal
urgency, source freshness, and resource availability. It has no runtime/user-model
reader and cannot infer latent user intent. Wiring is intentionally left to an
explicit review/ablation composition; production defaults are untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .langchao_engine import CompetitionEdge, LangchaoParameters
from .langchao_reward import AttentionProfile
from .langchao_types import MotivationDirection

LANGCHAO_ATTENTION_RECIPE_AUDIT_VERSION = "langchao.attention-recipe-audit.v1"
BASELINE_RECIPE = "off"
B2_RECIPE = "B2"
B3_RECIPE = "B3"
RECIPE_CHOICES = (BASELINE_RECIPE, B2_RECIPE, B3_RECIPE)

BASELINE_RECIPE_VERSION = "langchao.recipe.off-all-one-no-competition.v1"
B2_RECIPE_VERSION = "langchao.recipe.b2-all-one-no-competition.v1"
B3_RECIPE_VERSION = "langchao.recipe.b3-explicit-attention-full-competition.v1"
BASELINE_ATTENTION_VERSION = "langchao.attention.all-one.v1"
B2_ATTENTION_VERSION = "langchao.attention.b2-all-one.v1"
B3_ATTENTION_VERSION = "langchao.attention.b3-explicit-signals.v1"


def parameter_version_for_recipe(plan: "LangchaoAttentionRecipePlan") -> str:
    """Audit version recorded in state without relaxing engine schema validation."""
    if not isinstance(plan, LangchaoAttentionRecipePlan):
        raise TypeError("plan must be LangchaoAttentionRecipePlan")
    if plan.selection == BASELINE_RECIPE:
        return plan.parameters.parameter_version
    return f"{plan.parameters.parameter_version}+{plan.audit_version}+{plan.recipe_version}"


def _unit_signal(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")
    return result


def normalize_recipe(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("recipe must be a string")
    normalized = value.strip()
    if normalized.lower() == BASELINE_RECIPE:
        return BASELINE_RECIPE
    normalized = normalized.upper()
    if normalized not in {B2_RECIPE, B3_RECIPE}:
        raise ValueError(f"recipe must be one of {RECIPE_CHOICES!r}")
    return normalized


@dataclass(frozen=True, slots=True, kw_only=True)
class ExplicitAttentionSignals:
    """Bounded operational inputs; deliberately excludes inferred user psychology."""

    goal_urgency: float = 0.0
    source_freshness: float = 0.0
    resource_availability: float = 0.0

    def __post_init__(self) -> None:
        for name in ("goal_urgency", "source_freshness", "resource_availability"):
            _unit_signal(name, getattr(self, name))

    @property
    def score(self) -> float:
        return math.fsum((
            0.45 * float(self.goal_urgency),
            0.35 * float(self.source_freshness),
            0.20 * float(self.resource_availability),
        ))

    def to_dict(self) -> dict[str, float]:
        return {
            "goal_urgency": float(self.goal_urgency),
            "source_freshness": float(self.source_freshness),
            "resource_availability": float(self.resource_availability),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionRecipeCandidate:
    candidate_id: str
    direction: MotivationDirection
    signals: ExplicitAttentionSignals = ExplicitAttentionSignals()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id must be a non-empty string")
        if not isinstance(self.direction, MotivationDirection):
            raise TypeError("direction must be MotivationDirection")
        if not isinstance(self.signals, ExplicitAttentionSignals):
            raise TypeError("signals must be ExplicitAttentionSignals")


@dataclass(frozen=True, slots=True, kw_only=True)
class LangchaoAttentionRecipePlan:
    selection: str
    recipe_version: str
    audit_version: str
    attention_profile: AttentionProfile
    parameters: LangchaoParameters
    competition_edges: tuple[CompetitionEdge, ...]
    candidates: tuple[AttentionRecipeCandidate, ...]

    def audit_dict(self) -> dict[str, object]:
        return {
            "selection": self.selection,
            "recipe_version": self.recipe_version,
            "audit_version": self.audit_version,
            "attention_version": self.attention_profile.version,
            "parameter_version": self.parameters.parameter_version,
            "competition_edge_version": (
                self.competition_edges[0].edge_version if self.competition_edges else None
            ),
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "direction": item.direction.value,
                    "signals": item.signals.to_dict(),
                }
                for item in self.candidates
            ],
        }


def compile_attention_recipe(
    *,
    selection: str,
    candidates: tuple[AttentionRecipeCandidate, ...],
    baseline_parameters: LangchaoParameters,
) -> LangchaoAttentionRecipePlan:
    """Compile a deterministic B2/B3 plan without mutating production defaults."""

    selected = normalize_recipe(selection)
    if not isinstance(candidates, tuple) or any(
        not isinstance(item, AttentionRecipeCandidate) for item in candidates
    ):
        raise TypeError("candidates must be a tuple of AttentionRecipeCandidate values")
    candidate_ids = tuple(item.candidate_id for item in candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidates must have unique candidate_id values")
    if not isinstance(baseline_parameters, LangchaoParameters):
        raise TypeError("baseline_parameters must be LangchaoParameters")

    ordered = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    if selected in {BASELINE_RECIPE, B2_RECIPE}:
        attention_version = (
            BASELINE_ATTENTION_VERSION if selected == BASELINE_RECIPE else B2_ATTENTION_VERSION
        )
        recipe_version = (
            BASELINE_RECIPE_VERSION if selected == BASELINE_RECIPE else B2_RECIPE_VERSION
        )
        attention = AttentionProfile(
            version=attention_version,
            direction_weights=tuple((direction, 1.0) for direction in MotivationDirection),
        )
        return LangchaoAttentionRecipePlan(
            selection=selected,
            recipe_version=recipe_version,
            audit_version=LANGCHAO_ATTENTION_RECIPE_AUDIT_VERSION,
            attention_profile=attention,
            parameters=replace(baseline_parameters, competition_gain=0.0),
            competition_edges=(),
            candidates=ordered,
        )

    by_direction: dict[MotivationDirection, list[float]] = {
        direction: [] for direction in MotivationDirection
    }
    for item in ordered:
        by_direction[item.direction].append(item.signals.score)

    # Strictly positive floor preserves reachability. The upper bound prevents an
    # extreme explicit signal from making integration numerically ill-conditioned.
    attention = AttentionProfile(
        version=B3_ATTENTION_VERSION,
        direction_weights=tuple(
            (direction, 1.0 if not by_direction[direction]
             else 0.25 + 1.75 * max(by_direction[direction]))
            for direction in MotivationDirection
        ),
    )
    divisor = max(1, len(ordered) - 1)
    edges = tuple(
        CompetitionEdge(left=left.candidate_id, right=right.candidate_id,
                        weight=1.0 / divisor)
        for left in ordered for right in ordered if left.candidate_id != right.candidate_id
    )
    return LangchaoAttentionRecipePlan(
        selection=selected,
        recipe_version=B3_RECIPE_VERSION,
        audit_version=LANGCHAO_ATTENTION_RECIPE_AUDIT_VERSION,
        attention_profile=attention,
        parameters=replace(baseline_parameters, competition_gain=1.0),
        competition_edges=edges,
        candidates=ordered,
    )


__all__ = [
    "AttentionRecipeCandidate", "B2_ATTENTION_VERSION", "B2_RECIPE",
    "B2_RECIPE_VERSION", "B3_ATTENTION_VERSION", "B3_RECIPE", "B3_RECIPE_VERSION",
    "BASELINE_ATTENTION_VERSION", "BASELINE_RECIPE", "BASELINE_RECIPE_VERSION",
    "ExplicitAttentionSignals", "LANGCHAO_ATTENTION_RECIPE_AUDIT_VERSION",
    "LangchaoAttentionRecipePlan", "RECIPE_CHOICES", "compile_attention_recipe",
    "normalize_recipe", "parameter_version_for_recipe",
]
