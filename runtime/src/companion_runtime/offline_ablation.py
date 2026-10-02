"""Pure, offline B0--B3 ablation runner for frozen decision fixtures.

The runner accepts canonical JSON facts only.  It does not construct a Runtime, open a
repository, call a semantic provider, commit an action, or touch a delivery platform.
Its output is evidence about deterministic selection mechanics, not user effects.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .langchao_engine import CompetitionEdge, LangchaoParameters, advance_langchao
from .langchao_types import LangchaoState, MotivationDirection

ABLATION_INPUT_VERSION = "offline-ablation-input.v1"
ABLATION_RESULT_VERSION = "offline-ablation-result.v1"
ABLATION_RUNNER_VERSION = "offline-ablation-runner.v1"
GROUPS = ("B0", "B1", "B2", "B3")
_DIRECTIONS = tuple(direction.value for direction in MotivationDirection)


class AblationFixtureError(ValueError):
    """The canonical fixture is incomplete or internally inconsistent."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AblationFixtureError(f"{name} must be an object")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise AblationFixtureError(f"{name} must be an array")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AblationFixtureError(f"{name} must be a non-empty string")
    return value


def _number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AblationFixtureError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise AblationFixtureError(f"{name} must be finite and >= {minimum}")
    return result


def _utc(value: Any, name: str) -> datetime:
    text = _text(value, name)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AblationFixtureError(f"{name} must be ISO-8601") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise AblationFixtureError(f"{name} must be UTC")
    return stamp


def _versions(fixture: Mapping[str, Any]) -> dict[str, str]:
    raw = _object(fixture.get("versions"), "versions")
    required = ("candidate_supply", "forecasts", "values", "time", "permissions")
    result = {name: _text(raw.get(name), f"versions.{name}") for name in required}
    optional = ("attention", "competition_edges", "parameters")
    result.update(
        (name, _text(raw[name], f"versions.{name}")) for name in optional if name in raw
    )
    return result


def _direction_map(value: Any, name: str, *, default: float | None = None) -> dict[str, float]:
    raw = _object(value, name)
    extra = set(raw) - set(_DIRECTIONS)
    if extra:
        raise AblationFixtureError(f"{name} contains unknown directions: {sorted(extra)}")
    result: dict[str, float] = {}
    for direction in _DIRECTIONS:
        if direction in raw:
            result[direction] = _number(raw[direction], f"{name}.{direction}", minimum=0.0)
        elif default is not None:
            result[direction] = default
        else:
            raise AblationFixtureError(f"{name}.{direction} is required")
    return result


def _forecast_utility(
    candidate_id: str,
    forecasts: Mapping[str, Any],
    values: Mapping[str, float],
    attention: Mapping[str, float],
) -> tuple[float, list[str], dict[str, float]]:
    rows = _array(forecasts.get(candidate_id), f"forecasts.{candidate_id}")
    unknown: list[str] = []
    totals = {direction: 0.0 for direction in _DIRECTIONS}
    seen: set[str] = set()
    for index, raw_row in enumerate(rows):
        row = _object(raw_row, f"forecasts.{candidate_id}[{index}]")
        outcome_id = _text(row.get("outcome_id"), "outcome_id")
        if outcome_id in seen:
            raise AblationFixtureError(f"duplicate outcome_id {outcome_id!r} for {candidate_id}")
        seen.add(outcome_id)
        probability = row.get("probability")
        if probability is None:
            unknown.append(outcome_id)
            continue
        probability = _number(probability, f"{outcome_id}.probability", minimum=0.0)
        if probability > 1.0:
            raise AblationFixtureError(f"{outcome_id}.probability must be <= 1")
        amount = _number(row.get("amount"), f"{outcome_id}.amount")
        directions = _object(row.get("directions"), f"{outcome_id}.directions")
        for direction, raw_weight in directions.items():
            if direction not in totals:
                raise AblationFixtureError(f"unknown direction {direction!r}")
            weight = _number(raw_weight, f"{outcome_id}.directions.{direction}")
            totals[direction] += amount * probability * weight
    weighted = {
        direction: totals[direction] * values[direction] * attention[direction]
        for direction in _DIRECTIONS
    }
    return math.fsum(weighted.values()), sorted(unknown), weighted


