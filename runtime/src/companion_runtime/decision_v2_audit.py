"""Isolated decision-v2 audit contracts and in-memory recorder.

This module is deliberately pure logic: it has no database, scheduler, delivery, or
legacy-runtime imports.  A caller may persist :meth:`DecisionAuditRecorder.to_dict`
elsewhere, but merely importing this module cannot change legacy behaviour or schema.

The recorder describes what happened; it does not make a decision.  In particular the
hazard draw is supplied by the caller and is mandatory whenever a trial is recorded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

DECISION_V2_AUDIT_CONTRACT_VERSION = "decision-audit-v2.0"
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class DecisionAuditError(ValueError):
    """Base class for an invalid decision-v2 audit record."""


class DecisionStageError(DecisionAuditError):
    """Raised when a stage violates the audit state machine."""


class IdempotencyConflict(DecisionAuditError):
    """Raised when the same stage is retried with different facts."""


class DecisionStage(str, Enum):
    """Stable names for the minimum decision-v2 audit stages."""

    WAKE = "wake"
    PERMISSIONS = "permissions"
    CANDIDATE_ELIGIBLE = "candidate_eligible"
    HAZARD_TRIAL_PERFORMED = "hazard_trial_performed"
    HAZARD_TRIAL_WON = "hazard_trial_won"
    COMMITTED = "committed"
    RENDERED = "rendered"
    SEND_ACK = "send_ack"
    SEND_FAIL = "send_fail"
    RECONCILED = "reconciled"


_TERMINAL_SEND_STAGES = {DecisionStage.SEND_ACK, DecisionStage.SEND_FAIL}
_ALLOWED_NEXT: dict[DecisionStage | None, frozenset[DecisionStage]] = {
    None: frozenset({DecisionStage.WAKE}),
    DecisionStage.WAKE: frozenset({DecisionStage.PERMISSIONS}),
    DecisionStage.PERMISSIONS: frozenset(
        {DecisionStage.CANDIDATE_ELIGIBLE, DecisionStage.RECONCILED}
    ),
    DecisionStage.CANDIDATE_ELIGIBLE: frozenset(
        {DecisionStage.HAZARD_TRIAL_PERFORMED, DecisionStage.RECONCILED}
    ),
    DecisionStage.HAZARD_TRIAL_PERFORMED: frozenset(
        {DecisionStage.HAZARD_TRIAL_WON, DecisionStage.RECONCILED}
    ),
    DecisionStage.HAZARD_TRIAL_WON: frozenset(
        {DecisionStage.COMMITTED, DecisionStage.RECONCILED}
    ),
    DecisionStage.COMMITTED: frozenset(
        {DecisionStage.RENDERED, DecisionStage.RECONCILED}
    ),
    DecisionStage.RENDERED: frozenset(
        {DecisionStage.SEND_ACK, DecisionStage.SEND_FAIL, DecisionStage.RECONCILED}
    ),
    DecisionStage.SEND_ACK: frozenset({DecisionStage.RECONCILED}),
    DecisionStage.SEND_FAIL: frozenset({DecisionStage.RECONCILED}),
    DecisionStage.RECONCILED: frozenset(),
}


def _text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DecisionAuditError(f"{name} must be a non-empty string")


def _finite(name: str, value: float, *, minimum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionAuditError(f"{name} must be a number")
    if not math.isfinite(float(value)):
        raise DecisionAuditError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise DecisionAuditError(f"{name} must be >= {minimum}")


def _probability(name: str, value: float | None) -> None:
    if value is None:
        return
    _finite(name, value)
    if not 0.0 <= float(value) <= 1.0:
        raise DecisionAuditError(f"{name} must be in [0, 1]")


def _timestamp(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DecisionAuditError(f"{name} must be a timezone-aware datetime")


def _json_value(value: Any, path: str = "details") -> JsonValue:
    """Copy and validate JSON data, rejecting NaN and mutable/non-string-key surprises."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DecisionAuditError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DecisionAuditError(f"{path} keys must be strings")
            result[key] = _json_value(item, f"{path}.{key}")
        return result
    raise DecisionAuditError(f"{path} must contain only JSON values")


