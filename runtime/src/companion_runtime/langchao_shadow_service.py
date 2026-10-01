"""Isolated application service wiring runtime adapter output to 「浪潮」 shadow.

The service intentionally owns no CLI, scheduler, sender, outbox, or dispatch-claim
capability.  Its collaborators are the narrow v12/v13/v14 repositories plus the
shadow audit adapter and a read-only authority reader.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, ContextManager, Mapping, Protocol, runtime_checkable

from .langchao_authority import AuthorityEngine, AuthorityMode
from .langchao_engine import CompetitionEdge, LangchaoParameters
from .langchao_runtime_adapter import BuiltCandidateContracts, BuiltShadowRound
from .langchao_shadow import LangchaoShadowRepository, ShadowRunResult, run_langchao_shadow
from .langchao_state_repository import LangchaoStateConflictError
from .langchao_types import SettlementType


class LangchaoShadowAuthorityError(RuntimeError):
    """The active authority is incompatible with an isolated shadow run."""


class LangchaoShadowCASConflictError(RuntimeError):
    """A contract pointer changed to a revision other than the requested one."""


@runtime_checkable
class AuthorityReader(Protocol):
    """Read-only authority surface; notably it cannot create dispatch claims."""

    def get_active(self) -> Any | None: ...


@dataclass(frozen=True, slots=True)
class _Authority:
    engine_key: str
    mode: str
    may_dispatch: bool
    revision: int | None


def _field(value: Any, name: str, index: int | None = None) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    if hasattr(value, name):
        return getattr(value, name)
    if index is not None:
        return value[index]
    raise TypeError(f"authority/active row has no {name!r} field")


def _authority(value: Any) -> _Authority:
    # Domain ActiveAuthority wraps its immutable revision; SQL rows are flat.
    revision_object = getattr(value, "revision", None)
    if revision_object is not None and not isinstance(revision_object, int):
        value = revision_object
    engine = _field(value, "engine_key")
    mode = _field(value, "mode")
    may_dispatch = _field(value, "may_dispatch")
    revision = _field(value, "revision")
    return _Authority(
        engine_key=engine.value if isinstance(engine, AuthorityEngine) else str(engine),
        mode=mode.value if isinstance(mode, AuthorityMode) else str(mode),
        may_dispatch=bool(may_dispatch),
        revision=int(revision) if revision is not None else None,
    )


def _active_coordinates(row: Any, revision_name: str) -> tuple[int, int] | None:
    if row is None:
        return None
    return int(_field(row, revision_name)), int(_field(row, "pointer_version"))


class LangchaoShadowService:
    """Persist one built adapter round and evaluate it in shadow mode only."""

    def __init__(
        self,
        *,
        contract_repository: Any,
        outcome_repository: Any,
        state_repository: Any,
        shadow_repository: LangchaoShadowRepository,
        authority_reader: AuthorityReader,
        transaction_factory: Any | None = None,
        allow_live_evaluation: bool = False,
    ) -> None:
        if not isinstance(authority_reader, AuthorityReader):
            raise TypeError("authority_reader must implement AuthorityReader")
        if not isinstance(shadow_repository, LangchaoShadowRepository):
            raise TypeError("shadow_repository must implement LangchaoShadowRepository")
        self.contracts = contract_repository
        self.outcomes = outcome_repository
        self.states = state_repository
        self.shadow = shadow_repository
        self.authority_reader = authority_reader
        self._transaction_factory = transaction_factory
        self._allow_live_evaluation = bool(allow_live_evaluation)

    def _transaction(self) -> ContextManager[Any]:
        if self._transaction_factory is not None:
            return self._transaction_factory()
        connection = getattr(self.contracts, "connection", None)
        transaction = getattr(connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext(connection)

    def _validate_authority(self, value: Any) -> _Authority:
        if value is None:
            raise LangchaoShadowAuthorityError("scope has no active authority")
        active = _authority(value)
        allowed = (
            active.engine_key == AuthorityEngine.RUNTIME_V2.value
            and active.mode == AuthorityMode.LIVE.value
            and active.may_dispatch
        ) or (
            active.engine_key == AuthorityEngine.LANGCHAO.value
            and active.mode == AuthorityMode.SHADOW.value
            and not active.may_dispatch
        ) or (
            # Live reuses the same deterministic contract/state/audit phase, then a
            # separate capability-bearing service performs the dispatch transaction.
            self._allow_live_evaluation
            and active.engine_key == AuthorityEngine.LANGCHAO.value
            and active.mode == AuthorityMode.LIVE.value
            and active.may_dispatch
        )
        if not allowed:
            raise LangchaoShadowAuthorityError(
                "evaluation requires runtime_v2/live, langchao/shadow, or langchao/live authority"
            )
        return active

    @staticmethod
    def _activate_exact(
        *, get_active: Any, activate: Any, identity_keyword: str,
        identity: str, revision: int,
    ) -> None:
        current = _active_coordinates(get_active(**{identity_keyword: identity}), "revision")
        if current is not None and current[0] == revision:
            return
        expected = 0 if current is None else current[1]
        if activate(**{identity_keyword: identity, "revision": revision, "expected_pointer_version": expected}):
            return
        # Rebuild once after a CAS loss.  Reuse only if the winner published our
        # exact target; never blindly retry against a newly observed pointer.
        rebuilt = _active_coordinates(get_active(**{identity_keyword: identity}), "revision")
        if rebuilt is not None and rebuilt[0] == revision:
            return
        raise LangchaoShadowCASConflictError(
            f"{identity_keyword} pointer changed away from requested revision {revision}"
        )

    def _persist_contract(self, item: BuiltCandidateContracts) -> None:
        if item.goal.revision <= item.initial_goal.revision:
            raise ValueError("built contracts must encode initial goal -> reward -> later bound goal")
        self.contracts.put_goal_revision(item.initial_goal)
        self._activate_exact(
            get_active=self.contracts.get_active_goal, activate=self.contracts.activate_goal,
            identity_keyword="goal_id", identity=item.initial_goal.goal_id,
            revision=item.initial_goal.revision,
        )
        self.contracts.put_reward_revision(item.reward)
        self._activate_exact(
            get_active=self.contracts.get_active_reward, activate=self.contracts.activate_reward,
            identity_keyword="reward_contract_id", identity=item.reward.reward_contract_id,
            revision=item.reward.revision,
        )
        self.contracts.put_goal_revision(item.goal)
        self._activate_exact(
            get_active=self.contracts.get_active_goal, activate=self.contracts.activate_goal,
            identity_keyword="goal_id", identity=item.goal.goal_id, revision=item.goal.revision,
        )
        self.contracts.put_candidate_revision(item.candidate)
        self._activate_exact(
            get_active=self.contracts.get_active_candidate, activate=self.contracts.activate_candidate,
            identity_keyword="candidate_id", identity=item.candidate.candidate_id,
            revision=item.candidate.semantic_revision,
        )

        expected = tuple(
            token for token in item.reward.outcome_tokens
            if token.settlement_type is SettlementType.EXPECTED
        )
        for token in expected:
            self.outcomes.put_outcome_revision(
                token, revision=1, reward_contract_id=item.reward.reward_contract_id,
                reward_contract_revision=item.reward.revision,
            )
            self._activate_exact(
                get_active=self.outcomes.get_active_outcome, activate=self.outcomes.activate_outcome,
                identity_keyword="token_id", identity=token.token_id, revision=1,
            )
        self.outcomes.bind_reward_outcomes(
            reward_contract_id=item.reward.reward_contract_id,
            reward_contract_revision=item.reward.revision,
            outcome_revisions=tuple((token.token_id, 1) for token in expected),
        )

    def run(
        self,
        built: BuiltShadowRound,
        *,
        now: datetime,
        parameters: LangchaoParameters,
        edges: tuple[CompetitionEdge, ...] = (),
        decision_budget: float | None = None,
        baseline_candidate_id: str | None = None,
        baseline_defer_reason: str | None = None,
        run_id: str,
        idempotency_key: str,
        utility_scale: float = 1.0,
        tie_break_order: tuple[str, ...] = (),
        before_commit: Callable[[ShadowRunResult, BuiltShadowRound, Any], Any] | None = None,
    ) -> ShadowRunResult:
        if not isinstance(built, BuiltShadowRound):
            raise TypeError("built must be BuiltShadowRound")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not built.contracts:
            raise ValueError("built shadow round has no admitted candidates")

        authority = self._validate_authority(self.authority_reader.get_active())
        if before_commit is not None and not self._allow_live_evaluation:
            raise LangchaoShadowAuthorityError(
                "before_commit requires an explicit live-evaluation capability"
            )
        existing = self.shadow.get_shadow_run(
            scope_key=built.state.scope_key, idempotency_key=idempotency_key,
        )
        if existing is not None:
            if existing.run_id != run_id or existing.scope_key != built.state.scope_key:
                raise RuntimeError("shadow service idempotency conflict")
            decision_round_id = getattr(existing, "decision_round_id", None)
            if decision_round_id is None and hasattr(existing, "payload"):
                decision_round_id = existing.payload.get("decision_round_id")
            if decision_round_id != built.state.decision_round_id:
                raise RuntimeError("shadow service idempotency conflict")
            return existing

        with self._transaction() as connection:
            for item in built.contracts:
                self._persist_contract(item)

            candidate_revisions = {
                item.candidate.candidate_id: item.candidate.semantic_revision for item in built.contracts
            }
            loaded = self.states.load_active_state()
            if loaded is None:
                previous_state = built.state
                pointer = self.states.begin_round(
                    previous_state, run_mode="shadow", candidate_revisions=candidate_revisions,
                    expected_pointer_version=0, authority_revision=authority.revision,
                )
            else:
                active_state, pointer = loaded
                compatible = (
                    active_state.decision_round_id == built.state.decision_round_id
                    and active_state.working_set == built.state.working_set
                )
                status = self.states.get_round_status(round_id=active_state.decision_round_id)
                if compatible:
                    if status != "open":
                        raise LangchaoStateConflictError("compatible shadow round is already closed")
                    previous_state = active_state
                else:
                    if status == "open":
                        if not self.states.abort_open_round(
                            round_id=active_state.decision_round_id, ended_at=now,
                            expected_pointer_version=pointer,
                        ):
                            raise LangchaoStateConflictError("open round changed before abort")
                    previous_state = built.state
                    pointer = self.states.begin_round(
                        previous_state, run_mode="shadow", candidate_revisions=candidate_revisions,
                        expected_pointer_version=pointer, authority_revision=authority.revision,
                    )

            result = run_langchao_shadow(
                repository=self.shadow, run_id=run_id, idempotency_key=idempotency_key,
                source_input_cursor=built.state.event_cursor,
                source_input_version=built.adapter_version,
                inputs=built.inputs, value_profile=built.value_profile,
                attention_profile=built.attention_profile, previous_state=previous_state,
                until=now, parameters=parameters, competition_edges=edges,
                utility_scale=utility_scale, decision_budget_seconds=decision_budget,
                tie_break_order=tie_break_order, expected_state_pointer_version=pointer,
                baseline_candidate_id=baseline_candidate_id,
                baseline_defer_reason=baseline_defer_reason,
            )
            # Deliberately after contract/state/evaluation-audit writes and before the
            # single outer transaction exits. Any callback failure rolls all of them
            # back together with its live claim/attempt/outbox/snapshot writes.
            if before_commit is not None:
                before_commit(result, built, connection)
            return result


__all__ = [
    "AuthorityReader", "LangchaoShadowAuthorityError", "LangchaoShadowCASConflictError",
    "LangchaoShadowService",
]