def _rank(rows: Sequence[dict[str, Any]], score: str) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (-float(row[score]), str(row["candidate_id"])))
    return [{**row, "rank": index} for index, row in enumerate(ordered, 1)]


def _hazard(advantage: float, elapsed: float, tempo: Mapping[str, Any]) -> dict[str, Any]:
    base = _number(tempo.get("hazard_base"), "hazard_base", minimum=0.0)
    beta = _number(tempo.get("hazard_beta"), "hazard_beta", minimum=0.0)
    if beta == 0:
        raise AblationFixtureError("hazard_beta must be positive")
    scaled = beta * advantage
    softplus = scaled if scaled > 50 else math.exp(scaled) if scaled < -50 else math.log1p(math.exp(scaled))
    rate = base * softplus
    cumulative = rate * elapsed
    return {"advantage": advantage, "lambda_rate": rate, "elapsed_allowed_seconds": elapsed,
            "cumulative_lambda": cumulative, "probability": 1.0 - math.exp(-cumulative)}


def _runtime_group(
    *, name: str, candidates: list[dict[str, Any]], permissions: Mapping[str, Any],
    tempo: Mapping[str, Any], hazard_mode: str, fixed_draw: float | None,
    versions: Mapping[str, str],
) -> dict[str, Any]:
    threshold = _number(tempo.get("utility_threshold", 0.0), "utility_threshold")
    elapsed = _number(tempo.get("elapsed_allowed_seconds"), "elapsed_allowed_seconds", minimum=0.0)
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        verdict = _object(permissions[candidate_id], f"permissions.{candidate_id}")
        allowed = verdict.get("allowed")
        if not isinstance(allowed, bool):
            raise AblationFixtureError(f"permissions.{candidate_id}.allowed must be boolean")
        reasons = verdict.get("reasons", [])
        if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
            raise AblationFixtureError(f"permissions.{candidate_id}.reasons must be strings")
        score = candidate["runtime_score"]
        eligible = allowed and score >= threshold
        rows.append({
            "candidate_id": candidate_id, "score": score, "eligible": eligible,
            "blocked": not allowed, "permission_reasons": reasons,
            "unknown": candidate["unknown"], "score_terms": candidate["runtime_terms"],
        })
    ranks = _rank(rows, "score")
    eligible = [row for row in ranks if row["eligible"]]
    chosen = eligible[0] if eligible else None
    hazard = None if chosen is None else _hazard(chosen["score"] - threshold, elapsed, tempo)
    if chosen is None:
        decision = {"status": "defer", "candidate_id": None, "reason": "no_eligible_candidate"}
    elif hazard_mode == "report":
        decision = {"status": "defer", "candidate_id": chosen["candidate_id"],
                    "reason": "hazard_report_only"}
    else:
        assert fixed_draw is not None and hazard is not None
        won = fixed_draw < hazard["probability"]
        decision = {"status": "selected" if won else "defer",
                    "candidate_id": chosen["candidate_id"],
                    "reason": "fixed_draw_won" if won else "fixed_draw_not_won"}
        hazard["fixed_draw"] = fixed_draw
    return {
        "group": name, "engine": "runtime_v2", "candidate_ranks": ranks,
        "defer": decision["status"] == "defer", "unknown": sorted({item for row in rows for item in row["unknown"]}),
        "decision": decision, "hazard": hazard,
        "versions": {**versions, "engine": "runtime-v2.0", "tempo": _text(tempo.get("version"), "tempo.version")},
    }