def _number_map(name: str, value: Mapping[str, float]) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise DecisionAuditError(f"{name} must be a mapping")
    copied: dict[str, float] = {}
    for key, number in value.items():
        _text(f"{name} key", key)
        _finite(f"{name}[{key!r}]", number)
        copied[key] = float(number)
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateAssessment:
    """Auditable inputs and utility decomposition for one candidate."""

    candidate_id: str
    prediction_snapshot_id: str
    used_bounds: Mapping[str, float]
    utility_terms: Mapping[str, float]
    repeat_key: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _text("candidate_id", self.candidate_id)
        _text("prediction_snapshot_id", self.prediction_snapshot_id)
        _text("repeat_key", self.repeat_key)
        object.__setattr__(self, "used_bounds", _number_map("used_bounds", self.used_bounds))
        object.__setattr__(self, "utility_terms", _number_map("utility_terms", self.utility_terms))
        if not isinstance(self.reasons, tuple):
            raise DecisionAuditError("reasons must be a tuple")
        for reason in self.reasons:
            _text("reason", reason)
        if len(set(self.reasons)) != len(self.reasons):
            raise DecisionAuditError("reasons must not contain duplicates")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "candidate_id": self.candidate_id,
            "prediction_snapshot_id": self.prediction_snapshot_id,
            "used_bounds": dict(self.used_bounds),
            "utility_terms": dict(self.utility_terms),
            "repeat_key": self.repeat_key,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionRun:
    """Frozen numerical and version context for exactly one decision attempt."""

    decision_id: str
    scope: str
    policy_version: str
    contract_version: str
    feature_version: str
    parameter_version: str
    D: float
    lambda_rate: float
    delta_allowed_seconds: float
    cumulative_lambda: float
    trial_probability: float | None
    random_draw: float | None
    chosen: str | None

    def __post_init__(self) -> None:
        for name in (
            "decision_id",
            "scope",
            "policy_version",
            "contract_version",
            "feature_version",
            "parameter_version",
        ):
            _text(name, getattr(self, name))
        _finite("D", self.D)
        _finite("lambda_rate", self.lambda_rate, minimum=0.0)
        _finite("delta_allowed_seconds", self.delta_allowed_seconds, minimum=0.0)
        _finite("cumulative_lambda", self.cumulative_lambda, minimum=0.0)
        _probability("trial_probability", self.trial_probability)
        _probability("random_draw", self.random_draw)
        if (self.trial_probability is None) != (self.random_draw is None):
            raise DecisionAuditError(
                "trial_probability and random_draw must either both be recorded or both be absent"
            )
        if self.chosen is not None:
            _text("chosen", self.chosen)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "decision_id": self.decision_id,
            "scope": self.scope,
            "policy_version": self.policy_version,
            "contract_version": self.contract_version,
            "feature_version": self.feature_version,
            "parameter_version": self.parameter_version,
            "D": float(self.D),
            "lambda": float(self.lambda_rate),
            "delta_allowed_seconds": float(self.delta_allowed_seconds),
            "cumulative_lambda": float(self.cumulative_lambda),
            "trial_probability": None
            if self.trial_probability is None
            else float(self.trial_probability),
            "random_draw": None if self.random_draw is None else float(self.random_draw),
            "chosen": self.chosen,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionAuditEvent:
    """One immutable state transition."""

    stage: DecisionStage
    occurred_at: datetime
    details: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.stage, DecisionStage):
            raise DecisionAuditError("stage must be a DecisionStage")
        _timestamp("occurred_at", self.occurred_at)
        copied = _json_value(self.details)
        if not isinstance(copied, dict):  # defensive; details is declared Mapping
            raise DecisionAuditError("details must be a mapping")
        object.__setattr__(self, "details", MappingProxyType(copied))

    def to_dict(self) -> dict[str, JsonValue]:
        # Re-copy recursively so callers cannot mutate nested recorder state through the
        # serialization result.
        details = _json_value(self.details)
        assert isinstance(details, dict)
        return {
            "stage": self.stage.value,
            "occurred_at": self.occurred_at.isoformat(),
            "details": details,
        }


