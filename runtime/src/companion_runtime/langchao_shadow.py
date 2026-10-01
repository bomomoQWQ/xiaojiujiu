"""Minimal, side-effect-bounded coordinator for 「浪潮」 shadow evaluation.

The coordinator can persist only a new state revision and an immutable shadow audit.
Its repository capability is intentionally narrow: execution and learning surfaces are
not part of the protocol.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, ContextManager, Mapping, Protocol, TypeAlias, runtime_checkable

from .langchao_engine import AdvanceResult, CompetitionEdge, LangchaoParameters, advance_langchao
from .langchao_reward import (
    AttentionProfile,
    CandidateCostTerm,
    OutcomeForecast,
    RewardCompilation,
    TemplateProbabilityPolicy,
    ValueProfile,
    compile_candidate_reward,
)
from .langchao_types import ActionCandidateContract, GoalContract, LangchaoState, RewardContract

LANGCHAO_SHADOW_VERSION = "langchao.shadow.v1"
LANGCHAO_SHADOW_MODE = "shadow"


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredShadowRun:
    """Minimal verified projection returned for a persisted idempotency hit."""

    scope_key: str
    run_id: str
    idempotency_key: str
    input_sha256: str
    audit_sha256: str
    payload: Mapping[str, Any]


ShadowRunResult: TypeAlias = "ShadowRunRecord | StoredShadowRun"


def _text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parameters_dict(value: LangchaoParameters) -> dict[str, Any]:
    return {
        "leak": value.leak,
        "competition_gain": value.competition_gain,
        "decision_threshold": value.decision_threshold,
        "time_scale_seconds": value.time_scale_seconds,
        "max_step_seconds": value.max_step_seconds,
        "crossing_tolerance": value.crossing_tolerance,
        "tie_tolerance": value.tie_tolerance,
        "parameter_version": value.parameter_version,
    }


def _edge_dict(value: CompetitionEdge) -> dict[str, Any]:
    return {"left": value.left, "right": value.right, "weight": value.weight, "edge_version": value.edge_version}


def _advance_dict(value: AdvanceResult) -> dict[str, Any]:
    return {
        "state": value.state.to_dict(),
        "steps": [
            {
                "started_at": step.started_at.isoformat(),
                "ended_at": step.ended_at.isoformat(),
                "readiness_before": dict(step.readiness_before),
                "readiness_after": dict(step.readiness_after),
                "attraction": dict(step.attraction),
                "first_crossing_candidates": list(step.first_crossing_candidates),
                "step_version": step.step_version,
            }
            for step in value.steps
        ],
        "decision_candidate_id": value.decision_candidate_id,
        "decision_at": value.decision_at.isoformat() if value.decision_at else None,
        "defer_reason": value.defer_reason,
        "result_version": value.result_version,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowCandidateInput:
    goal: GoalContract
    reward: RewardContract
    candidate: ActionCandidateContract
    forecasts: tuple[OutcomeForecast, ...]
    costs: tuple[CandidateCostTerm, ...] = ()
    template_probability_policy: TemplateProbabilityPolicy = ()
    template_policy_version: str | None = None
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.goal, GoalContract):
            raise TypeError("goal must be GoalContract")
        if not isinstance(self.reward, RewardContract):
            raise TypeError("reward must be RewardContract")
        if not isinstance(self.candidate, ActionCandidateContract):
            raise TypeError("candidate must be ActionCandidateContract")
        if self.goal.scope_key != self.reward.scope_key or self.goal.scope_key != self.candidate.scope_key:
            raise ValueError("goal, reward, and candidate must share scope_key")
        if self.reward.goal_id != self.goal.goal_id or self.goal.goal_id not in self.candidate.goal_refs:
            raise ValueError("candidate and reward must reference the supplied goal")
        if self.candidate.reward_contract_ref != self.reward.reward_contract_id:
            raise ValueError("candidate must reference the supplied reward")
        if set(self.candidate.expected_outcome_token_ids) != {
            token.token_id for token in self.reward.outcome_tokens if token.settlement_type.value == "expected"
        }:
            raise ValueError("candidate expected outcomes must match reward expected outcomes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal.to_dict(),
            "reward": self.reward.to_dict(),
            "candidate": self.candidate.to_dict(),
            "forecasts": [item.to_dict() for item in self.forecasts],
            "costs": [item.to_dict() for item in self.costs],
            "template_probability_policy": dict(self.template_probability_policy),
            "template_policy_version": self.template_policy_version,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowComparison:
    """A data-only comparison against an already-computed v2 baseline."""

    baseline_candidate_id: str | None
    shadow_candidate_id: str | None
    same_candidate: bool
    baseline_defer_reason: str | None = None
    shadow_defer_reason: str | None = None
    baseline_version: str = "runtime.decision.v2"

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_candidate_id": self.baseline_candidate_id,
            "shadow_candidate_id": self.shadow_candidate_id,
            "same_candidate": self.same_candidate,
            "baseline_defer_reason": self.baseline_defer_reason,
            "shadow_defer_reason": self.shadow_defer_reason,
            "baseline_version": self.baseline_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowRunRecord:
    scope_key: str
    run_id: str
    idempotency_key: str
    source_input_cursor: str
    source_input_version: str
    decision_round_id: str
    candidate_id: str | None
    defer_reason: str | None
    comparison: ShadowComparison
    compilations: tuple[RewardCompilation, ...]
    advance_result: AdvanceResult
    input_sha256: str
    audit_sha256: str
    mode: str = LANGCHAO_SHADOW_MODE
    sent_count: int = 0
    reward_count: int = 0
    training_count: int = 0
    quota_count: int = 0
    outbox_id: None = None
    shadow_version: str = LANGCHAO_SHADOW_VERSION

    def __post_init__(self) -> None:
        for name in ("scope_key", "run_id", "idempotency_key", "source_input_cursor", "source_input_version", "decision_round_id"):
            _text(name, getattr(self, name))
        if self.mode != LANGCHAO_SHADOW_MODE:
            raise ValueError("mode must be shadow")
        if any(getattr(self, name) != 0 for name in ("sent_count", "reward_count", "training_count", "quota_count")):
            raise ValueError("shadow side-effect counts must be zero")
        if self.outbox_id is not None:
            raise ValueError("shadow output identifier must be null")
        if not isinstance(self.advance_result, AdvanceResult):
            raise TypeError("advance_result must be AdvanceResult")
        for name in ("input_sha256", "audit_sha256"):
            value = getattr(self, name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "source_input_cursor": self.source_input_cursor,
            "source_input_version": self.source_input_version,
            "decision_round_id": self.decision_round_id,
            "mode": self.mode,
            "candidate_id": self.candidate_id,
            "defer_reason": self.defer_reason,
            "comparison": self.comparison.to_dict(),
            "audit": {
                "compilations": [item.to_dict() for item in self.compilations],
                "advance_result": _advance_dict(self.advance_result),
                "shadow_version": self.shadow_version,
            },
            "input_sha256": self.input_sha256,
            "audit_sha256": self.audit_sha256,
            "sent_count": self.sent_count,
            "reward_count": self.reward_count,
            "training_count": self.training_count,
            "quota_count": self.quota_count,
            "outbox_id": self.outbox_id,
        }


@runtime_checkable
class LangchaoShadowRepository(Protocol):
    """Only capabilities the shadow coordinator is allowed to possess."""

    def transaction(self) -> ContextManager[None]: ...

    def get_shadow_run(self, *, scope_key: str, idempotency_key: str) -> ShadowRunResult | None: ...

    def put_state_revision(
        self, *, result: AdvanceResult, input_state: LangchaoState,
        candidate_revisions: dict[str, int], expected_pointer_version: int,
    ) -> None: ...

    def put_shadow_run(self, run: ShadowRunRecord) -> None: ...


class LangchaoShadowPostgresRepository:
    """Adapter combining the v14 state repository with v16 shadow audit writes."""

    def __init__(self, connection: Any, *, state_repository: Any) -> None:
        self.connection = connection
        self.state_repository = state_repository

    def transaction(self) -> ContextManager[None]:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    def get_shadow_run(self, *, scope_key: str, idempotency_key: str) -> StoredShadowRun | None:
        row = self.connection.execute(
            """SELECT run_id, input_sha256, audit_sha256, source_input_cursor,
                      source_input_version, decision_round_id, mode, candidate_id,
                      defer_reason, comparison, audit, sent_count, reward_count,
                      training_count, quota_count, outbox_id
               FROM langchao_shadow_runs
               WHERE scope_key = %s AND idempotency_key = %s""",
            (scope_key, idempotency_key),
        ).fetchone()
        if row is None:
            return None

        def value(key: str, index: int) -> Any:
            return row[key] if isinstance(row, Mapping) else row[index]

        comparison = value("comparison", 9)
        audit = value("audit", 10)
        if isinstance(comparison, str):
            comparison = json.loads(comparison)
        if isinstance(audit, str):
            audit = json.loads(audit)
        if not isinstance(comparison, Mapping) or not isinstance(audit, Mapping):
            raise RuntimeError("persisted shadow audit JSON is invalid")
        audit_payload = {
            "compilations": audit.get("compilations"),
            "advance_result": audit.get("advance_result"),
            "comparison": dict(comparison),
            "shadow_version": audit.get("shadow_version"),
        }
        audit_digest = _sha256(audit_payload)
        stored_digest = str(value("audit_sha256", 2))
        if audit_digest != stored_digest:
            raise RuntimeError("persisted shadow audit hash mismatch")
        payload = {
            "scope_key": scope_key,
            "run_id": value("run_id", 0),
            "idempotency_key": idempotency_key,
            "source_input_cursor": value("source_input_cursor", 3),
            "source_input_version": value("source_input_version", 4),
            "decision_round_id": value("decision_round_id", 5),
            "mode": value("mode", 6),
            "candidate_id": value("candidate_id", 7),
            "defer_reason": value("defer_reason", 8),
            "comparison": dict(comparison),
            "audit": dict(audit),
            "input_sha256": value("input_sha256", 1),
            "audit_sha256": stored_digest,
            "sent_count": value("sent_count", 11),
            "reward_count": value("reward_count", 12),
            "training_count": value("training_count", 13),
            "quota_count": value("quota_count", 14),
            "outbox_id": value("outbox_id", 15),
        }
        if payload["mode"] != LANGCHAO_SHADOW_MODE or any(
            payload[name] != 0 for name in ("sent_count", "reward_count", "training_count", "quota_count")
        ) or payload["outbox_id"] is not None:
            raise RuntimeError("persisted shadow row violates zero-side-effect invariants")
        return StoredShadowRun(
            scope_key=scope_key,
            run_id=str(payload["run_id"]),
            idempotency_key=idempotency_key,
            input_sha256=str(payload["input_sha256"]),
            audit_sha256=stored_digest,
            payload=payload,
        )

    def put_state_revision(
        self, *, result: AdvanceResult, input_state: LangchaoState,
        candidate_revisions: dict[str, int], expected_pointer_version: int,
    ) -> None:
        self.state_repository.append_advance(
            result, input_state=input_state, candidate_revisions=candidate_revisions,
            expected_pointer_version=expected_pointer_version,
        )

    def put_shadow_run(self, run: ShadowRunRecord) -> None:
        payload = run.to_dict()
        comparison = _canonical(payload["comparison"])
        audit = _canonical(payload["audit"])
        self.connection.execute(
            """INSERT INTO langchao_shadow_runs
               (scope_key, run_id, idempotency_key, source_input_cursor, source_input_version,
                decision_round_id, mode, candidate_id, defer_reason, comparison, audit,
                input_sha256, audit_sha256, sent_count, reward_count, training_count,
                quota_count, outbox_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                       %s, %s, 0, 0, 0, 0, NULL)
               ON CONFLICT (scope_key, idempotency_key) DO NOTHING""",
            (run.scope_key, run.run_id, run.idempotency_key, run.source_input_cursor,
             run.source_input_version, run.decision_round_id, run.mode, run.candidate_id,
             run.defer_reason, comparison, audit, run.input_sha256, run.audit_sha256),
        )
        row = self.connection.execute(
            """SELECT run_id, input_sha256, audit_sha256 FROM langchao_shadow_runs
               WHERE scope_key = %s AND idempotency_key = %s""",
            (run.scope_key, run.idempotency_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("shadow audit insert was not observable")
        def value(key: str, index: int) -> Any:
            return row[key] if hasattr(row, "keys") else row[index]
        if (value("run_id", 0), value("input_sha256", 1), value("audit_sha256", 2)) != (
            run.run_id, run.input_sha256, run.audit_sha256,
        ):
            raise RuntimeError("shadow idempotency conflict")


class FakeLangchaoShadowRepository:
    """Transactional in-memory fake implementing exactly the safe protocol."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.states: list[LangchaoState] = []
        self.runs: dict[tuple[str, str], ShadowRunRecord] = {}
        self.fail_on = fail_on

    @contextmanager
    def transaction(self):
        states = copy.copy(self.states)
        runs = copy.copy(self.runs)
        try:
            yield
        except BaseException:
            self.states = states
            self.runs = runs
            raise

    def get_shadow_run(self, *, scope_key: str, idempotency_key: str) -> ShadowRunRecord | None:
        return self.runs.get((scope_key, idempotency_key))

    def put_state_revision(
        self, *, result: AdvanceResult, input_state: LangchaoState,
        candidate_revisions: dict[str, int], expected_pointer_version: int,
    ) -> None:
        del input_state, candidate_revisions, expected_pointer_version
        if self.fail_on == "state":
            raise RuntimeError("injected state persistence failure")
        self.states.append(result.state)

    def put_shadow_run(self, run: ShadowRunRecord) -> None:
        if self.fail_on == "audit":
            raise RuntimeError("injected audit persistence failure")
        key = (run.scope_key, run.idempotency_key)
        existing = self.runs.get(key)
        if existing is not None and existing != run:
            raise RuntimeError("shadow idempotency conflict")
        self.runs[key] = run


