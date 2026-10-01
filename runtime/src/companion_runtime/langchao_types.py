"""「浪潮」决策引擎的隔离、不可变且 JSON-safe 的契约。

本模块只定义 M1 数据传输对象，不接入 Runtime、数据库、调度器或决策流程。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, TypeAlias

LANGCHAO_CONTRACT_VERSION = "1"
LANGCHAO_GOAL_CONTRACT_VERSION = "langchao.goal.v1"
LANGCHAO_REWARD_CONTRACT_VERSION = "langchao.reward.v1"
LANGCHAO_CANDIDATE_CONTRACT_VERSION = "langchao.candidate.v1"
LANGCHAO_OUTCOME_TOKEN_VERSION = "langchao.outcome-token.v1"
LANGCHAO_STATE_VERSION = "langchao.state.v1"

JsonScalar: TypeAlias = None | str | bool | int | float
FrozenEnvelope: TypeAlias = tuple[tuple[str, JsonScalar], ...]
FrozenScores: TypeAlias = tuple[tuple[str, float], ...]
DirectionWeights: TypeAlias = tuple[tuple["MotivationDirection", float], ...]


class GoalKind(str, Enum):
    """「浪潮」目标形态。"""

    CONTINUOUS_NEED = "continuous_need"
    FINITE = "finite"
    OPEN_ACTIVITY = "open_activity"


class GoalOwnership(str, Enum):
    """目标的事实来源与归属。"""

    USER_REQUEST = "user_request"
    SELF_COMMITMENT = "self_commitment"
    SHARED_ARRANGEMENT = "shared_arrangement"
    SELF_WISH = "self_wish"
    SELF_INTEREST = "self_interest"


class GoalStatus(str, Enum):
    """目标生命周期状态。"""

    ADOPTED = "adopted"
    ACTIONABLE = "actionable"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    DROPPED = "dropped"
    INVALIDATED = "invalidated"


class MotivationDirection(str, Enum):
    """结果价值的八个动机方向。"""

    APPROACH = "approach"
    EXPRESSION = "expression"
    EXPLORATION = "exploration"
    CARE = "care"
    COMMITMENT = "commitment"
    REPAIR = "repair"
    AUTONOMY = "autonomy"
    REST = "rest"


class OutcomeStatus(str, Enum):
    """结果令牌的观察与结算状态。"""

    UNEXECUTED = "unexecuted"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    NOT_OBSERVED = "not_observed"
    CENSORED = "censored"
    UNATTRIBUTABLE = "unattributable"
    CORRECTED = "corrected"


class SettlementType(str, Enum):
    """结果令牌属于预期、实际或更正账。"""

    EXPECTED = "expected"
    ACTUAL = "actual"
    CORRECTION = "correction"


class CandidateKind(str, Enum):
    """候选行为的执行类别。"""

    EXTERNAL_MESSAGE = "external_message"
    INTERNAL_PROCESS = "internal_process"
    DEFER_OR_REST = "defer_or_rest"


class CandidateState(str, Enum):
    """候选从提议到退出的生命周期。"""

    PROPOSED = "proposed"
    VALIDATED = "validated"
    COMPETITIVE = "competitive"
    DORMANT = "dormant"
    RESERVED = "reserved"
    RETIRED = "retired"


class RetirementReason(str, Enum):
    """候选退出竞争的原因。"""

    COMPLETED = "completed"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"
    DROPPED = "dropped"
    WINDOW_CLOSED = "window_closed"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_optional_text(name: str, value: str | None) -> None:
    if value is not None:
        _require_text(name, value)


def _require_version(name: str, value: str, expected: str) -> None:
    if value != expected:
        raise ValueError(f"{name} must be {expected!r}")


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def _require_time_order(created_at: datetime, updated_at: datetime) -> None:
    _require_utc("created_at", created_at)
    _require_utc("updated_at", updated_at)
    if updated_at < created_at:
        raise ValueError("updated_at must not precede created_at")


def _require_int(name: str, value: int, *, minimum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")


def _require_number(name: str, value: float | int) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")


def _require_strings(name: str, values: tuple[str, ...], *, nonempty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if nonempty and not values:
        raise ValueError(f"{name} must not be empty")
    for value in values:
        _require_text(f"{name} item", value)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


def _require_enum_tuple(name: str, values: tuple[Any, ...], enum_type: type[Enum], *, nonempty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if nonempty and not values:
        raise ValueError(f"{name} must not be empty")
    if any(not isinstance(value, enum_type) for value in values):
        raise TypeError(f"{name} must contain only {enum_type.__name__} values")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


def _require_scalar(value: JsonScalar) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("envelope float values must be finite")
        return
    raise TypeError("envelope values must be JSON scalars")


def _require_envelope(envelope: FrozenEnvelope) -> None:
    if not isinstance(envelope, tuple):
        raise TypeError("envelope must be a tuple of key/value pairs")
    keys: list[str] = []
    for item in envelope:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("each envelope entry must be a (key, JSON scalar) tuple")
        key, value = item
        _require_text("envelope key", key)
        _require_scalar(value)
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("envelope keys must not contain duplicates")


def _require_direction_weights(weights: DirectionWeights) -> None:
    if not isinstance(weights, tuple):
        raise TypeError("direction_weights must be a tuple of pairs")
    directions: list[MotivationDirection] = []
    for item in weights:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("direction_weights entries must be pairs")
        direction, weight = item
        if not isinstance(direction, MotivationDirection):
            raise TypeError("direction_weights keys must be MotivationDirection values")
        _require_number("direction weight", weight)
        directions.append(direction)
    if len(set(directions)) != len(directions):
        raise ValueError("direction_weights must not contain duplicate keys")


def _require_scores(name: str, scores: FrozenScores, expected_keys: tuple[str, ...]) -> None:
    if not isinstance(scores, tuple):
        raise TypeError(f"{name} must be a tuple of pairs")
    keys: list[str] = []
    for item in scores:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"{name} entries must be pairs")
        key, value = item
        _require_text(f"{name} key", key)
        _require_number(f"{name} value", value)
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{name} must not contain duplicate keys")
    if set(keys) != set(expected_keys):
        raise ValueError(f"{name} keys must exactly match working_set")


@dataclass(frozen=True, slots=True, kw_only=True)
class GoalContract:
    """一个有来源、边界、停止条件和稳定身份的「浪潮」目标。"""

    goal_id: str
    scope_key: str
    episode_id: str
    semantic_key: str
    kind: GoalKind
    ownership: GoalOwnership
    desired_change: str
    status: GoalStatus
    evidence_refs: tuple[str, ...]
    excluded_outcomes: tuple[str, ...]
    completion_outcome_keys: tuple[str, ...]
    allowed_candidate_kinds: tuple[CandidateKind, ...]
    created_at: datetime
    updated_at: datetime
    wait_for_refs: tuple[str, ...] = ()
    resume_condition_refs: tuple[str, ...] = ()
    matter_id: str | None = None
    parent_goal_id: str | None = None
    reward_contract_id: str | None = None
    revision: int = 1
    contract_version: str = LANGCHAO_GOAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("goal_id", "scope_key", "episode_id", "semantic_key", "desired_change"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.kind, GoalKind):
            raise TypeError("kind must be a GoalKind")
        if not isinstance(self.ownership, GoalOwnership):
            raise TypeError("ownership must be a GoalOwnership")
        if not isinstance(self.status, GoalStatus):
            raise TypeError("status must be a GoalStatus")
        _require_strings("evidence_refs", self.evidence_refs)
        _require_strings("excluded_outcomes", self.excluded_outcomes)
        _require_strings("completion_outcome_keys", self.completion_outcome_keys, nonempty=True)
        _require_enum_tuple("allowed_candidate_kinds", self.allowed_candidate_kinds, CandidateKind, nonempty=True)
        _require_strings("wait_for_refs", self.wait_for_refs)
        _require_strings("resume_condition_refs", self.resume_condition_refs)
        for name in ("matter_id", "parent_goal_id", "reward_contract_id"):
            _require_optional_text(name, getattr(self, name))
        if self.parent_goal_id == self.goal_id:
            raise ValueError("parent_goal_id must not equal goal_id")
        if self.status is GoalStatus.WAITING and not self.wait_for_refs:
            raise ValueError("waiting goals require wait_for_refs")
        if self.status is not GoalStatus.WAITING and self.wait_for_refs:
            raise ValueError("only waiting goals may carry wait_for_refs")
        if self.status is GoalStatus.PAUSED and not self.resume_condition_refs:
            raise ValueError("paused goals require resume_condition_refs")
        if self.status is not GoalStatus.PAUSED and self.resume_condition_refs:
            raise ValueError("only paused goals may carry resume_condition_refs")
        _require_time_order(self.created_at, self.updated_at)
        _require_int("revision", self.revision, minimum=1)
        _require_version("contract_version", self.contract_version, LANGCHAO_GOAL_CONTRACT_VERSION)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id, "scope_key": self.scope_key, "episode_id": self.episode_id,
            "semantic_key": self.semantic_key, "kind": self.kind.value, "ownership": self.ownership.value,
            "desired_change": self.desired_change, "status": self.status.value,
            "evidence_refs": list(self.evidence_refs), "excluded_outcomes": list(self.excluded_outcomes),
            "completion_outcome_keys": list(self.completion_outcome_keys),
            "allowed_candidate_kinds": [item.value for item in self.allowed_candidate_kinds],
            "wait_for_refs": list(self.wait_for_refs), "resume_condition_refs": list(self.resume_condition_refs),
            "matter_id": self.matter_id, "parent_goal_id": self.parent_goal_id,
            "reward_contract_id": self.reward_contract_id, "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(), "revision": self.revision,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OutcomeToken:
    """可幂等核对的单项预期、实际结果或更正。"""

    token_id: str
    scope_key: str
    goal_id: str
    episode_id: str
    outcome_key: str
    settlement_type: SettlementType
    status: OutcomeStatus
    base_amount: float
    direction_weights: DirectionWeights
    evidence_version: str
    idempotency_key: str
    evidence_refs: tuple[str, ...] = ()
    milestone_id: str | None = None
    observation_started_at: datetime | None = None
    observation_ends_at: datetime | None = None
    corrects_token_id: str | None = None
    token_version: str = LANGCHAO_OUTCOME_TOKEN_VERSION

    def __post_init__(self) -> None:
        for name in ("token_id", "scope_key", "goal_id", "episode_id", "outcome_key", "evidence_version", "idempotency_key"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.settlement_type, SettlementType):
            raise TypeError("settlement_type must be a SettlementType")
        if not isinstance(self.status, OutcomeStatus):
            raise TypeError("status must be an OutcomeStatus")
        _require_number("base_amount", self.base_amount)
        _require_direction_weights(self.direction_weights)
        _require_strings("evidence_refs", self.evidence_refs)
        _require_optional_text("milestone_id", self.milestone_id)
        _require_optional_text("corrects_token_id", self.corrects_token_id)
        if (self.observation_started_at is None) != (self.observation_ends_at is None):
            raise ValueError("observation window timestamps must be both present or both absent")
        if self.observation_started_at is not None and self.observation_ends_at is not None:
            _require_utc("observation_started_at", self.observation_started_at)
            _require_utc("observation_ends_at", self.observation_ends_at)
            if self.observation_ends_at <= self.observation_started_at:
                raise ValueError("observation_ends_at must be after observation_started_at")
        if self.status is OutcomeStatus.PENDING and self.observation_ends_at is None:
            raise ValueError("pending outcomes require an observation window")
        if self.settlement_type is SettlementType.CORRECTION:
            if self.status is not OutcomeStatus.CORRECTED or self.corrects_token_id is None:
                raise ValueError("correction settlements require corrected status and corrects_token_id")
            if self.corrects_token_id == self.token_id:
                raise ValueError("corrects_token_id must not equal token_id")
        elif self.status is OutcomeStatus.CORRECTED or self.corrects_token_id is not None:
            raise ValueError("corrected status and corrects_token_id require correction settlement")
        if self.settlement_type is SettlementType.EXPECTED and self.status not in {OutcomeStatus.UNEXECUTED, OutcomeStatus.PENDING}:
            raise ValueError("expected settlements must be unexecuted or pending")
        if self.settlement_type is SettlementType.ACTUAL and self.status in {OutcomeStatus.UNEXECUTED, OutcomeStatus.CORRECTED}:
            raise ValueError("actual settlements cannot be unexecuted or corrected")
        _require_version("token_version", self.token_version, LANGCHAO_OUTCOME_TOKEN_VERSION)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id, "scope_key": self.scope_key, "goal_id": self.goal_id,
            "episode_id": self.episode_id, "outcome_key": self.outcome_key,
            "settlement_type": self.settlement_type.value, "status": self.status.value,
            "base_amount": self.base_amount,
            "direction_weights": {key.value: value for key, value in self.direction_weights},
            "evidence_version": self.evidence_version, "idempotency_key": self.idempotency_key,
            "evidence_refs": list(self.evidence_refs), "milestone_id": self.milestone_id,
            "observation_started_at": self.observation_started_at.isoformat() if self.observation_started_at else None,
            "observation_ends_at": self.observation_ends_at.isoformat() if self.observation_ends_at else None,
            "corrects_token_id": self.corrects_token_id, "token_version": self.token_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RewardContract:
    """目标共享的结果计价、重叠控制与总额上限契约。"""

    reward_contract_id: str
    scope_key: str
    goal_id: str
    episode_id: str
    template_key: str
    unit: str
    outcome_tokens: tuple[OutcomeToken, ...]
    total_cap: float
    overlap_group: str
    created_at: datetime
    updated_at: datetime
    revision: int = 1
    contract_version: str = LANGCHAO_REWARD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("reward_contract_id", "scope_key", "goal_id", "episode_id", "template_key", "unit", "overlap_group"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.outcome_tokens, tuple):
            raise TypeError("outcome_tokens must be a tuple")
        if not self.outcome_tokens:
            raise ValueError("outcome_tokens must not be empty")
        if any(not isinstance(item, OutcomeToken) for item in self.outcome_tokens):
            raise TypeError("outcome_tokens must contain only OutcomeToken values")
        token_ids = tuple(item.token_id for item in self.outcome_tokens)
        idempotency_keys = tuple(item.idempotency_key for item in self.outcome_tokens)
        if len(set(token_ids)) != len(token_ids):
            raise ValueError("outcome_tokens must not contain duplicate token_id values")
        if len(set(idempotency_keys)) != len(idempotency_keys):
            raise ValueError("outcome_tokens must not contain duplicate idempotency_key values")
        for item in self.outcome_tokens:
            if (item.scope_key, item.goal_id, item.episode_id) != (self.scope_key, self.goal_id, self.episode_id):
                raise ValueError("outcome tokens must match reward scope, goal, and episode")
        _require_number("total_cap", self.total_cap)
        if self.total_cap < 0:
            raise ValueError("total_cap must be non-negative")
        _require_time_order(self.created_at, self.updated_at)
        _require_int("revision", self.revision, minimum=1)
        _require_version("contract_version", self.contract_version, LANGCHAO_REWARD_CONTRACT_VERSION)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reward_contract_id": self.reward_contract_id, "scope_key": self.scope_key,
            "goal_id": self.goal_id, "episode_id": self.episode_id, "template_key": self.template_key,
            "unit": self.unit, "outcome_tokens": [item.to_dict() for item in self.outcome_tokens],
            "total_cap": self.total_cap, "overlap_group": self.overlap_group,
            "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat(),
            "revision": self.revision, "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionCandidateContract:
    """「浪潮」可竞争但尚未获得执行权的具体行动候选。"""

    candidate_id: str
    scope_key: str
    semantic_key: str
    goal_refs: tuple[str, ...]
    kind: CandidateKind
    action_template: str
    input_refs: tuple[str, ...]
    reward_contract_ref: str
    expected_outcome_token_ids: tuple[str, ...]
    capability_refs: tuple[str, ...]
    permission_ref: str
    precondition_refs: tuple[str, ...]
    invalidation_refs: tuple[str, ...]
    envelope: FrozenEnvelope
    state: CandidateState
    available_from: datetime
    expires_at: datetime | None
    resource_budget: float
    based_on_state_version: int
    created_at: datetime
    updated_at: datetime
    attempt_budget: int = 1
    retirement_reason: RetirementReason | None = None
    semantic_revision: int = 1
    contract_version: str = LANGCHAO_CANDIDATE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("candidate_id", "scope_key", "semantic_key", "action_template", "reward_contract_ref", "permission_ref"):
            _require_text(name, getattr(self, name))
        for name in ("goal_refs", "input_refs", "expected_outcome_token_ids", "capability_refs", "precondition_refs", "invalidation_refs"):
            _require_strings(name, getattr(self, name), nonempty=name in {"goal_refs", "expected_outcome_token_ids"})
        if not isinstance(self.kind, CandidateKind):
            raise TypeError("kind must be a CandidateKind")
        if not isinstance(self.state, CandidateState):
            raise TypeError("state must be a CandidateState")
        if self.retirement_reason is not None and not isinstance(self.retirement_reason, RetirementReason):
            raise TypeError("retirement_reason must be a RetirementReason or None")
        _require_envelope(self.envelope)
        _require_utc("available_from", self.available_from)
        if self.expires_at is not None:
            _require_utc("expires_at", self.expires_at)
            if self.expires_at <= self.available_from:
                raise ValueError("expires_at must be after available_from")
        _require_number("resource_budget", self.resource_budget)
        if self.resource_budget < 0:
            raise ValueError("resource_budget must be non-negative")
        _require_int("attempt_budget", self.attempt_budget, minimum=1)
        _require_int("based_on_state_version", self.based_on_state_version, minimum=0)
        _require_int("semantic_revision", self.semantic_revision, minimum=1)
        if self.state is CandidateState.RETIRED and self.retirement_reason is None:
            raise ValueError("retired candidates require retirement_reason")
        if self.state is not CandidateState.RETIRED and self.retirement_reason is not None:
            raise ValueError("only retired candidates may carry retirement_reason")
        _require_time_order(self.created_at, self.updated_at)
        _require_version("contract_version", self.contract_version, LANGCHAO_CANDIDATE_CONTRACT_VERSION)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id, "scope_key": self.scope_key,
            "semantic_key": self.semantic_key, "goal_refs": list(self.goal_refs), "kind": self.kind.value,
            "action_template": self.action_template, "input_refs": list(self.input_refs),
            "reward_contract_ref": self.reward_contract_ref,
            "expected_outcome_token_ids": list(self.expected_outcome_token_ids),
            "capability_refs": list(self.capability_refs), "permission_ref": self.permission_ref,
            "precondition_refs": list(self.precondition_refs), "invalidation_refs": list(self.invalidation_refs),
            "envelope": dict(self.envelope), "state": self.state.value,
            "available_from": self.available_from.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "resource_budget": self.resource_budget, "attempt_budget": self.attempt_budget,
            "retirement_reason": self.retirement_reason.value if self.retirement_reason else None,
            "based_on_state_version": self.based_on_state_version, "semantic_revision": self.semantic_revision,
            "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat(),
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class LangchaoState:
    """一个可回放的「浪潮」竞争场状态快照。"""

    scope_key: str
    decision_round_id: str
    working_set: tuple[str, ...]
    readiness: FrozenScores
    attraction: FrozenScores
    attention: DirectionWeights
    advanced_at: datetime
    based_on_state_version: int
    event_cursor: str
    goal_snapshot_version: str
    reward_snapshot_version: str
    candidate_snapshot_version: str
    prediction_snapshot_version: str
    value_profile_version: str
    attention_version: str
    parameter_version: str
    permission_version: str
    revision: int = 1
    state_version: str = LANGCHAO_STATE_VERSION

    def __post_init__(self) -> None:
        for name in ("scope_key", "decision_round_id", "event_cursor", "goal_snapshot_version", "reward_snapshot_version", "candidate_snapshot_version", "prediction_snapshot_version", "value_profile_version", "attention_version", "parameter_version", "permission_version"):
            _require_text(name, getattr(self, name))
        _require_strings("working_set", self.working_set)
        _require_scores("readiness", self.readiness, self.working_set)
        _require_scores("attraction", self.attraction, self.working_set)
        for _, value in self.readiness:
            if not 0.0 <= value <= 1.0:
                raise ValueError("readiness values must be in [0, 1]")
        _require_direction_weights(self.attention)
        attention_keys = tuple(direction for direction, _ in self.attention)
        if len(self.attention) != len(MotivationDirection) or set(attention_keys) != set(MotivationDirection):
            raise ValueError("attention must contain exactly one entry per MotivationDirection")
        for _, value in self.attention:
            if value < 0:
                raise ValueError("attention values must be non-negative")
        _require_utc("advanced_at", self.advanced_at)
        _require_int("based_on_state_version", self.based_on_state_version, minimum=0)
        _require_int("revision", self.revision, minimum=1)
        _require_version("state_version", self.state_version, LANGCHAO_STATE_VERSION)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key, "decision_round_id": self.decision_round_id,
            "working_set": list(self.working_set), "readiness": dict(self.readiness),
            "attraction": dict(self.attraction),
            "attention": {key.value: value for key, value in self.attention},
            "advanced_at": self.advanced_at.isoformat(),
            "based_on_state_version": self.based_on_state_version, "event_cursor": self.event_cursor,
            "goal_snapshot_version": self.goal_snapshot_version,
            "reward_snapshot_version": self.reward_snapshot_version,
            "candidate_snapshot_version": self.candidate_snapshot_version,
            "prediction_snapshot_version": self.prediction_snapshot_version,
            "value_profile_version": self.value_profile_version, "attention_version": self.attention_version,
            "parameter_version": self.parameter_version, "permission_version": self.permission_version,
            "revision": self.revision, "state_version": self.state_version,
        }


__all__ = [
    "ActionCandidateContract", "CandidateKind", "CandidateState", "DirectionWeights",
    "FrozenEnvelope", "FrozenScores", "GoalContract", "GoalKind", "GoalOwnership", "GoalStatus",
    "JsonScalar", "LANGCHAO_CANDIDATE_CONTRACT_VERSION", "LANGCHAO_CONTRACT_VERSION",
    "LANGCHAO_GOAL_CONTRACT_VERSION", "LANGCHAO_OUTCOME_TOKEN_VERSION",
    "LANGCHAO_REWARD_CONTRACT_VERSION", "LANGCHAO_STATE_VERSION", "LangchaoState",
    "MotivationDirection", "OutcomeStatus", "OutcomeToken", "RetirementReason",
    "RewardContract", "SettlementType",
]