class DecisionAuditRecorder:
    """Append-only, idempotent state-machine recorder for one :class:`DecisionRun`."""

    def __init__(
        self,
        run: DecisionRun,
        assessments: tuple[CandidateAssessment, ...] = (),
    ) -> None:
        if not isinstance(run, DecisionRun):
            raise TypeError("run must be a DecisionRun")
        if not isinstance(assessments, tuple):
            raise TypeError("assessments must be a tuple")
        if any(not isinstance(item, CandidateAssessment) for item in assessments):
            raise TypeError("assessments must contain only CandidateAssessment records")
        ids = [item.candidate_id for item in assessments]
        if len(set(ids)) != len(ids):
            raise DecisionAuditError("candidate assessments must have unique candidate_id values")
        if run.chosen is not None and run.chosen not in ids:
            raise DecisionAuditError("chosen must identify one of the candidate assessments")
        self.run = run
        self.assessments = assessments
        self._events: list[DecisionAuditEvent] = []
        self._by_stage: dict[DecisionStage, DecisionAuditEvent] = {}

    @property
    def events(self) -> tuple[DecisionAuditEvent, ...]:
        return tuple(self._events)

    @property
    def current_stage(self) -> DecisionStage | None:
        return None if not self._events else self._events[-1].stage

    def record(
        self,
        stage: DecisionStage | str,
        *,
        occurred_at: datetime,
        details: Mapping[str, Any] | None = None,
    ) -> DecisionAuditEvent:
        """Record a transition, returning the existing event on an identical retry.

        Idempotency is per decision and stage.  A retry may have a later timestamp, but its
        factual ``details`` must match; conflicting facts are rejected rather than hidden.
        """

        try:
            normalized_stage = stage if isinstance(stage, DecisionStage) else DecisionStage(stage)
        except (TypeError, ValueError) as exc:
            raise DecisionStageError(f"unknown decision stage: {stage!r}") from exc
        normalized_details = _json_value({} if details is None else details)
        if not isinstance(normalized_details, dict):
            raise DecisionAuditError("details must be a mapping")

        existing = self._by_stage.get(normalized_stage)
        if existing is not None:
            if dict(existing.details) != normalized_details:
                raise IdempotencyConflict(
                    f"stage {normalized_stage.value!r} was already recorded with different details"
                )
            return existing

        allowed = _ALLOWED_NEXT[self.current_stage]
        if normalized_stage not in allowed:
            expected = ", ".join(sorted(item.value for item in allowed)) or "no further stage"
            current = "start" if self.current_stage is None else self.current_stage.value
            raise DecisionStageError(
                f"cannot record {normalized_stage.value!r} after {current!r}; expected {expected}"
            )
        self._validate_stage_facts(normalized_stage)
        event = DecisionAuditEvent(
            stage=normalized_stage,
            occurred_at=occurred_at,
            details=normalized_details,
        )
        if self._events and event.occurred_at < self._events[-1].occurred_at:
            raise DecisionStageError("event timestamps must be monotonic")
        self._events.append(event)
        self._by_stage[normalized_stage] = event
        return event

    def _validate_stage_facts(self, stage: DecisionStage) -> None:
        if stage is DecisionStage.CANDIDATE_ELIGIBLE and not self.assessments:
            raise DecisionStageError("candidate_eligible requires at least one assessment")
        if stage is DecisionStage.HAZARD_TRIAL_PERFORMED:
            if self.run.trial_probability is None or self.run.random_draw is None:
                raise DecisionStageError(
                    "hazard_trial_performed requires trial_probability and random_draw"
                )
        if stage is DecisionStage.HAZARD_TRIAL_WON:
            probability = self.run.trial_probability
            draw = self.run.random_draw
            if probability is None or draw is None or not draw < probability:
                raise DecisionStageError("hazard_trial_won requires random_draw < trial_probability")
        if stage is DecisionStage.COMMITTED and self.run.chosen is None:
            raise DecisionStageError("committed requires a chosen candidate")
        if stage in _TERMINAL_SEND_STAGES:
            # The transition table already requires rendered; these explicit checks make the
            # central safety invariant clear even if transitions are extended in the future.
            if DecisionStage.COMMITTED not in self._by_stage:
                raise DecisionStageError(f"{stage.value} requires committed")
            if DecisionStage.RENDERED not in self._by_stage:
                raise DecisionStageError(f"{stage.value} requires rendered")

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a detached JSON-safe snapshot of the complete audit so far."""

        return {
            "audit_contract_version": DECISION_V2_AUDIT_CONTRACT_VERSION,
            "run": self.run.to_dict(),
            "assessments": [item.to_dict() for item in self.assessments],
            "events": [event.to_dict() for event in self._events],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DecisionAuditRecorder":
        """Restore and validate a persisted audit snapshot through the state machine.

        Recovery deliberately replays every event rather than assigning private fields.  A
        corrupt, newer, or internally inconsistent snapshot is therefore rejected before it
        can authorise a late acknowledgement.
        """

        if not isinstance(payload, Mapping):
            raise DecisionAuditError("audit snapshot must be a mapping")
        if payload.get("audit_contract_version") != DECISION_V2_AUDIT_CONTRACT_VERSION:
            raise DecisionAuditError("unsupported decision audit contract version")
        run_payload = payload.get("run")
        if not isinstance(run_payload, Mapping):
            raise DecisionAuditError("audit run must be a mapping")
        run = DecisionRun(
            decision_id=str(run_payload.get("decision_id", "")),
            scope=str(run_payload.get("scope", "")),
            policy_version=str(run_payload.get("policy_version", "")),
            contract_version=str(run_payload.get("contract_version", "")),
            feature_version=str(run_payload.get("feature_version", "")),
            parameter_version=str(run_payload.get("parameter_version", "")),
            D=run_payload.get("D"),
            lambda_rate=run_payload.get("lambda"),
            delta_allowed_seconds=run_payload.get("delta_allowed_seconds"),
            cumulative_lambda=run_payload.get("cumulative_lambda"),
            trial_probability=run_payload.get("trial_probability"),
            random_draw=run_payload.get("random_draw"),
            chosen=run_payload.get("chosen"),
        )
        assessments_payload = payload.get("assessments")
        if not isinstance(assessments_payload, list):
            raise DecisionAuditError("audit assessments must be a list")
        assessments: list[CandidateAssessment] = []
        for item in assessments_payload:
            if not isinstance(item, Mapping):
                raise DecisionAuditError("audit assessment must be a mapping")
            assessments.append(
                CandidateAssessment(
                    candidate_id=str(item.get("candidate_id", "")),
                    prediction_snapshot_id=str(item.get("prediction_snapshot_id", "")),
                    used_bounds=item.get("used_bounds", {}),
                    utility_terms=item.get("utility_terms", {}),
                    repeat_key=str(item.get("repeat_key", "")),
                    reasons=tuple(item.get("reasons", ())),
                )
            )
        recorder = cls(run, tuple(assessments))
        events = payload.get("events")
        if not isinstance(events, list):
            raise DecisionAuditError("audit events must be a list")
        for event in events:
            if not isinstance(event, Mapping):
                raise DecisionAuditError("audit event must be a mapping")
            occurred_at = event.get("occurred_at")
            if isinstance(occurred_at, str):
                occurred_at = datetime.fromisoformat(occurred_at)
            recorder.record(
                str(event.get("stage", "")),
                occurred_at=occurred_at,
                details=event.get("details", {}),
            )
        return recorder
