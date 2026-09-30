"""Fake end-to-end tests for the isolated v2 runtime composition root."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from companion_runtime.motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
from companion_runtime.repeat_v2 import RepeatSubjectV2, SendAcknowledgedExposureV2
from companion_runtime.runtime_v2 import (
    BoundaryVerdictV2,
    CandidateV2,
    CommitReceiptV2,
    DecisionConfigV2,
    LegacyUserEventResult,
    PredictionSetV2,
    SendAckV2,
    V2RuntimeCoordinator,
)
from companion_runtime.user_model_v2_labels import TargetObservationV2
from companion_runtime.user_model_v2_service import (
    ActiveTrainingRecordV2,
    PreparedExposureV2,
    UserModelV2Service,
)
from companion_runtime.user_model_v2_types import (
    LabelStatus,
    SupportStatus,
    Target,
    TargetLabelV2,
    TargetPredictionV2,
)

NOW = datetime(2027, 1, 2, 8, tzinfo=timezone.utc)
SCOPE = "user:42/channel:direct"


def prediction(target: Target, point: float, lower: float, upper: float) -> TargetPredictionV2:
    return TargetPredictionV2(
        prediction_id=f"prediction:{target.value}",
        scope_key=SCOPE,
        target=target,
        point=point,
        lower=lower,
        upper=upper,
        interval_level=0.9,
        interval_kind="laplace",
        support=SupportStatus.INFORMATIVE,
        predicted_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


class FakePostgresRepository:
    """Implements both coordinator and UserModelV2Service repository protocols."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.prepared: dict[tuple[str, str], PreparedExposureV2] = {}
        self.active: dict[tuple[str, str, Target], tuple[TargetLabelV2, int]] = {}
        self.audits: dict[str, dict] = {}
        self.matter_events = []
        self.exposures: list[SendAcknowledgedExposureV2] = []
        self.predictions = PredictionSetV2(
            snapshot_id="prediction:snapshot:1",
            parameter_version="parameters:4",
            reply=prediction(Target.REPLY, 0.8, 0.7, 0.9),
            continuation=prediction(Target.CONTINUE, 0.7, 0.6, 0.8),
            negative=prediction(Target.NEGATIVE, 0.05, 0.01, 0.1),
        )

    # UserModelV2Service repository boundary.
    def get_prepared_exposure(self, *, scope_key, idempotency_key):
        return self.prepared.get((scope_key, idempotency_key))

    def put_prepared_exposure(self, *, prepared, idempotency_key):
        self.order.append("prepare_exposure")
        key = (prepared.exposure.scope_key, idempotency_key)
        winner = self.prepared.setdefault(key, prepared)
        for label in winner.labels:
            self.active.setdefault(
                (label.scope_key, label.exposure_id, label.target), (label, 1)
            )
        self.exposures.append(
            SendAcknowledgedExposureV2(
                exposure_id=winner.exposure.exposure_id,
                acknowledged_at_utc=winner.exposure.occurred_at,
                concern_id="interview",
                action_goal_id="ask-result",
            )
        )
        return winner

    def get_active_label(self, *, scope_key, exposure_id, target):
        return self.active.get((scope_key, exposure_id, target))

    def compare_and_swap_active_label(
        self, *, label, revision, expected_revision, idempotency_key
    ):
        key = (label.scope_key, label.exposure_id, label.target)
        current = self.active.get(key)
        if current is None or current[1] != expected_revision:
            return False
        self.active[key] = (label, revision)
        return True

    def list_active_training_records(self, *, scope_key, target):
        return tuple(
            ActiveTrainingRecordV2(label=label, features=prepared.features)
            for prepared in self.prepared.values()
            for label, _revision in [self.active[(scope_key, prepared.exposure.exposure_id, target)]]
        )

    # Coordinator PostgreSQL boundary.
    def prediction_for(self, *, scope_key, candidate, now):
        assert scope_key == SCOPE
        return self.predictions

    def acknowledged_exposures(self, *, scope_key, now):
        return tuple(self.exposures)

    def user_matter_events(self, *, scope_key, now):
        return tuple(self.matter_events)

    def settleable_exposures(self, *, scope_key, observations, as_of):
        exposure_ids = {
            exposure_id
            for observation in observations
            for exposure_id in observation.candidate_exposure_ids
        }
        return tuple(
            prepared
            for prepared in self.prepared.values()
            if prepared.exposure.exposure_id in exposure_ids
        )

    def append_user_matter_events(self, *, scope_key, events):
        self.matter_events.extend(events)

    def save_decision_audit(self, *, decision_id, audit):
        self.audits[decision_id] = dict(audit)


class FakeLegacyBridge:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.next_event = LegacyUserEventResult(event_id="unused", occurred_at=NOW)
        self.candidate = CandidateV2(
            candidate_id="candidate:interview",
            action={"type": "follow_up", "intent": "问面试结果"},
            internal_utility=0.2,
            coefficients=UserUtilityCoefficientsV2(
                v_reply=1.0, v_continue=0.5, c_negative=1.0
            ),
            repeat_subject=RepeatSubjectV2(
                concern_id="interview", action_goal_id="ask-result"
            ),
            policy=CandidatePolicyV2(
                low_pressure=True, low_frequency=True, easy_to_ignore=True
            ),
            source_event_ids=("source:user:1",),
        )

    def ingest_user_event(self, event):
        self.order.append("legacy_user_event")
        return self.next_event

    def candidates(self, *, scope_key, now):
        self.order.append("legacy_candidates")
        return (self.candidate,)

    def boundary_verdict(self, *, candidate, scope_key, now):
        self.order.append("legacy_boundary")
        return BoundaryVerdictV2()

    def commit_candidate(self, *, decision_id, candidate, now):
        self.order.append("legacy_outbox_commit")
        return CommitReceiptV2(
            decision_id=decision_id,
            candidate_id=candidate.candidate_id,
            attempt_id="attempt:1",
            render_outbox_id="outbox:render:1",
        )

    def mark_rendered(self, *, decision_id, outbox_id, now):
        self.order.append("legacy_rendered")

    def acknowledge_send(self, ack):
        self.order.append("legacy_send_ack")
        return ack.sent


