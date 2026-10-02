"""Pre-launch audit against a disposable, real PostgreSQL database.

Run explicitly with either ``CR_TEST_PG_DSN`` or ``LANGCHAO_TEST_POSTGRES_DSN``.
The suite creates and drops one isolated schema.  It never constructs a transport,
sender, webhook, OneBot client, or other network-delivery component: "sending" is
represented only by durable claim/attempt/outbox rows and terminal ACK repository calls.
"""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

psycopg = pytest.importorskip("psycopg")

from companion_runtime.langchao_authority import AuthorityEngine, AuthorityMode
from companion_runtime.langchao_authority_repository import (
    AuthorityConflictError,
    LangchaoAuthorityRepository,
)
from companion_runtime.langchao_live_repository import LangchaoLiveCommit, LangchaoLiveRepository
from companion_runtime.langchao_types import (
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    SettlementType,
)
from companion_runtime.user_model_v2_migrations import migrate

_DSN = (
    os.environ.get("CR_TEST_PG_DSN", "").strip()
    or os.environ.get("LANGCHAO_TEST_POSTGRES_DSN", "").strip()
)
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="set CR_TEST_PG_DSN or LANGCHAO_TEST_POSTGRES_DSN to a disposable PostgreSQL database",
)


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def _connect(schema: str, *, autocommit: bool = False):
    connection = psycopg.connect(_DSN, autocommit=autocommit, row_factory=psycopg.rows.dict_row)
    connection.execute(f'SET search_path TO "{schema}", public')
    if not autocommit:
        connection.commit()
    return connection


@pytest.fixture(scope="module")
def pg_schema():
    schema = "langchao_prelaunch_" + uuid.uuid4().hex[:12]
    connection = psycopg.connect(_DSN, autocommit=False, row_factory=psycopg.rows.dict_row)
    try:
        with connection.transaction():
            first = migrate(connection, schema=schema, runner_version="langchao-prelaunch-audit/1")
        assert first.applied == tuple(range(1, 21))
        assert first.current_version == 20
        yield schema
    finally:
        connection.rollback()
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()


@pytest.fixture()
def connection(pg_schema):
    connection = _connect(pg_schema)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _activate_langchao(connection, scope: str) -> None:
    repository = LangchaoAuthorityRepository(connection, scope_key=scope)
    repository.bootstrap()
    repository.switch_authority(
        engine_key=AuthorityEngine.LANGCHAO,
        mode=AuthorityMode.LIVE,
        expected_pointer_version=1,
        in_flight_count=0,
        reason="prelaunch PostgreSQL audit",
    )


def _seed_claim_attempt_outbox(
    connection,
    *,
    scope: str,
    round_id: str,
    candidate_id: str,
    attempt_id: str,
    outbox_id: str,
    claim_id: str,
) -> None:
    authority = LangchaoAuthorityRepository(connection, scope_key=scope)
    authority.create_live_dispatch_claim(
        claim_id=claim_id,
        round_id=round_id,
        candidate_id=candidate_id,
        candidate_version="audit-candidate-v1",
        attempt_id=attempt_id,
        render_outbox_id=outbox_id,
        idempotency_key=f"prelaunch:{round_id}",
        expected_engine=AuthorityEngine.LANGCHAO,
    )
    now = datetime.now(timezone.utc)
    connection.execute(
        """INSERT INTO action_attempts
           (attempt_id,candidate_id,state,intent,created_at,updated_at,committed_at,outbox_id)
           VALUES (%s,%s,'committed','prelaunch-audit',%s,%s,%s,%s)""",
        (attempt_id, candidate_id, now, now, now, outbox_id),
    )
    connection.execute(
        """INSERT INTO outbox
           (outbox_id,kind,payload_json,status,priority,created_at)
           VALUES (%s,'render',%s::jsonb,'pending',100,%s)""",
        (outbox_id, json.dumps({"attempt_id": attempt_id, "decision_id": round_id}), now),
    )


