"""Contract tests for 「浪潮」 v12 persistence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from companion_runtime.langchao_repository import (
    LangchaoReferenceError,
    LangchaoRepository,
    LangchaoRevisionConflictError,
)
from companion_runtime.langchao_schema import LANGCHAO_SCHEMA_V12_STATEMENTS
from companion_runtime.langchao_types import (
    ActionCandidateContract,
    CandidateKind,
    CandidateState,
    GoalContract,
    GoalKind,
    GoalOwnership,
    GoalStatus,
)
from companion_runtime.user_model_v2_migrations import migration_records
from companion_runtime.user_model_v2_schema import MIGRATIONS

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
SCOPE = "scope:langchao"


class Cursor:
    def __init__(self, row=None, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class Connection:
    def __init__(self, rows=(), rowcounts=()):
        self.rows = list(rows)
        self.rowcounts = list(rowcounts)
        self.calls = []
        self.transactions = 0

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        row = self.rows.pop(0) if self.rows else None
        rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        return Cursor(row, rowcount)

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


def goal(*, scope=SCOPE, revision=1, desired_change="help"):
    return GoalContract(
        goal_id="goal:1", scope_key=scope, episode_id="episode:1", semantic_key="goal:semantic",
        kind=GoalKind.FINITE, ownership=GoalOwnership.USER_REQUEST,
        desired_change=desired_change, status=GoalStatus.ACTIONABLE, evidence_refs=(),
        excluded_outcomes=(), completion_outcome_keys=("done",),
        allowed_candidate_kinds=(CandidateKind.INTERNAL_PROCESS,), created_at=NOW,
        updated_at=NOW, revision=revision,
    )


def candidate(*, revision=1):
    return ActionCandidateContract(
        candidate_id="candidate:1", scope_key=SCOPE, semantic_key="candidate:semantic",
        goal_refs=("goal:1",), kind=CandidateKind.INTERNAL_PROCESS,
        action_template="think", input_refs=(), reward_contract_ref="reward:1",
        expected_outcome_token_ids=("token:1",), capability_refs=(),
        permission_ref="permission:1", precondition_refs=(), invalidation_refs=(),
        envelope=(), state=CandidateState.PROPOSED, available_from=NOW, expires_at=None,
        resource_budget=1.0, based_on_state_version=0, created_at=NOW,
        updated_at=NOW, semantic_revision=revision,
    )


def test_v12_is_append_only_and_has_stable_checksum():
    assert MIGRATIONS[11] == (12, LANGCHAO_SCHEMA_V12_STATEMENTS)
    records = migration_records()
    assert tuple(record.version for record in records[:12]) == tuple(range(1, 13))
    assert len(records[11].checksum) == 64
    assert records[11].checksum == migration_records()[11].checksum


def test_schema_has_scoped_identity_revision_pointer_and_exact_refs():
    ddl = " ".join("\n".join(LANGCHAO_SCHEMA_V12_STATEMENTS).split()).upper()
    for kind in ("GOAL", "REWARD", "CANDIDATE"):
        assert f"LANGCHAO_{kind}_IDENTITIES" in ddl
        assert f"LANGCHAO_{kind}_REVISIONS" in ddl
        assert f"LANGCHAO_{kind}_ACTIVE" in ddl
    assert "PRIMARY KEY (SCOPE_KEY, GOAL_ID, REVISION)" in ddl
    assert "PRIMARY KEY (SCOPE_KEY, REWARD_CONTRACT_ID, REVISION)" in ddl
    assert "PRIMARY KEY (SCOPE_KEY, CANDIDATE_ID, REVISION)" in ddl
    assert "UNIQUE (SCOPE_KEY, SEMANTIC_KEY)" in ddl
    assert "PAYLOAD JSONB" in ddl and "PAYLOAD_SHA256 TEXT" in ddl
    assert "POINTER_VERSION BIGINT" in ddl
    assert "LANGCHAO_CANDIDATE_GOAL_REFS" in ddl
    assert "FOREIGN KEY (SCOPE_KEY, GOAL_ID, GOAL_REVISION)" in ddl
    assert "(SCOPE_KEY, REWARD_CONTRACT_ID, REWARD_REVISION)" in ddl
    assert "REFERENCES LANGCHAO_REWARD_REVISIONS (SCOPE_KEY, REWARD_CONTRACT_ID, REVISION)" in ddl
    assert "GOAL REVISION 1 MAY LEAVE REWARD_CONTRACT_ID NULL" in ddl
    assert "ON DELETE RESTRICT" in ddl
    assert "NAN" not in ddl  # excluded through x = x; infinities are explicit literals
    assert "'INFINITY'::DOUBLE PRECISION" in ddl
    assert ddl.count("BEFORE UPDATE OR DELETE") == 3


def test_repository_is_configuration_scope_bound_and_requires_exact_dto():
    repository = LangchaoRepository(Connection(), scope_key=SCOPE)
    with pytest.raises(ValueError, match="scope_key"):
        repository.put_goal_revision(goal(scope="scope:other"))
    with pytest.raises(TypeError, match="GoalContract"):
        repository.put_goal_revision({"scope_key": SCOPE})  # type: ignore[arg-type]


def test_put_goal_is_idempotent_by_payload_hash_and_conflicts_hard():
    existing = {"goal_id": "goal:1", "revision": 1, "payload_matches": True}
    conn = Connection(rows=[None, {"semantic_key": "goal:semantic"}, existing])
    result = LangchaoRepository(conn, scope_key=SCOPE).put_goal_revision(goal())
    assert result is existing
    assert conn.transactions == 1
    assert len(conn.calls) == 3
    hash_sql, hash_params = conn.calls[-1]
    assert "payload_sha256 = %s AS payload_matches" in hash_sql
    assert hash_params[1:] == (SCOPE, "goal:1", 1)

    conflict = Connection(rows=[None, {"semantic_key": "goal:semantic"}, {"payload_matches": False}])
    with pytest.raises(LangchaoRevisionConflictError):
        LangchaoRepository(conflict, scope_key=SCOPE).put_goal_revision(goal(desired_change="different"))


def test_activate_uses_advisory_lock_first_creation_zero_and_cas():
    conn = Connection(rows=[None, (1,), None, None])
    repository = LangchaoRepository(conn, scope_key=SCOPE)
    assert repository.activate_goal(goal_id="goal:1", revision=1, expected_pointer_version=0)
    assert conn.transactions == 1
    assert "pg_advisory_xact_lock" in conn.calls[0][0]
    assert conn.calls[1][1] == (SCOPE, "goal:1", 1)
    assert "FOR UPDATE" in conn.calls[2][0]
    assert conn.calls[3][1] == (SCOPE, "goal:1", 1, 1, 0)

    mismatch = Connection(rows=[None, (1,), {"pointer_version": 3}])
    assert not LangchaoRepository(mismatch, scope_key=SCOPE).activate_goal(
        goal_id="goal:1", revision=1, expected_pointer_version=2
    )
    assert len(mismatch.calls) == 3


def test_candidate_freezes_active_reward_revision_and_requires_active_reward():
    # identity insert/select, no existing candidate, active reward rev 1, active goal
    # rev 4, candidate insert, then candidate-goal exact-ref insert.
    conn = Connection(rows=[
        None, {"semantic_key": "candidate:semantic"}, None,
        {"revision": 1}, {"revision": 4}, {"candidate_id": "candidate:1"}, None,
    ])
    repository = LangchaoRepository(conn, scope_key=SCOPE)
    repository.put_candidate_revision(candidate(revision=1))
    insert_sql, insert_params = conn.calls[5]
    assert "reward_contract_id, reward_revision" in insert_sql
    assert insert_params[8:11] == ("reward:1", 1, 1.0)

    # After the active reward pointer advances, a new candidate revision freezes 2;
    # the prior immutable candidate insert remains bound to revision 1.
    advanced = Connection(rows=[
        None, {"semantic_key": "candidate:semantic"}, None,
        {"revision": 2}, {"revision": 4}, {"candidate_id": "candidate:1"}, None,
    ])
    LangchaoRepository(advanced, scope_key=SCOPE).put_candidate_revision(candidate(revision=2))
    assert advanced.calls[5][1][8:11] == ("reward:1", 2, 1.0)
    assert conn.calls[5][1][9] == 1

    missing = Connection(rows=[None, {"semantic_key": "candidate:semantic"}, None, None])
    with pytest.raises(LangchaoReferenceError, match="has no active revision"):
        LangchaoRepository(missing, scope_key=SCOPE).put_candidate_revision(candidate())
    assert len(missing.calls) == 4  # no candidate revision was inserted


def test_get_active_joins_exact_pointer_revision_without_max():
    conn = Connection(rows=[{"goal_id": "goal:1", "revision": 2, "pointer_version": 7}])
    row = LangchaoRepository(conn, scope_key=SCOPE).get_active_goal(goal_id="goal:1")
    assert row["revision"] == 2
    sql, params = conn.calls[0]
    assert "r.revision = a.revision" in sql
    assert "max(" not in sql.lower()
    assert params == (SCOPE, "goal:1")