def _langchao_group(
    *, name: str, candidates: list[dict[str, Any]], permissions: Mapping[str, Any],
    time: Mapping[str, Any], values: Mapping[str, float], attention: Mapping[str, float],
    edges: tuple[CompetitionEdge, ...], competition_gain: float, versions: Mapping[str, str],
) -> dict[str, Any]:
    allowed = [item for item in candidates if bool(_object(permissions[item["candidate_id"]], "permission").get("allowed"))]
    blocked_ids = {item["candidate_id"] for item in candidates} - {item["candidate_id"] for item in allowed}
    tempo = _object(time.get("matched_tempo"), "time.matched_tempo")
    params_raw = _object(time.get("langchao_parameters"), "time.langchao_parameters")
    scale = _number(params_raw.get("utility_scale"), "utility_scale", minimum=0.0)
    if scale == 0:
        raise AblationFixtureError("utility_scale must be positive")
    readiness_raw = _object(time.get("initial_readiness"), "time.initial_readiness")
    attractions: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    for item in candidates:
        forecast_score, unknown, weighted = _forecast_utility(item["candidate_id"], item["forecast_rows"], values, attention)
        attraction = (item["internal_utility"] + forecast_score) / scale
        attractions[item["candidate_id"]] = attraction
        reasons = list(_object(permissions[item["candidate_id"]], "permission").get("reasons", []))
        rows.append({"candidate_id": item["candidate_id"], "score": attraction,
                     "eligible": item["candidate_id"] not in blocked_ids,
                     "blocked": item["candidate_id"] in blocked_ids,
                     "permission_reasons": reasons, "unknown": unknown,
                     "score_terms": {"internal": item["internal_utility"], "weighted_directions": weighted}})
    ranked = _rank(rows, "score")
    working = tuple(row["candidate_id"] for row in ranked if not row["blocked"])
    if not working:
        return {"group": name, "engine": "langchao", "candidate_ranks": ranked, "defer": True,
                "unknown": sorted({item for row in rows for item in row["unknown"]}),
                "decision": {"status": "defer", "candidate_id": None, "reason": "no_permitted_candidate"},
                "hazard": None, "versions": {**versions, "engine": "langchao.advance-result.v1",
                "tempo": _text(tempo.get("version"), "tempo.version")}}
    now = _utc(time.get("now"), "time.now")
    budget = _number(tempo.get("decision_budget_seconds"), "decision_budget_seconds", minimum=0.0)
    state = LangchaoState(
        scope_key="offline:ablation", decision_round_id="offline:" + name,
        working_set=working,
        readiness=tuple((key, _number(readiness_raw.get(key, 0.0), f"initial_readiness.{key}", minimum=0.0)) for key in working),
        attraction=tuple((key, attractions[key]) for key in working),
        attention=tuple((direction, attention[direction.value]) for direction in MotivationDirection),
        advanced_at=now, based_on_state_version=0, event_cursor="offline",
        goal_snapshot_version=versions["candidate_supply"], reward_snapshot_version=versions["values"],
        candidate_snapshot_version=versions["candidate_supply"], prediction_snapshot_version=versions["forecasts"],
        value_profile_version=versions["values"], attention_version=versions.get("attention", "attention:all-one"),
        parameter_version="langchao.parameters.v1", permission_version=versions["permissions"],
    )
    parameters = LangchaoParameters(
        leak=_number(params_raw.get("leak"), "leak", minimum=0.0), competition_gain=competition_gain,
        decision_threshold=_number(params_raw.get("decision_threshold"), "decision_threshold", minimum=0.0),
        time_scale_seconds=_number(params_raw.get("time_scale_seconds"), "time_scale_seconds", minimum=0.0),
        max_step_seconds=_number(params_raw.get("max_step_seconds"), "max_step_seconds", minimum=0.0),
        crossing_tolerance=_number(params_raw.get("crossing_tolerance"), "crossing_tolerance", minimum=0.0),
        tie_tolerance=_number(params_raw.get("tie_tolerance"), "tie_tolerance", minimum=0.0),
    )
    result = advance_langchao(
        state, until=now + timedelta(seconds=budget), parameters=parameters,
        competition_edges=tuple(edge for edge in edges if edge.left in working and edge.right in working),
        decision_budget_seconds=budget, tie_break_order=working,
    )
    readiness = dict(result.state.readiness)
    for row in ranked:
        row["final_readiness"] = readiness.get(row["candidate_id"])
    decision = ({"status": "selected", "candidate_id": result.decision, "reason": "threshold_crossed"}
                if result.decision else {"status": "defer", "candidate_id": None,
                "reason": result.defer_reason or "no_threshold_crossing"})
    return {
        "group": name, "engine": "langchao", "candidate_ranks": ranked,
        "defer": result.decision is None, "unknown": sorted({item for row in rows for item in row["unknown"]}),
        "decision": decision, "hazard": None,
        "versions": {**versions, "engine": result.result_version,
                     "parameters": parameters.parameter_version,
                     "tempo": _text(tempo.get("version"), "tempo.version")},
    }