def _commit(
    *,
    scope: str,
    round_id: str,
    candidate_id: str,
    attempt_id: str,
    outbox_id: str,
    claim_id: str,
    expected_tokens: tuple[OutcomeToken, ...] = (),
    reward_contract_id: str = "audit-reward",
) -> LangchaoLiveCommit:
    return LangchaoLiveCommit(
        scope_key=scope,
        round_id=round_id,
        langchao_candidate_id=candidate_id,
        candidate_revision=1,
        source_candidate_id=candidate_id,
        reward_contract_id=reward_contract_id,
        reward_revision=1,
        expected_tokens=expected_tokens,
        attempt_id=attempt_id,
        render_outbox_id=outbox_id,
        claim_id=claim_id,
        committed_at=datetime.now(timezone.utc),
    )


def _seed_reward(connection, *, scope: str, reward_id: str, goal_id: str, episode_id: str) -> None:
    now = datetime.now(timezone.utc)
    payload = json.dumps(
        {"goal_id": goal_id, "episode_id": episode_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        "INSERT INTO langchao_goal_identities(scope_key,goal_id,semantic_key) VALUES (%s,%s,%s)",
        (scope, goal_id, _name("goal-semantic")),
    )
    connection.execute(
        """INSERT INTO langchao_reward_identities
           (scope_key,reward_contract_id,semantic_key) VALUES (%s,%s,%s)""",
        (scope, reward_id, _name("reward-semantic")),
    )
    connection.execute(
        """INSERT INTO langchao_reward_revisions
           (scope_key,reward_contract_id,revision,payload,payload_sha256,goal_id,total_cap,
            contract_created_at,contract_updated_at)
           VALUES (%s,%s,1,%s::jsonb,%s,%s,100,%s,%s)""",
        (scope, reward_id, payload, "0" * 64, goal_id, now, now),
    )


def _delivery_token(*, scope: str, goal_id: str, episode_id: str) -> OutcomeToken:
    return OutcomeToken(
        token_id=_name("expected-delivery"),
        scope_key=scope,
        goal_id=goal_id,
        episode_id=episode_id,
        outcome_key="delivery",
        settlement_type=SettlementType.EXPECTED,
        status=OutcomeStatus.UNEXECUTED,
        base_amount=1.0,
        direction_weights=((MotivationDirection.EXPRESSION, 1.0),),
        evidence_version="langchao.prelaunch-audit.v1",
        idempotency_key=_name("expected-delivery-idem"),
    )


def test_v20_migration_second_pass_is_strictly_idempotent(pg_schema) -> None:
    connection = _connect(pg_schema)
    try:
        with connection.transaction():
            result = migrate(connection, schema=pg_schema, runner_version="langchao-prelaunch-audit/1")
        assert result.current_version == 20
        assert result.applied == ()
        assert result.already_present == tuple(range(1, 21))
        v20 = connection.execute(
            "SELECT runner_version FROM schema_migrations_v2 WHERE version = 20"
        ).fetchone()
        assert v20 is not None
    finally:
        connection.close()


@pytest.mark.parametrize("failure", ("orphan", "wrong_claim"))
def test_v20_exact_live_fk_rejects_orphan_and_wrong_claim(connection, failure: str) -> None:
    scope, round_id = _name("scope"), _name("round")
    candidate_id, attempt_id = _name("candidate"), _name("attempt")
    outbox_id, claim_id = _name("render"), _name("claim")

    with pytest.raises(psycopg.IntegrityError):
        with connection.transaction():
            if failure == "wrong_claim":
                _activate_langchao(connection, scope)
                _seed_claim_attempt_outbox(
                    connection,
                    scope=scope,
                    round_id=round_id,
                    candidate_id=candidate_id,
                    attempt_id=attempt_id,
                    outbox_id=outbox_id,
                    claim_id=claim_id,
                )
            LangchaoLiveRepository(connection, scope_key=scope).save_commit(
                _commit(
                    scope=scope,
                    round_id=round_id,
                    candidate_id=candidate_id,
                    attempt_id=attempt_id,
                    outbox_id=outbox_id,
                    claim_id=claim_id if failure == "orphan" else _name("wrong-claim"),
                )
            )
            # The exact witness is intentionally deferred, so force audit-time validation.
            connection.execute("SET CONSTRAINTS fk_langchao_live_commit_exact_claim IMMEDIATE")


def test_authority_pointer_cas_has_one_concurrent_winner(pg_schema) -> None:
    scope = _name("cas-scope")
    setup = _connect(pg_schema)
    try:
        with setup.transaction():
            LangchaoAuthorityRepository(setup, scope_key=scope).bootstrap()
    finally:
        setup.close()

    gate = Barrier(2)

    def compete(engine: AuthorityEngine) -> str:
        worker = _connect(pg_schema)
        try:
            gate.wait(timeout=10)
            try:
                with worker.transaction():
                    LangchaoAuthorityRepository(worker, scope_key=scope).switch_authority(
                        engine_key=engine,
                        mode=AuthorityMode.LIVE,
                        expected_pointer_version=1,
                        in_flight_count=0,
                        reason=f"concurrent CAS to {engine.value}",
                    )
            except AuthorityConflictError:
                return "conflict"
            return "won"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, (AuthorityEngine.RUNTIME_V2, AuthorityEngine.LANGCHAO)))
    assert sorted(results) == ["conflict", "won"]

    observer = _connect(pg_schema, autocommit=True)
    try:
        row = observer.execute(
            "SELECT pointer_version FROM langchao_authority_active WHERE scope_key=%s", (scope,)
        ).fetchone()
        assert row["pointer_version"] == 2
        count = observer.execute(
            "SELECT count(*) AS n FROM langchao_authority_revisions WHERE scope_key=%s", (scope,)
        ).fetchone()
        assert count["n"] == 2
    finally:
        observer.close()


