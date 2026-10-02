"""Real-PostgreSQL regression coverage for the Langchao v21-v23 repairs.

Run against a disposable database with ``CR_TEST_PG_DSN`` (preferred) or
``LANGCHAO_TEST_POSTGRES_DSN``.  Every run owns and drops an isolated schema.
"""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")

from companion_runtime.langchao_live_repository import LangchaoLiveCommit, LangchaoLiveRepository
from companion_runtime.langchao_outcome_repository import LangchaoOutcomeRepository
from companion_runtime.langchao_user_outcomes import LangchaoUserOutcomeSettler
from companion_runtime.langchao_social_wiring import (
    LangchaoSocialSnapshotService,
    build_langchao_social_service,
    decode_exact_ref,
    encode_exact_ref,
)
from companion_runtime.langchao_types import (
    MotivationDirection,
    OutcomeStatus,
    OutcomeToken,
    SettlementType,
)
from companion_runtime.privacy_deletion_repository import (
    DeletionRequest,
    DeletionStrategy,
    PrivacyDeletionRepository,
    WORK_KINDS,
)
from companion_runtime.user_model_v2_labels import SettlementContextV2, TargetObservationV2
from companion_runtime.user_model_v2_migrations import migrate
from companion_runtime.user_model_v2_schema import USER_MODEL_SCHEMA_VERSION
from companion_runtime.user_model_v2_service import UserModelV2Service
from companion_runtime.user_model_v2_service_repository import PostgresUserModelV2ServiceRepository
from companion_runtime.user_model_v2_types import LabelStatus, Target

_DSN = (
    os.environ.get("CR_TEST_PG_DSN", "").strip()
    or os.environ.get("LANGCHAO_TEST_POSTGRES_DSN", "").strip()
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _DSN,
        reason="set CR_TEST_PG_DSN or LANGCHAO_TEST_POSTGRES_DSN to disposable PostgreSQL",
    ),
]

NOW = datetime(2027, 1, 2, 12, tzinfo=timezone.utc)
WEIGHTS = ((MotivationDirection.APPROACH, 1.0),)


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def _connect(schema: str, *, autocommit: bool = False):
    connection = psycopg.connect(_DSN, autocommit=autocommit, row_factory=psycopg.rows.dict_row)
    connection.execute(f'SET search_path TO "{schema}", public')
    if not autocommit:
        connection.commit()
    return connection


@pytest.fixture(scope="module")
def pg_schema():
    schema = "langchao_repairs_" + uuid.uuid4().hex[:12]
    connection = psycopg.connect(_DSN, row_factory=psycopg.rows.dict_row)
    try:
        with connection.transaction():
            result = migrate(connection, schema=schema, runner_version="langchao-repairs-pg/1")
        assert result.current_version == USER_MODEL_SCHEMA_VERSION == 23
        yield schema
    finally:
        connection.rollback()
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()


@pytest.fixture
def connection(pg_schema):
    value = _connect(pg_schema)
    try:
        yield value
    finally:
        value.rollback()
        value.close()


def _deletion(scope: str, request_id: str) -> DeletionRequest:
    return DeletionRequest(
        scope_key=scope,
        request_id=request_id,
        requested_by="user",
        selector_kind="source",
        selector={"source_kind": "event", "source_id": _id("private-event")},
        strategy=DeletionStrategy.TOMBSTONE,
        requested_at=NOW,
    )


def test_v21_privacy_request_is_durable_duplicate_safe_and_scope_isolated(pg_schema) -> None:
    scope_a, scope_b, request_id = _id("privacy-a"), _id("privacy-b"), _id("delete")
    request = _deletion(scope_a, request_id)
    first = _connect(pg_schema)
    try:
        assert PrivacyDeletionRepository(first, scope_key=scope_a).create_request(request) is True
    finally:
        first.close()  # committed transaction survives a process/connection boundary

    restarted = _connect(pg_schema)
    try:
        local = PrivacyDeletionRepository(restarted, scope_key=scope_a)
        assert local.create_request(request) is False
        assert local.status(request_id=request_id)["status"] == "pending"
        rows = restarted.execute(
            """SELECT work_kind,status,attempt_count FROM privacy_deletion_work_v1
               WHERE scope_key=%s AND request_id=%s ORDER BY ordinal""",
            (scope_a, request_id),
        ).fetchall()
        assert len(rows) == len(WORK_KINDS)
        assert {row["status"] for row in rows} == {"pending"}
        assert {row["attempt_count"] for row in rows} == {0}

        foreign = PrivacyDeletionRepository(restarted, scope_key=scope_b)
        assert foreign.status(request_id=request_id) is None
        assert foreign.claim_next(
            request_id=request_id,
            worker_id="foreign-worker",
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        ) is None
        with pytest.raises(ValueError, match="another scope"):
            foreign.create_request(request)
    finally:
        restarted.close()


