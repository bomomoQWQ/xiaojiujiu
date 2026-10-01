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


def active_authority_coordinates(value: Any) -> tuple[str, str, bool]:
    if value is None:
        return AuthorityEngine.NONE.value, AuthorityMode.DISABLED.value, False
    nested = getattr(value, "revision", None)
    if nested is not None and not isinstance(nested, int):
        value = nested
    def field(name: str) -> Any:
        return value[name] if isinstance(value, Mapping) else getattr(value, name)
    engine, mode = field("engine_key"), field("mode")
    return (
        engine.value if isinstance(engine, AuthorityEngine) else str(engine),
        mode.value if isinstance(mode, AuthorityMode) else str(mode),
        bool(field("may_dispatch")),
    )


@dataclass(slots=True)
class LangchaoLiveRunner:
    """Reuse fixed-contract evaluation, then dispatch only its decided winner."""

    evaluator: Any
    service: LangchaoLiveService
    repository: LangchaoLiveRepository

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
        """Settle one terminal send result, recovering the snapshot after restart."""
        return self.repository.settle_terminal(
            round_id=ack.decision_id,
            attempt_id=ack.attempt_id,
            ack_id=ack.send_outbox_id,
            sent=bool(ack.sent and confirmed),
            acknowledged_at=ack.acknowledged_at,
        )

    def recover_pending(self) -> tuple[Any, ...]:
        """Expose durable pending commits for startup/recovery diagnostics."""
        return self.repository.pending()


def build_langchao_live_runner(
    *, connection: Any, scope_key: str, runtime: Any, legacy_bridge: Any,
) -> LangchaoLiveRunner:
    evaluator = build_langchao_shadow_runner(
        connection=connection, scope_key=scope_key, runtime=runtime,
        allow_live_evaluation=True,
        transaction_factory=runtime.db.transaction,
    )
    authority = LangchaoAuthorityRepository(connection, scope_key=scope_key)
    contracts = LangchaoRepository(connection, scope_key=scope_key)
    service = LangchaoLiveService(
        scope_key=scope_key, authority_reader=authority,
        legacy_bridge=legacy_bridge, contract_repository=contracts,
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
        engine, mode, may_dispatch = active_authority_coordinates(
            self.authority_reader.get_active()
        )
        if (engine, mode, may_dispatch) == (
            AuthorityEngine.RUNTIME_V2.value, AuthorityMode.LIVE.value, True,
        ):
            decision = self.v2_coordinator.decide_endogenous(
                decision_id=decision_id, now=now,
                elapsed_allowed_seconds=elapsed_allowed_seconds,
            )
            if self.langchao_shadow_runner is not None:
                self.langchao_shadow_runner.run(decision, now=now)
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
        # shadow, none, and disabled authorities cannot commit.  Do not run a baseline
        # v2 decision merely for comparison because that path owns hazard and commit.
        return self.v2_coordinator.assess_endogenous(
            decision_id=decision_id, now=now,
            elapsed_allowed_seconds=elapsed_allowed_seconds,
        )


__all__ = ["AuthorityRoutedEndogenousRound", "active_authority_coordinates"]
