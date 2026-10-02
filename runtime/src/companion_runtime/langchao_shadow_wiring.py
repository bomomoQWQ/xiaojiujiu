"""Opt-in production composition for the non-dispatching 浪潮 shadow path.

This is deliberately an adapter/composition boundary.  It receives an already-made
Runtime-v2 baseline decision, snapshots only explicit facts, and gives the shadow
service repositories which share the existing PostgreSQL connection.  No sender,
outbox, exposure, quota, or authority-claim capability is exposed to the runner.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping

from .capability_witness import (
    PostgresWitnessRepository,
    WitnessValidationError,
    WitnessValidator,
    requirement_from_mapping,
)
from .decision_v2_audit import CandidateAssessment
from .langchao_authority_repository import LangchaoAuthorityRepository
from .langchao_attention_recipe import (
    BASELINE_RECIPE,
    AttentionRecipeCandidate,
    ExplicitAttentionSignals,
    compile_attention_recipe,
    normalize_recipe,
    parameter_version_for_recipe,
)
from .langchao_engine import LangchaoParameters
from .langchao_exploration import ExplorationResultKind, ExplorationWorkSegment
from .langchao_history import condition_forecasts_from_history, observations_from_rows
from .langchao_outcome_repository import LangchaoOutcomeRepository
from .langchao_permission import read_runtime_permission
from .langchao_repository import LangchaoRepository
from .langchao_runtime_adapter import (
    RuntimeCandidateFacts,
    RuntimeCandidateInput,
    RuntimeFactSnapshot,
    build_shadow_round,
)
from .langchao_shadow import LangchaoShadowPostgresRepository
from .langchao_shadow_service import LangchaoShadowService
from .langchao_state_repository import LangchaoStateRepository
from .langchao_types import GoalOwnership, MotivationDirection
from .motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
from .repeat_v2 import RepeatCostBreakdownV2, RepeatSubjectV2
from .runtime_v2 import CandidateDecisionV2, CandidateV2, EndogenousDecisionV2


DEFAULT_LANGCHAO_PARAMETERS = LangchaoParameters(
    leak=0.2,
    competition_gain=0.0,
    decision_threshold=0.99,
    time_scale_seconds=10.0,
    max_step_seconds=0.5,
    crossing_tolerance=1e-7,
    tie_tolerance=1e-6,
)


class _BorrowedConnection:
    """Forward SQL while leaving transaction ownership to the service.

    Psycopg nested ``connection.transaction()`` contexts are savepoints.  The shadow
    service already owns one atomic transaction spanning every repository; allowing
    each repository to open another context caused savepoint lifetime failures on the
    shared autocommit connection.  Repository transaction blocks therefore become
    no-ops only in this composition, while all SQL still uses the same connection.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def transaction(self) -> Any:
        return nullcontext()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ReadOnlyAuthority:
    """Capability wrapper which intentionally omits ``create_dispatch_claim``."""

    def __init__(self, repository: LangchaoAuthorityRepository) -> None:
        self._repository = repository

    def get_active(self) -> Any | None:
        return self._repository.get_active()


