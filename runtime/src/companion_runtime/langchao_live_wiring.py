"""Authority-first scheduler routing for Runtime-v2 and 「浪潮」 live modes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .langchao_authority import AuthorityEngine, AuthorityMode
from .langchao_authority_repository import LangchaoAuthorityRepository
from .langchao_live import LangchaoLiveService
from .langchao_live_repository import LangchaoLiveRepository
from .langchao_repository import LangchaoRepository
from .langchao_shadow_wiring import build_langchao_shadow_runner


def _active_authority(value: Any) -> tuple[str, str, bool, int | None]:
    if value is None:
        return AuthorityEngine.NONE.value, AuthorityMode.DISABLED.value, False, None
    nested = getattr(value, "revision", None)
    if nested is not None and not isinstance(nested, int):
        value = nested

    def field(name: str, default: Any = ...) -> Any:
        if isinstance(value, Mapping):
            if default is ...:
                return value[name]
            return value.get(name, default)
        if default is ...:
            return getattr(value, name)
        return getattr(value, name, default)

    engine, mode = field("engine_key"), field("mode")
    revision = field("revision", None)
    return (
        engine.value if isinstance(engine, AuthorityEngine) else str(engine),
        mode.value if isinstance(mode, AuthorityMode) else str(mode),
        bool(field("may_dispatch")),
        int(revision) if revision is not None else None,
    )


def active_authority_coordinates(value: Any) -> tuple[str, str, bool]:
    engine, mode, may_dispatch, _revision = _active_authority(value)
    return engine, mode, may_dispatch


@dataclass(slots=True)
class LangchaoLiveRunner:
    """Reuse fixed-contract evaluation, then dispatch only its decided winner."""

    evaluator: Any
    service: LangchaoLiveService
    repository: LangchaoLiveRepository
    exposure_repository: Any | None = None
    user_model: Any | None = None
    horizons: Any | None = None

    def run(self, assessment: Any, *, now: datetime) -> Any:
        sources = {
            item.candidate.candidate_id: item.candidate
            for item in assessment.assessments
        }
        live_result: Any | None = None

        def commit(result: Any, built: Any, connection: Any) -> None:
            nonlocal live_result
            candidate_id = getattr(result, "candidate_id", None)
            if candidate_id is None and hasattr(result, "payload"):
                candidate_id = result.payload.get("candidate_id")
            live_result = self.service.execute_in_transaction(
                connection,
                built=built, decision_candidate_id=candidate_id,
                assessed_candidates=sources, now=now,
                persist_snapshot=self.repository.save_commit,
            )

        evaluated = self.evaluator.run(assessment, now=now, before_commit=commit)
        return evaluated if live_result is None else live_result

    def after_legacy_rendered(self, *, decision_id: str, outbox_id: str, now: datetime) -> None:
        """Rendering has no reward effect; the durable commit already names its outbox."""
        del decision_id, outbox_id, now

    def after_legacy_send_ack(self, ack: Any, *, confirmed: bool = True) -> tuple[Any, ...]:
        """Settle delivery and create the same exposure/labels as Runtime-v2."""
        sent = bool(ack.sent and confirmed)
        settled = self.repository.settle_terminal(
            round_id=ack.decision_id,
            attempt_id=ack.attempt_id,
            ack_id=ack.send_outbox_id,
            sent=sent,
            acknowledged_at=ack.acknowledged_at,
        )
        if sent and self.exposure_repository is not None and self.user_model is not None:
            commit = self.repository.get(ack.decision_id)
            if commit is None:
                raise RuntimeError("sent Langchao acknowledgement has no durable commit")
            self.exposure_repository.prepare_exposure_and_expectation(
                user_model=self.user_model,
                scope_key=commit.scope_key,
                exposure_id=ack.attempt_id,
                idempotency_key=f"send-ack:{ack.send_outbox_id}",
                occurred_at=ack.acknowledged_at,
                action=ack.action,
                context_provider=ack.context_provider,
                horizons=self.horizons,
                delivery_basis=getattr(ack, "delivery_basis", None) or __import__(
                    "companion_runtime.user_model_v2_types", fromlist=["DeliveryBasis"]
                ).DeliveryBasis.DELIVERED,
                source_event_ids=tuple(getattr(ack, "source_event_ids", ())),
            )
        return settled

    def recover_pending(self) -> tuple[Any, ...]:
        """Expose durable pending commits for startup/recovery diagnostics."""
        return self.repository.pending()


def build_langchao_live_runner(
    *, connection: Any, scope_key: str, runtime: Any, legacy_bridge: Any,
    witness_reader: Any | None = None,
    attention_recipe: str = "off", internal_exploration_enabled: bool = True,
) -> LangchaoLiveRunner:
    evaluator = build_langchao_shadow_runner(
        connection=connection, scope_key=scope_key, runtime=runtime,
        allow_live_evaluation=True,
        transaction_factory=runtime.db.transaction,
        attention_recipe=attention_recipe,
        internal_exploration_enabled=internal_exploration_enabled,
    )
    authority = LangchaoAuthorityRepository(connection, scope_key=scope_key)
    contracts = LangchaoRepository(connection, scope_key=scope_key)
    service = LangchaoLiveService(
        scope_key=scope_key, authority_reader=authority,
        legacy_bridge=legacy_bridge, contract_repository=contracts,
        witness_reader=witness_reader,
    )
    live_repository = LangchaoLiveRepository(connection, scope_key=scope_key)
    return LangchaoLiveRunner(
        evaluator=evaluator, service=service, repository=live_repository
    )


@dataclass(slots=True)
class AuthorityRoutedEndogenousRound:
    """Split scheduler evaluation from dispatch before either engine may commit."""

    scope_key: str
    v2_coordinator: Any
    authority_reader: Any
    langchao_live_runner: Any | None = None
    langchao_shadow_runner: Any | None = None
    live_enabled: bool = False
    live_scope_allowlist: tuple[str, ...] = ()

    def run(self, *, decision_id: str, now: datetime, elapsed_allowed_seconds: float) -> Any:
        active = self.authority_reader.get_active()
        engine, mode, may_dispatch, authority_revision = _active_authority(active)
        if (engine, mode, may_dispatch) == (
            AuthorityEngine.RUNTIME_V2.value, AuthorityMode.LIVE.value, True,
        ):
            decision = self.v2_coordinator.decide_endogenous(
                decision_id=decision_id, now=now,
                elapsed_allowed_seconds=elapsed_allowed_seconds,
            )
            if self.langchao_shadow_runner is not None:
                self.langchao_shadow_runner.run(
                    decision, now=now, authority_revision=authority_revision,
                )
            return decision
        if (engine, mode, may_dispatch) == (
            AuthorityEngine.LANGCHAO.value, AuthorityMode.LIVE.value, True,
        ):
            # Comparator first, with a mechanically side-effect-free coordinator path.
            assessment = self.v2_coordinator.assess_endogenous(
                decision_id=decision_id, now=now,
                elapsed_allowed_seconds=elapsed_allowed_seconds,
            )
            if not self.live_enabled or self.scope_key not in self.live_scope_allowlist:
                return assessment
            if self.langchao_live_runner is None:
                raise RuntimeError("langchao/live authority has no configured live runner")
            return self.langchao_live_runner.run(assessment, now=now)
        # Shadow authority evaluates the same pure v2 assessment, then persists only
        # Langchao contracts/state/audit.  It must never enter v2 decide/commit.
        if (engine, mode, may_dispatch) == (
            AuthorityEngine.LANGCHAO.value, AuthorityMode.SHADOW.value, False,
        ):
            assessment = self.v2_coordinator.assess_endogenous(
                decision_id=decision_id, now=now,
                elapsed_allowed_seconds=elapsed_allowed_seconds,
            )
            if self.langchao_shadow_runner is None:
                raise RuntimeError("langchao/shadow authority has no configured shadow runner")
            return self.langchao_shadow_runner.run(
                assessment, now=now, authority_revision=authority_revision,
            )
        # none, disabled, malformed, and dispatch-ineligible live authorities only
        # assess. Do not run an engine merely for comparison because decide owns
        # hazard/commit and shadow owns durable audit writes.
        assessment = self.v2_coordinator.assess_endogenous(
            decision_id=decision_id, now=now,
            elapsed_allowed_seconds=elapsed_allowed_seconds,
        )
        return assessment


__all__ = ["AuthorityRoutedEndogenousRound", "active_authority_coordinates"]
