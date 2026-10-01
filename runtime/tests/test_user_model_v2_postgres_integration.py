"""Live PostgreSQL integration tests for the v2 migration/repository slice.

Set ``CR_TEST_PG_DSN`` to a disposable database.  The tests create and drop their own
schema, so they never depend on or mutate the legacy Runtime tables.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from companion_runtime.user_model_v2_migrations import migrate
from companion_runtime.user_model_v2_repository import UserModelV2Repository
from companion_runtime.user_model_v2_service import UserModelV2Service
from companion_runtime.user_model_v2_service_repository import PostgresUserModelV2ServiceRepository
from companion_runtime.composition_v2 import PostgresUserModelV2PredictionRepository
from companion_runtime.maintenance_v2 import V2Maintenance, V2MaintenanceConfig
from companion_runtime.motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
from companion_runtime.repeat_v2 import RepeatSubjectV2
from companion_runtime.runtime_repository_v2 import PostgresV2RuntimeRepository
from companion_runtime.runtime_v2 import CandidateV2, CommittedDecisionV2, PredictionSetV2
from companion_runtime.user_model_v2_prediction import UserModelV2PredictionService
from companion_runtime.decision_v2_audit import (
    CandidateAssessment, DecisionAuditRecorder, DecisionRun, DecisionStage,
)
from companion_runtime.user_model_v2_types import LabelStatus, SupportStatus, Target, TargetPredictionV2

_DSN = os.environ.get("CR_TEST_PG_DSN", "").strip()
pytestmark = pytest.mark.skipif(not _DSN, reason="set CR_TEST_PG_DSN for live PostgreSQL tests")


@pytest.fixture()
def pg_schema():
    schema = "v2test_" + uuid.uuid4().hex[:12]
    connection = psycopg.connect(_DSN, autocommit=False)
    connection.row_factory = psycopg.rows.dict_row
    try:
        with connection.transaction():
            result = migrate(connection, schema=schema)
        assert result.applied == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        yield connection, schema
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()


def _search_path(connection, schema: str) -> None:
    connection.execute(f'SET search_path TO "{schema}", public')


def test_native_migrations_are_idempotent_and_create_scoped_tables(pg_schema) -> None:
    connection, schema = pg_schema
    with connection.transaction():
        second = migrate(connection, schema=schema)
    assert second.applied == ()
    assert second.already_present == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)

    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name",
        (schema,),
    ).fetchall()
    names = {row["table_name"] for row in rows}
    assert {
        "interaction_exposures_v2",
        "interaction_target_labels_v2",
        "user_model_active_labels_v2",
        "prediction_snapshots_v2",
        "expectations_v2",
        "user_model_parameter_snapshots_v2",
        "user_model_active_parameters_v2",
        "wait_processes_v2",
        "schema_migrations_v2",
        "mechanical_history_import_audits_v1",
        "mechanical_history_import_quarantine_v1",
    } <= names


def test_repository_idempotency_scope_and_label_revision_on_real_postgres(pg_schema) -> None:
    connection, schema = pg_schema
    _search_path(connection, schema)
    repository = UserModelV2Repository(connection)
    now = datetime.now(timezone.utc)
    exposure_id = uuid.uuid4()
    other_scope_exposure = uuid.uuid4()

    with connection.transaction():
        first = repository.insert_exposure(
            scope_key="scope:a",
            exposure_id=exposure_id,
            occurred_at=now,
            action={"type": "contact", "proactive": True},
            context={"busy_probability": 0.2},
            propensity=0.5,
            idempotency_key="exp:scope-a:one",
        )
        replay = repository.insert_exposure(
            scope_key="scope:a",
            exposure_id=uuid.uuid4(),
            occurred_at=now,
            action={"type": "contact", "proactive": True},
            context={"busy_probability": 0.2},
            propensity=0.5,
            idempotency_key="exp:scope-a:one",
        )
        repository.insert_exposure(
            scope_key="scope:b",
            exposure_id=other_scope_exposure,
            occurred_at=now,
            action={"type": "contact", "proactive": True},
            context={},
            propensity=0.5,
            idempotency_key="exp:scope-b:one",
        )
    assert first == replay == exposure_id

    first_label = uuid.uuid4()
    second_label = uuid.uuid4()
    with connection.transaction():
        assert repository.insert_label_revision_and_activate(
            scope_key="scope:a",
            target_label_id=first_label,
            exposure_id=exposure_id,
            labelled_at=now,
            target_name="reply",
            target_value=True,
            evidence={"event": "reply-1"},
            expected_pointer_version=None,
            idempotency_key="label:scope-a:reply:1",
        )
    with connection.transaction():
        assert repository.insert_label_revision_and_activate(
            scope_key="scope:a",
            target_label_id=second_label,
            exposure_id=exposure_id,
            labelled_at=now,
            target_name="reply",
            target_value=False,
            evidence={"event": "late-correction"},
            expected_pointer_version=1,
            idempotency_key="label:scope-a:reply:2",
        )
    active = repository.get_active_label(
        scope_key="scope:a", exposure_id=exposure_id, target_name="reply"
    )
    assert active["target_label_id"] == second_label
    assert active["pointer_version"] == 2
    assert (
        repository.get_active_label(
            scope_key="scope:b", exposure_id=exposure_id, target_name="reply"
        )
        is None
    )


def test_service_adapter_round_trip_and_scope_isolation_on_real_postgres(pg_schema) -> None:
    connection, schema = pg_schema
    _search_path(connection, schema)
    adapter = PostgresUserModelV2ServiceRepository(connection)
    service = UserModelV2Service(adapter)
    now = datetime.now(timezone.utc)
    horizons = {target: (index + 1) * 60 for index, target in enumerate(Target)}
    exposure_id = str(uuid.uuid4())

    first = service.prepare_exposure(
        scope_key="scope:service-a",
        exposure_id=exposure_id,
        idempotency_key="delivery:service:one",
        occurred_at=now,
        action={"type": "follow_up", "proactive": True},
        context_provider=lambda: {"busy_probability": 0.2, "recent_contact_count": 3},
        delivery_confirmed=True,
        horizons=horizons,
        source_event_ids=("delivery-1",),
    )
    replay = service.prepare_exposure(
        scope_key="scope:service-a",
        exposure_id=str(uuid.uuid4()),
        idempotency_key="delivery:service:one",
        occurred_at=now,
        action={},
        context_provider=lambda: pytest.fail("replay must not recompute context"),
        delivery_confirmed=True,
        horizons=horizons,
    )
    assert first is not None and replay == first
    assert tuple(label.target for label in first.labels) == tuple(Target)
    assert adapter.get_active_label(
        scope_key="scope:service-a", exposure_id=exposure_id, target=Target.REPLY
    ) == (first.labels[0], 1)
    assert adapter.get_active_label(
        scope_key="scope:service-b", exposure_id=exposure_id, target=Target.REPLY
    ) is None
    assert tuple(
        adapter.list_active_training_records(scope_key="scope:service-b", target=Target.REPLY)
    ) == ()


def test_send_ack_freezes_expectation_once_and_maintenance_settles_real_postgres(pg_schema) -> None:
    connection, schema = pg_schema
    _search_path(connection, schema)
    scope = "scope:production-chain"
    adapter = PostgresUserModelV2ServiceRepository(connection)
    service = UserModelV2Service(adapter)
    prediction = UserModelV2PredictionService(
        PostgresUserModelV2PredictionRepository(adapter.repository)
    )
    runtime = PostgresV2RuntimeRepository(
        connection,
        prediction_service=prediction,
        service_repository=adapter,
        scope_key=scope,
        state_version_provider=lambda: 17,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    horizons = {target: 1 for target in Target}
    context_calls = 0

    def context():
        nonlocal context_calls
        context_calls += 1
        return {"busy_probability": 0.2, "recent_contact_count": 1}

    first = runtime.prepare_exposure_and_expectation(
        user_model=service,
        scope_key=scope,
        exposure_id=str(uuid.uuid4()),
        idempotency_key="send-ack:one",
        occurred_at=now,
        action={"type": "follow_up", "proactive": True},
        context_provider=context,
        horizons=horizons,
        delivery_basis=first_basis(),
        source_event_ids=("send-event:one",),
        concern_id="concern:one",
    )
    replay = runtime.prepare_exposure_and_expectation(
        user_model=service,
        scope_key=scope,
        exposure_id=str(uuid.uuid4()),
        idempotency_key="send-ack:one",
        occurred_at=now + timedelta(hours=1),
        action={},
        context_provider=lambda: pytest.fail("replay must not collect context or predict again"),
        horizons=horizons,
        delivery_basis=first_basis(),
        source_event_ids=(),
    )
    assert replay == first
    assert context_calls == 1
    row = connection.execute(
        "SELECT expectation, due_at FROM expectations_v2 WHERE scope_key = %s",
        (scope,),
    ).fetchone()
    assert row["expectation"]["envelope"]["based_on_state_version"] == 17
    assert row["expectation"]["exposure_id"] == first.exposure.exposure_id
    assert connection.execute(
        "SELECT count(*) FROM expectations_v2 WHERE scope_key = %s", (scope,)
    ).fetchone()["count"] == 1

    maintenance = V2Maintenance(
        scope_key=scope,
        user_model=service,
        repository=runtime,
        config=V2MaintenanceConfig(
            minimum_interval_seconds=1,
            fit_interval_seconds=9999,
        ),
        monotonic=iter((1.0, 2.0)).__next__,
    )
    result = maintenance.run_due(now=now + timedelta(seconds=2), fit=False)
    assert result.label_revisions_written == 4
    assert result.expectation_revisions_written == 4
    settlements = connection.execute(
        "SELECT target_name, settlement, emotion_shadow "
        "FROM runtime_v2_expectation_settlements WHERE scope_key = %s",
        (scope,),
    ).fetchall()
    assert len(settlements) == 4
    # Negative means that a negative reaction did not happen. It must remain target
    # outcome 0, not be reinterpreted as generic expectation/goal satisfaction.
    negative = next(item for item in settlements if item["target_name"] == "negative")
    assert negative["settlement"]["actual_outcome"] == 0.0
    assert negative["settlement"]["residual"] is None  # unavailable cold-start prediction
    assert negative["emotion_shadow"]["actual_outcome"] == 0.0
    status = connection.execute(
        "SELECT status, resolution FROM expectations_v2 WHERE scope_key = %s", (scope,)
    ).fetchone()
    assert status["status"] == "settled"
    assert "not goal satisfaction" in status["resolution"]["semantics"]
    replay_result = maintenance.run_due(
        now=now + timedelta(seconds=3), force=True, fit=False
    )
    assert replay_result.expectation_revisions_written == 0


def test_committed_decision_round_trips_and_terminal_ack_is_once(pg_schema) -> None:
    connection, schema = pg_schema
    _search_path(connection, schema)
    scope = "scope:committed"
    adapter = PostgresUserModelV2ServiceRepository(connection)
    prediction_service = UserModelV2PredictionService(
        PostgresUserModelV2PredictionRepository(adapter.repository)
    )
    runtime = PostgresV2RuntimeRepository(
        connection,
        prediction_service=prediction_service,
        service_repository=adapter,
        scope_key=scope,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def prediction(target: Target) -> TargetPredictionV2:
        return TargetPredictionV2(
            prediction_id=f"prediction:{target.value}", scope_key=scope, target=target,
            point=.5, lower=.4, upper=.6, interval_level=.9, interval_kind="laplace",
            support=SupportStatus.INFORMATIVE, predicted_at=now,
            created_at=now, updated_at=now,
        )

    candidate = CandidateV2(
        candidate_id="candidate:one", action={"type": "share", "proactive": True},
        internal_utility=.25,
        coefficients=UserUtilityCoefficientsV2(v_reply=1, v_continue=.5, c_negative=1),
        repeat_subject=RepeatSubjectV2(concern_id="concern:one"),
        policy=CandidatePolicyV2(low_pressure=True, low_frequency=True, easy_to_ignore=True),
        source_event_ids=("event:source",),
    )
    predictions = PredictionSetV2(
        snapshot_id="snapshot:one", parameter_version="parameter:one",
        reply=prediction(Target.REPLY), continuation=prediction(Target.CONTINUE),
        negative=prediction(Target.NEGATIVE),
    )
    recorder = DecisionAuditRecorder(DecisionRun(
        decision_id="decision:one", scope=scope, policy_version="runtime-v2.0",
        contract_version="runtime-v2-coordinator.0", feature_version="user-model-v2.0",
        parameter_version="parameter:one", D=1, lambda_rate=1,
        delta_allowed_seconds=1, cumulative_lambda=1,
        trial_probability=1, random_draw=0, chosen="candidate:one",
    ), (CandidateAssessment(
        candidate_id="candidate:one", prediction_snapshot_id="snapshot:one",
        used_bounds={"reply": .4}, utility_terms={"net": .1},
        repeat_key="concern:one", reasons=("eligible",),
    ),))
    recorder.record(DecisionStage.WAKE, occurred_at=now)
    recorder.record(DecisionStage.PERMISSIONS, occurred_at=now)
    recorder.record(DecisionStage.CANDIDATE_ELIGIBLE, occurred_at=now)
    recorder.record(DecisionStage.HAZARD_TRIAL_PERFORMED, occurred_at=now)
    recorder.record(DecisionStage.HAZARD_TRIAL_WON, occurred_at=now)
    recorder.record(DecisionStage.COMMITTED, occurred_at=now)
    audit = recorder.to_dict()
    committed = CommittedDecisionV2(
        decision_id="decision:one", scope_key=scope, candidate=candidate,
        predictions=predictions, cold_start_exploration=True, audit=audit,
        attempt_id="attempt:one", render_outbox_id="outbox:render:one", committed_at=now,
    )
    runtime.save_committed_decision(committed=committed)
    assert runtime.recover_committed_decision(scope_key=scope, decision_id="decision:one") == committed
    assert runtime.mark_committed_decision_ack_once(
        scope_key=scope, decision_id="decision:one", ack_id="outbox:send:one",
        status="failed", acknowledged_at=now + timedelta(seconds=1),
    ) is True
    assert runtime.mark_committed_decision_ack_once(
        scope_key=scope, decision_id="decision:one", ack_id="outbox:send:two",
        status="sent", acknowledged_at=now + timedelta(seconds=2),
    ) is False
    recovered = runtime.recover_committed_decision(scope_key=scope, decision_id="decision:one")
    assert recovered is not None
    assert recovered.terminal_ack_id == "outbox:send:one"
    assert recovered.terminal_status == "failed"


def first_basis():
    from companion_runtime.user_model_v2_types import DeliveryBasis

    return DeliveryBasis.DELIVERED