def run_langchao_shadow(
    *,
    repository: LangchaoShadowRepository,
    run_id: str,
    idempotency_key: str,
    source_input_cursor: str,
    source_input_version: str,
    inputs: tuple[ShadowCandidateInput, ...],
    value_profile: ValueProfile,
    attention_profile: AttentionProfile,
    previous_state: LangchaoState,
    until: datetime,
    parameters: LangchaoParameters,
    competition_edges: tuple[CompetitionEdge, ...] = (),
    utility_scale: float,
    decision_budget_seconds: float | None = None,
    tie_break_order: tuple[str, ...] = (),
    expected_state_pointer_version: int = 0,
    baseline_candidate_id: str | None = None,
    baseline_defer_reason: str | None = None,
) -> ShadowRunResult:
    """Compile attractions, advance 「浪潮」, and atomically persist audit only."""

    for name, value in (("run_id", run_id), ("idempotency_key", idempotency_key), ("source_input_cursor", source_input_cursor), ("source_input_version", source_input_version)):
        _text(name, value)
    if not isinstance(repository, LangchaoShadowRepository):
        raise TypeError("repository must implement LangchaoShadowRepository")
    if not isinstance(inputs, tuple) or not inputs or any(not isinstance(item, ShadowCandidateInput) for item in inputs):
        raise TypeError("inputs must be a non-empty tuple of ShadowCandidateInput values")
    if any(item.candidate.scope_key != previous_state.scope_key for item in inputs):
        raise ValueError("all inputs must match previous_state scope_key")
    candidate_ids = tuple(item.candidate.candidate_id for item in inputs)
    if len(set(candidate_ids)) != len(candidate_ids) or set(candidate_ids) != set(previous_state.working_set):
        raise ValueError("inputs must cover previous_state working_set exactly")

    ordered_inputs = tuple(sorted(inputs, key=lambda item: item.candidate.candidate_id))
    input_payload = {
        "source_input_cursor": source_input_cursor,
        "source_input_version": source_input_version,
        "inputs": [item.to_dict() for item in ordered_inputs],
        "value_profile": value_profile.to_dict(),
        "attention_profile": attention_profile.to_dict(),
        "previous_state": previous_state.to_dict(),
        "until": until.isoformat(),
        "parameters": _parameters_dict(parameters),
        "competition_edges": [_edge_dict(item) for item in sorted(competition_edges, key=lambda edge: (edge.left, edge.right))],
        "utility_scale": utility_scale,
        "decision_budget_seconds": decision_budget_seconds,
        "tie_break_order": list(tie_break_order),
        "expected_state_pointer_version": expected_state_pointer_version,
    }
    input_sha256 = _sha256(input_payload)

    def verified_existing(existing: ShadowRunResult) -> ShadowRunResult:
        if (
            existing.scope_key != previous_state.scope_key
            or existing.idempotency_key != idempotency_key
            or existing.run_id != run_id
            or existing.input_sha256 != input_sha256
        ):
            raise RuntimeError("shadow idempotency conflict")
        return existing

    existing = repository.get_shadow_run(scope_key=previous_state.scope_key, idempotency_key=idempotency_key)
    if existing is not None:
        return verified_existing(existing)

    compilations = tuple(
        compile_candidate_reward(
            candidate_id=item.candidate.candidate_id,
            reward_contract=item.reward,
            forecasts=item.forecasts,
            value_profile=value_profile,
            attention_profile=attention_profile,
            utility_scale=utility_scale,
            costs=item.costs,
            template_probability_policy=item.template_probability_policy,
            template_policy_version=item.template_policy_version,
            source_refs=item.source_refs,
        )
        for item in ordered_inputs
    )
    attraction = {item.candidate_id: item.attraction for item in compilations}
    prepared_state = replace(
        previous_state,
        attraction=tuple((candidate_id, attraction[candidate_id]) for candidate_id in previous_state.working_set),
        value_profile_version=value_profile.version,
        attention=attention_profile.direction_weights,
        attention_version=attention_profile.version,
    )
    advanced = advance_langchao(
        prepared_state,
        until=until,
        parameters=parameters,
        competition_edges=competition_edges,
        decision_budget_seconds=decision_budget_seconds,
        tie_break_order=tie_break_order,
    )
    comparison = ShadowComparison(
        baseline_candidate_id=baseline_candidate_id,
        shadow_candidate_id=advanced.decision_candidate_id,
        same_candidate=baseline_candidate_id == advanced.decision_candidate_id,
        baseline_defer_reason=baseline_defer_reason,
        shadow_defer_reason=advanced.defer_reason,
    )
    audit_payload = {
        "compilations": [item.to_dict() for item in compilations],
        "advance_result": _advance_dict(advanced),
        "comparison": comparison.to_dict(),
        "shadow_version": LANGCHAO_SHADOW_VERSION,
    }
    record = ShadowRunRecord(
        scope_key=previous_state.scope_key,
        run_id=run_id,
        idempotency_key=idempotency_key,
        source_input_cursor=source_input_cursor,
        source_input_version=source_input_version,
        decision_round_id=previous_state.decision_round_id,
        candidate_id=advanced.decision_candidate_id,
        defer_reason=advanced.defer_reason,
        comparison=comparison,
        compilations=compilations,
        advance_result=advanced,
        input_sha256=input_sha256,
        audit_sha256=_sha256(audit_payload),
    )
    with repository.transaction():
        race = repository.get_shadow_run(scope_key=record.scope_key, idempotency_key=idempotency_key)
        if race is not None:
            return verified_existing(race)
        repository.put_state_revision(
            result=advanced,
            input_state=prepared_state,
            candidate_revisions={item.candidate.candidate_id: item.candidate.semantic_revision for item in inputs},
            expected_pointer_version=expected_state_pointer_version,
        )
        repository.put_shadow_run(record)
    return record


__all__ = [
    "FakeLangchaoShadowRepository",
    "LANGCHAO_SHADOW_MODE",
    "LANGCHAO_SHADOW_VERSION",
    "LangchaoShadowPostgresRepository",
    "LangchaoShadowRepository",
    "ShadowCandidateInput",
    "ShadowComparison",
    "ShadowRunRecord",
    "ShadowRunResult",
    "StoredShadowRun",
    "run_langchao_shadow",
]