class LangchaoRevisionResolver:
    """Allocate revisions above all persisted revisions for a semantic identity.

    Shadow contracts include their creation time and runtime-state basis in the
    immutable payload, so a new baseline decision is a new immutable revision.  The
    active payload/hash is read as part of resolution (and max revision protects a
    stale/non-active branch); consequently this never assumes revision ``1``.
    """

    _DEFINITIONS = {
        "goal": ("langchao_goal_revisions", "langchao_goal_active", "goal_id"),
        "reward": ("langchao_reward_revisions", "langchao_reward_active", "reward_contract_id"),
        "candidate": ("langchao_candidate_revisions", "langchao_candidate_active", "candidate_id"),
    }

    def __init__(self, connection: Any, *, scope_key: str) -> None:
        self.connection = connection
        self.scope_key = scope_key

    @staticmethod
    def _value(row: Any, key: str, index: int) -> Any:
        return row[key] if isinstance(row, Mapping) else row[index]

    @staticmethod
    def _semantic(payload: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        value.pop("revision", None)
        value.pop("semantic_revision", None)
        value.pop("created_at", None)
        value.pop("updated_at", None)
        # Candidate state basis is intentionally revisioned; goal/reward template
        # contracts are reusable when this normalized core is unchanged.
        return value

    def resolve(self, kind: str, identity: str, dto: Any) -> tuple[int, datetime | None]:
        revisions, _active, identity_column = self._DEFINITIONS[kind]
        rows = self.connection.execute(
            f"SELECT revision, payload FROM {revisions} WHERE scope_key = %s AND {identity_column} = %s ORDER BY revision",
            (self.scope_key, identity),
        ).fetchall()
        wanted = self._semantic(dto.to_dict())
        highest = 0
        for row in rows:
            revision = int(self._value(row, "revision", 0))
            highest = max(highest, revision)
            payload = self._value(row, "payload", 1)
            if isinstance(payload, str):
                payload = json.loads(payload)
            if isinstance(payload, Mapping) and self._semantic(payload) == wanted:
                created = payload.get("created_at")
                if isinstance(created, str):
                    created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                return revision, created if isinstance(created, datetime) else None
        return highest + 1, None


def _explicit_ref(action: Mapping[str, Any], name: str) -> str | None:
    value = action.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_tuple(action: Mapping[str, Any], name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    value = action.get(name)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"action.{name} must be an explicit list/tuple")
    result = tuple(str(item).strip() for item in value)
    if nonempty and not result:
        raise ValueError(f"action.{name} must not be empty")
    if any(not item for item in result):
        raise ValueError(f"action.{name} items must be non-empty strings")
    return result


_EXPLORATION_ARTIFACT_TYPES = {
    ExplorationResultKind.CAPABILITY: "exploration_capability",
    ExplorationResultKind.ARTIFACT: "exploration_artifact",
    ExplorationResultKind.NO_CONCLUSION: "exploration_work_segment",
}


def _exploration_segment(
    action: Mapping[str, Any], *, scope_key: str, witness_reader: Any | None,
) -> ExplorationWorkSegment | None:
    """Build a segment only after re-reading its exact durable witness.

    The action is an untrusted claim envelope.  Its scope/result/evidence become facts
    only when the capability/artifact ledger has the exact successful active row.  A
    no-conclusion additionally requires an ``exploration_work_segment`` artifact: prose
    saying "nothing found" is not a completed work segment.
    """

    kind = str(action.get("type") or "").strip().lower()
    if kind not in {"internal_exploration", "exploration_work_segment"}:
        return None
    if action.get("internal") is not True or action.get("segment_completed") is not True:
        return None
    if witness_reader is None:
        raise WitnessValidationError("completed exploration has no trusted witness reader")
    raw_witness = action.get("exploration_witness")
    if not isinstance(raw_witness, Mapping):
        raise WitnessValidationError("completed exploration has no witness requirement")
    requirement = requirement_from_mapping(raw_witness, scope_key=scope_key)
    witness = WitnessValidator(witness_reader).validate(requirement)
    if witness.operation != "internal_exploration":
        raise WitnessValidationError("exploration operation witness mismatch")

    result = _explicit_ref(action, "result_kind")
    try:
        result_kind = ExplorationResultKind(result or "")
    except ValueError:
        raise ValueError("action.result_kind must be an exploration terminal result") from None
    if witness.artifact_type != _EXPLORATION_ARTIFACT_TYPES[result_kind]:
        raise WitnessValidationError("exploration result kind witness mismatch")

    evidence_refs = _string_tuple(action, "evidence_refs", nonempty=True)
    if evidence_refs != witness.source_refs:
        raise WitnessValidationError("exploration evidence witness mismatch")
    result_ref = f"artifact:sha256:{witness.artifact_sha256}"
    if _explicit_ref(action, "result_ref") != result_ref:
        raise WitnessValidationError("exploration result reference witness mismatch")
    return ExplorationWorkSegment(
        segment_id=_explicit_ref(action, "segment_id") or "",
        problem_ref=_explicit_ref(action, "problem_ref") or "",
        question=_explicit_ref(action, "question") or "",
        executable_steps=_string_tuple(action, "executable_steps", nonempty=True),
        result_kind=result_kind,
        result_ref=result_ref,
        evidence_refs=evidence_refs,
    )


def _facts_for(
    item: CandidateDecisionV2, *, internal_exploration_enabled: bool = True,
    scope_key: str = "", witness_reader: Any | None = None,
) -> RuntimeCandidateFacts | None:
    candidate = item.candidate
    action = candidate.action
    kind = str(action.get("type") or "").strip().lower()
    evidence = tuple(dict.fromkeys(candidate.source_event_ids)) or (f"candidate:{candidate.candidate_id}",)
    segment = (_exploration_segment(action, scope_key=scope_key, witness_reader=witness_reader)
               if internal_exploration_enabled else None)
    common = dict(
        candidate_id=candidate.candidate_id,
        ownership=GoalOwnership.SELF_WISH,
        evidence_refs=evidence,
        capability_refs=(() if kind in {"rest", "defer", "internal_rest", "internal_exploration", "exploration_work_segment"}
                         else ("external_message",)),
        repeat_soft_cost=float(item.repeat.total_cost),
        repeat_cost_refs=(item.repeat.policy_version,),
        blocked=item.blocked,
        hard_repeat=bool(item.repeat.hard_limit_reasons),
        block_reasons=item.reasons,
        legacy_internal_utility=float(candidate.internal_utility),
    )
    if segment is not None:
        exploration_common = dict(common)
        exploration_common.update(
            ownership=GoalOwnership.SELF_INTEREST,
            evidence_refs=tuple(dict.fromkeys((*evidence, *segment.evidence_refs))),
        )
        return RuntimeCandidateFacts(
            **exploration_common, template_key="exploration.v1", exploration_segment=segment,
        )
    if kind in {"internal_exploration", "exploration_work_segment"}:
        # Disabled or incomplete segments are omitted, never relabelled as contact.
        return None
    if kind in {"rest", "defer", "internal_rest"}:
        return RuntimeCandidateFacts(
            **common, template_key="internal_rest.v1",
            self_regulation_ref=str(action.get("self_regulation_ref") or "runtime:self-regulation"),
            rest_realized_policy=True,
        )
    if kind in {"share", "emotional_expression", "expression"}:
        memory = _explicit_ref(action, "memory_ref")
        social = _explicit_ref(action, "social_ref")
        if memory is None and social is None:
            return None
        return RuntimeCandidateFacts(
            **common, template_key="expression.v1", memory_ref=memory, social_ref=social,
            expression_delivered_policy=True,
        )
    if kind in {"follow_up", "check_in", "followup"}:
        unfinished = _explicit_ref(action, "unfinished_id")
        unfinished = unfinished or candidate.repeat_subject.concern_id
        if not unfinished:
            return None
        return RuntimeCandidateFacts(**common, template_key="followup.v1", unfinished_id=unfinished)
    if kind in {"contact", "reminder"}:
        return RuntimeCandidateFacts(
            **common, template_key="contact.v1", subject_ref="relationship-continuity"
        )
    # Unsupported candidate kinds (notably repair/apology/confront) must not be
    # relabelled as contact. Their omission remains visible in baseline audit facts.
    return None


def _explicit_attention_signal(action: Mapping[str, Any], name: str) -> float:
    value = action.get(name, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"action.{name} must be numeric")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"action.{name} must be between 0 and 1")
    return value