def test_single_transaction_callback_failure_rolls_back_every_witness(pg_schema) -> None:
    scope, round_id = _name("rollback-scope"), _name("rollback-round")
    candidate_id, attempt_id = _name("candidate"), _name("attempt")
    outbox_id, claim_id = _name("render"), _name("claim")
    connection = _connect(pg_schema)

    def failing_callback() -> None:
        LangchaoLiveRepository(connection, scope_key=scope).save_commit(
            _commit(
                scope=scope,
                round_id=round_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                outbox_id=outbox_id,
                claim_id=claim_id,
            )
        )
        raise RuntimeError("injected prelaunch callback failure")

    try:
        with pytest.raises(RuntimeError, match="callback failure"):
            with connection.transaction():
                _activate_langchao(connection, scope)
                _seed_claim_attempt_outbox(
                    connection,
                    scope=scope,
                    round_id=round_id,
                    candidate_id=candidate_id,
                    attempt_id=attempt_id,
                    outbox_id=outbox_id,
                    claim_id=claim_id,
                )
                failing_callback()
    finally:
        connection.close()

    observer = _connect(pg_schema, autocommit=True)
    try:
        probes = (
            ("langchao_authority_active", "scope_key", scope),
            ("live_dispatch_claims", "claim_id", claim_id),
            ("action_attempts", "attempt_id", attempt_id),
            ("outbox", "outbox_id", outbox_id),
            ("langchao_live_commits", "round_id", round_id),
        )
        for table, column, value in probes:
            row = observer.execute(
                f"SELECT count(*) AS n FROM {table} WHERE {column}=%s", (value,)
            ).fetchone()
            assert row["n"] == 0, table
    finally:
        observer.close()


