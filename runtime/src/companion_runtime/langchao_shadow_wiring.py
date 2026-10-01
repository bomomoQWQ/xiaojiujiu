"""Opt-in production composition for the non-dispatching 浪潮 shadow path.

This is deliberately an adapter/composition boundary.  It receives an already-made
Runtime-v2 baseline decision, snapshots only explicit facts, and gives the shadow
service repositories which share the existing PostgreSQL connection.  No sender,
outbox, exposure, quota, or authority-claim capability is exposed to the runner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Mapping

from .decision_v2_audit import CandidateAssessment
from .langchao_authority_repository import LangchaoAuthorityRepository
from .langchao_engine import LangchaoParameters
from .langchao_outcome_repository import LangchaoOutcomeRepository
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
from .langchao_types import GoalOwnership
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


def _facts_for(item: CandidateDecisionV2) -> RuntimeCandidateFacts | None:
    candidate = item.candidate
    action = candidate.action
    kind = str(action.get("type") or "").strip().lower()
    evidence = tuple(dict.fromkeys(candidate.source_event_ids)) or (f"candidate:{candidate.candidate_id}",)
    common = dict(
        candidate_id=candidate.candidate_id,
        ownership=GoalOwnership.SELF_WISH,
        evidence_refs=evidence,
        repeat_soft_cost=float(item.repeat.total_cost),
        repeat_cost_refs=(item.repeat.policy_version,),
        blocked=item.blocked,
        hard_repeat=bool(item.repeat.hard_limit_reasons),
        block_reasons=item.reasons,
        legacy_internal_utility=float(candidate.internal_utility),
    )
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

    def run(self, decision: EndogenousDecisionV2, *, now: datetime) -> Any | None:
        pairs = tuple((item, _facts_for(item)) for item in decision.assessments)
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
        preliminary = RuntimeFactSnapshot(
            scope_key=self.scope_key,
            episode_id=decision.decision_id,
            source_cursor=f"runtime-version:{state_version}",
            values=runtime_state.values.to_dict(),
            candidates=tuple(facts_list),
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
        built = build_shadow_round(snapshot=snapshot, inputs=inputs, advanced_at=now,
                                   previous_state=previous, based_on_state_version=state_version)
        baseline = decision.chosen_candidate_id if decision.acted else None
        defer = None if decision.acted else decision.reason
        return self.service.run(
            built, now=now, parameters=self.parameters,
            baseline_candidate_id=baseline, baseline_defer_reason=defer,
            run_id=f"langchao-shadow:{decision.decision_id}",
            idempotency_key=f"runtime-v2:{decision.decision_id}",
        )


def build_langchao_shadow_runner(*, connection: Any, scope_key: str, runtime: Any) -> LangchaoShadowRunner:
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
        transaction_factory=connection.transaction,
    )
    return LangchaoShadowRunner(scope_key=scope_key, runtime=runtime, service=service,
                                state_repository=states,
                                revisions=LangchaoRevisionResolver(connection, scope_key=scope_key))


__all__ = [
    "DEFAULT_LANGCHAO_PARAMETERS", "LangchaoRevisionResolver", "LangchaoShadowRunner",
    "build_langchao_shadow_runner",
]