def _attention_candidate(
    item: Any, *, admitted_facts: RuntimeCandidateFacts | None = None,
) -> AttentionRecipeCandidate:
    candidate = item.candidate
    facts = admitted_facts if admitted_facts is not None else _facts_for(item)
    if facts is None:
        raise ValueError("attention candidate requires admitted runtime facts")
    direction = {
        "contact.v1": MotivationDirection.APPROACH,
        "expression.v1": MotivationDirection.EXPRESSION,
        "followup.v1": MotivationDirection.CARE,
        "internal_rest.v1": MotivationDirection.REST,
        "exploration.v1": MotivationDirection.EXPLORATION,
    }[facts.template_key]
    action = candidate.action
    return AttentionRecipeCandidate(
        candidate_id=candidate.candidate_id,
        direction=direction,
        signals=ExplicitAttentionSignals(
            goal_urgency=_explicit_attention_signal(action, "goal_urgency"),
            source_freshness=_explicit_attention_signal(action, "source_freshness"),
            resource_availability=_explicit_attention_signal(action, "resource_availability"),
        ),
    )


def _assessment(item: CandidateDecisionV2) -> CandidateAssessment:
    return CandidateAssessment(
        candidate_id=item.candidate.candidate_id,
        prediction_snapshot_id=item.predictions.snapshot_id,
        used_bounds=item.user_utility.used_bounds.to_dict(),
        utility_terms={
            **item.user_utility.decomposition.to_dict(),
            "internal": float(item.candidate.internal_utility),
            "repeat_cost": -float(item.repeat.total_cost),
            "net": float(item.net_utility),
        },
        repeat_key=(item.candidate.repeat_subject.action_goal_id
                    or item.candidate.repeat_subject.concern_id
                    or "candidate:" + item.candidate.candidate_id),
        reasons=item.reasons or ("eligible",),
    )