def run_ablation(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Run all four groups over one immutable canonical fixture."""

    fixture = _object(fixture, "fixture")
    if fixture.get("fixture_version") != ABLATION_INPUT_VERSION:
        raise AblationFixtureError(f"fixture_version must be {ABLATION_INPUT_VERSION}")
    fixture_id = _text(fixture.get("fixture_id"), "fixture_id")
    versions = _versions(fixture)
    candidate_rows = _array(fixture.get("candidates"), "candidates")
    forecasts = _object(fixture.get("forecasts"), "forecasts")
    permissions = _object(fixture.get("permissions"), "permissions")
    values_raw = _object(fixture.get("values"), "values")
    values = _direction_map(values_raw.get("direction_weights"), "values.direction_weights")
    time = _object(fixture.get("time"), "time")
    candidates: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(candidate_rows):
        item = _object(raw, f"candidates[{index}]")
        candidate_id = _text(item.get("candidate_id"), "candidate_id")
        if candidate_id in ids:
            raise AblationFixtureError(f"duplicate candidate_id {candidate_id!r}")
        ids.add(candidate_id)
        internal = _number(item.get("internal_utility"), f"{candidate_id}.internal_utility")
        runtime_user = _number(item.get("runtime_user_utility"), f"{candidate_id}.runtime_user_utility")
        repeat_cost = _number(item.get("repeat_cost", 0.0), f"{candidate_id}.repeat_cost", minimum=0.0)
        if candidate_id not in forecasts or candidate_id not in permissions:
            raise AblationFixtureError(f"{candidate_id} requires forecasts and permissions")
        forecast_rows = {candidate_id: forecasts[candidate_id]}
        unknown = sorted(
            _text(_object(row, "forecast").get("outcome_id"), "outcome_id")
            for row in _array(forecasts[candidate_id], f"forecasts.{candidate_id}")
            if _object(row, "forecast").get("probability") is None
        )
        candidates.append({"candidate_id": candidate_id, "internal_utility": internal,
                           "runtime_score": internal + runtime_user - repeat_cost,
                           "runtime_terms": {"internal": internal, "user": runtime_user,
                                             "repeat_cost": -repeat_cost},
                           "unknown": unknown, "forecast_rows": forecast_rows})
    if set(forecasts) != ids or set(permissions) != ids:
        raise AblationFixtureError("forecasts and permissions must exactly cover candidates")
    hazard = _object(fixture.get("hazard", {"mode": "report"}), "hazard")
    mode = hazard.get("mode", "report")
    if mode not in {"report", "fixed_draw"}:
        raise AblationFixtureError("hazard.mode must be report or fixed_draw")
    draw = None
    if mode == "fixed_draw":
        draw = _number(hazard.get("draw"), "hazard.draw", minimum=0.0)
        if draw >= 1.0:
            raise AblationFixtureError("hazard.draw must be < 1")
    baseline = _object(time.get("runtime_v2_baseline"), "time.runtime_v2_baseline")
    matched = _object(time.get("matched_tempo"), "time.matched_tempo")
    attention = _direction_map(fixture.get("attention", {}), "attention", default=1.0)
    edges = tuple(
        CompetitionEdge(left=_text(_object(raw, "edge").get("left"), "edge.left"),
                        right=_text(_object(raw, "edge").get("right"), "edge.right"),
                        weight=_number(_object(raw, "edge").get("weight"), "edge.weight", minimum=0.0))
        for raw in _array(fixture.get("competition_edges", []), "competition_edges")
    )
    lang_params = _object(time.get("langchao_parameters"), "time.langchao_parameters")
    full_gain = _number(lang_params.get("competition_gain"), "competition_gain", minimum=0.0)
    groups = [
        _runtime_group(name="B0", candidates=candidates, permissions=permissions, tempo=baseline,
                       hazard_mode=mode, fixed_draw=draw, versions=versions),
        _runtime_group(name="B1", candidates=candidates, permissions=permissions, tempo=matched,
                       hazard_mode=mode, fixed_draw=draw, versions=versions),
        _langchao_group(name="B2", candidates=candidates, permissions=permissions, time=time,
                        values=values, attention={key: 1.0 for key in _DIRECTIONS}, edges=(),
                        competition_gain=0.0, versions=versions),
        _langchao_group(name="B3", candidates=candidates, permissions=permissions, time=time,
                        values=values, attention=attention, edges=edges,
                        competition_gain=full_gain, versions=versions),
    ]
    return {
        "result_version": ABLATION_RESULT_VERSION, "runner_version": ABLATION_RUNNER_VERSION,
        "fixture_id": fixture_id, "fixture_sha256": _digest(fixture),
        "offline_only": True, "platform_contacted": False,
        "claim_scope": "selection_mechanics_only_no_user_effect_claim",
        "groups": groups,
    }


def audit_ablation(fixture: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Fail-closed audit of a result against its canonical fixture."""

    issues: list[str] = []
    if result.get("result_version") != ABLATION_RESULT_VERSION:
        issues.append("invalid_result_version")
    if result.get("fixture_sha256") != _digest(fixture):
        issues.append("fixture_hash_mismatch")
    if result.get("offline_only") is not True or result.get("platform_contacted") is not False:
        issues.append("offline_boundary_not_declared")
    groups = result.get("groups")
    if not isinstance(groups, list) or [item.get("group") for item in groups if isinstance(item, Mapping)] != list(GROUPS):
        issues.append("groups_not_exact_B0_B1_B2_B3")
    else:
        by_name = {item["group"]: item for item in groups}
        expected_ids = sorted(item["candidate_id"] for item in fixture["candidates"])
        for name in GROUPS:
            rows = by_name[name].get("candidate_ranks")
            if not isinstance(rows, list) or sorted(row.get("candidate_id") for row in rows) != expected_ids:
                issues.append(f"{name}:candidate_denominator_mismatch")
            if "defer" not in by_name[name] or "unknown" not in by_name[name] or "decision" not in by_name[name]:
                issues.append(f"{name}:incomplete_outcome")
        if by_name["B0"].get("engine") != "runtime_v2" or by_name["B1"].get("engine") != "runtime_v2":
            issues.append("runtime_groups_wrong_engine")
        if by_name["B2"].get("engine") != "langchao" or by_name["B3"].get("engine") != "langchao":
            issues.append("langchao_groups_wrong_engine")
        supply_versions = {item.get("versions", {}).get("candidate_supply") for item in groups}
        if len(supply_versions) != 1:
            issues.append("candidate_supply_not_frozen")
        if by_name["B1"].get("versions", {}).get("tempo") != by_name["B2"].get("versions", {}).get("tempo") or by_name["B2"].get("versions", {}).get("tempo") != by_name["B3"].get("versions", {}).get("tempo"):
            issues.append("matched_tempo_version_mismatch")
    return {"audit_version": "offline-ablation-audit.v1", "status": "pass" if not issues else "fail",
            "fixture_sha256": _digest(fixture), "issues": issues,
            "user_effects_evaluated": False}


__all__ = ["ABLATION_INPUT_VERSION", "ABLATION_RESULT_VERSION", "ABLATION_RUNNER_VERSION",
           "AblationFixtureError", "audit_ablation", "canonical_json_bytes", "run_ablation"]
