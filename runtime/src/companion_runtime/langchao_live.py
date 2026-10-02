"""Minimal authority-gated live execution boundary for 「浪潮」.

Numerical evaluation and execution are deliberately separate.  A caller may build and
advance contracts freely, but only this service maps a decided external contract back
to its exact legacy candidate and requests the atomic claim/attempt/outbox commit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .capability_witness import WitnessValidator, candidate_witness_requirement
from .langchao_authority import AuthorityEngine, AuthorityMode
from .langchao_no_send import NoSendReason, NoSendResult
from .langchao_permission import PermissionProjection, read_runtime_permission
from .langchao_runtime_adapter import BuiltCandidateContracts, BuiltShadowRound
from .langchao_live_repository import LangchaoLiveCommit
from .langchao_types import CandidateKind, CandidateState
from .runtime_v2 import CandidateV2, CommitReceiptV2


class LangchaoLiveValidationError(RuntimeError):
    """The selected contract no longer matches its live execution envelope."""


@dataclass(frozen=True, slots=True)
class LangchaoLiveResult:
    round_id: str
    candidate_id: str | None
    source_candidate_id: str | None
    committed: bool
    reason: str
    receipt: CommitReceiptV2 | None = None
    stage: str = "live"
    permission_version: str | None = None
    details: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "stage": self.stage,
            "round_id": self.round_id,
            "candidate_id": self.candidate_id,
            "source_candidate_id": self.source_candidate_id,
            "permission_version": self.permission_version,
            "details": dict(self.details),
            "committed": self.committed,
            "receipt": self.receipt,
        }


def _authority(value: Any) -> tuple[str, str, bool, int]:
    if value is None:
        raise LangchaoLiveValidationError("scope has no active authority")
    revision_object = getattr(value, "revision", None)
    if revision_object is not None and not isinstance(revision_object, int):
        value = revision_object
    def field(name: str) -> Any:
        return value[name] if isinstance(value, Mapping) else getattr(value, name)
    engine = field("engine_key")
    mode = field("mode")
    return (
        engine.value if isinstance(engine, AuthorityEngine) else str(engine),
        mode.value if isinstance(mode, AuthorityMode) else str(mode),
        bool(field("may_dispatch")),
        int(field("revision")),
    )


def _digest(candidate: Any) -> str:
    encoded = json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class LangchaoLiveService:
    """Validate one numerical decision and cross the legacy transaction boundary."""

    def __init__(
        self, *, scope_key: str, authority_reader: Any, legacy_bridge: Any,
        contract_repository: Any | None = None,
        witness_reader: Any | None = None,
        permission_reader: Any | None = None,
    ) -> None:
        if not scope_key.strip():
            raise ValueError("scope_key is required")
        self.scope_key = scope_key
        self.authority_reader = authority_reader
        self.legacy = legacy_bridge
        self.contracts = contract_repository
        self.witnesses = WitnessValidator(witness_reader) if witness_reader is not None else None
        self.permission_reader = permission_reader

    def execute_in_transaction(
        self,
        connection: Any,
        *,
        built: BuiltShadowRound,
        decision_candidate_id: str | None,
        assessed_candidates: Mapping[str, CandidateV2],
        now: datetime,
        persist_snapshot: Any | None = None,
    ) -> LangchaoLiveResult | NoSendResult:
        """Commit at most one exact external candidate; rest/defer never sends."""

        engine, mode, may_dispatch, _revision = _authority(self.authority_reader.get_active())
        if (engine, mode, may_dispatch) != (
            AuthorityEngine.LANGCHAO.value, AuthorityMode.LIVE.value, True,
        ):
            raise LangchaoLiveValidationError("active authority is not langchao/live")
        if built.state.scope_key != self.scope_key:
            raise LangchaoLiveValidationError("built round scope mismatch")
        round_id = built.state.decision_round_id
        if decision_candidate_id is None:
            return LangchaoLiveResult(round_id, None, None, False, "deferred")
        if decision_candidate_id not in built.state.working_set:
            raise LangchaoLiveValidationError("decision candidate is outside working set")
        matches = [item for item in built.contracts
                   if item.candidate.candidate_id == decision_candidate_id]
        if len(matches) != 1:
            raise LangchaoLiveValidationError("decision candidate has no unique built contract")
        item = matches[0]
        self._validate_contract(item, built=built, now=now)
        if item.candidate.kind is CandidateKind.DEFER_OR_REST:
            return LangchaoLiveResult(
                round_id, decision_candidate_id, item.source_candidate_id, False, "internal_rest"
            )
        if item.candidate.kind is CandidateKind.INTERNAL_PROCESS:
            # Initial live support is deliberately assessment-only.  The work segment
            # remains an internal candidate; it never crosses the legacy send boundary.
            return LangchaoLiveResult(
                round_id, decision_candidate_id, item.source_candidate_id, False, "internal_candidate"
            )
        permission = self._current_permission(now)
        if permission is not None and not permission.allowed:
            return NoSendResult(reason=NoSendReason.PERMISSION_REVOKED, stage="permission",
                round_id=round_id, candidate_id=decision_candidate_id,
                permission_version=permission.permission_version)
        if permission is not None and (
                permission.permission_version != built.state.permission_version
                or item.candidate.permission_ref != permission.permission_version):
            return NoSendResult(reason=NoSendReason.INVALIDATED, stage="permission",
                round_id=round_id, candidate_id=decision_candidate_id,
                permission_version=permission.permission_version,
                details=(("expected_permission_version", built.state.permission_version),))
        source = assessed_candidates.get(item.source_candidate_id)
        if source is None or source.candidate_id != item.provenance.candidate_id:
            raise LangchaoLiveValidationError("legacy candidate provenance mismatch")
        boundary = self.legacy.boundary_verdict(
            candidate=source, scope_key=self.scope_key, now=now
        )
        if boundary.blocked:
            return NoSendResult(reason=NoSendReason.PERMISSION_REVOKED, stage="permission",
                round_id=round_id, candidate_id=decision_candidate_id,
                permission_version=(built.state.permission_version if permission is None
                                    else permission.permission_version),
                details=tuple(("boundary", str(reason)) for reason in boundary.reasons))
        if persist_snapshot is None:
            raise LangchaoLiveValidationError("live dispatch requires durable snapshot persistence")

        def persist(receipt: CommitReceiptV2) -> Any:
            if not receipt.dispatch_claim_id:
                raise LangchaoLiveValidationError("live dispatch receipt has no claim identity")
            snapshot = LangchaoLiveCommit(
                scope_key=self.scope_key,
                round_id=round_id,
                langchao_candidate_id=item.candidate.candidate_id,
                candidate_revision=item.candidate.semantic_revision,
                source_candidate_id=item.source_candidate_id,
                reward_contract_id=item.reward.reward_contract_id,
                reward_revision=item.reward.revision,
                expected_tokens=item.reward.outcome_tokens,
                attempt_id=receipt.attempt_id,
                render_outbox_id=receipt.render_outbox_id,
                claim_id=receipt.dispatch_claim_id,
                committed_at=now,
            )
            return persist_snapshot(snapshot)

        receipt = self.legacy.commit_langchao_candidate_in_transaction(
            connection,
            round_id=round_id,
            langchao_candidate_id=item.candidate.candidate_id,
            candidate_revision=item.candidate.semantic_revision,
            candidate_version=_digest(item.candidate),
            source_candidate=source,
            reward_contract_id=item.reward.reward_contract_id,
            reward_revision=item.reward.revision,
            now=now,
            persist_snapshot=persist,
        )
        return LangchaoLiveResult(
            round_id, decision_candidate_id, item.source_candidate_id, True, "committed", receipt
        )

    def _current_permission(self, now: datetime) -> PermissionProjection | None:
        if self.permission_reader is not None:
            projection = self.permission_reader(now=now)
            if projection is not None and not isinstance(projection, PermissionProjection):
                raise TypeError("permission_reader must return PermissionProjection or None")
            return projection
        runtime = getattr(self.legacy, "runtime", None)
        if runtime is None:
            return None
        return read_runtime_permission(runtime, scope_key=self.scope_key, now=now)

    def _validate_contract(
        self, item: BuiltCandidateContracts, *, built: BuiltShadowRound, now: datetime
    ) -> None:
        candidate = item.candidate
        if candidate.scope_key != self.scope_key:
            raise LangchaoLiveValidationError("candidate scope mismatch")
        if candidate.state is not CandidateState.COMPETITIVE:
            raise LangchaoLiveValidationError("candidate is not competitive")
        if candidate.available_from > now or (
            candidate.expires_at is not None and now >= candidate.expires_at
        ):
            raise LangchaoLiveValidationError("candidate is outside availability boundaries")
        if candidate.based_on_state_version != built.state.based_on_state_version:
            raise LangchaoLiveValidationError("candidate state-version boundary is stale")
        if self.contracts is not None:
            exact = (
                (self.contracts.get_active_goal(goal_id=item.goal.goal_id), item.goal.revision),
                (self.contracts.get_active_reward(
                    reward_contract_id=item.reward.reward_contract_id
                ), item.reward.revision),
                (self.contracts.get_active_candidate(
                    candidate_id=candidate.candidate_id
                ), candidate.semantic_revision),
            )
            for row, revision in exact:
                if row is None:
                    raise LangchaoLiveValidationError("required contract is not active")
                actual = row["revision"] if isinstance(row, Mapping) else row[1]
                if int(actual) != revision:
                    raise LangchaoLiveValidationError("active contract revision is stale")
        if item.goal.goal_id not in candidate.goal_refs:
            raise LangchaoLiveValidationError("candidate goal reference mismatch")
        if candidate.reward_contract_ref != item.reward.reward_contract_id:
            raise LangchaoLiveValidationError("candidate reward reference mismatch")
        expected = tuple(token.token_id for token in item.reward.outcome_tokens)
        if candidate.expected_outcome_token_ids != expected:
            raise LangchaoLiveValidationError("candidate outcome-token envelope mismatch")
        if candidate.kind is CandidateKind.EXTERNAL_MESSAGE:
            if not candidate.permission_ref:
                raise LangchaoLiveValidationError("external candidate has no permission reference")
            if not candidate.input_refs or not item.shadow_input.source_refs:
                raise LangchaoLiveValidationError("external candidate has no source references")
            # The fixed runtime adapter uses this explicit capability marker.  Older
            # persisted contracts without it are assessment-only and cannot go live.
            if "external_message" not in candidate.capability_refs:
                raise LangchaoLiveValidationError("external-message capability is absent")
        requirement = candidate_witness_requirement(candidate, scope_key=self.scope_key)
        if requirement is not None:
            if self.witnesses is None:
                raise LangchaoLiveValidationError("operation candidate has no witness validator")
            try:
                self.witnesses.validate(requirement)
            except RuntimeError as exc:
                raise LangchaoLiveValidationError(str(exc)) from exc


__all__ = [
    "LangchaoLiveResult", "LangchaoLiveService", "LangchaoLiveValidationError",
]