@dataclass(slots=True)
class LangchaoShadowRunner:
    scope_key: str
    runtime: Any
    service: LangchaoShadowService
    state_repository: LangchaoStateRepository
    revisions: LangchaoRevisionResolver
    parameters: LangchaoParameters = DEFAULT_LANGCHAO_PARAMETERS
    attention_recipe: str = BASELINE_RECIPE
    internal_exploration_enabled: bool = True
    exploration_witness_reader: Any | None = None
    outcome_repository: Any | None = None
    last_built: BuiltShadowRound | None = None

    def run(
        self, decision: EndogenousDecisionV2, *, now: datetime,
        before_commit: Any | None = None,
        authority_revision: int | None = None,
    ) -> Any | None:
        pairs = tuple(
            (item, _facts_for(
                item,
                internal_exploration_enabled=self.internal_exploration_enabled,
                scope_key=self.scope_key,
                witness_reader=self.exploration_witness_reader,
            ))
            for item in decision.assessments
        )
        pairs = tuple((item, facts) for item, facts in pairs if facts is not None)
        inputs = [RuntimeCandidateInput(candidate=item.candidate, predictions=item.predictions,
                                        assessment=_assessment(item)) for item, _facts in pairs]
        facts_list = [facts for _item, facts in pairs]
        # Always provide the internal alternative. It is a synthetic, non-dispatching
        # shadow candidate and needs no user prediction set.
        rest_id = "runtime-shadow:internal-rest"
        rest_candidate = CandidateV2(
            candidate_id=rest_id, action={"type": "internal_rest", "self_regulation_ref": "runtime:self-regulation"},
            internal_utility=0.0,
            coefficients=UserUtilityCoefficientsV2(v_reply=0.0, v_continue=0.0, c_negative=0.0),
            repeat_subject=RepeatSubjectV2(), policy=CandidatePolicyV2(), source_event_ids=(),
        )
        rest_assessment = CandidateAssessment(
            candidate_id=rest_id, prediction_snapshot_id="none:internal-rest",
            used_bounds={}, utility_terms={"internal": 0.0, "repeat_cost": 0.0, "net": 0.0},
            repeat_key=rest_id, reasons=("synthetic_internal_alternative",),
        )
        inputs.append(RuntimeCandidateInput(candidate=rest_candidate, predictions=None,
                                            assessment=rest_assessment))
        facts_list.append(RuntimeCandidateFacts(
            candidate_id=rest_id, template_key="internal_rest.v1",
            ownership=GoalOwnership.SELF_WISH,
            evidence_refs=("runtime:self-regulation",),
            self_regulation_ref="runtime:self-regulation", rest_realized_policy=True,
        ))
        runtime_state = self.runtime.state()
        state_version = int(runtime_state.version)
        permission = read_runtime_permission(self.runtime, scope_key=self.scope_key, now=now)
        preliminary = RuntimeFactSnapshot(
            scope_key=self.scope_key,
            episode_id=decision.decision_id,
            source_cursor=f"runtime-version:{state_version}",
            values=runtime_state.values.to_dict(),
            candidates=tuple(facts_list),
            permission=permission,
        )
        inputs = tuple(inputs)
        active = self.state_repository.load_active_state()
        previous = None if active is None else active[0]
        if previous is not None and self.state_repository.get_round_status(
            round_id=previous.decision_round_id
        ) != "open":
            previous = None
        probe = build_shadow_round(snapshot=preliminary, inputs=inputs, advanced_at=now,
                                   previous_state=previous, based_on_state_version=state_version)
        if not probe.contracts:
            return None
        contracts_by_source = {item.source_candidate_id: item for item in probe.contracts}
        allocated: list[RuntimeCandidateFacts] = []
        for facts in preliminary.candidates:
            contract = contracts_by_source.get(facts.candidate_id)
            if contract is None:
                allocated.append(facts)
                continue
            goal_initial, _ = self.revisions.resolve(
                "goal", contract.goal.goal_id, contract.initial_goal
            )
            goal_bound, _ = self.revisions.resolve("goal", contract.goal.goal_id, contract.goal)
            # Both absent probes normalize to the same next revision; preserve the
            # required goal1 -> reward -> goal2 sequence for first publication.
            if goal_bound <= goal_initial:
                goal_bound = goal_initial + 1
            reward, _ = self.revisions.resolve(
                "reward", contract.reward.reward_contract_id, contract.reward
            )
            candidate, _ = self.revisions.resolve(
                "candidate", contract.candidate.candidate_id, contract.candidate
            )
            allocated.append(replace(facts, initial_goal_revision=goal_initial,
                                     bound_goal_revision=goal_bound, reward_revision=reward,
                                     candidate_revision=candidate))
        snapshot = replace(preliminary, candidates=tuple(allocated))
        selected_recipe = normalize_recipe(self.attention_recipe)
        recipe_candidates = tuple(
            AttentionRecipeCandidate(
                candidate_id=contract.candidate.candidate_id,
                direction=_attention_candidate(source, admitted_facts=facts).direction,
                signals=_attention_candidate(source, admitted_facts=facts).signals,
            )
            for source, facts in pairs
            for contract in probe.contracts
            if contract.source_candidate_id == source.candidate.candidate_id
        )
        recipe_candidates += tuple(
            AttentionRecipeCandidate(
                candidate_id=contract.candidate.candidate_id,
                direction=MotivationDirection.REST,
                signals=ExplicitAttentionSignals(),
            )
            for contract in probe.contracts
            if contract.source_candidate_id == rest_id
        )
        recipe_plan = compile_attention_recipe(
            selection=selected_recipe,
            candidates=recipe_candidates,
            baseline_parameters=self.parameters,
        )
        built = build_shadow_round(
            snapshot=snapshot, inputs=inputs, advanced_at=now,
            previous_state=previous, based_on_state_version=state_version,
            parameter_version=parameter_version_for_recipe(recipe_plan),
            attention_profile=recipe_plan.attention_profile,
        )
        # Realized user outcomes are never appended as bonus terms.  Active settled
        # history conditions the next round's expected forecasts before compilation.
        if self.outcome_repository is not None:
            history = observations_from_rows(
                self.outcome_repository.list_settled_user_observations()
            )
            conditioned = tuple(
                condition_forecasts_from_history(item, history) for item in built.inputs
            )
            contracts = tuple(
                replace(contract, shadow_input=conditioned[index])
                for index, contract in enumerate(built.contracts)
            )
            built = replace(built, inputs=conditioned, contracts=contracts)
        # Live wiring consumes this exact immutable build after numerical evaluation;
        # it must never rebuild from a potentially changed legacy candidate pool.
        self.last_built = built
        baseline = decision.chosen_candidate_id if decision.acted else None
        defer = None if decision.acted else decision.reason
        return self.service.run(
            built, now=now, parameters=recipe_plan.parameters,
            edges=recipe_plan.competition_edges,
            baseline_candidate_id=baseline, baseline_defer_reason=defer,
            run_id=f"langchao-shadow:{decision.decision_id}",
            idempotency_key=f"runtime-v2:{decision.decision_id}",
            before_commit=before_commit,
            expected_authority_revision=authority_revision,
        )


