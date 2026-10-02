"""Pure adapter from explicit runtime-v2 facts to isolated 「浪潮」 contracts.

This module deliberately has no database, clock, random, LLM, scheduler, CLI, or
sending dependency.  Every timestamp and every fact which can change ownership or
admission is supplied by the caller.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .decision_v2_audit import CandidateAssessment
from .langchao_permission import PermissionProjection
from .langchao_exploration import (
    EXPLORATION_PROCESS_CAP,
    EXPLORATION_PROCESS_VALUE,
    ExplorationWorkSegment,
)
from .langchao_reward import AttentionProfile, CandidateCostTerm, OutcomeForecast, ValueProfile
from .langchao_shadow import ShadowCandidateInput
from .langchao_types import (
    ActionCandidateContract,
    CandidateKind,
    CandidateState,
    GoalContract,
    GoalKind,
    GoalOwnership,
    GoalStatus,
    LangchaoState,
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    RewardContract,
    SettlementType,
)
from .runtime_v2 import CandidateV2, PredictionSetV2
from .user_model_v2_types import SupportStatus, TargetPredictionV2

LANGCHAO_RUNTIME_ADAPTER_VERSION = "langchao.runtime-adapter.v1"
RUNTIME_FACT_SNAPSHOT_VERSION = "runtime.fact-snapshot.v1"
BUILT_SHADOW_ROUND_VERSION = "langchao.built-shadow-round.v1"
VALUE_PROFILE_VERSION = "langchao.runtime-values.v1"
ATTENTION_PROFILE_VERSION = "langchao.attention.all-one.v1"
TEMPLATE_POLICY_VERSION = "langchao.template-policy.v1"
TEMPLATE_REWARD_POLICY_VERSION = "langchao.template-reward.v1"
TEMPLATE_V1_REWARD_AMOUNTS = {
    "reply": 1.0,
    "continuation": 0.5,
    "negative": -1.5,
    # Delivery is an execution witness, not a forecasted user reward.  A zero
    # default keeps it out of expected utility while preserving a token which the
    # terminal send hook can settle explicitly.
    "delivery": 0.0,
    "local": 0.25,
    "exploration_process": EXPLORATION_PROCESS_VALUE,
}
FIXED_VALUE_TOTAL = 8.0
_ID_NAMESPACE = uuid.UUID("da6d1426-18e6-58e1-a7bc-cfa0bb59a54f")
_TEMPLATE_KEYS = frozenset({
    "contact.v1", "expression.v1", "followup.v1", "internal_rest.v1", "exploration.v1",
})


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")


def _stable_id(kind: str, *identity: str) -> str:
    # Identity inputs are semantic facts only.  Time, decision ids and prediction ids
    # are intentionally absent from every call site.
    return str(uuid.uuid5(_ID_NAMESPACE, "\x1f".join((kind, *identity))))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_unordered(rows: tuple[Mapping[str, Any], ...]) -> str:
    encoded = sorted(_canonical(dict(row)) for row in rows)
    return hashlib.sha256(_canonical(encoded).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeCandidateFacts:
    """Explicit semantic/admission facts for one runtime-v2 candidate."""

    candidate_id: str
    template_key: str
    ownership: GoalOwnership
    evidence_refs: tuple[str, ...]
    subject_ref: str | None = None
    memory_ref: str | None = None
    social_ref: str | None = None
    unfinished_id: str | None = None
    self_regulation_ref: str | None = None
    permission_ref: str = "runtime.permission.v2"
    capability_refs: tuple[str, ...] = ()
    precondition_refs: tuple[str, ...] = ()
    invalidation_refs: tuple[str, ...] = ()
    repeat_soft_cost: float = 0.0
    repeat_cost_refs: tuple[str, ...] = ()
    blocked: bool = False
    hard_repeat: bool = False
    block_reasons: tuple[str, ...] = ()
    expression_delivered_policy: bool = False
    rest_realized_policy: bool = False
    exploration_segment: ExplorationWorkSegment | None = None
    initial_goal_revision: int = 1
    reward_revision: int = 1
    bound_goal_revision: int = 2
    candidate_revision: int = 1
    legacy_internal_need: float | None = None
    legacy_internal_utility: float | None = None
    legacy_relevance: float | None = None

    def __post_init__(self) -> None:
        _text("candidate_id", self.candidate_id)
        if self.template_key not in _TEMPLATE_KEYS:
            raise ValueError(f"template_key must be one of {sorted(_TEMPLATE_KEYS)!r}")
        if not isinstance(self.ownership, GoalOwnership):
            raise TypeError("ownership must be explicit GoalOwnership")
        if isinstance(self.repeat_soft_cost, bool) or not isinstance(self.repeat_soft_cost, (int, float)):
            raise TypeError("repeat_soft_cost must be numeric")
        if not math.isfinite(float(self.repeat_soft_cost)) or self.repeat_soft_cost < 0:
            raise ValueError("repeat_soft_cost must be finite and non-negative")
        for name in ("initial_goal_revision", "reward_revision", "bound_goal_revision", "candidate_revision"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer supplied by the revision resolver")
        if self.bound_goal_revision <= self.initial_goal_revision:
            raise ValueError("bound_goal_revision must be greater than initial_goal_revision")
        if self.template_key == "contact.v1" and self.subject_ref != "relationship-continuity":
            raise ValueError("contact.v1 subject_ref must be 'relationship-continuity'")
        if self.template_key == "expression.v1" and not (self.memory_ref or self.social_ref):
            raise ValueError("expression.v1 requires memory_ref or social_ref")
        if self.template_key == "followup.v1" and not self.unfinished_id:
            raise ValueError("followup.v1 requires unfinished_id")
        if self.template_key == "internal_rest.v1" and not self.self_regulation_ref:
            raise ValueError("internal_rest.v1 requires self_regulation_ref")
        if self.template_key == "exploration.v1" and self.exploration_segment is None:
            raise ValueError("exploration.v1 requires exploration_segment")
        if self.template_key != "exploration.v1" and self.exploration_segment is not None:
            raise ValueError("exploration_segment is only valid for exploration.v1")

    @property
    def semantic_subject(self) -> str:
        if self.template_key == "contact.v1":
            return "relationship-continuity"
        if self.template_key == "expression.v1":
            return self.memory_ref or self.social_ref or ""  # validated above
        if self.template_key == "followup.v1":
            return self.unfinished_id or ""
        if self.template_key == "internal_rest.v1":
            return self.self_regulation_ref or ""
        return self.exploration_segment.problem_ref if self.exploration_segment is not None else ""


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeFactSnapshot:
    scope_key: str
    episode_id: str
    source_cursor: str
    values: Mapping[str, float]
    candidates: tuple[RuntimeCandidateFacts, ...]
    permission: PermissionProjection | None = None
    snapshot_version: str = RUNTIME_FACT_SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        for name in ("scope_key", "episode_id", "source_cursor"):
            _text(name, getattr(self, name))
        if self.snapshot_version != RUNTIME_FACT_SNAPSHOT_VERSION:
            raise ValueError(f"snapshot_version must be {RUNTIME_FACT_SNAPSHOT_VERSION!r}")
        if self.permission is not None:
            if not isinstance(self.permission, PermissionProjection):
                raise TypeError("permission must be PermissionProjection or None")
            if self.permission.scope_key != self.scope_key:
                raise ValueError("permission projection scope must match snapshot scope")
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("candidate facts must have unique candidate_id values")


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeCandidateInput:
    candidate: CandidateV2
    predictions: PredictionSetV2 | None
    assessment: CandidateAssessment

    def __post_init__(self) -> None:
        if self.candidate.candidate_id != self.assessment.candidate_id:
            raise ValueError("assessment must describe candidate")
        if self.predictions is not None and self.predictions.snapshot_id != self.assessment.prediction_snapshot_id:
            raise ValueError("assessment must describe prediction snapshot")


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyProvenance:
    candidate_id: str
    internal_utility: float
    internal_need: float | None
    relevance: float | None
    coefficients: tuple[tuple[str, float], ...]
    assessment_utility_terms: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltCandidateContracts:
    """Construction sequence is explicit: goal1 -> reward -> goal2 -> candidate."""

    source_candidate_id: str
    initial_goal: GoalContract
    reward: RewardContract
    goal: GoalContract
    candidate: ActionCandidateContract
    shadow_input: ShadowCandidateInput
    provenance: LegacyProvenance


@dataclass(frozen=True, slots=True, kw_only=True)
class DroppedCandidate:
    source_candidate_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateIdMapping:
    source_candidate_id: str
    langchao_candidate_id: str
    goal_id: str
    reward_contract_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltShadowRound:
    inputs: tuple[ShadowCandidateInput, ...]
    contracts: tuple[BuiltCandidateContracts, ...]
    state: LangchaoState
    value_profile: ValueProfile
    attention_profile: AttentionProfile
    dropped: tuple[DroppedCandidate, ...]
    id_mapping: tuple[CandidateIdMapping, ...]
    goal_snapshot_version: str
    reward_snapshot_version: str
    candidate_snapshot_version: str
    prediction_snapshot_version: str
    adapter_version: str = LANGCHAO_RUNTIME_ADAPTER_VERSION
    built_version: str = BUILT_SHADOW_ROUND_VERSION


def value_profile_from_runtime(values: Mapping[str, float]) -> ValueProfile:
    """Map the eight runtime axes and normalize them to a fixed total of 8.

    A zero-sum input is defined as the neutral all-one profile rather than being
    guessed from defaults.  Negative, missing, extra, Boolean or non-finite axes fail.
    """

    mapping = {
        "autonomy": MotivationDirection.AUTONOMY,
        "boundary_respect": MotivationDirection.REST,
        "emotional_expression": MotivationDirection.EXPRESSION,
        "relationship_maintenance": MotivationDirection.APPROACH,
        "user_care": MotivationDirection.CARE,
        "conflict_directness": MotivationDirection.REPAIR,
        "stability_commitment": MotivationDirection.COMMITMENT,
        "curiosity": MotivationDirection.EXPLORATION,
    }
    if set(values) != set(mapping):
        raise ValueError("values must contain exactly the eight runtime value axes")
    raw = {direction: 0.0 for direction in MotivationDirection}
    # The legacy axes are assigned one-to-one to the eight 「浪潮」 directions;
    # boundary respect prices restraint/rest while conflict directness prices repair.
    for name, direction in mapping.items():
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
            raise ValueError(f"values[{name!r}] must be finite and non-negative")
        raw[direction] += float(value)
    total = math.fsum(raw.values())
    normalized = ({direction: 1.0 for direction in MotivationDirection} if total == 0 else
                  {direction: raw[direction] * FIXED_VALUE_TOTAL / total for direction in MotivationDirection})
    return ValueProfile(version=VALUE_PROFILE_VERSION,
                        direction_weights=tuple((direction, normalized[direction]) for direction in MotivationDirection),
                        total_weight=FIXED_VALUE_TOTAL)


def all_one_attention() -> AttentionProfile:
    return AttentionProfile(version=ATTENTION_PROFILE_VERSION,
                            direction_weights=tuple((direction, 1.0) for direction in MotivationDirection))


def _usable(prediction: TargetPredictionV2, bound: str) -> float | None:
    if prediction.support is SupportStatus.UNAVAILABLE or prediction.point is None:
        return None
    value = getattr(prediction, bound)
    return None if value is None else float(value)


def _identity_rows(items: tuple[Any, ...], method: str = "to_dict") -> tuple[Mapping[str, Any], ...]:
    return tuple(getattr(item, method)() for item in items)


def _template_spec(facts: RuntimeCandidateFacts) -> tuple[CandidateKind, GoalKind, MotivationDirection, str]:
    if facts.template_key == "contact.v1":
        return CandidateKind.EXTERNAL_MESSAGE, GoalKind.CONTINUOUS_NEED, MotivationDirection.APPROACH, "relationship_continuity"
    if facts.template_key == "expression.v1":
        return CandidateKind.EXTERNAL_MESSAGE, GoalKind.FINITE, MotivationDirection.EXPRESSION, "expression_delivered"
    if facts.template_key == "followup.v1":
        return CandidateKind.EXTERNAL_MESSAGE, GoalKind.FINITE, MotivationDirection.CARE, "unfinished_progress"
    if facts.template_key == "internal_rest.v1":
        return CandidateKind.DEFER_OR_REST, GoalKind.CONTINUOUS_NEED, MotivationDirection.REST, "rest_realized"
    return CandidateKind.INTERNAL_PROCESS, GoalKind.OPEN_ACTIVITY, MotivationDirection.EXPLORATION, "work_segment_completed"


def _build_one(snapshot: RuntimeFactSnapshot, facts: RuntimeCandidateFacts, source: RuntimeCandidateInput,
               advanced_at: datetime, based_on_state_version: int) -> BuiltCandidateContracts:
    kind, goal_kind, direction, local_outcome = _template_spec(facts)
    subject = facts.semantic_subject
    episode_subject = "fixed" if facts.template_key in {"contact.v1", "internal_rest.v1"} else subject
    episode_id = _stable_id("episode", snapshot.scope_key, facts.template_key, episode_subject)
    identity = (snapshot.scope_key, episode_id, facts.template_key, subject)
    goal_id = _stable_id("goal", *identity)
    reward_id = _stable_id("reward", *identity)
    candidate_id = _stable_id("candidate", *identity)

    tokens: list[OutcomeToken] = []
    forecasts: list[OutcomeForecast] = []
    if facts.template_key in {"contact.v1", "expression.v1", "followup.v1"}:
        if source.predictions is None:
            raise ValueError(f"{facts.template_key} requires PredictionSetV2")
        predicted = (
            ("reply", source.predictions.reply, "lower", TEMPLATE_V1_REWARD_AMOUNTS["reply"]),
            ("continuation", source.predictions.continuation, "lower", TEMPLATE_V1_REWARD_AMOUNTS["continuation"]),
            ("negative", source.predictions.negative, "upper", TEMPLATE_V1_REWARD_AMOUNTS["negative"]),
        )
        reply_probability = _usable(source.predictions.reply, "lower")
        for outcome, prediction, bound, amount in predicted:
            probability = _usable(prediction, bound)
            if outcome == "continuation":
                probability = None if reply_probability is None or probability is None else reply_probability * probability
            token_id = _stable_id("token", *identity, outcome)
            tokens.append(OutcomeToken(token_id=token_id, scope_key=snapshot.scope_key, goal_id=goal_id,
                episode_id=episode_id, outcome_key=outcome, settlement_type=SettlementType.EXPECTED,
                status=OutcomeStatus.UNEXECUTED, base_amount=float(amount), direction_weights=((direction, 1.0),),
                evidence_version=TEMPLATE_REWARD_POLICY_VERSION,
                idempotency_key=_stable_id("token-idempotency", *identity, outcome),
                # Forecast prediction ids are audit inputs, not stable reward terms.
                evidence_refs=(facts.template_key, outcome)))
            forecasts.append(OutcomeForecast(token_id=token_id, probability=probability,
                support=prediction.support.value, status="available" if probability is not None else "unknown",
                source_version=source.predictions.parameter_version))
        # A deterministic, zero-valued delivery placeholder is forecast explicitly at
        # probability zero: it cannot accrue utility before an actual send ack.
        delivery_id = _stable_id("token", *identity, "delivery")
        tokens.append(OutcomeToken(token_id=delivery_id, scope_key=snapshot.scope_key,
            goal_id=goal_id, episode_id=episode_id, outcome_key="delivery",
            settlement_type=SettlementType.EXPECTED, status=OutcomeStatus.UNEXECUTED,
            base_amount=TEMPLATE_V1_REWARD_AMOUNTS["delivery"],
            direction_weights=((direction, 1.0),), evidence_version=TEMPLATE_REWARD_POLICY_VERSION,
            idempotency_key=_stable_id("token-idempotency", *identity, "delivery"),
            evidence_refs=(facts.template_key, "delivery")))
        forecasts.append(OutcomeForecast(token_id=delivery_id, probability=0.0,
            support="execution_only", status="deterministic_zero",
            source_version=TEMPLATE_REWARD_POLICY_VERSION))

    template_policy: tuple[tuple[str, float], ...] = ()
    local_policy = ((facts.template_key == "expression.v1" and facts.expression_delivered_policy) or
                    (facts.template_key == "internal_rest.v1" and facts.rest_realized_policy) or
                    facts.template_key == "exploration.v1")
    has_local_outcome = local_policy or facts.template_key == "internal_rest.v1"
    if has_local_outcome:
        token_id = _stable_id("token", *identity, local_outcome)
        local_amount = (TEMPLATE_V1_REWARD_AMOUNTS["exploration_process"]
                        if facts.template_key == "exploration.v1"
                        else TEMPLATE_V1_REWARD_AMOUNTS["local"])
        local_evidence = ((facts.template_key,) if facts.exploration_segment is None else
                          tuple(dict.fromkeys((facts.template_key, facts.exploration_segment.result_ref)
                                              + facts.exploration_segment.evidence_refs)))
        tokens.append(OutcomeToken(token_id=token_id, scope_key=snapshot.scope_key, goal_id=goal_id,
            episode_id=episode_id, outcome_key=local_outcome, settlement_type=SettlementType.EXPECTED,
            status=OutcomeStatus.UNEXECUTED, base_amount=local_amount, direction_weights=((direction, 1.0),),
            evidence_version=TEMPLATE_REWARD_POLICY_VERSION,
            idempotency_key=_stable_id("token-idempotency", *identity, local_outcome), evidence_refs=local_evidence))
        forecasts.append(OutcomeForecast(token_id=token_id, probability=None,
            support="template_policy" if local_policy else "unavailable",
            status="unknown", source_version=TEMPLATE_REWARD_POLICY_VERSION))
        if local_policy:
            template_policy = ((token_id, 1.0),)

    completion = (local_outcome,) if has_local_outcome else ("reply", "continuation", "negative")
    # Template contracts are timeless semantic definitions. Round/state time remains
    # on the candidate and state snapshot, allowing goal/reward revisions to be reused.
    contract_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
    initial_goal = GoalContract(goal_id=goal_id, scope_key=snapshot.scope_key, episode_id=episode_id,
        semantic_key=f"{facts.template_key}:{subject}", kind=goal_kind, ownership=facts.ownership,
        desired_change=local_outcome, status=GoalStatus.ACTIONABLE, evidence_refs=facts.evidence_refs,
        excluded_outcomes=("user_intent_inference",), completion_outcome_keys=completion,
        allowed_candidate_kinds=(kind,), matter_id=facts.unfinished_id, created_at=contract_at, updated_at=contract_at,
        revision=facts.initial_goal_revision)
    reward_cap = (EXPLORATION_PROCESS_CAP if facts.template_key == "exploration.v1" else
                  math.fsum(abs(token.base_amount) for token in tokens))
    reward = RewardContract(reward_contract_id=reward_id, scope_key=snapshot.scope_key, goal_id=goal_id,
        episode_id=episode_id, template_key=facts.template_key, unit="utility",
        outcome_tokens=tuple(tokens), total_cap=reward_cap,
        overlap_group=f"{facts.template_key}:{subject}", created_at=contract_at, updated_at=contract_at,
        revision=facts.reward_revision)
    # Goal1 -> reward -> goal2 uses distinct revisions allocated upstream.
    goal = replace(initial_goal, reward_contract_id=reward_id, revision=facts.bound_goal_revision)
    contract = ActionCandidateContract(candidate_id=candidate_id, scope_key=snapshot.scope_key,
        semantic_key=f"{facts.template_key}:{subject}", goal_refs=(goal_id,), kind=kind,
        action_template=facts.template_key, input_refs=tuple(dict.fromkeys(facts.evidence_refs + source.candidate.source_event_ids)),
        reward_contract_ref=reward_id, expected_outcome_token_ids=tuple(token.token_id for token in tokens),
        capability_refs=facts.capability_refs, permission_ref=facts.permission_ref,
        precondition_refs=facts.precondition_refs, invalidation_refs=facts.invalidation_refs,
        # Legacy runtime candidate ids are provenance, not semantic contract data.
        # Keeping them out of the envelope makes identity and payload agree.
        envelope=(("subject_ref", subject),) if facts.exploration_segment is None else (
            ("subject_ref", subject),
            ("work_segment_id", facts.exploration_segment.segment_id),
            ("result_kind", facts.exploration_segment.result_kind.value),
            ("result_ref", facts.exploration_segment.result_ref),
            ("records_progress", facts.exploration_segment.records_progress),
        ),
        state=CandidateState.COMPETITIVE, available_from=advanced_at, expires_at=None, resource_budget=0.0,
        based_on_state_version=based_on_state_version, created_at=advanced_at, updated_at=advanced_at,
        # State version is an optimistic reference, not a semantic contract revision.
        # The service/repository resolver supplies candidate_revision explicitly.
        semantic_revision=facts.candidate_revision)
    costs = (() if facts.repeat_soft_cost == 0 else (CandidateCostTerm(kind="repeat_soft_cost",
        amount=float(facts.repeat_soft_cost), evidence_refs=facts.repeat_cost_refs),))
    shadow = ShadowCandidateInput(goal=goal, reward=reward, candidate=contract, forecasts=tuple(forecasts),
        costs=costs, template_probability_policy=template_policy,
        template_policy_version=TEMPLATE_POLICY_VERSION if template_policy else None,
        source_refs=tuple(dict.fromkeys(facts.evidence_refs + (snapshot.source_cursor,))))
    provenance = LegacyProvenance(candidate_id=source.candidate.candidate_id,
        internal_utility=float(source.candidate.internal_utility), internal_need=facts.legacy_internal_need,
        relevance=facts.legacy_relevance,
        coefficients=(("v_reply", source.candidate.coefficients.v_reply),
                      ("v_continue", source.candidate.coefficients.v_continue),
                      ("c_negative", source.candidate.coefficients.c_negative)),
        assessment_utility_terms=tuple(sorted((str(k), float(v)) for k, v in source.assessment.utility_terms.items())))
    return BuiltCandidateContracts(source_candidate_id=source.candidate.candidate_id, initial_goal=initial_goal,
        reward=reward, goal=goal, candidate=contract, shadow_input=shadow, provenance=provenance)


def build_shadow_round(*, snapshot: RuntimeFactSnapshot, inputs: tuple[RuntimeCandidateInput, ...],
                       advanced_at: datetime, previous_state: LangchaoState | None = None,
                       based_on_state_version: int = 0,
                       parameter_version: str = "langchao.parameters.v1",
                       permission: PermissionProjection | None = None,
                       permission_version: str = "runtime.permission.v2",
                       attention_profile: AttentionProfile | None = None) -> BuiltShadowRound:
    """Build deterministic contracts and bootstrap/reconcile an initial state.

    ``permission_version`` remains a compatibility shim for offline/legacy callers.
    Production supplies ``snapshot.permission`` (or the explicit ``permission`` arg),
    and the exact projection version is bound to candidates and state.
    """
    _utc("advanced_at", advanced_at)
    projection = permission or snapshot.permission
    if permission is not None and snapshot.permission is not None and permission != snapshot.permission:
        raise ValueError("explicit permission must equal snapshot permission projection")
    if projection is not None:
        if projection.scope_key != snapshot.scope_key:
            raise ValueError("permission projection scope must match snapshot scope")
        permission_version = projection.permission_version
    by_id = {item.candidate.candidate_id: item for item in inputs}
    if len(by_id) != len(inputs):
        raise ValueError("inputs must have unique candidate ids")
    facts_by_id = {item.candidate_id: item for item in snapshot.candidates}
    if set(by_id) != set(facts_by_id):
        raise ValueError("snapshot candidate facts and inputs must cover exactly the same ids")

    dropped: list[DroppedCandidate] = []
    built: list[BuiltCandidateContracts] = []
    for source_id in sorted(by_id):
        facts = facts_by_id[source_id]
        reasons = list(facts.block_reasons)
        if (projection is not None and not projection.allowed
                and facts.template_key in {"contact.v1", "expression.v1", "followup.v1"}):
            reasons.append("permission_denied")
        if facts.blocked:
            reasons.append("blocked")
        if facts.hard_repeat:
            reasons.append("hard_repeat")
        if reasons:
            dropped.append(DroppedCandidate(source_candidate_id=source_id, reasons=tuple(dict.fromkeys(reasons))))
            continue
        bound_facts = (replace(facts, permission_ref=permission_version)
                       if projection is not None and facts.template_key in {
                           "contact.v1", "expression.v1", "followup.v1"
                       } else facts)
        item = _build_one(snapshot, bound_facts, by_id[source_id], advanced_at, based_on_state_version)
        if any(existing.candidate.candidate_id == item.candidate.candidate_id for existing in built):
            dropped.append(DroppedCandidate(source_candidate_id=source_id, reasons=("duplicate_semantic_candidate",)))
            continue
        built.append(item)

    mappings = tuple(CandidateIdMapping(source_candidate_id=item.source_candidate_id,
        langchao_candidate_id=item.candidate.candidate_id, goal_id=item.goal.goal_id,
        reward_contract_id=item.reward.reward_contract_id) for item in built)
    goals = tuple(item.goal for item in built)
    rewards = tuple(item.reward for item in built)
    candidates = tuple(item.candidate for item in built)
    goal_hash = _hash_unordered(_identity_rows(goals))
    reward_hash = _hash_unordered(_identity_rows(rewards))
    # Round snapshot hashes describe supplied source snapshots, not adapter creation time.
    candidate_hash = _hash_unordered(tuple({
        "candidate_id": item.source_candidate_id,
        "action": dict(by_id[item.source_candidate_id].candidate.action),
        "policy": {
            "low_pressure": by_id[item.source_candidate_id].candidate.policy.low_pressure,
            "low_frequency": by_id[item.source_candidate_id].candidate.policy.low_frequency,
            "easy_to_ignore": by_id[item.source_candidate_id].candidate.policy.easy_to_ignore,
            "continuous_follow_up": by_id[item.source_candidate_id].candidate.policy.continuous_follow_up,
            "sensitive": by_id[item.source_candidate_id].candidate.policy.sensitive,
        },
        "source_event_ids": list(by_id[item.source_candidate_id].candidate.source_event_ids),
    } for item in built))
    prediction_hash = _hash_unordered(tuple(
        {"prediction_set": None} if by_id[item.source_candidate_id].predictions is None else {
            "snapshot_id": by_id[item.source_candidate_id].predictions.snapshot_id,
            "parameter_version": by_id[item.source_candidate_id].predictions.parameter_version,
            "reply": by_id[item.source_candidate_id].predictions.reply.to_dict(),
            "continuation": by_id[item.source_candidate_id].predictions.continuation.to_dict(),
            "negative": by_id[item.source_candidate_id].predictions.negative.to_dict(),
        } for item in built
    ))
    working_set = tuple(sorted(item.candidate.candidate_id for item in built))
    round_id = _stable_id(
        "round", snapshot.scope_key, snapshot.source_cursor,
        candidate_hash, prediction_hash, permission_version,
    )
    permission_compatible = (
        previous_state is not None
        and previous_state.permission_version == permission_version
    )
    previous_readiness = (
        dict(previous_state.readiness) if permission_compatible else {}
    )
    same_working_set = (
        permission_compatible
        and previous_state.working_set == working_set
    )
    readiness = tuple((item, previous_readiness.get(item, 0.0)) for item in working_set)
    value_profile = value_profile_from_runtime(snapshot.values)
    attention = all_one_attention() if attention_profile is None else attention_profile
    if not isinstance(attention, AttentionProfile):
        raise TypeError("attention_profile must be an AttentionProfile or None")
    state = LangchaoState(scope_key=snapshot.scope_key, decision_round_id=round_id,
        working_set=working_set, readiness=readiness, attraction=tuple((item, 0.0) for item in working_set),
        attention=attention.direction_weights, advanced_at=(previous_state.advanced_at if same_working_set else advanced_at),
        based_on_state_version=based_on_state_version, event_cursor=snapshot.source_cursor,
        goal_snapshot_version=goal_hash, reward_snapshot_version=reward_hash,
        candidate_snapshot_version=candidate_hash, prediction_snapshot_version=prediction_hash,
        value_profile_version=value_profile.version, attention_version=attention.version,
        parameter_version=parameter_version, permission_version=permission_version,
        revision=previous_state.revision if same_working_set else 1)
    if same_working_set:
        state = replace(previous_state, event_cursor=snapshot.source_cursor,
            goal_snapshot_version=goal_hash, reward_snapshot_version=reward_hash,
            candidate_snapshot_version=candidate_hash, prediction_snapshot_version=prediction_hash,
            value_profile_version=value_profile.version, attention=attention.direction_weights,
            attention_version=attention.version, parameter_version=parameter_version,
            permission_version=permission_version)
    return BuiltShadowRound(inputs=tuple(item.shadow_input for item in built), contracts=tuple(built), state=state,
        value_profile=value_profile, attention_profile=attention, dropped=tuple(dropped), id_mapping=mappings,
        goal_snapshot_version=goal_hash, reward_snapshot_version=reward_hash,
        candidate_snapshot_version=candidate_hash, prediction_snapshot_version=prediction_hash)


__all__ = [
    "ATTENTION_PROFILE_VERSION", "BUILT_SHADOW_ROUND_VERSION", "BuiltCandidateContracts",
    "BuiltShadowRound", "CandidateIdMapping", "DroppedCandidate", "FIXED_VALUE_TOTAL",
    "LANGCHAO_RUNTIME_ADAPTER_VERSION", "LegacyProvenance", "RUNTIME_FACT_SNAPSHOT_VERSION",
    "RuntimeCandidateFacts", "RuntimeCandidateInput", "RuntimeFactSnapshot", "TEMPLATE_POLICY_VERSION",
    "VALUE_PROFILE_VERSION", "all_one_attention", "build_shadow_round", "value_profile_from_runtime",
]
