"""纯函数、完全隔离的「浪潮」数值核。

本模块只推进 :class:`LangchaoState`；不接 Runtime、数据库、发送或随机源。
每个冻结小步都从同一个 ``before`` 快照计算全部候选，以保证结果与遍历顺序无关。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .langchao_types import FrozenScores, LangchaoState

LANGCHAO_PARAMETERS_VERSION = "langchao.parameters.v1"
LANGCHAO_COMPETITION_EDGE_VERSION = "langchao.competition-edge.v1"
LANGCHAO_INTEGRATION_STEP_VERSION = "langchao.integration-step.v1"
LANGCHAO_ADVANCE_RESULT_VERSION = "langchao.advance-result.v1"


def _number(name: str, value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def _pairs(name: str, values: FrozenScores) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple of pairs")
    keys: list[str] = []
    for item in values:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"{name} entries must be pairs")
        key, value = item
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be non-empty strings")
        _number(f"{name} value", value)
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{name} must not contain duplicate keys")


@dataclass(frozen=True, slots=True, kw_only=True)
class LangchaoParameters:
    """带版本、严格校验的「浪潮」积分参数。"""

    leak: float
    competition_gain: float
    decision_threshold: float
    time_scale_seconds: float
    max_step_seconds: float
    crossing_tolerance: float
    tie_tolerance: float
    parameter_version: str = LANGCHAO_PARAMETERS_VERSION

    def __post_init__(self) -> None:
        for name in (
            "leak", "competition_gain", "decision_threshold", "time_scale_seconds",
            "max_step_seconds", "crossing_tolerance", "tie_tolerance",
        ):
            object.__setattr__(self, name, _number(name, getattr(self, name)))
        if self.leak < 0:
            raise ValueError("leak must be non-negative")
        if self.competition_gain < 0:
            raise ValueError("competition_gain must be non-negative")
        if not 0 < self.decision_threshold <= 1:
            raise ValueError("decision_threshold must be in (0, 1]")
        if self.time_scale_seconds <= 0:
            raise ValueError("time_scale_seconds must be positive")
        if self.max_step_seconds <= 0:
            raise ValueError("max_step_seconds must be positive")
        if self.crossing_tolerance <= 0:
            raise ValueError("crossing_tolerance must be positive")
        if self.tie_tolerance < 0:
            raise ValueError("tie_tolerance must be non-negative")
        if self.parameter_version != LANGCHAO_PARAMETERS_VERSION:
            raise ValueError(f"parameter_version must be {LANGCHAO_PARAMETERS_VERSION!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompetitionEdge:
    """有向竞争边：``right`` 以 ``weight`` 抑制 ``left``。"""

    left: str
    right: str
    weight: float
    edge_version: str = LANGCHAO_COMPETITION_EDGE_VERSION

    def __post_init__(self) -> None:
        for name in ("left", "right"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.left == self.right:
            raise ValueError("competition edge endpoints must differ")
        object.__setattr__(self, "weight", _number("weight", self.weight))
        if self.weight < 0:
            raise ValueError("weight must be non-negative")
        if self.edge_version != LANGCHAO_COMPETITION_EDGE_VERSION:
            raise ValueError(f"edge_version must be {LANGCHAO_COMPETITION_EDGE_VERSION!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationStep:
    """一个冻结系数小步的可审计轨迹。"""

    started_at: datetime
    ended_at: datetime
    readiness_before: FrozenScores
    readiness_after: FrozenScores
    attraction: FrozenScores
    first_crossing_candidates: tuple[str, ...] = ()
    step_version: str = LANGCHAO_INTEGRATION_STEP_VERSION

    def __post_init__(self) -> None:
        _utc("started_at", self.started_at)
        _utc("ended_at", self.ended_at)
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        for name in ("readiness_before", "readiness_after", "attraction"):
            _pairs(name, getattr(self, name))
        keys = tuple(key for key, _ in self.readiness_before)
        if set(key for key, _ in self.readiness_after) != set(keys):
            raise ValueError("readiness_after keys must match readiness_before")
        if set(key for key, _ in self.attraction) != set(keys):
            raise ValueError("attraction keys must match readiness_before")
        if not isinstance(self.first_crossing_candidates, tuple):
            raise TypeError("first_crossing_candidates must be a tuple")
        if len(set(self.first_crossing_candidates)) != len(self.first_crossing_candidates):
            raise ValueError("first_crossing_candidates must be unique")
        if not set(self.first_crossing_candidates).issubset(keys):
            raise ValueError("first_crossing_candidates must belong to the working set")
        if self.step_version != LANGCHAO_INTEGRATION_STEP_VERSION:
            raise ValueError(f"step_version must be {LANGCHAO_INTEGRATION_STEP_VERSION!r}")

    @property
    def start(self) -> datetime:
        """Compatibility spelling matching the mathematical interval start."""
        return self.started_at

    @property
    def end(self) -> datetime:
        """Compatibility spelling matching the mathematical interval end."""
        return self.ended_at

    @property
    def before_readiness(self) -> FrozenScores:
        return self.readiness_before

    @property
    def after_readiness(self) -> FrozenScores:
        return self.readiness_after


@dataclass(frozen=True, slots=True, kw_only=True)
class AdvanceResult:
    """一次推进的不可变结果；决定与暂缓互斥。"""

    state: LangchaoState
    steps: tuple[IntegrationStep, ...]
    decision_candidate_id: str | None = None
    decision_at: datetime | None = None
    defer_reason: str | None = None
    result_version: str = LANGCHAO_ADVANCE_RESULT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.state, LangchaoState):
            raise TypeError("state must be a LangchaoState")
        if not isinstance(self.steps, tuple) or any(not isinstance(item, IntegrationStep) for item in self.steps):
            raise TypeError("steps must be a tuple of IntegrationStep values")
        if (self.decision_candidate_id is None) != (self.decision_at is None):
            raise ValueError("decision candidate and time must be both present or both absent")
        if self.decision_candidate_id is not None:
            if self.decision_candidate_id not in self.state.working_set:
                raise ValueError("decision candidate must belong to the working set")
            _utc("decision_at", self.decision_at)  # type: ignore[arg-type]
        if self.defer_reason is not None and self.decision_candidate_id is not None:
            raise ValueError("decision and defer_reason are mutually exclusive")
        if self.result_version != LANGCHAO_ADVANCE_RESULT_VERSION:
            raise ValueError(f"result_version must be {LANGCHAO_ADVANCE_RESULT_VERSION!r}")

    @property
    def decision(self) -> str | None:
        return self.decision_candidate_id


def _validated_order(order: tuple[str, ...], working_set: tuple[str, ...], *, required: bool) -> dict[str, int]:
    if not isinstance(order, tuple):
        raise TypeError("tie_break_order must be a tuple")
    if len(order) != len(set(order)):
        raise ValueError("tie_break_order must contain unique candidates")
    if order and set(order) != set(working_set):
        raise ValueError("tie_break_order must cover the working set exactly")
    if required and set(order) != set(working_set):
        raise ValueError("tie_break_order must cover the working set exactly for a tie")
    return {candidate_id: index for index, candidate_id in enumerate(order)}


def _coefficients(
    working_set: tuple[str, ...], readiness: dict[str, float], attraction: dict[str, float],
    parameters: LangchaoParameters, inhibition: dict[str, tuple[tuple[str, float], ...]],
) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for candidate_id in working_set:
        support = attraction[candidate_id]
        a = max(support, 0.0)
        competition = math.fsum(weight * readiness[other] for other, weight in inhibition[candidate_id])
        b = parameters.leak + max(-support, 0.0) + parameters.competition_gain * competition
        result[candidate_id] = (a, b)
    return result


def _value_after(value: float, a: float, b: float, seconds: float, time_scale: float) -> float:
    rate = a + b
    if rate == 0 or seconds == 0:
        return value
    equilibrium = a / rate
    updated = equilibrium + (value - equilibrium) * math.exp(-rate * seconds / time_scale)
    return min(1.0, max(0.0, updated))


def _crossing_seconds(
    before: float, a: float, b: float, threshold: float, upper_seconds: float,
    time_scale: float, tolerance: float,
) -> float | None:
    if before >= threshold:
        return 0.0
    if _value_after(before, a, b, upper_seconds, time_scale) < threshold:
        return None
    low, high = 0.0, upper_seconds
    while high - low > tolerance:
        middle = (low + high) / 2.0
        if _value_after(before, a, b, middle, time_scale) >= threshold:
            high = middle
        else:
            low = middle
    return high


def advance_langchao(
    state: LangchaoState,
    *,
    until: datetime,
    parameters: LangchaoParameters,
    competition_edges: tuple[CompetitionEdge, ...] = (),
    decision_budget_seconds: float | None = None,
    tie_break_order: tuple[str, ...] = (),
) -> AdvanceResult:
    """按真实时间推进「浪潮」，在首达决定边界时立即停止。"""

    if not isinstance(state, LangchaoState):
        raise TypeError("state must be a LangchaoState")
    if not isinstance(parameters, LangchaoParameters):
        raise TypeError("parameters must be LangchaoParameters")
    _utc("until", until)
    if until < state.advanced_at:
        raise ValueError("until must not precede state.advanced_at")
    if not isinstance(competition_edges, tuple):
        raise TypeError("competition_edges must be a tuple")
    if any(not isinstance(edge, CompetitionEdge) for edge in competition_edges):
        raise TypeError("competition_edges must contain only CompetitionEdge values")
    order_index = _validated_order(tie_break_order, state.working_set, required=False)

    budget: float | None = None
    if decision_budget_seconds is not None:
        budget = _number("decision_budget_seconds", decision_budget_seconds)
        if budget < 0:
            raise ValueError("decision_budget_seconds must be non-negative")

    working = set(state.working_set)
    seen_edges: set[tuple[str, str]] = set()
    inhibition_lists: dict[str, list[tuple[str, float]]] = {item: [] for item in state.working_set}
    for edge in competition_edges:
        if edge.left not in working or edge.right not in working:
            raise ValueError("competition edge endpoints must belong to the working set")
        key = (edge.left, edge.right)
        if key in seen_edges:
            raise ValueError("competition edges must not contain duplicate directed edges")
        seen_edges.add(key)
        inhibition_lists[edge.left].append((edge.right, edge.weight))
    inhibition = {
        key: tuple(sorted(values, key=lambda item: item[0])) for key, values in inhibition_lists.items()
    }

    readiness = dict(state.readiness)
    attraction = dict(state.attraction)
    threshold = parameters.decision_threshold
    initially_ready = tuple(candidate_id for candidate_id in state.working_set if readiness[candidate_id] >= threshold)
    if initially_ready:
        _validated_order(tie_break_order, state.working_set, required=len(initially_ready) > 1)
        chosen = min(initially_ready, key=lambda item: order_index.get(item, 0))
        new_state = replace(
            state, parameter_version=parameters.parameter_version, revision=state.revision + 1,
        )
        return AdvanceResult(state=new_state, steps=(), decision_candidate_id=chosen, decision_at=state.advanced_at)

    requested_seconds = (until - state.advanced_at).total_seconds()
    exhausted = budget is not None and budget < requested_seconds
    advance_seconds = min(requested_seconds, budget) if budget is not None else requested_seconds
    target = state.advanced_at + timedelta(seconds=advance_seconds)
    cursor = state.advanced_at
    steps: list[IntegrationStep] = []

    while cursor < target:
        duration = min(parameters.max_step_seconds, (target - cursor).total_seconds())
        before = dict(readiness)
        coefficients = _coefficients(state.working_set, before, attraction, parameters, inhibition)
        tentative = {
            candidate_id: _value_after(
                before[candidate_id], *coefficients[candidate_id], duration, parameters.time_scale_seconds,
            )
            for candidate_id in state.working_set
        }
        crossing_times: dict[str, float] = {}
        for candidate_id in state.working_set:
            crossing = _crossing_seconds(
                before[candidate_id], *coefficients[candidate_id], threshold, duration,
                parameters.time_scale_seconds, parameters.crossing_tolerance,
            )
            if crossing is not None:
                crossing_times[candidate_id] = crossing

        if crossing_times:
            earliest = min(crossing_times.values())
            tied = tuple(sorted(
                (candidate_id for candidate_id, crossing in crossing_times.items()
                 if crossing - earliest <= parameters.tie_tolerance),
            ))
            _validated_order(tie_break_order, state.working_set, required=len(tied) > 1)
            chosen = min(tied, key=lambda item: order_index.get(item, 0))
            event_seconds = crossing_times[chosen]
            readiness = {
                candidate_id: _value_after(
                    before[candidate_id], *coefficients[candidate_id], event_seconds,
                    parameters.time_scale_seconds,
                )
                for candidate_id in state.working_set
            }
            ended_at = cursor + timedelta(seconds=event_seconds)
            steps.append(IntegrationStep(
                started_at=cursor, ended_at=ended_at,
                readiness_before=tuple((item, before[item]) for item in state.working_set),
                readiness_after=tuple((item, readiness[item]) for item in state.working_set),
                attraction=tuple((item, attraction[item]) for item in state.working_set),
                first_crossing_candidates=tied,
            ))
            new_state = replace(
                state,
                readiness=tuple((item, readiness[item]) for item in state.working_set),
                advanced_at=ended_at,
                parameter_version=parameters.parameter_version,
                revision=state.revision + 1,
            )
            return AdvanceResult(
                state=new_state, steps=tuple(steps), decision_candidate_id=chosen, decision_at=ended_at,
            )

        ended_at = cursor + timedelta(seconds=duration)
        readiness = tentative
        steps.append(IntegrationStep(
            started_at=cursor, ended_at=ended_at,
            readiness_before=tuple((item, before[item]) for item in state.working_set),
            readiness_after=tuple((item, readiness[item]) for item in state.working_set),
            attraction=tuple((item, attraction[item]) for item in state.working_set),
        ))
        cursor = ended_at

    new_state = replace(
        state,
        readiness=tuple((item, readiness[item]) for item in state.working_set),
        advanced_at=target,
        parameter_version=parameters.parameter_version,
        revision=state.revision + 1,
    )
    return AdvanceResult(
        state=new_state,
        steps=tuple(steps),
        defer_reason="decision_budget_exhausted" if exhausted or (budget is not None and advance_seconds == budget) else None,
    )


__all__ = [
    "AdvanceResult", "CompetitionEdge", "IntegrationStep", "LANGCHAO_ADVANCE_RESULT_VERSION",
    "LANGCHAO_COMPETITION_EDGE_VERSION", "LANGCHAO_INTEGRATION_STEP_VERSION",
    "LANGCHAO_PARAMETERS_VERSION", "LangchaoParameters",
    "advance_langchao",
]