def test_v21_expired_lease_is_reclaimed_once_after_restart(pg_schema) -> None:
    scope, request_id = _id("lease-scope"), _id("lease-request")
    setup = _connect(pg_schema)
    try:
        repo = PrivacyDeletionRepository(setup, scope_key=scope)
        assert repo.create_request(_deletion(scope, request_id))
        claimed = repo.claim_next(
            request_id=request_id,
            worker_id="worker:crashed",
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=5),
        )
        assert claimed is not None
        work_kind = claimed.work_kind
    finally:
        setup.close()

    before_expiry = _connect(pg_schema)
    try:
        # The next ordinal is claimable, but the leased first work item itself must not
        # be duplicated before expiry.
        row = before_expiry.execute(
            """SELECT status,lease_owner,attempt_count FROM privacy_deletion_work_v1
               WHERE scope_key=%s AND request_id=%s AND work_kind=%s""",
            (scope, request_id, work_kind),
        ).fetchone()
        assert row == {"status": "running", "lease_owner": "worker:crashed", "attempt_count": 1}
    finally:
        before_expiry.close()

    restarted = _connect(pg_schema)
    try:
        reclaimed = PrivacyDeletionRepository(restarted, scope_key=scope).claim_next(
            request_id=request_id,
            worker_id="worker:replacement",
            now=NOW + timedelta(minutes=6),
            lease_expires_at=NOW + timedelta(minutes=11),
        )
        assert reclaimed is not None and reclaimed.work_kind == work_kind
        row = restarted.execute(
            """SELECT status,lease_owner,attempt_count FROM privacy_deletion_work_v1
               WHERE scope_key=%s AND request_id=%s AND work_kind=%s""",
            (scope, request_id, work_kind),
        ).fetchone()
        assert row == {"status": "running", "lease_owner": "worker:replacement", "attempt_count": 2}
    finally:
        restarted.close()


def _insert_witness(connection, *, scope: str, task: str, digest: str, task_status: str = "succeeded",
                    artifact_status: str = "active") -> str:
    witness_id = str(uuid.uuid4())
    tombstoned = artifact_status == "tombstoned"
    connection.execute(
        """INSERT INTO capability_artifact_witnesses
           (witness_id,scope_key,capability,operation,task_run_id,task_status,
            artifact_sha256,artifact_type,artifact_status,created_at,source_refs,
            tombstoned_at,tombstone_reason)
           VALUES (%s,%s,'web_retrieval','research',%s,%s,%s,'research_report',%s,%s,
                   %s::jsonb,%s,%s)""",
        (
            witness_id, scope, task, task_status, digest, artifact_status, NOW,
            json.dumps(["source:1"]), NOW if tombstoned else None,
            "privacy deletion" if tombstoned else None,
        ),
    )
    return witness_id