def coordinator(order: list[str]) -> tuple[V2RuntimeCoordinator, FakeLegacyBridge, FakePostgresRepository]:
    legacy = FakeLegacyBridge(order)
    repository = FakePostgresRepository(order)
    runtime = V2RuntimeCoordinator(
        scope_key=SCOPE,
        legacy=legacy,
        user_model=UserModelV2Service(repository),
        repository=repository,
        config=DecisionConfigV2(hazard_base=1.0, hazard_beta=4.0),
        rng=random.Random(0),
    )
    return runtime, legacy, repository


def test_fake_e2e_ack_then_prepare_and_user_event_settles_v2() -> None:
    order: list[str] = []
    runtime, legacy, repository = coordinator(order)

    decision = runtime.decide_endogenous(
        decision_id="decision:1", now=NOW, elapsed_allowed_seconds=10.0
    )
    assert decision.acted is True
    assert decision.commit is not None
    assert order[:3] == ["legacy_candidates", "legacy_boundary", "legacy_outbox_commit"]
    assessment = decision.assessments[0]
    assert assessment.user_utility.used_bounds.p_reply_lower == 0.7
    assert assessment.user_utility.used_bounds.p_negative_upper == 0.1
    assert assessment.predictions.snapshot_id == "prediction:snapshot:1"

    runtime.mark_rendered(
        decision_id="decision:1", outbox_id="outbox:render:1", now=NOW + timedelta(seconds=1)
    )
    context_calls = 0

    def context():
        nonlocal context_calls
        context_calls += 1
        return {"busy_probability": 0.1, "recent_contact_count": 0}

    prepared = runtime.acknowledge_send(
        SendAckV2(
            decision_id="decision:1",
            attempt_id="attempt:1",
            send_outbox_id="outbox:send:1",
            acknowledged_at=NOW + timedelta(seconds=2),
            sent=True,
            action=legacy.candidate.action,
            context_provider=context,
            source_event_ids=("event:proactive-sent:1",),
        )
    )
    assert prepared is not None
    assert context_calls == 1
    assert order.index("legacy_send_ack") < order.index("prepare_exposure")
    assert prepared.exposure.source_event_ids == (
        "source:user:1",
        "event:proactive-sent:1",
    )

    reply_at = NOW + timedelta(minutes=5)
    legacy.next_event = LegacyUserEventResult(
        event_id="event:user:reply",
        occurred_at=reply_at,
        observations=(
            TargetObservationV2(
                event_id="event:user:reply",
                target=Target.REPLY,
                occurred_at=reply_at,
                value=True,
                candidate_exposure_ids=("attempt:1",),
            ),
        ),
    )
    runtime.process_user_event({"kind": "user_message", "text": "结果很好"})
    label, revision = repository.active[(SCOPE, "attempt:1", Target.REPLY)]
    assert label.status is LabelStatus.OBSERVED_POSITIVE
    assert revision == 2
    assert order.index("legacy_user_event") > order.index("prepare_exposure")
    assert [event["stage"] for event in repository.audits["decision:1"]["events"]] == [
        "wake",
        "permissions",
        "candidate_eligible",
        "hazard_trial_performed",
        "hazard_trial_won",
        "committed",
        "rendered",
        "send_ack",
        "reconciled",
    ]


def test_failed_send_never_collects_context_or_prepares_exposure() -> None:
    order: list[str] = []
    runtime, legacy, repository = coordinator(order)
    runtime.decide_endogenous(
        decision_id="decision:failed", now=NOW, elapsed_allowed_seconds=10.0
    )
    runtime.mark_rendered(
        decision_id="decision:failed",
        outbox_id="outbox:render:1",
        now=NOW + timedelta(seconds=1),
    )
    context_calls = 0

    def context():
        nonlocal context_calls
        context_calls += 1
        return {}

    result = runtime.acknowledge_send(
        SendAckV2(
            decision_id="decision:failed",
            attempt_id="attempt:1",
            send_outbox_id="outbox:send:failed",
            acknowledged_at=NOW + timedelta(seconds=2),
            sent=False,
            action=legacy.candidate.action,
            context_provider=context,
        )
    )
    assert result is None
    assert context_calls == 0
    assert repository.prepared == {}
    assert order[-1] == "legacy_send_ack"
    assert [event["stage"] for event in repository.audits["decision:failed"]["events"][-2:]] == [
        "send_fail",
        "reconciled",
    ]


def test_endogenous_path_honours_legacy_boundary_before_commit() -> None:
    order: list[str] = []
    runtime, legacy, repository = coordinator(order)

    def blocked(**kwargs):
        return BoundaryVerdictV2(blocked=True, reasons=("topic_avoid",))

    legacy.boundary_verdict = blocked  # type: ignore[method-assign]
    decision = runtime.decide_endogenous(
        decision_id="decision:blocked", now=NOW, elapsed_allowed_seconds=10.0
    )
    assert decision.acted is False
    assert decision.reason == "no_eligible_candidate"
    assert "legacy_outbox_commit" not in order
    assert decision.assessments[0].blocked is True
    assert "boundary:topic_avoid" in decision.assessments[0].reasons
    assert repository.audits["decision:blocked"]["events"][-1]["stage"] == "reconciled"
