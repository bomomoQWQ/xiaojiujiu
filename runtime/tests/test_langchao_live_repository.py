from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from companion_runtime.langchao_live_repository import LangchaoLiveCommit, LangchaoLiveRepository
from companion_runtime.langchao_runtime_adapter import TEMPLATE_REWARD_POLICY_VERSION
from companion_runtime.langchao_types import (
    MotivationDirection, OutcomeStatus, OutcomeToken, SettlementType,
)

NOW = datetime(2026, 3, 3, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, row=None, rows=()):
        self.row, self.rows = row, list(rows)
    def fetchone(self):
        return self.row
    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self):
        self.row = None
    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO langchao_live_commits"):
            if self.row is not None:
                return Cursor()
            self.row = {
                "scope_key": params[0], "round_id": params[1],
                "langchao_candidate_id": params[2], "candidate_revision": params[3],
                "source_candidate_id": params[4], "reward_contract_id": params[5],
                "reward_revision": params[6], "snapshot": params[8],
                "attempt_id": params[9], "render_outbox_id": params[10],
                "claim_id": params[11], "committed_at": params[12], "terminal_ack_id": None,
                "terminal_ack_kind": None,
            }
            return Cursor(row={"round_id": params[1]})
        if "FROM langchao_live_commits WHERE scope_key=%s AND round_id=%s" in normalized:
            return Cursor(row=self.row if self.row and self.row["round_id"] == params[1] else None)
        if "WHERE scope_key=%s AND terminal_ack_kind IS NULL" in normalized:
            return Cursor(rows=() if self.row is None or self.row["terminal_ack_kind"] else (self.row,))
        if normalized.startswith("UPDATE langchao_live_commits"):
            if self.row is None or self.row["terminal_ack_kind"] is not None:
                return Cursor()
            self.row["terminal_ack_id"], self.row["terminal_ack_kind"] = params[:2]
            return Cursor(row={"round_id": self.row["round_id"]})
        raise AssertionError(normalized)


def token(key: str, amount: float = 1.0) -> OutcomeToken:
    return OutcomeToken(
        token_id=f"token:{key}", scope_key="scope", goal_id="goal", episode_id="episode",
        outcome_key=key, settlement_type=SettlementType.EXPECTED,
        status=OutcomeStatus.UNEXECUTED, base_amount=amount,
        direction_weights=((MotivationDirection.EXPRESSION, 1.0),),
        evidence_version=TEMPLATE_REWARD_POLICY_VERSION, idempotency_key=f"expected:{key}",
    )


def repository():
    connection = Connection()
    repo = LangchaoLiveRepository(connection, scope_key="scope")
    writes, activations = [], []
    repo.outcomes = SimpleNamespace(
        put_outcome_revision=lambda outcome, **kw: writes.append((outcome, kw)),
        activate_outcome=lambda **kw: activations.append(kw) or True,
    )
    commit = LangchaoLiveCommit(
        scope_key="scope", round_id="round", langchao_candidate_id="lc", candidate_revision=2,
        source_candidate_id="legacy", reward_contract_id="reward", reward_revision=3,
        expected_tokens=(token("reply"), token("continuation"), token("negative", -1), token("delivery", 0)),
        attempt_id="attempt", render_outbox_id="render", claim_id="claim", committed_at=NOW,
    )
    repo.save_commit(commit)
    return repo, connection, writes, activations


def test_success_settles_delivery_only_and_repeat_is_exactly_once():
    repo, connection, writes, activations = repository()
    assert [item.round_id for item in repo.pending()] == ["round"]
    settled = repo.settle_terminal(round_id="round", attempt_id="attempt", ack_id="send",
                                   sent=True, acknowledged_at=NOW)
    assert [item.outcome_key for item in settled] == ["delivery"]
    assert settled[0].status is OutcomeStatus.CONFIRMED
    assert len(writes) == len(activations) == 1
    assert repo.settle_terminal(round_id="round", attempt_id="attempt", ack_id="send",
                                sent=True, acknowledged_at=NOW) == ()
    assert len(writes) == 1 and repo.pending() == ()


def test_failed_ack_censors_execution_and_never_settles_user_outcomes():
    repo, _connection, writes, _activations = repository()
    settled = repo.settle_terminal(round_id="round", attempt_id="attempt", ack_id="send",
                                   sent=False, acknowledged_at=NOW)
    assert len(settled) == 1 and settled[0].outcome_key == "delivery"
    assert settled[0].status is OutcomeStatus.CENSORED and settled[0].base_amount == 0
    assert {item[0].outcome_key for item in writes}.isdisjoint({"reply", "continuation", "negative"})


def test_restart_repository_recovers_snapshot_and_rejects_late_conflict():
    repo, connection, writes, _activations = repository()
    restarted = LangchaoLiveRepository(connection, scope_key="scope")
    restarted.outcomes = repo.outcomes
    recovered = restarted.get("round")
    assert recovered is not None and recovered.attempt_id == "attempt"
    restarted.settle_terminal(round_id="round", attempt_id="attempt", ack_id="send",
                              sent=True, acknowledged_at=NOW)
    assert len(writes) == 1
    with pytest.raises(ValueError, match="different terminal"):
        restarted.settle_terminal(round_id="round", attempt_id="attempt", ack_id="late",
                                  sent=False, acknowledged_at=NOW)