def test_v22_artifact_lookup_requires_exact_scope_task_hash_and_success_active_status(connection) -> None:
    scope, task, digest = _id("witness-scope"), _id("task"), "a" * 64
    _insert_witness(connection, scope=scope, task=task, digest=digest)
    connection.commit()

    def exact(scope_key: str, task_run_id: str, artifact_hash: str):
        return connection.execute(
            """SELECT witness_id FROM capability_artifact_witnesses
               WHERE scope_key=%s AND task_run_id=%s AND artifact_sha256=%s
                 AND task_status='succeeded' AND artifact_status='active'""",
            (scope_key, task_run_id, artifact_hash),
        ).fetchone()

    assert exact(scope, task, digest) is not None
    assert exact(_id("other-scope"), task, digest) is None
    assert exact(scope, _id("other-task"), digest) is None
    assert exact(scope, task, "b" * 64) is None

    _insert_witness(
        connection, scope=scope, task=_id("failed-task"), digest="c" * 64,
        task_status="failed",
    )
    _insert_witness(
        connection, scope=scope, task=_id("dead-task"), digest="d" * 64,
        artifact_status="tombstoned",
    )
    assert connection.execute(
        """SELECT count(*) AS n FROM capability_artifact_witnesses
           WHERE scope_key=%s AND task_status='succeeded' AND artifact_status='active'""",
        (scope,),
    ).fetchone()["n"] == 1


@pytest.mark.parametrize(
    ("digest", "task_status", "artifact_status"),
    [("not-a-hash", "succeeded", "active"), ("e" * 64, "invented", "active"),
     ("f" * 64, "succeeded", "missing")],
)
def test_v22_database_rejects_invalid_hash_and_status_values(
    connection, digest: str, task_status: str, artifact_status: str
) -> None:
    with pytest.raises(psycopg.IntegrityError):
        with connection.transaction():
            _insert_witness(
                connection, scope=_id("scope"), task=_id("task"), digest=digest,
                task_status=task_status, artifact_status=artifact_status,
            )


