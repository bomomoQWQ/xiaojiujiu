"""PostgreSQL negative/recovery tests for Langchao v26 live settlement invariants."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from companion_runtime.langchao_authority import AuthorityEngine, AuthorityMode
from companion_runtime.langchao_authority_repository import (
    AuthorityInFlightError,
    LangchaoAuthorityRepository,
)
from companion_runtime.langchao_live_repository import LangchaoLiveCommit, LangchaoLiveRepository
from companion_runtime.langchao_no_send import NoSendReason, NoSendResult
from companion_runtime.langchao_no_send_repository import LangchaoNoSendConflictError
from companion_runtime.langchao_types import (
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    SettlementType,
)
from companion_runtime.user_model_v2_migrations import migrate
from companion_runtime.user_model_v2_schema import USER_MODEL_SCHEMA_VERSION

_DSN = os.environ.get("CR_TEST_PG_DSN", "").strip() or os.environ.get(
    "LANGCHAO_TEST_POSTGRES_DSN", ""
).strip()
pytestmark = pytest.mark.skipif(not _DSN, reason="set a disposable PostgreSQL test DSN")
NOW = datetime(2027, 1, 4, 12, 0, tzinfo=timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


@pytest.fixture(scope="module")
def pg_schema():
    schema = "langchao_v26_" + uuid.uuid4().hex[:12]
    conn = psycopg.connect(_DSN, autocommit=False, row_factory=psycopg.rows.dict_row)
    try:
        with conn.transaction():
            result = migrate(conn, schema=schema, runner_version="langchao-v26-test/1")
        assert result.current_version == USER_MODEL_SCHEMA_VERSION
        yield schema
    finally:
        conn.rollback()
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


@pytest.fixture()
def connection(pg_schema):
    conn = psycopg.connect(_DSN, autocommit=False, row_factory=psycopg.rows.dict_row)
    conn.execute(f'SET search_path TO "{pg_schema}", public')
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _seed_candidate_revision(conn, *, scope: str, candidate_id: str, revision: int = 1) -> None:
    conn.execute(
        "INSERT INTO langchao_candidate_identities(scope_key,candidate_id,semantic_key) VALUES (%s,%s,%s)",
        (scope, candidate_id, _id("semantic")),
    )
    conn.execute(
        """INSERT INTO langchao_candidate_revisions
           (scope_key,candidate_id,revision,payload,payload_sha256,kind,state,
            reward_contract_id,reward_revision,resource_budget,available_from,
            contract_created_at,contract_updated_at)
           VALUES (%s,%s,%s,%s::jsonb,%s,'external_message','competitive',%s,1,1,%s,%s,%s)""",
        (scope, candidate_id, revision, json.dumps({"candidate_id": candidate_id}), "0" * 64,
         _id("reward-placeholder"), NOW, NOW, NOW),
    )


def _activate_langchao(conn, scope: str) -> LangchaoAuthorityRepository:
    authority = LangchaoAuthorityRepository(conn, scope_key=scope)
    authority.bootstrap(created_at=NOW)
    authority.switch_authority(
        engine_key=AuthorityEngine.LANGCHAO,
        mode=AuthorityMode.LIVE,
        expected_pointer_version=1,
        in_flight_count=0,
        reason="v26 test",
        created_at=NOW,
    )
    return authority


def _seed_semantic_claim(
    conn, *, scope: str, candidate_id: str, revision: int, attempt_id: str
) -> str:
    authority = _activate_langchao(conn, scope)
    dispatch_id = _id("semantic-dispatch")
    authority.create_dispatch_claim(
        dispatch_id=dispatch_id,
        candidate_id=candidate_id,
        candidate_revision=revision,
        attempt_id=attempt_id,
        idempotency_key=_id("semantic-idem"),
        created_at=NOW,
    )
    return dispatch_id


def _commit(
    *, scope: str, round_id: str, candidate_id: str, attempt_id: str,
    semantic_dispatch_id: str | None, expected_tokens: tuple[OutcomeToken, ...] = (),
) -> LangchaoLiveCommit:
    return LangchaoLiveCommit(
        scope_key=scope,
        round_id=round_id,
        langchao_candidate_id=candidate_id,
        candidate_revision=1,
        source_candidate_id=_id("source"),
        reward_contract_id=_id("reward"),
        reward_revision=1,
        expected_tokens=expected_tokens,
        attempt_id=attempt_id,
        render_outbox_id=_id("render"),
        claim_id=_id("mechanical-claim"),
        semantic_dispatch_id=semantic_dispatch_id,
        authority_revision=2,
        committed_at=NOW,
    )


def _seed_direct_live_commit(
    conn, *, scope: str, round_id: str, candidate_id: str, attempt_id: str,
    semantic_dispatch_id: str, expected_tokens: tuple[OutcomeToken, ...] = (),
) -> None:
    snapshot = {"version": "langchao.live-commit.v2", "tokens": [t.to_dict() for t in expected_tokens]}
    conn.execute(
        """INSERT INTO langchao_live_commits
           (scope_key,round_id,langchao_candidate_id,candidate_revision,source_candidate_id,
            reward_contract_id,reward_revision,expected_token_ids,snapshot,attempt_id,
            render_outbox_id,claim_id,semantic_dispatch_id,authority_revision,committed_at)
           VALUES (%s,%s,%s,1,%s,%s,1,%s::jsonb,%s::jsonb,%s,%s,%s,%s,2,%s)""",
        (scope, round_id, candidate_id, _id("source"), _id("reward"),
         json.dumps([t.token_id for t in expected_tokens]), json.dumps(snapshot), attempt_id,
         _id("render"), _id("mechanical-claim"), semantic_dispatch_id, NOW),
    )


def test_v26_schema_adds_semantic_dispatch_witness_and_recovery_columns(connection) -> None:
    columns = {
        row["column_name"]: row["is_nullable"]
        for row in connection.execute(
            """SELECT column_name,is_nullable FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name='langchao_live_commits'"""
        ).fetchall()
    }
    assert columns["semantic_dispatch_id"] == "YES"  # rolling deploy: old writers remain legal
    assert columns["authority_revision"] == "YES"
    assert columns["settlement_status"] == "NO"
    assert columns["settlement_completed_at"] == "YES"
    assert columns["exposure_recovery_required"] == "NO"
    assert USER_MODEL_SCHEMA_VERSION == 26


def test_v26_semantic_dispatch_fk_rejects_wrong_candidate_revision(connection) -> None:
    scope, candidate, attempt = _id("scope"), _id("candidate"), _id("attempt")
    _seed_candidate_revision(connection, scope=scope, candidate_id=candidate)
    dispatch_id = _seed_semantic_claim(
        connection, scope=scope, candidate_id=candidate, revision=1, attempt_id=attempt
    )
    with pytest.raises(psycopg.IntegrityError):
        with connection.transaction():
            _seed_direct_live_commit(
                connection,
                scope=scope,
                round_id=_id("round"),
                candidate_id=_id("wrong-candidate"),
                attempt_id=attempt,
                semantic_dispatch_id=dispatch_id,
            )
            connection.execute("SET CONSTRAINTS fk_langchao_live_commit_semantic_dispatch IMMEDIATE")


def test_v26_exact_semantic_claim_round_trips(connection) -> None:
    scope, candidate, attempt = _id("scope"), _id("candidate"), _id("attempt")
    _seed_candidate_revision(connection, scope=scope, candidate_id=candidate)
    dispatch_id = _seed_semantic_claim(
        connection, scope=scope, candidate_id=candidate, revision=1, attempt_id=attempt
    )
    with connection.transaction():
        _seed_direct_live_commit(
            connection,
            scope=scope,
            round_id=_id("round"),
            candidate_id=candidate,
            attempt_id=attempt,
            semantic_dispatch_id=dispatch_id,
        )
        connection.execute("SET CONSTRAINTS fk_langchao_live_commit_semantic_dispatch IMMEDIATE")


def test_terminal_retry_repairs_outcome_after_ack_was_already_written(connection, monkeypatch) -> None:
    scope, round_id = _id("scope"), _id("round")
    candidate, attempt = _id("candidate"), _id("attempt")
    reward, goal, episode = _id("reward"), _id("goal"), _id("episode")
    _seed_candidate_revision(connection, scope=scope, candidate_id=candidate)
    dispatch_id = _seed_semantic_claim(
        connection, scope=scope, candidate_id=candidate, revision=1, attempt_id=attempt
    )
    payload = json.dumps({"goal_id": goal, "episode_id": episode})
    connection.execute(
        "INSERT INTO langchao_goal_identities(scope_key,goal_id,semantic_key) VALUES (%s,%s,%s)",
        (scope, goal, _id("goal-semantic")),
    )
    connection.execute(
        "INSERT INTO langchao_reward_identities(scope_key,reward_contract_id,semantic_key) VALUES (%s,%s,%s)",
        (scope, reward, _id("reward-semantic")),
    )
    connection.execute(
        """INSERT INTO langchao_reward_revisions
           (scope_key,reward_contract_id,revision,payload,payload_sha256,goal_id,total_cap,
            contract_created_at,contract_updated_at)
           VALUES (%s,%s,1,%s::jsonb,%s,%s,100,%s,%s)""",
        (scope, reward, payload, "0" * 64, goal, NOW, NOW),
    )
    expected = OutcomeToken(
        token_id=_id("expected"), scope_key=scope, goal_id=goal, episode_id=episode,
        outcome_key="delivery", settlement_type=SettlementType.EXPECTED,
        status=OutcomeStatus.UNEXECUTED, base_amount=1.0,
        direction_weights=((MotivationDirection.EXPRESSION, 1.0),),
        evidence_version="v26-test", idempotency_key=_id("expected-idem"),
    )
    snapshot = {"version": "langchao.live-commit.v2", "tokens": [expected.to_dict()]}
    connection.execute(
        """INSERT INTO langchao_live_commits
           (scope_key,round_id,langchao_candidate_id,candidate_revision,source_candidate_id,
            reward_contract_id,reward_revision,expected_token_ids,snapshot,attempt_id,
            render_outbox_id,claim_id,semantic_dispatch_id,authority_revision,committed_at,
            terminal_ack_id,terminal_ack_kind,terminal_acknowledged_at,settlement_status)
           VALUES (%s,%s,%s,1,%s,%s,1,%s::jsonb,%s::jsonb,%s,%s,%s,%s,2,%s,%s,'sent',%s,'pending')""",
        (scope, round_id, candidate, _id("source"), reward, json.dumps([expected.token_id]),
         json.dumps(snapshot), attempt, _id("render"), _id("mechanical-claim"),
         dispatch_id, NOW, "ack:already", NOW),
    )
    repo = LangchaoLiveRepository(connection, scope_key=scope)
    settled = repo.settle_terminal(
        round_id=round_id,
        attempt_id=attempt,
        ack_id="ack:already",
        sent=True,
        acknowledged_at=NOW,
    )
    assert len(settled) == 1
    row = connection.execute(
        "SELECT settlement_status,settlement_completed_at FROM langchao_live_commits WHERE scope_key=%s AND round_id=%s",
        (scope, round_id),
    ).fetchone()
    assert row["settlement_status"] == "completed"
    assert row["settlement_completed_at"] is not None


def test_terminal_failure_rolls_back_ack_and_outcomes_together(connection, monkeypatch) -> None:
    scope, round_id = _id("scope"), _id("round")
    candidate, attempt = _id("candidate"), _id("attempt")
    _seed_candidate_revision(connection, scope=scope, candidate_id=candidate)
    dispatch_id = _seed_semantic_claim(
        connection, scope=scope, candidate_id=candidate, revision=1, attempt_id=attempt
    )
    _seed_direct_live_commit(
        connection, scope=scope, round_id=round_id, candidate_id=candidate,
        attempt_id=attempt, semantic_dispatch_id=dispatch_id,
    )
    repo = LangchaoLiveRepository(connection, scope_key=scope)

    def explode(**_kwargs):
        raise RuntimeError("injected outcome failure")

    monkeypatch.setattr(repo.outcomes, "put_outcome_revision_in_transaction", explode)
    with pytest.raises(RuntimeError, match="injected outcome failure"):
        with connection.transaction():
            repo.settle_terminal(
                round_id=round_id, attempt_id=attempt, ack_id=_id("ack"), sent=True,
                acknowledged_at=NOW,
            )
    row = connection.execute(
        "SELECT terminal_ack_id,terminal_ack_kind,settlement_status FROM langchao_live_commits WHERE scope_key=%s AND round_id=%s",
        (scope, round_id),
    ).fetchone()
    assert row == {"terminal_ack_id": None, "terminal_ack_kind": None, "settlement_status": "pending"}


def test_authority_switch_derives_in_flight_and_rejects_caller_zero(connection) -> None:
    scope, candidate, attempt = _id("scope"), _id("candidate"), _id("attempt")
    _seed_candidate_revision(connection, scope=scope, candidate_id=candidate)
    authority = _activate_langchao(connection, scope)
    authority.create_dispatch_claim(
        dispatch_id=_id("dispatch"), candidate_id=candidate, candidate_revision=1,
        attempt_id=attempt, idempotency_key=_id("idem"), created_at=NOW,
    )
    with pytest.raises(AuthorityInFlightError):
        authority.switch_authority(
            engine_key=AuthorityEngine.RUNTIME_V2,
            mode=AuthorityMode.LIVE,
            expected_pointer_version=2,
            in_flight_count=0,
            reason="caller lied about in-flight work",
            created_at=NOW,
        )


def test_no_send_cannot_coexist_with_durable_commit(connection) -> None:
    scope, candidate, attempt = _id("scope"), _id("candidate"), _id("attempt")
    _seed_candidate_revision(connection, scope=scope, candidate_id=candidate)
    dispatch_id = _seed_semantic_claim(
        connection, scope=scope, candidate_id=candidate, revision=1, attempt_id=attempt
    )
    round_id = _id("round")
    _seed_direct_live_commit(
        connection, scope=scope, round_id=round_id, candidate_id=candidate,
        attempt_id=attempt, semantic_dispatch_id=dispatch_id,
    )
    repo = LangchaoLiveRepository(connection, scope_key=scope)
    with pytest.raises(LangchaoNoSendConflictError):
        repo.save_no_send(NoSendResult(
            reason=NoSendReason.DISPATCH_FAILED,
            stage="dispatch",
            round_id=round_id,
        ))
