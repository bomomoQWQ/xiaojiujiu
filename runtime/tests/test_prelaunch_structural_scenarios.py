"""Isolated structural/fault-injection scenarios for the prelaunch tranche.

This module tests only first-class capabilities that exist. Scenarios whose requested
contract is absent are registered as ``blocked`` in the evidence plan rather than being
represented by xfail, pass, string matching, or a weaker neighbouring mechanism.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_runtime.langchao_live_repository import LangchaoLiveCommit, LangchaoLiveRepository
from companion_runtime.langchao_runtime_adapter import TEMPLATE_REWARD_POLICY_VERSION
from companion_runtime.langchao_shadow import FakeLangchaoShadowRepository
from companion_runtime.langchao_social import RuleInputKind, SocialRuleInput, build_social_proposal
from companion_runtime.langchao_social_repository import CommitDisposition, SourceResolutionStatus
from companion_runtime.langchao_social_types import SocialRole, SourceKind, SourceRef, sha256_json
from companion_runtime.langchao_types import MotivationDirection, OutcomeStatus, OutcomeToken, SettlementType
from companion_runtime.user_model_v2_labels import SettlementContextV2, settle_target_label
from companion_runtime.user_model_v2_types import DeliveryBasis, InteractionExposureV2, LabelStatus, Target

from test_langchao_shadow import run as run_shadow
from test_langchao_social_repository import (
    Connection as SocialConnection,
    Cursor as SocialCursor,
    Resolver,
    proposal as social_proposal,
    repository as social_repository,
    resolution,
    source as social_source,
    stored,
)

NOW = datetime(2026, 10, 2, 8, tzinfo=timezone.utc)
RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = RUNTIME_ROOT / "audit" / "prelaunch_structural_scenarios_v1.json"
RESULT_SCHEMA_PATH = RUNTIME_ROOT / "audit" / "prelaunch_structural_result_v1.schema.json"
EXPECTED_IDS = ("T02", "T03", "T05", "T08", "T10", "T12", "T14", "T15", "T16", "T20")


def _plan() -> dict[str, object]:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def test_result_schema_is_parseable_and_closed() -> None:
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == "langchao.prelaunch-structural-result.v1"
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["scenarioResult"]["additionalProperties"] is False
    assert set(schema["$defs"]["scenarioResult"]["properties"]["status"]["enum"]) == {
        "passed_limited", "failed", "blocked"
    }


def test_evidence_plan_has_explicit_executable_or_blocked_scenarios() -> None:
    plan = _plan()
    scenarios = plan["scenarios"]
    assert isinstance(scenarios, list)
    assert tuple(item["id"] for item in scenarios) == EXPECTED_IDS
    for item in scenarios:
        nodes = item["pytest_nodes"]
        blocker = item["blocked_reason"]
        assert bool(nodes) != bool(blocker), item["id"]
        assert item["fault_injection"] and item["claim_limit"]
    assert {item["id"] for item in scenarios if item["blocked_reason"]} == {
        "T02", "T12", "T15", "T16"
    }


def test_t03_shadow_positive_rehearsal_has_zero_protected_writes() -> None:
    repository = FakeLangchaoShadowRepository()
    result = run_shadow(repository)
    assert result.candidate_id == "candidate:1", "positive rehearsal control must actually select"
    assert result.mode == "shadow"
    assert (result.sent_count, result.reward_count, result.training_count, result.quota_count) == (0, 0, 0, 0)
    assert result.outbox_id is None
    assert len(repository.states) == len(repository.runs) == 1
    assert not any(hasattr(repository, name) for name in ("send", "train", "write_label", "write_exposure"))


def _source(*, scope: str) -> SourceRef:
    return SourceRef(
        scope_key=scope,
        source_kind=SourceKind.EVENT,
        source_id=f"event:{scope}",
        source_revision=1,
        source_sha256=sha256_json({"keywords": ["实验", "模型"], "private_scope": scope}),
        observed_at=NOW,
    )


def test_t05_scope_isolation_rejects_cross_user_source() -> None:
    cross_scope = SocialRuleInput(
        kind=RuleInputKind.UNFINISHED,
        source=_source(scope="fixture:user:b"),
        summary="实验 模型",
        role=SocialRole.SHARED,
        semantic_key="shared-keywords-do-not-grant-access",
    )
    with pytest.raises(ValueError, match="scope_key"):
        build_social_proposal(
            scope_key="fixture:user:a",
            created_at=NOW,
            inputs=(cross_scope,),
        )

    same_scope = dataclasses.replace(cross_scope, source=_source(scope="fixture:user:a"))
    proposal = build_social_proposal(
        scope_key="fixture:user:a",
        created_at=NOW,
        inputs=(same_scope,),
    )
    assert proposal.items
    assert {ref.scope_key for item in proposal.items for ref in item.sources} == {"fixture:user:a"}


class _Cursor:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _LiveConnection:
    def __init__(self):
        self.row = None
        self.insert_count = 0

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO langchao_live_commits"):
            if self.row is not None:
                return _Cursor()
            self.insert_count += 1
            self.row = {
                "scope_key": params[0], "round_id": params[1],
                "langchao_candidate_id": params[2], "candidate_revision": params[3],
                "source_candidate_id": params[4], "reward_contract_id": params[5],
                "reward_revision": params[6], "snapshot": params[8],
                "attempt_id": params[9], "render_outbox_id": params[10],
                "claim_id": params[11], "committed_at": params[12],
                "terminal_ack_id": None, "terminal_ack_kind": None,
            }
            return _Cursor(row={"round_id": params[1]})
        if "FROM langchao_live_commits WHERE scope_key=%s AND round_id=%s" in normalized:
            return _Cursor(row=self.row if self.row and self.row["round_id"] == params[1] else None)
        if "WHERE scope_key=%s AND terminal_ack_kind IS NULL" in normalized:
            rows = () if self.row is None or self.row["terminal_ack_kind"] else (self.row,)
            return _Cursor(rows=rows)
        if normalized.startswith("UPDATE langchao_live_commits"):
            if self.row is None or self.row["terminal_ack_kind"] is not None:
                return _Cursor()
            self.row["terminal_ack_id"], self.row["terminal_ack_kind"] = params[:2]
            return _Cursor(row={"round_id": self.row["round_id"]})
        raise AssertionError(normalized)


def _token(key: str, amount: float = 1.0) -> OutcomeToken:
    return OutcomeToken(
        token_id=f"token:{key}", scope_key="fixture:user:live", goal_id="goal", episode_id="episode",
        outcome_key=key, settlement_type=SettlementType.EXPECTED,
        status=OutcomeStatus.UNEXECUTED, base_amount=amount,
        direction_weights=((MotivationDirection.EXPRESSION, 1.0),),
        evidence_version=TEMPLATE_REWARD_POLICY_VERSION, idempotency_key=f"expected:{key}",
    )


def _live_repository():
    connection = _LiveConnection()
    repository = LangchaoLiveRepository(connection, scope_key="fixture:user:live")
    writes: list[tuple[OutcomeToken, object]] = []
    activations: list[object] = []
    repository.outcomes = SimpleNamespace(
        put_outcome_revision=lambda outcome, **kwargs: writes.append((outcome, kwargs)),
        activate_outcome=lambda **kwargs: activations.append(kwargs) or True,
    )
    commit = LangchaoLiveCommit(
        scope_key="fixture:user:live", round_id="round", langchao_candidate_id="candidate",
        candidate_revision=1, source_candidate_id="legacy", reward_contract_id="reward",
        reward_revision=1,
        expected_tokens=(
            _token("reply"), _token("continuation"), _token("negative", -1), _token("delivery", 0)
        ),
        attempt_id="attempt", render_outbox_id="render", claim_id="claim", committed_at=NOW,
    )
    repository.save_commit(commit)
    return repository, connection, writes, activations, commit


def test_t08_duplicate_ack_settles_delivery_once_only() -> None:
    repository, _connection, writes, activations, _commit = _live_repository()
    settlements = [
        repository.settle_terminal(
            round_id="round", attempt_id="attempt", ack_id="ack:t08", sent=True, acknowledged_at=NOW
        )
        for _ in range(3)
    ]
    assert [len(item) for item in settlements] == [1, 0, 0]
    assert settlements[0][0].outcome_key == "delivery"
    assert settlements[0][0].status is OutcomeStatus.CONFIRMED
    assert len(writes) == len(activations) == 1
    assert {item[0].outcome_key for item in writes}.isdisjoint({"reply", "continuation", "negative"})


def test_t10_censored_window_never_becomes_negative() -> None:
    end = NOW + timedelta(hours=6)
    exposure = InteractionExposureV2(
        exposure_id="exp:t10", scope_key="fixture:user:t10", occurred_at=NOW,
        window_started_at=NOW, window_ends_at=end, horizon_seconds=6 * 60 * 60,
        delivery_basis=DeliveryBasis.DELIVERED, created_at=NOW, updated_at=NOW,
        source_event_ids=("delivery:t10",),
    )
    partial = settle_target_label(
        exposure, Target.REPLY, (),
        SettlementContextV2(as_of=NOW + timedelta(hours=2), observation_complete=False),
    )
    complete = settle_target_label(
        exposure, Target.REPLY, (),
        SettlementContextV2(as_of=end, observation_complete=True),
    )
    assert partial.status is LabelStatus.CENSORED and partial.value is None
    assert complete.status is LabelStatus.OBSERVED_NEGATIVE and complete.value is False


def test_t14_send_failure_censors_delivery_without_user_rejection() -> None:
    repository, _connection, writes, activations, _commit = _live_repository()
    settled = repository.settle_terminal(
        round_id="round", attempt_id="attempt", ack_id="ack:failed", sent=False, acknowledged_at=NOW
    )
    assert len(settled) == 1
    assert settled[0].outcome_key == "delivery"
    assert settled[0].status is OutcomeStatus.CENSORED
    assert settled[0].base_amount == 0
    assert len(writes) == len(activations) == 1
    assert {item[0].outcome_key for item in writes}.isdisjoint({"reply", "continuation", "negative"})


def test_t14_unknown_ack_remains_pending_without_second_commit() -> None:
    repository, connection, writes, activations, commit = _live_repository()
    assert repository.pending() == (commit,)
    repository.save_commit(commit)
    assert connection.insert_count == 1
    assert repository.pending() == (commit,)
    assert writes == [] and activations == []


def test_t20_tombstone_retains_minimal_identity_and_invalidates_dependents() -> None:
    ref = social_source()
    item = {
        "proposal_id": "proposal:1", "proposal_revision": 1,
        "item_type": "shared_matter", "role": "shared", "summary": "literal private text",
        "attributes": {}, "supersedes_item_key": None, "supersedes_revision": None,
    }
    connection = SocialConnection([
        None, None, {"pointer_version": 3}, None, None,
        {"source_sha256": ref.source_sha256, "source_status": "tombstoned"},
        SocialCursor(rows=[{"item_key": "item:1", "item_revision": 1}]),
        None, {"source_count": 1}, None, item, {"revision": 2}, None, None,
        SocialCursor(rows=[]), SocialCursor(rows=[{"link_key": "link:1", "link_revision": 1}]), None,
        SocialCursor(rowcount=1),
    ])
    social_repository(connection).tombstone_source(ref, reason="user deletion", occurred_at=NOW)
    source_insert = next(
        (sql, params) for sql, params in connection.calls if "INSERT INTO langchao_social_sources" in sql
    )
    assert "payload" not in source_insert[0].lower() and "summary" not in source_insert[0].lower()
    assert ref.source_sha256 in source_insert[1] and "user deletion" in source_insert[1]
    assert "literal private text" not in repr(source_insert[1])
    assert any(
        "'invalidated'" in sql for sql, _params in connection.calls
        if "INSERT INTO langchao_social_items" in sql
    )


def test_t20_deleted_source_discards_stale_proposal() -> None:
    value = social_proposal()
    ref = value.items[0].sources[0]
    deleted_resolver = Resolver(resolution(ref, status=SourceResolutionStatus.TOMBSTONED))
    connection = SocialConnection([None, stored(value), None])
    result = social_repository(connection, deleted_resolver).commit_proposal(
        proposal_id=value.proposal_id,
        revision=1,
        expected_projection_pointer_version=0,
    )
    assert result.disposition is CommitDisposition.DISCARDED
    assert "unavailable" in result.reason
    assert not any("INSERT INTO langchao_social_items" in sql for sql, _params in connection.calls)