def _seed_reward_and_exposure(connection, *, scope: str, reward: str, goal: str,
                              episode: str, exposure_id: str) -> None:
    payload = json.dumps({"goal_id": goal, "episode_id": episode}, sort_keys=True)
    connection.execute(
        "INSERT INTO langchao_goal_identities(scope_key,goal_id,semantic_key) VALUES (%s,%s,%s)",
        (scope, goal, _id("goal-semantic")),
    )
    connection.execute(
        """INSERT INTO langchao_reward_identities
           (scope_key,reward_contract_id,semantic_key) VALUES (%s,%s,%s)""",
        (scope, reward, _id("reward-semantic")),
    )
    connection.execute(
        """INSERT INTO langchao_reward_revisions
           (scope_key,reward_contract_id,revision,payload,payload_sha256,goal_id,total_cap,
            contract_created_at,contract_updated_at)
           VALUES (%s,%s,1,%s::jsonb,%s,%s,10,%s,%s)""",
        (scope, reward, payload, "0" * 64, goal, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO interaction_exposures_v2
           (exposure_id,scope_key,idempotency_key,occurred_at,action,context,propensity)
           VALUES (%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,1.0)""",
        (exposure_id, scope, _id("exposure-idem"), NOW),
    )


def _actual(*, scope: str, goal: str, episode: str, token_id: str, idem: str,
            status: OutcomeStatus = OutcomeStatus.NOT_OBSERVED,
            settlement: SettlementType = SettlementType.ACTUAL,
            corrects: str | None = None) -> OutcomeToken:
    return OutcomeToken(
        token_id=token_id,
        scope_key=scope,
        goal_id=goal,
        episode_id=episode,
        outcome_key="reply",
        settlement_type=settlement,
        status=status,
        base_amount=0.0 if status is not OutcomeStatus.CONFIRMED else 1.0,
        direction_weights=WEIGHTS,
        evidence_version="langchao.repairs-pg.v1",
        idempotency_key=idem,
        observation_started_at=NOW,
        observation_ends_at=NOW + timedelta(hours=1),
        corrects_token_id=corrects,
    )


def _seed_atomic_user_settlement(connection, *, scope: str, attempt: str):
    reward, goal, episode = _id("reward"), _id("goal"), _id("episode")
    adapter = PostgresUserModelV2ServiceRepository(connection)
    prepared = UserModelV2Service(adapter).prepare_exposure(
        scope_key=scope,
        exposure_id=attempt,
        idempotency_key=f"delivery:{attempt}",
        occurred_at=NOW,
        action={"type": "reply"},
        context_provider=lambda: {},
        delivery_confirmed=True,
        horizons={target: 3600 for target in Target},
    )
    assert prepared is not None
    _seed_reward_and_exposure(
        connection, scope=scope, reward=reward, goal=goal, episode=episode,
        exposure_id=str(uuid.uuid4()),
    )
    expected = OutcomeToken(
        token_id=_id("expected"), scope_key=scope, goal_id=goal, episode_id=episode,
        outcome_key="reply", settlement_type=SettlementType.EXPECTED,
        status=OutcomeStatus.UNEXECUTED, base_amount=1.0, direction_weights=WEIGHTS,
        evidence_version="atomic-user-settlement.v1", idempotency_key=_id("expected-idem"),
    )
    round_id = _id("round")
    candidate_id = _id("candidate")
    outbox_id = _id("outbox")
    claim_id = _id("claim")
    from companion_runtime.langchao_authority_repository import LangchaoAuthorityRepository
    from companion_runtime.langchao_authority import AuthorityEngine, AuthorityMode
    authority = LangchaoAuthorityRepository(connection, scope_key=scope)
    authority.initialize(
        engine_key=AuthorityEngine.LANGCHAO, mode=AuthorityMode.LIVE,
        reason="atomic outcome test", created_at=NOW,
    )
    authority.create_live_dispatch_claim(
        claim_id=claim_id, round_id=round_id, candidate_id=candidate_id,
        candidate_version="atomic-outcome.v1", attempt_id=attempt,
        render_outbox_id=outbox_id, idempotency_key=f"atomic:{attempt}",
        expected_engine=AuthorityEngine.LANGCHAO, created_at=NOW,
    )
    connection.execute(
        """INSERT INTO action_attempts
           (attempt_id,candidate_id,state,intent,created_at,updated_at,committed_at,outbox_id,
            dispatch_scope_key,dispatch_claim_id)
           VALUES (%s,%s,'committed','atomic-outcome',%s,%s,%s,%s,%s,%s)""",
        (attempt, candidate_id, NOW, NOW, NOW, outbox_id, scope, claim_id),
    )
    connection.execute(
        """INSERT INTO outbox
           (outbox_id,kind,payload_json,status,priority,created_at,dispatch_scope_key,dispatch_claim_id)
           VALUES (%s,'render',%s::jsonb,'pending',100,%s,%s,%s)""",
        (outbox_id, json.dumps({"attempt_id": attempt, "decision_id": round_id}), NOW, scope, claim_id),
    )
    live = LangchaoLiveRepository(connection, scope_key=scope)
    live.save_commit(LangchaoLiveCommit(
        scope_key=scope, round_id=round_id, langchao_candidate_id=candidate_id,
        candidate_revision=1, source_candidate_id=_id("source"),
        reward_contract_id=reward, reward_revision=1, expected_tokens=(expected,),
        attempt_id=attempt, render_outbox_id=outbox_id, claim_id=claim_id,
        committed_at=NOW,
    ))
    connection.execute(
        """UPDATE langchao_live_commits
           SET terminal_ack_id=%s, terminal_ack_kind='sent', terminal_acknowledged_at=%s
           WHERE scope_key=%s AND attempt_id=%s""",
        (_id("ack"), NOW, scope, attempt),
    )
    connection.commit()
    return prepared, reward


def _reply_observation(prepared):
    return TargetObservationV2(
        event_id=_id("reply-event"), target=Target.REPLY,
        occurred_at=NOW + timedelta(minutes=1), value=True,
        candidate_exposure_ids=(prepared.exposure.exposure_id,),
    )


def test_v23_observer_failure_rolls_back_label_and_outcome_together(connection) -> None:
    scope, attempt = _id("atomic-rollback"), _id("attempt")
    prepared, _reward = _seed_atomic_user_settlement(connection, scope=scope, attempt=attempt)
    adapter = PostgresUserModelV2ServiceRepository(connection)
    real = LangchaoUserOutcomeSettler(LangchaoLiveRepository(connection, scope_key=scope))

    class FailingObserver:
        def settle_labels(self, labels):
            real.settle_labels(labels)
            raise RuntimeError("injected observer failure")

    service = UserModelV2Service(adapter, outcome_observer=FailingObserver())
    with pytest.raises(RuntimeError, match="injected observer failure"):
        service.settle_observation(
            prepared=prepared, observations=(_reply_observation(prepared),),
            context=SettlementContextV2(as_of=NOW + timedelta(minutes=2)),
            target=Target.REPLY,
        )
    active = adapter.get_active_label(
        scope_key=scope, exposure_id=prepared.exposure.exposure_id, target=Target.REPLY,
    )
    assert active is not None and active[1] == 1 and active[0].status is LabelStatus.PENDING
    assert connection.execute(
        "SELECT count(*) AS n FROM langchao_outcome_revisions WHERE scope_key=%s", (scope,),
    ).fetchone()["n"] == 0
    assert connection.execute(
        "SELECT count(*) AS n FROM langchao_user_outcome_active WHERE scope_key=%s", (scope,),
    ).fetchone()["n"] == 0


def test_v23_concurrent_duplicate_user_settlement_commits_one_revision(pg_schema) -> None:
    scope, attempt = _id("atomic-concurrent"), _id("attempt")
    setup = _connect(pg_schema)
    try:
        prepared, _reward = _seed_atomic_user_settlement(
            setup, scope=scope, attempt=attempt,
        )
    finally:
        setup.close()
    gate = Barrier(2)

    def settle(_worker_number: int):
        worker = _connect(pg_schema)
        try:
            adapter = PostgresUserModelV2ServiceRepository(worker)
            persisted = adapter.get_prepared_exposure(
                scope_key=scope, idempotency_key=f"delivery:{attempt}",
            )
            assert persisted is not None
            service = UserModelV2Service(
                adapter,
                outcome_observer=LangchaoUserOutcomeSettler(
                    LangchaoLiveRepository(worker, scope_key=scope)
                ),
            )
            gate.wait(timeout=10)
            return service.settle_observation(
                prepared=persisted, observations=(_reply_observation(persisted),),
                context=SettlementContextV2(as_of=NOW + timedelta(minutes=2)),
                target=Target.REPLY,
            )[0].status
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(settle, range(2))) == [
            LabelStatus.OBSERVED_POSITIVE, LabelStatus.OBSERVED_POSITIVE,
        ]
    observer = _connect(pg_schema, autocommit=True)
    try:
        assert observer.execute(
            "SELECT count(*) AS n FROM interaction_target_labels_v2 "
            "WHERE scope_key=%s AND exposure_id=%s AND target_name='reply'",
            (scope, prepared.exposure.exposure_id),
        ).fetchone()["n"] == 2
        assert observer.execute(
            "SELECT count(*) AS n FROM langchao_outcome_revisions WHERE scope_key=%s", (scope,),
        ).fetchone()["n"] == 1
        assert observer.execute(
            "SELECT count(*) AS n FROM langchao_user_outcome_active WHERE scope_key=%s", (scope,),
        ).fetchone()["n"] == 1
    finally:
        observer.close()


def test_v23_restart_replay_keeps_atomic_user_settlement_exactly_once(pg_schema) -> None:
    scope, attempt = _id("atomic-restart"), _id("attempt")
    first = _connect(pg_schema)
    try:
        prepared, _reward = _seed_atomic_user_settlement(first, scope=scope, attempt=attempt)
        UserModelV2Service(
            PostgresUserModelV2ServiceRepository(first),
            outcome_observer=LangchaoUserOutcomeSettler(
                LangchaoLiveRepository(first, scope_key=scope)
            ),
        ).settle_observation(
            prepared=prepared, observations=(_reply_observation(prepared),),
            context=SettlementContextV2(as_of=NOW + timedelta(minutes=2)),
            target=Target.REPLY,
        )
    finally:
        first.close()

    restarted = _connect(pg_schema)
    try:
        adapter = PostgresUserModelV2ServiceRepository(restarted)
        persisted = adapter.get_prepared_exposure(
            scope_key=scope, idempotency_key=f"delivery:{attempt}",
        )
        assert persisted is not None
        UserModelV2Service(
            adapter,
            outcome_observer=LangchaoUserOutcomeSettler(
                LangchaoLiveRepository(restarted, scope_key=scope)
            ),
        ).settle_observation(
            prepared=persisted, observations=(_reply_observation(persisted),),
            context=SettlementContextV2(as_of=NOW + timedelta(minutes=2)),
            target=Target.REPLY,
        )
        assert restarted.execute(
            "SELECT count(*) AS n FROM interaction_target_labels_v2 "
            "WHERE scope_key=%s AND exposure_id=%s AND target_name='reply'",
            (scope, persisted.exposure.exposure_id),
        ).fetchone()["n"] == 2
        assert restarted.execute(
            "SELECT count(*) AS n FROM langchao_outcome_revisions WHERE scope_key=%s", (scope,),
        ).fetchone()["n"] == 1
        assert restarted.execute(
            "SELECT source_label_revision,pointer_version FROM langchao_user_outcome_active "
            "WHERE scope_key=%s", (scope,),
        ).fetchone() == {"source_label_revision": 2, "pointer_version": 1}
    finally:
        restarted.close()


def test_v23_active_pointer_and_correction_lineage_are_durable(connection) -> None:
    scope, reward, goal, episode = _id("outcome-scope"), _id("reward"), _id("goal"), _id("episode")
    exposure = str(uuid.uuid4())
    _seed_reward_and_exposure(
        connection, scope=scope, reward=reward, goal=goal, episode=episode, exposure_id=exposure,
    )
    repo = LangchaoOutcomeRepository(connection, scope_key=scope)
    first_id, correction_id = _id("actual"), _id("correction")
    first = _actual(scope=scope, goal=goal, episode=episode, token_id=first_id, idem=_id("idem"))
    repo.put_outcome_revision(first, revision=1, reward_contract_id=reward, reward_contract_revision=1)
    assert repo.activate_observation(
        reward_contract_id=reward, episode_id=episode, outcome_key="reply",
        token_id=first_id, revision=1, source_exposure_id=exposure,
        source_label_revision=2, expected_pointer_version=0,
    )

    correction = _actual(
        scope=scope, goal=goal, episode=episode, token_id=correction_id, idem=_id("idem"),
        status=OutcomeStatus.CORRECTED, settlement=SettlementType.CORRECTION, corrects=first_id,
    )
    repo.put_outcome_revision(
        correction, revision=1, reward_contract_id=reward, reward_contract_revision=1,
        corrects_revision=1,
    )
    assert repo.activate_observation(
        reward_contract_id=reward, episode_id=episode, outcome_key="reply",
        token_id=correction_id, revision=1, source_exposure_id=exposure,
        source_label_revision=3, expected_pointer_version=1,
    )
    assert not repo.activate_observation(
        reward_contract_id=reward, episode_id=episode, outcome_key="reply",
        token_id=first_id, revision=1, source_exposure_id=exposure,
        source_label_revision=2, expected_pointer_version=2,
    )
    active = repo.get_active_observation(
        reward_contract_id=reward, episode_id=episode, outcome_key="reply",
    )
    assert active["token_id"] == correction_id
    assert active["pointer_version"] == 2
    assert active["source_label_revision"] == 3
    assert active["corrects_token_id"] == first_id
    assert active["corrects_revision"] == 1


def test_v23_concurrent_duplicate_pointer_publish_has_one_winner(pg_schema) -> None:
    scope, reward, goal, episode = _id("cas-scope"), _id("reward"), _id("goal"), _id("episode")
    exposure = str(uuid.uuid4())
    token_ids = (_id("actual-a"), _id("actual-b"))
    setup = _connect(pg_schema)
    try:
        with setup.transaction():
            _seed_reward_and_exposure(
                setup, scope=scope, reward=reward, goal=goal, episode=episode,
                exposure_id=exposure,
            )
            repo = LangchaoOutcomeRepository(setup, scope_key=scope)
            for index, token_id in enumerate(token_ids):
                repo.put_outcome_revision(
                    _actual(
                        scope=scope, goal=goal, episode=episode, token_id=token_id,
                        idem=f"{scope}:concurrent:{index}", status=OutcomeStatus.CONFIRMED,
                    ),
                    revision=1, reward_contract_id=reward, reward_contract_revision=1,
                )
    finally:
        setup.close()

    gate = Barrier(2)

    def publish(token_id: str) -> bool:
        worker = _connect(pg_schema)
        try:
            gate.wait(timeout=10)
            with worker.transaction():
                return LangchaoOutcomeRepository(worker, scope_key=scope).activate_observation(
                    reward_contract_id=reward, episode_id=episode, outcome_key="reply",
                    token_id=token_id, revision=1, source_exposure_id=exposure,
                    source_label_revision=2, expected_pointer_version=0,
                )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, token_ids))
    assert sorted(results) == [False, True]
    observer = _connect(pg_schema, autocommit=True)
    try:
        row = observer.execute(
            """SELECT token_id,pointer_version,source_label_revision
               FROM langchao_user_outcome_active
               WHERE scope_key=%s AND reward_contract_id=%s AND episode_id=%s
                 AND outcome_key='reply'""",
            (scope, reward, episode),
        ).fetchone()
        assert row["token_id"] in token_ids
        assert row["pointer_version"] == 1 and row["source_label_revision"] == 2
        assert observer.execute(
            """SELECT count(*) AS n FROM langchao_user_outcome_active
               WHERE scope_key=%s AND reward_contract_id=%s AND episode_id=%s""",
            (scope, reward, episode),
        ).fetchone()["n"] == 1
    finally:
        observer.close()


class _MemoryProjection:
    def __init__(self, row):
        self.row = row

    def get_memory(self, memory_id):
        return self.row if memory_id == self.row.memory_id else None

    get = get_memory

    def list_memories(self, *, status, limit):
        assert self.row.status in status and limit == 100
        return [self.row]


class _EmptyProjection:
    def list_open(self):
        return []

    def active(self, now):
        return []

    def get(self, object_id):
        return None


def test_social_production_service_emits_and_revalidates_exact_postgres_refs(connection) -> None:
    scope = _id("social-scope")
    memory = SimpleNamespace(
        memory_id=_id("memory"), kind="relationship", summary="共同看过海",
        structured={}, topics=["海"], importance=0.8, confidence=0.9,
        status="active", source_event_ids=[_id("event")], created_at=NOW, updated_at=NOW,
    )
    runtime = SimpleNamespace(projections=SimpleNamespace(
        memory=_MemoryProjection(memory), unfinished=_EmptyProjection(), boundaries=_EmptyProjection(),
    ))
    service = build_langchao_social_service(connection=connection, scope_key=scope, runtime=runtime)
    assert isinstance(service, LangchaoSocialSnapshotService)
    result = service.refresh(now=NOW + timedelta(minutes=1))
    assert result is not None

    refs = service.candidate_refs([f"memory:{memory.memory_id}"])
    assert set(refs) == {"memory_ref", "social_ref"}
    memory_parts = decode_exact_ref(refs["memory_ref"])
    social_parts = decode_exact_ref(refs["social_ref"])
    assert memory_parts[0:3] == ("memory", scope, memory.memory_id)
    assert social_parts[0] == "social" and social_parts[1] == scope
    assert service.validate_candidate_action(refs)

    # Production validation is exact, not merely an existence check.
    bad_hash = dict(refs)
    kind, ref_scope, object_id, revision, _digest = social_parts
    bad_hash["social_ref"] = encode_exact_ref(
        kind, scope_key=ref_scope, object_id=object_id, revision=revision, digest="f" * 64,
    )
    assert not service.validate_candidate_action(bad_hash)
    cross_scope = dict(refs)
    cross_scope["social_ref"] = encode_exact_ref(
        kind, scope_key=_id("other-scope"), object_id=object_id,
        revision=revision, digest=social_parts[4],
    )
    assert not service.validate_candidate_action(cross_scope)

    item_source = connection.execute(
        """SELECT s.scope_key,s.source_id,s.source_revision,s.source_sha256
           FROM langchao_social_item_sources AS s
           WHERE s.scope_key=%s AND s.item_key=%s AND s.item_revision=%s""",
        (scope, object_id, revision),
    ).fetchone()
    assert item_source is not None
    assert item_source["scope_key"] == scope
    assert item_source["source_id"] == memory.memory_id
    assert item_source["source_revision"] == memory_parts[3]
    assert item_source["source_sha256"] == memory_parts[4]