def test_terminal_ack_concurrency_is_exactly_once(pg_schema) -> None:
    scope, round_id = _name("ack-scope"), _name("ack-round")
    candidate_id, attempt_id = _name("candidate"), _name("attempt")
    outbox_id, claim_id = _name("render"), _name("claim")
    reward_id, goal_id, episode_id = _name("reward"), _name("goal"), _name("episode")
    expected = _delivery_token(scope=scope, goal_id=goal_id, episode_id=episode_id)
    setup = _connect(pg_schema)
    try:
        with setup.transaction():
            _activate_langchao(setup, scope)
            _seed_reward(
                setup, scope=scope, reward_id=reward_id, goal_id=goal_id, episode_id=episode_id
            )
            _seed_claim_attempt_outbox(
                setup,
                scope=scope,
                round_id=round_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                outbox_id=outbox_id,
                claim_id=claim_id,
            )
            LangchaoLiveRepository(setup, scope_key=scope).save_commit(
                _commit(
                    scope=scope,
                    round_id=round_id,
                    candidate_id=candidate_id,
                    attempt_id=attempt_id,
                    outbox_id=outbox_id,
                    claim_id=claim_id,
                    expected_tokens=(expected,),
                    reward_contract_id=reward_id,
                )
            )
    finally:
        setup.close()

    gate = Barrier(2)
    ack_id = _name("terminal-ack")

    def acknowledge() -> int:
        worker = _connect(pg_schema)
        try:
            gate.wait(timeout=10)
            with worker.transaction():
                settled = LangchaoLiveRepository(worker, scope_key=scope).settle_terminal(
                    round_id=round_id,
                    attempt_id=attempt_id,
                    ack_id=ack_id,
                    sent=True,
                    acknowledged_at=datetime.now(timezone.utc),
                )
            return len(settled)
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        settled_counts = list(pool.map(lambda _index: acknowledge(), range(2)))
    assert sorted(settled_counts) == [0, 1]

    observer = _connect(pg_schema, autocommit=True)
    try:
        commit_row = observer.execute(
            """SELECT terminal_ack_id,terminal_ack_kind
               FROM langchao_live_commits WHERE scope_key=%s AND round_id=%s""",
            (scope, round_id),
        ).fetchone()
        assert commit_row == {"terminal_ack_id": ack_id, "terminal_ack_kind": "sent"}
        outcome_count = observer.execute(
            """SELECT count(*) AS n FROM langchao_outcome_revisions
               WHERE scope_key=%s AND evidence_version='langchao.live-ack.v1'""",
            (scope,),
        ).fetchone()
        assert outcome_count["n"] == 1
    finally:
        observer.close()


def test_restart_recovers_pending_without_dispatching(pg_schema) -> None:
    scope, round_id = _name("restart-scope"), _name("restart-round")
    candidate_id, attempt_id = _name("candidate"), _name("attempt")
    outbox_id, claim_id = _name("render"), _name("claim")
    first = _connect(pg_schema)
    try:
        with first.transaction():
            _activate_langchao(first, scope)
            _seed_claim_attempt_outbox(
                first,
                scope=scope,
                round_id=round_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                outbox_id=outbox_id,
                claim_id=claim_id,
            )
            LangchaoLiveRepository(first, scope_key=scope).save_commit(
                _commit(
                    scope=scope,
                    round_id=round_id,
                    candidate_id=candidate_id,
                    attempt_id=attempt_id,
                    outbox_id=outbox_id,
                    claim_id=claim_id,
                )
            )
    finally:
        first.close()

    restarted = _connect(pg_schema)
    try:
        pending = LangchaoLiveRepository(restarted, scope_key=scope).pending()
        assert len(pending) == 1
        assert pending[0].round_id == round_id
        assert pending[0].attempt_id == attempt_id
        # Reading recovery state cannot perform a real send or mutate the durable outbox.
        row = restarted.execute(
            "SELECT status,attempts,acked_at FROM outbox WHERE outbox_id=%s", (outbox_id,)
        ).fetchone()
        assert row == {"status": "pending", "attempts": 0, "acked_at": None}
    finally:
        restarted.close()


def test_cross_scope_live_commit_is_rejected_by_exact_fk(connection) -> None:
    claim_scope, commit_scope = _name("scope-a"), _name("scope-b")
    round_id, candidate_id = _name("round"), _name("candidate")
    attempt_id, outbox_id, claim_id = _name("attempt"), _name("render"), _name("claim")

    with pytest.raises(psycopg.IntegrityError):
        with connection.transaction():
            _activate_langchao(connection, claim_scope)
            _seed_claim_attempt_outbox(
                connection,
                scope=claim_scope,
                round_id=round_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                outbox_id=outbox_id,
                claim_id=claim_id,
            )
            LangchaoLiveRepository(connection, scope_key=commit_scope).save_commit(
                _commit(
                    scope=commit_scope,
                    round_id=round_id,
                    candidate_id=candidate_id,
                    attempt_id=attempt_id,
                    outbox_id=outbox_id,
                    claim_id=claim_id,
                )
            )
            connection.execute("SET CONSTRAINTS fk_langchao_live_commit_exact_claim IMMEDIATE")
