"""Isolated black-box acceptance for the Langchao live path.

No server, socket, provider, model, or platform sender is constructed.  The render result
is a fixed artifact and the bridge only records durable component calls in memory.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from companion_runtime.langchao_authority import AuthorityEngine, AuthorityMode
from companion_runtime.langchao_engine import LangchaoParameters, advance_langchao
from companion_runtime.langchao_live import LangchaoLiveService, LangchaoLiveValidationError
from companion_runtime.langchao_live_repository import LangchaoLiveRepository
from companion_runtime.langchao_live_wiring import AuthorityRoutedEndogenousRound, LangchaoLiveRunner
from companion_runtime.langchao_runtime_adapter import BuiltCandidateContracts, BuiltShadowRound, LegacyProvenance
from companion_runtime.langchao_types import CandidateKind, OutcomeStatus
from companion_runtime.runtime_v2 import BoundaryVerdictV2, CandidateV2, CommitReceiptV2
from test_langchao_live_repository import Connection as AckConnection
from test_langchao_shadow import candidate_input, state, weights

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SCOPE = "user:isolated-blackbox"
FIXED_RENDER_ARTIFACT = {
    "artifact_id": "render:fixed:v1",
    "content_type": "text/plain",
    "sha256": "4d04c6d55a2f82ea1adf9e108128378904a5824693bf38f05c4a8f7d8df0f51",
    "text": "固定渲染：仅用于隔离验收，不会发送。",
}


class Authority:
    def get_active(self):
        return SimpleNamespace(
            engine_key=AuthorityEngine.LANGCHAO,
            mode=AuthorityMode.LIVE,
            may_dispatch=True,
            revision=1,
        )


class ThresholdEvaluator:
    """Numerically crosses the threshold, then invokes the real live callback."""

    def __init__(self, built: BuiltShadowRound, connection: AckConnection):
        self.built = built
        self.connection = connection
        self.crossed = False

    def run(self, _assessment, *, now, before_commit):
        parameters = LangchaoParameters(
            leak=0.0, competition_gain=0.0, decision_threshold=0.5,
            time_scale_seconds=1.0, max_step_seconds=0.25,
            crossing_tolerance=1e-8, tie_tolerance=1e-8,
        )
        advanced = advance_langchao(
            self.built.state, until=now + timedelta(seconds=2), parameters=parameters
        )
        assert advanced.decision_candidate_id is not None
        self.crossed = True
        result = SimpleNamespace(candidate_id=advanced.decision_candidate_id)
        before_commit(result, self.built, self.connection)
        return result


class FakeBridge:
    """Pure component double: fixed render artifact, no transport/send capability."""

    def __init__(self, *, revoked: bool = False):
        self.revoked = revoked
        self.commit_calls = []
        self.artifacts = []

    def boundary_verdict(self, **_kwargs):
        return BoundaryVerdictV2(blocked=self.revoked, reasons=("permission_revoked",) if self.revoked else ())

    def commit_langchao_candidate_in_transaction(self, connection, *, persist_snapshot, **kwargs):
        index = len(self.commit_calls) + 1
        receipt = CommitReceiptV2(
            decision_id=kwargs["round_id"], candidate_id=kwargs["source_candidate"].candidate_id,
            attempt_id=f"attempt:{index}", render_outbox_id=f"render:{index}",
            dispatch_claim_id=f"claim:{index}",
        )
        self.commit_calls.append(kwargs)
        self.artifacts.append(FIXED_RENDER_ARTIFACT)
        persist_snapshot(receipt)
        return receipt


class AssessmentCoordinator:
    def __init__(self, candidate):
        self.assessment = SimpleNamespace(assessments=(SimpleNamespace(candidate=candidate),))
        self.assess_calls = 0
        self.decide_calls = 0

    def assess_endogenous(self, **_kwargs):
        self.assess_calls += 1
        return self.assessment

    def decide_endogenous(self, **_kwargs):
        self.decide_calls += 1
        raise AssertionError("runtime-v2 commit path must not run under langchao/live")


def _built(*, rest: bool = False) -> tuple[BuiltShadowRound, CandidateV2]:
    source = candidate_input()
    candidate = replace(
        source.candidate,
        scope_key=SCOPE,
        kind=CandidateKind.DEFER_OR_REST if rest else CandidateKind.EXTERNAL_MESSAGE,
        capability_refs=() if rest else ("external_message",),
        permission_ref="permission:fixture:rest" if rest else "permission:fixture:live",
        input_refs=("event:fixture",),
        based_on_state_version=7,
    )
    goal = replace(source.goal, scope_key=SCOPE, allowed_candidate_kinds=(candidate.kind,))
    tokens = tuple(replace(token, scope_key=SCOPE) for token in source.reward.outcome_tokens)
    reward = replace(source.reward, scope_key=SCOPE, outcome_tokens=tokens)
    candidate = replace(
        candidate, goal_refs=(goal.goal_id,), reward_contract_ref=reward.reward_contract_id,
        expected_outcome_token_ids=tuple(token.token_id for token in tokens),
    )
    shadow_input = replace(source, goal=goal, reward=reward, candidate=candidate, source_refs=("event:fixture",))
    external = CandidateV2(
        candidate_id="legacy:contact", action={"type": "contact"}, internal_utility=10.0,
        coefficients=SimpleNamespace(v_reply=1.0, v_continue=1.0, c_negative=1.0),
        repeat_subject=SimpleNamespace(), policy=SimpleNamespace(), source_event_ids=("event:fixture",),
    )
    contract = BuiltCandidateContracts(
        source_candidate_id=external.candidate_id, initial_goal=goal, reward=reward, goal=goal,
        candidate=candidate, shadow_input=shadow_input,
        provenance=LegacyProvenance(
            candidate_id=external.candidate_id, internal_utility=10.0, internal_need=1.0,
            relevance=1.0, coefficients=(), assessment_utility_terms=(),
        ),
    )
    round_state = replace(
        state(), scope_key=SCOPE, decision_round_id=f"round:{'rest' if rest else 'contact'}",
        working_set=(candidate.candidate_id,), readiness=((candidate.candidate_id, 0.0),),
        attraction=((candidate.candidate_id, 1.0),), attention=weights(),
    )
    built = BuiltShadowRound(
        inputs=(shadow_input,), contracts=(contract,), state=round_state,
        value_profile=SimpleNamespace(), attention_profile=SimpleNamespace(), dropped=(), id_mapping=(),
        goal_snapshot_version="g", reward_snapshot_version="r",
        candidate_snapshot_version="c", prediction_snapshot_version="p",
    )
    return built, external


def _stack(*, rest: bool = False, revoked: bool = False):
    built, source = _built(rest=rest)
    connection = AckConnection()
    repository = LangchaoLiveRepository(connection, scope_key=SCOPE)
    writes, activations = [], []
    repository.outcomes = SimpleNamespace(
        put_outcome_revision=lambda outcome, **kw: writes.append((outcome, kw)),
        activate_outcome=lambda **kw: activations.append(kw) or True,
    )
    bridge = FakeBridge(revoked=revoked)
    evaluator = ThresholdEvaluator(built, connection)
    runner = LangchaoLiveRunner(
        evaluator=evaluator,
        service=LangchaoLiveService(scope_key=SCOPE, authority_reader=Authority(), legacy_bridge=bridge),
        repository=repository,
    )
    coordinator = AssessmentCoordinator(source)
    router = AuthorityRoutedEndogenousRound(
        scope_key=SCOPE, v2_coordinator=coordinator, authority_reader=Authority(),
        langchao_live_runner=runner, live_enabled=True, live_scope_allowlist=(SCOPE,),
    )
    return router, runner, repository, connection, bridge, evaluator, coordinator, writes, activations


def test_external_contact_crosses_threshold_through_router_validator_bridge_and_ack_repository():
    router, runner, repository, _connection, bridge, evaluator, coordinator, writes, activations = _stack()
    result = router.run(decision_id="tick:contact", now=NOW, elapsed_allowed_seconds=2.0)
    assert evaluator.crossed and result.committed
    assert coordinator.assess_calls == 1 and coordinator.decide_calls == 0
    assert len(bridge.commit_calls) == 1 and bridge.artifacts == [FIXED_RENDER_ARTIFACT]
    pending = runner.recover_pending()
    assert len(pending) == 1 and pending[0].attempt_id == result.receipt.attempt_id
    ack = SimpleNamespace(
        decision_id=result.round_id, attempt_id=result.receipt.attempt_id,
        send_outbox_id="ack:success", sent=True, acknowledged_at=NOW + timedelta(seconds=3),
    )
    settled = runner.after_legacy_send_ack(ack, confirmed=True)
    assert [token.outcome_key for token in settled] == []  # fixture reward has no delivery token
    assert repository.pending() == () and writes == [] and activations == []


def test_rest_crosses_threshold_but_never_calls_bridge_or_creates_pending_send():
    router, _runner, repository, _connection, bridge, evaluator, *_ = _stack(rest=True)
    result = router.run(decision_id="tick:rest", now=NOW, elapsed_allowed_seconds=2.0)
    assert evaluator.crossed and result.reason == "internal_rest" and not result.committed
    assert bridge.commit_calls == [] and bridge.artifacts == [] and repository.pending() == ()


def test_revoked_candidate_creates_no_backlog_and_reenable_requires_a_fresh_round():
    router, _runner, repository, _connection, bridge, evaluator, *_ = _stack(revoked=True)
    with pytest.raises(LangchaoLiveValidationError, match="boundary blocks"):
        router.run(decision_id="tick:revoked", now=NOW, elapsed_allowed_seconds=2.0)
    assert evaluator.crossed and bridge.commit_calls == [] and repository.pending() == ()

    fresh_router, _fresh_runner, fresh_repository, _c, fresh_bridge, *_ = _stack(revoked=False)
    result = fresh_router.run(decision_id="tick:fresh", now=NOW + timedelta(seconds=5), elapsed_allowed_seconds=2.0)
    assert result.committed and len(fresh_bridge.commit_calls) == 1
    assert len(fresh_repository.pending()) == 1


def test_failed_ack_is_censored_not_negative_and_restart_recovers_pending():
    router, runner, repository, connection, _bridge, _evaluator, _coordinator, writes, activations = _stack()
    result = router.run(decision_id="tick:failure", now=NOW, elapsed_allowed_seconds=2.0)

    restarted = LangchaoLiveRepository(connection, scope_key=SCOPE)
    restarted.outcomes = repository.outcomes
    pending = restarted.pending()
    assert len(pending) == 1 and pending[0].round_id == result.round_id

    ack = SimpleNamespace(
        decision_id=result.round_id, attempt_id=result.receipt.attempt_id,
        send_outbox_id="ack:failed", sent=False, acknowledged_at=NOW + timedelta(seconds=4),
    )
    settled = LangchaoLiveRunner(
        evaluator=runner.evaluator, service=runner.service, repository=restarted
    ).after_legacy_send_ack(ack, confirmed=True)
    assert all(token.status is OutcomeStatus.CENSORED for token in settled)
    assert all(token.outcome_key not in {"negative", "reply", "continuation"} for token in settled)
    assert all(item[0].status is not OutcomeStatus.CONFIRMED for item in writes)
    assert restarted.pending() == ()
    assert len(writes) == len(activations) == len(settled)