def build_langchao_shadow_runner(
    *, connection: Any, scope_key: str, runtime: Any, allow_live_evaluation: bool = False,
    transaction_factory: Any | None = None, attention_recipe: str = BASELINE_RECIPE,
    internal_exploration_enabled: bool = True,
) -> LangchaoShadowRunner:
    """Build safe repositories on the same migrated PostgreSQL connection."""
    borrowed = _BorrowedConnection(connection)
    contracts = LangchaoRepository(borrowed, scope_key=scope_key)
    outcomes = LangchaoOutcomeRepository(borrowed, scope_key=scope_key)
    states = LangchaoStateRepository(borrowed, scope_key=scope_key)
    # Bootstrap precedes service execution and retains its repository-owned atomic
    # transaction. During a run this collaborator is read-only.
    authority = LangchaoAuthorityRepository(connection, scope_key=scope_key)
    if authority.get_active() is None:
        authority.bootstrap()  # runtime_v2/live only; bootstrap itself rechecks under lock.
    shadow = LangchaoShadowPostgresRepository(borrowed, state_repository=states)
    service = LangchaoShadowService(
        contract_repository=contracts,
        outcome_repository=outcomes,
        state_repository=states,
        shadow_repository=shadow,
        authority_reader=_ReadOnlyAuthority(authority),
        transaction_factory=transaction_factory or connection.transaction,
        allow_live_evaluation=allow_live_evaluation,
    )
    return LangchaoShadowRunner(scope_key=scope_key, runtime=runtime, service=service,
                                state_repository=states, outcome_repository=outcomes,
                                revisions=LangchaoRevisionResolver(connection, scope_key=scope_key),
                                attention_recipe=normalize_recipe(attention_recipe),
                                internal_exploration_enabled=bool(internal_exploration_enabled),
                                exploration_witness_reader=PostgresWitnessRepository(
                                    connection, scope_key=scope_key,
                                ))


__all__ = [
    "DEFAULT_LANGCHAO_PARAMETERS", "LangchaoRevisionResolver", "LangchaoShadowRunner",
    "build_langchao_shadow_runner",
]
