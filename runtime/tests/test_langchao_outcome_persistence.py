"""Contract tests for the isolated 「浪潮」 v13 outcome ledger."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.langchao_outcome_repository import (
    LangchaoOutcomeConflictError,
    LangchaoOutcomeReferenceError,
    LangchaoOutcomeRepository,
)
from companion_runtime.langchao_outcome_schema import LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS
from companion_runtime.langchao_types import (
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    SettlementType,
)
from companion_runtime.user_model_v2_migrations import migration_records
from companion_runtime.user_model_v2_schema import MIGRATIONS, USER_MODEL_SCHEMA_VERSION

NOW = datetime(2026, 3, 2, tzinfo=timezone.utc)
SCOPE = "scope:langchao-outcome"


class Cursor:
    def __init__(self, row=None, rows=None, rowcount=1):
        self._row = row
        self._rows = [] if rows is None else rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class Connection:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
        self.transactions = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        result = self.results.pop(0) if self.results else None
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, Cursor):
            return result
        return Cursor(row=result)

    @contextmanager
    def transaction(self):
        self.transactions += 1
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise


def outcome(
    *,
    token_id="token:expected",
    idempotency_key="idem:expected",
    settlement_type=SettlementType.EXPECTED,
    status=OutcomeStatus.UNEXECUTED,
    goal_id="goal:1",
    episode_id="episode:1",
    outcome_key="delivered",
    base_amount=0.6,
    direction_weights=((MotivationDirection.CARE, 0.75),),
    corrects_token_id=None,
    window=False,
):
    return OutcomeToken(
        token_id=token_id,
        scope_key=SCOPE,
        goal_id=goal_id,
        episode_id=episode_id,
        outcome_key=outcome_key,
        settlement_type=settlement_type,
        status=status,
        base_amount=base_amount,
        direction_weights=direction_weights,
        evidence_version="evidence:v1",
        idempotency_key=idempotency_key,
        observation_started_at=NOW if window else None,
        observation_ends_at=NOW + timedelta(hours=1) if window else None,
        corrects_token_id=corrects_token_id,
    )


def reward_row(*, cap=1.0):
    return {
        "goal_id": "goal:1",
        "payload": {"goal_id": "goal:1", "episode_id": "episode:1"},
        "total_cap": cap,
    }


def test_v13_is_append_only_and_checksum_is_stable():
    assert USER_MODEL_SCHEMA_VERSION >= 13
    assert next(item for item in MIGRATIONS if item[0] == 12)[0] == 12
    assert next(item for item in MIGRATIONS if item[0] == 13) == (
        13, LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS
    )
    records = migration_records()
    record = next(item for item in records if item.version == 13)
    assert len(record.checksum) == 64
    assert record.checksum == next(item for item in migration_records() if item.version == 13).checksum


def test_schema_has_scoped_exact_lineage_membership_invariants_and_immutability():
    ddl = " ".join("\n".join(LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS).split()).upper()
    assert "LANGCHAO_OUTCOME_IDENTITIES" in ddl
    assert "PRIMARY KEY (SCOPE_KEY, TOKEN_ID)" in ddl
    # Expected, actual and correction tokens may intentionally share outcome_key.
    assert "UNIQUE (SCOPE_KEY, REWARD_CONTRACT_ID, OUTCOME_KEY)" not in ddl
    assert "PRIMARY KEY (SCOPE_KEY, TOKEN_ID, REVISION)" in ddl
    assert "UNIQUE (SCOPE_KEY, IDEMPOTENCY_KEY)" in ddl
    assert "LANGCHAO_OUTCOME_ACTIVE" in ddl and "POINTER_VERSION BIGINT" in ddl
    assert "LANGCHAO_REWARD_OUTCOMES" in ddl
    assert "FOREIGN KEY (SCOPE_KEY, CORRECTS_TOKEN_ID, CORRECTS_REVISION)" in ddl
    assert "DEFERRABLE INITIALLY DEFERRED" in ddl
    assert "ON DELETE RESTRICT" in ddl
    assert "BEFORE UPDATE OR DELETE ON LANGCHAO_OUTCOME_REVISIONS" in ddl
    assert "BASE_AMOUNT = BASE_AMOUNT" in ddl
    assert ddl.count("'INFINITY'::DOUBLE PRECISION") >= 9
    assert "DIRECTION_WEIGHTS->'APPROACH' IS NOT NULL" in ddl
    assert "?" not in ddl
    assert "CORRECTS_TOKEN_ID <> TOKEN_ID" in ddl
    assert "STATUS <> 'PENDING' OR OBSERVATION_STARTED_AT IS NOT NULL" in ddl


def test_sparse_weight_is_zero_filled_to_eight_keys_without_rescaling_payload():
    inserted = {"token_id": "token:expected", "revision": 1}
    conn = Connection(results=[None, reward_row(), None, {
        "reward_contract_id": "reward:1", "outcome_key": "delivered"
    }, None, inserted])
    result = LangchaoOutcomeRepository(conn, scope_key=SCOPE).put_outcome_revision(
        outcome(), revision=1, reward_contract_id="reward:1", reward_contract_revision=1
    )
    assert result is inserted
    sql, params = conn.calls[-1]
    weights = json.loads(params[9])
    assert len(weights) == 8
    assert weights["care"] == 0.75
    assert sum(weights.values()) == 0.75
    payload = json.loads(params[17])
    assert payload["direction_weights"] == {"care": 0.75}
    assert "direction_weights" in sql


def test_idempotency_same_payload_returns_and_different_payload_conflicts_hard():
    same = {
        "token_id": "token:expected", "revision": 1,
        "reward_contract_id": "reward:1", "reward_contract_revision": 1,
        "payload_matches": True,
    }
    conn = Connection(results=[same])
    assert LangchaoOutcomeRepository(conn, scope_key=SCOPE).put_outcome_revision(
        outcome(), revision=1, reward_contract_id="reward:1", reward_contract_revision=1
    ) is same
    assert len(conn.calls) == 1

    conflict = Connection(results=[{"payload_matches": False}])
    with pytest.raises(LangchaoOutcomeConflictError, match="idempotency"):
        LangchaoOutcomeRepository(conflict, scope_key=SCOPE).put_outcome_revision(
            outcome(base_amount=0.7), revision=1,
            reward_contract_id="reward:1", reward_contract_revision=1,
        )
    assert conflict.rollbacks == 1


def test_correction_requires_exact_matching_lineage_and_never_self_corrects():
    correction = outcome(
        token_id="token:correction", idempotency_key="idem:correction",
        settlement_type=SettlementType.CORRECTION, status=OutcomeStatus.CORRECTED,
        corrects_token_id="token:actual",
    )
    corrected = {"payload": {
        "scope_key": SCOPE, "goal_id": "goal:1", "episode_id": "episode:1",
        "outcome_key": "delivered",
    }, "reward_contract_id": "reward:1", "outcome_key": "delivered"}
    conn = Connection(results=[None, reward_row(), None, {
        "reward_contract_id": "reward:1", "outcome_key": "delivered"
    }, None, corrected, {"token_id": "token:correction"}])
    LangchaoOutcomeRepository(conn, scope_key=SCOPE).put_outcome_revision(
        correction, revision=1, reward_contract_id="reward:1",
        reward_contract_revision=1, corrects_revision=2,
    )
    assert conn.calls[-1][1][15:17] == ("token:actual", 2)

    mismatch = dict(corrected)
    mismatch["payload"] = dict(corrected["payload"], episode_id="other")
    bad = Connection(results=[None, reward_row(), None, {
        "reward_contract_id": "reward:1", "outcome_key": "delivered"
    }, None, mismatch])
    with pytest.raises(LangchaoOutcomeReferenceError, match="scope, goal, episode"):
        LangchaoOutcomeRepository(bad, scope_key=SCOPE).put_outcome_revision(
            correction, revision=1, reward_contract_id="reward:1",
            reward_contract_revision=1, corrects_revision=2,
        )
    assert bad.rollbacks == 1

    with pytest.raises(ValueError, match="must not equal token_id"):
        outcome(
            token_id="token:self", idempotency_key="idem:self",
            settlement_type=SettlementType.CORRECTION, status=OutcomeStatus.CORRECTED,
            corrects_token_id="token:self",
        )


def test_window_and_scope_are_rejected_before_database_write():
    with pytest.raises(ValueError, match="observation window"):
        outcome(status=OutcomeStatus.PENDING)
    foreign = replace(outcome(), scope_key="other")
    repo = LangchaoOutcomeRepository(Connection(), scope_key=SCOPE)
    with pytest.raises(ValueError, match="scope_key"):
        repo.put_outcome_revision(
            foreign, revision=1, reward_contract_id="reward:1", reward_contract_revision=1
        )


def test_activate_is_cas_and_ledger_cap_counts_base_once_not_eight_times():
    active = Connection(results=[None, (1,), None, Cursor(rowcount=1)])
    assert LangchaoOutcomeRepository(active, scope_key=SCOPE).activate_outcome(
        token_id="token:expected", revision=1, expected_pointer_version=0
    )
    assert active.calls[-1][1] == (SCOPE, "token:expected", 1, 1, 0)

    expected_row = {
        "settlement_type": "expected", "base_amount": 0.6,
        "identity_reward_contract_id": "reward:1",
    }
    bind = Connection(results=[reward_row(cap=0.6), expected_row, Cursor(rows=[]), None])
    rows = LangchaoOutcomeRepository(bind, scope_key=SCOPE).bind_reward_outcomes(
        reward_contract_id="reward:1", reward_contract_revision=1,
        outcome_revisions=(("token:expected", 1),),
    )
    assert rows == (expected_row,)
    assert "INSERT INTO langchao_reward_outcomes" in bind.calls[-1][0]

    actual_row = dict(expected_row, settlement_type="actual")
    actual = Connection(results=[reward_row(), actual_row])
    with pytest.raises(LangchaoOutcomeReferenceError, match="expected ledger"):
        LangchaoOutcomeRepository(actual, scope_key=SCOPE).bind_reward_outcomes(
            reward_contract_id="reward:1", reward_contract_revision=1,
            outcome_revisions=(("token:actual", 1),),
        )
    assert actual.rollbacks == 1


def test_membership_conflict_and_lineage_query_use_exact_revisions():
    expected_row = {
        "settlement_type": "expected", "base_amount": 0.2,
        "identity_reward_contract_id": "reward:1",
    }
    existing = {"token_id": "token:other", "outcome_revision": 1, "ordinal": 0}
    conn = Connection(results=[reward_row(), expected_row, Cursor(rows=[existing])])
    with pytest.raises(LangchaoOutcomeConflictError, match="membership"):
        LangchaoOutcomeRepository(conn, scope_key=SCOPE).bind_reward_outcomes(
            reward_contract_id="reward:1", reward_contract_revision=1,
            outcome_revisions=(("token:expected", 1),),
        )
    lineage = Connection(results=[Cursor(rows=[{"token_id": "c"}, {"token_id": "a"}])])
    rows = LangchaoOutcomeRepository(lineage, scope_key=SCOPE).get_ledger_lineage(
        token_id="c", revision=3
    )
    assert [row["token_id"] for row in rows] == ["c", "a"]
    assert "WITH RECURSIVE" in lineage.calls[0][0]
    assert lineage.calls[0][1] == (SCOPE, "c", 3)


def test_transaction_rolls_back_failed_insert():
    conn = Connection(results=[None, reward_row(), None, {
        "reward_contract_id": "reward:1", "outcome_key": "delivered"
    }, None, RuntimeError("database insert failed")])
    with pytest.raises(RuntimeError, match="insert failed"):
        LangchaoOutcomeRepository(conn, scope_key=SCOPE).put_outcome_revision(
            outcome(), revision=1, reward_contract_id="reward:1", reward_contract_revision=1
        )
    assert conn.transactions == 1 and conn.rollbacks == 1


@pytest.mark.skipif(not os.getenv("LANGCHAO_TEST_POSTGRES_DSN"), reason="PostgreSQL DSN not configured")
def test_v13_postgres_migration_smoke():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(os.environ["LANGCHAO_TEST_POSTGRES_DSN"]) as connection:
        with connection.transaction(force_rollback=True):
            for statement in LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS:
                connection.execute(statement)
