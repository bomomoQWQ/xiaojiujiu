"""Tests for the minimal 「浪潮」 v16 shadow coordinator and audit schema."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import pytest

from companion_runtime.langchao_engine import LangchaoParameters
from companion_runtime.langchao_reward import AttentionProfile, CandidateCostTerm, OutcomeForecast, ValueProfile
from companion_runtime.langchao_shadow import (
    FakeLangchaoShadowRepository, LangchaoShadowPostgresRepository,
    ShadowCandidateInput, StoredShadowRun, run_langchao_shadow,
)
from companion_runtime.langchao_shadow_schema import LANGCHAO_SHADOW_SCHEMA_V16_STATEMENTS
from companion_runtime.user_model_v2_migrations import migration_records
from companion_runtime.user_model_v2_schema import MIGRATIONS, USER_MODEL_SCHEMA_VERSION, schema_statements
from companion_runtime.langchao_types import (
    ActionCandidateContract, CandidateKind, CandidateState, GoalContract, GoalKind,
    GoalOwnership, GoalStatus, LangchaoState, MotivationDirection, OutcomeStatus,
    OutcomeToken, RewardContract, SettlementType,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SCOPE = "user:shadow"


def weights():
    return tuple((direction, 1.0) for direction in MotivationDirection)


def candidate_input(*, probability: float | None = 1.0, cost: float = 0.0) -> ShadowCandidateInput:
    goal = GoalContract(
        goal_id="goal:1", scope_key=SCOPE, episode_id="episode:1", semantic_key="goal:semantic",
        kind=GoalKind.FINITE, ownership=GoalOwnership.USER_REQUEST, desired_change="help",
        status=GoalStatus.ACTIONABLE, evidence_refs=(), excluded_outcomes=(),
        completion_outcome_keys=("done",), allowed_candidate_kinds=(CandidateKind.EXTERNAL_MESSAGE,),
        created_at=NOW, updated_at=NOW,
    )
    token = OutcomeToken(
        token_id="token:1", scope_key=SCOPE, goal_id=goal.goal_id, episode_id=goal.episode_id,
        outcome_key="done", settlement_type=SettlementType.EXPECTED, status=OutcomeStatus.UNEXECUTED,
        base_amount=10.0, direction_weights=((MotivationDirection.CARE, 1.0),),
        evidence_version="evidence:1", idempotency_key="token-idem:1",
    )
    reward = RewardContract(
        reward_contract_id="reward:1", scope_key=SCOPE, goal_id=goal.goal_id,
        episode_id=goal.episode_id, template_key="help:v1", unit="utility", outcome_tokens=(token,),
        total_cap=10.0, overlap_group="help", created_at=NOW, updated_at=NOW,
    )
    candidate = ActionCandidateContract(
        candidate_id="candidate:1", scope_key=SCOPE, semantic_key="candidate:semantic",
        goal_refs=(goal.goal_id,), kind=CandidateKind.EXTERNAL_MESSAGE, action_template="reply",
        input_refs=("event:1",), reward_contract_ref=reward.reward_contract_id,
        expected_outcome_token_ids=(token.token_id,), capability_refs=("text",),
        permission_ref="permission:1", precondition_refs=(), invalidation_refs=(), envelope=(),
        state=CandidateState.COMPETITIVE, available_from=NOW, expires_at=None, resource_budget=1.0,
        based_on_state_version=7, created_at=NOW, updated_at=NOW,
    )
    costs = () if cost == 0 else (CandidateCostTerm(kind="effort", amount=cost),)
    return ShadowCandidateInput(
        goal=goal, reward=reward, candidate=candidate,
        forecasts=(OutcomeForecast(token_id=token.token_id, probability=probability,
            support="predictor", status="known" if probability is not None else "unknown",
            source_version="prediction:1"),), costs=costs,
    )


def state() -> LangchaoState:
    return LangchaoState(
        scope_key=SCOPE, decision_round_id="round:1", working_set=("candidate:1",),
        readiness=(("candidate:1", 0.0),), attraction=(("candidate:1", 0.0),), attention=weights(),
        advanced_at=NOW, based_on_state_version=7, event_cursor="event:1",
        goal_snapshot_version="goals:1", reward_snapshot_version="rewards:1",
        candidate_snapshot_version="candidates:1", prediction_snapshot_version="predictions:1",
        value_profile_version="values:old", attention_version="attention:old",
        parameter_version="parameters:old", permission_version="permissions:1",
    )


def parameters() -> LangchaoParameters:
    return LangchaoParameters(
        leak=0.0, competition_gain=0.0, decision_threshold=0.5, time_scale_seconds=1.0,
        max_step_seconds=10.0, crossing_tolerance=1e-7, tie_tolerance=1e-7,
    )


def run(repository, **changes):
    args = {
        "repository": repository, "run_id": "run:1", "idempotency_key": "idem:1",
        "source_input_cursor": "event:1", "source_input_version": "source:v1",
        "inputs": (candidate_input(),),
        "value_profile": ValueProfile(version="values:1", direction_weights=weights(), total_weight=8.0),
        "attention_profile": AttentionProfile(version="attention:1", direction_weights=weights()),
        "previous_state": state(), "until": NOW + timedelta(seconds=10),
        "parameters": parameters(), "utility_scale": 10.0,
        "baseline_candidate_id": "baseline:choice",
    }
    args.update(changes)
    return run_langchao_shadow(**args)


def test_selected_candidate_remains_zero_side_effect_and_comparison_is_data_only():
    repository = FakeLangchaoShadowRepository()
    result = run(repository)
    assert result.candidate_id == "candidate:1"
    assert result.mode == "shadow"
    assert (result.sent_count, result.reward_count, result.training_count, result.quota_count) == (0, 0, 0, 0)
    assert result.outbox_id is None
    assert result.comparison.baseline_candidate_id == "baseline:choice"
    assert result.comparison.same_candidate is False
    assert len(repository.states) == len(repository.runs) == 1
    assert not any(hasattr(repository, name) for name in ("send", "claim_reward", "train", "consume_quota"))


def test_exception_rolls_back_state_and_audit():
    repository = FakeLangchaoShadowRepository(fail_on="audit")
    with pytest.raises(RuntimeError, match="audit"):
        run(repository)
    assert repository.states == []
    assert repository.runs == {}


def test_idempotency_returns_original_without_second_state_revision():
    repository = FakeLangchaoShadowRepository()
    first = run(repository)
    second = run(repository)
    assert second is first
    assert len(repository.states) == len(repository.runs) == 1


def test_same_idempotency_with_different_run_or_input_is_hard_conflict_without_state_write():
    repository = FakeLangchaoShadowRepository()
    run(repository)
    with pytest.raises(RuntimeError, match="idempotency conflict"):
        run(repository, run_id="run:different")
    with pytest.raises(RuntimeError, match="idempotency conflict"):
        run(repository, until=NOW + timedelta(seconds=20))
    assert len(repository.states) == len(repository.runs) == 1


def test_same_semantic_input_has_same_hash_even_when_input_tuple_order_is_irrelevant():
    one = run(FakeLangchaoShadowRepository())
    two = run(FakeLangchaoShadowRepository(), run_id="run:2", idempotency_key="idem:2")
    assert one.input_sha256 == two.input_sha256
    assert one.audit_sha256 == two.audit_sha256


def test_unknown_forecast_is_audited_without_becoming_negative_reward():
    result = run(FakeLangchaoShadowRepository(), inputs=(candidate_input(probability=None),))
    compilation = result.compilations[0]
    assert compilation.unknown_outcomes == ("token:1",)
    assert dict(compilation.outcome_expected_values) == {"token:1": None}
    assert compilation.attraction == 0.0
    assert result.candidate_id is None


def test_decision_budget_defers_without_selection():
    result = run(
        FakeLangchaoShadowRepository(), decision_budget_seconds=0.01,
        until=NOW + timedelta(seconds=10),
    )
    assert result.candidate_id is None
    assert result.defer_reason == "decision_budget_exhausted"


def test_source_has_no_execution_or_learning_write_surface():
    source = Path(__file__).parents[1] / "src" / "companion_runtime" / "langchao_shadow.py"
    text = source.read_text(encoding="utf-8").lower()
    forbidden = (
        "put_outbox", "insert_outbox", "write_exposure", "put_exposure",
        "write_label", "put_label", "repeat_repository", "training_repository",
    )
    assert all(item not in text for item in forbidden)
    protocol = text[text.index("class langchaoshadowrepository"):text.index("class langchaoshadowpostgresrepository")]
    assert "put_state_revision" in protocol and "put_shadow_run" in protocol
    assert all(word not in protocol for word in ("send", "outbox", "exposure", "label", "reward", "train", "quota"))


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class ExistingAuditConnection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return Cursor(self.row)


class NeverWriteStateRepository:
    def __init__(self):
        self.writes = 0

    def append_advance(self, *args, **kwargs):
        self.writes += 1
        raise AssertionError("idempotent hit must not append state")


def test_postgres_existing_audit_returns_verified_lightweight_result_without_state_write():
    original = run(FakeLangchaoShadowRepository())
    payload = original.to_dict()
    row = {
        "run_id": original.run_id,
        "input_sha256": original.input_sha256,
        "audit_sha256": original.audit_sha256,
        "source_input_cursor": original.source_input_cursor,
        "source_input_version": original.source_input_version,
        "decision_round_id": original.decision_round_id,
        "mode": "shadow",
        "candidate_id": original.candidate_id,
        "defer_reason": original.defer_reason,
        "comparison": payload["comparison"],
        "audit": payload["audit"],
        "sent_count": 0,
        "reward_count": 0,
        "training_count": 0,
        "quota_count": 0,
        "outbox_id": None,
    }
    state_repository = NeverWriteStateRepository()
    connection = ExistingAuditConnection(row)
    repository = LangchaoShadowPostgresRepository(connection, state_repository=state_repository)

    result = run(repository)

    assert isinstance(result, StoredShadowRun)
    assert result.run_id == original.run_id
    assert result.input_sha256 == original.input_sha256
    assert result.audit_sha256 == original.audit_sha256
    assert state_repository.writes == 0
    assert len(connection.calls) == 1
    assert "FROM langchao_shadow_runs" in connection.calls[0][0]


def test_postgres_existing_audit_rejects_hash_mismatch_before_state_write():
    original = run(FakeLangchaoShadowRepository())
    payload = original.to_dict()
    row = {
        "run_id": original.run_id, "input_sha256": original.input_sha256,
        "audit_sha256": "0" * 64, "source_input_cursor": original.source_input_cursor,
        "source_input_version": original.source_input_version,
        "decision_round_id": original.decision_round_id, "mode": "shadow",
        "candidate_id": original.candidate_id, "defer_reason": original.defer_reason,
        "comparison": payload["comparison"], "audit": payload["audit"],
        "sent_count": 0, "reward_count": 0, "training_count": 0, "quota_count": 0,
        "outbox_id": None,
    }
    state_repository = NeverWriteStateRepository()
    repository = LangchaoShadowPostgresRepository(
        ExistingAuditConnection(row), state_repository=state_repository,
    )
    with pytest.raises(RuntimeError, match="hash mismatch"):
        run(repository)
    assert state_repository.writes == 0


def test_v16_migration_is_appended_after_v15_with_stable_checksum_and_create_order():
    assert USER_MODEL_SCHEMA_VERSION == 16
    assert MIGRATIONS[-1] == (16, LANGCHAO_SHADOW_SCHEMA_V16_STATEMENTS)
    assert tuple(version for version, _ in MIGRATIONS) == tuple(range(1, 17))
    records = migration_records()
    assert records[-1].version == 16
    assert len(records[-1].checksum) == 64
    assert records[-1].checksum == migration_records()[-1].checksum
    tables = tuple(
        match.group(1)
        for statement in schema_statements()
        if (match := re.search(
            r"CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)", statement, re.IGNORECASE
        ))
    )
    assert tables[-1] == "langchao_shadow_runs"


def test_v16_schema_enforces_shadow_zero_counts_null_output_and_immutability():
    ddl = " ".join("\n".join(LANGCHAO_SHADOW_SCHEMA_V16_STATEMENTS).split()).upper()
    assert "LANGCHAO_SHADOW_RUNS" in ddl
    for column in ("SENT_COUNT", "REWARD_COUNT", "TRAINING_COUNT", "QUOTA_COUNT"):
        assert f"{column} BIGINT NOT NULL DEFAULT 0 CHECK ({column} = 0)" in ddl
    assert "OUTBOX_ID TEXT CHECK (OUTBOX_ID IS NULL)" in ddl
    assert "MODE TEXT NOT NULL DEFAULT 'SHADOW' CHECK (MODE = 'SHADOW')" in ddl
    assert "UNIQUE (SCOPE_KEY, IDEMPOTENCY_KEY)" in ddl
    assert "FOREIGN KEY (SCOPE_KEY, DECISION_ROUND_ID) REFERENCES LANGCHAO_ROUNDS (SCOPE_KEY, ROUND_ID) ON DELETE RESTRICT" in ddl
    assert "BEFORE UPDATE OR DELETE ON LANGCHAO_SHADOW_RUNS" in ddl
    assert "SOURCE_INPUT_CURSOR" in ddl and "SOURCE_INPUT_VERSION" in ddl
    assert "COMPARISON JSONB" in ddl and "AUDIT JSONB" in ddl
    assert "INPUT_SHA256" in ddl and "AUDIT_SHA256" in ddl
