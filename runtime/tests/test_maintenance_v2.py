from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from companion_runtime.maintenance_v2 import (
    ActiveParameterPointerV2,
    DueExpectationV2,
    V2Maintenance,
    V2MaintenanceConfig,
)
from companion_runtime.user_model_v2_service import UserModelV2Service
from companion_runtime.user_model_v2_types import (
    ExpectationV2,
    LabelStatus,
    PredictionEnvelopeV2,
    SupportStatus,
    Target,
    TargetPredictionV2,
)
from test_user_model_v2_service import FakeRepository, HORIZONS, NOW, SCOPE, prepare


class MaintenanceRepository:
    def __init__(self, service_repo: FakeRepository) -> None:
        self.service_repo = service_repo
        self.due = []
        self.expectations = []
        self.revision_keys: set[str] = set()
        self.emotions = []
        self.pointer = ActiveParameterPointerV2(snapshot_id=None, activation_version=0)
        self.snapshots = []

    def list_due_pending_exposures(self, *, scope_key, as_of, limit):
        assert scope_key == SCOPE
        return tuple(self.due[:limit])

    def list_due_expectations(self, *, scope_key, as_of, limit):
        return tuple(self.expectations[:limit])

    def write_expectation_revision_once(self, *, scope_key, settlement, emotion_shadow, settled_at):
        if settlement.revision_key in self.revision_keys:
            return False
        self.revision_keys.add(settlement.revision_key)
        self.emotions.append(emotion_shadow)
        return True

    def get_active_parameter_pointer(self, *, scope_key):
        return self.pointer

    def save_parameter_snapshot_and_activate(self, **kwargs):
        if kwargs["expected_snapshot_id"] != self.pointer.snapshot_id:
            return False
        self.snapshots.append(kwargs)
        self.pointer = ActiveParameterPointerV2(
            snapshot_id=kwargs["snapshot_id"], activation_version=kwargs["activation_version"]
        )
        return True


def envelope():
    predictions = tuple(
        TargetPredictionV2(
            prediction_id=f"p-{target.value}", scope_key=SCOPE, target=target,
            point=.25, lower=.1, upper=.5, interval_level=.9,
            interval_kind="laplace", support=SupportStatus.SPARSE,
            predicted_at=NOW, created_at=NOW, updated_at=NOW,
        ) for target in Target
    )
    return PredictionEnvelopeV2(
        envelope_id="env", scope_key=SCOPE, predictions=predictions,
        predicted_at=NOW, based_on_state_version=1, created_at=NOW, updated_at=NOW,
    )


def expectation():
    return ExpectationV2(
        expectation_id="expect-1", exposure_id="exp-1", envelope=envelope(), scope_key=SCOPE,
        fixed_at=NOW, window_started_at=NOW, window_ends_at=NOW + timedelta(seconds=240),
        horizon_seconds=240, created_at=NOW, updated_at=NOW,
    )


def test_due_silence_settles_rn_false_ca_unknown_and_writes_each_revision_once():
    service_repo = FakeRepository()
    prepared = prepare(service_repo)
    repository = MaintenanceRepository(service_repo)
    repository.due = [prepared]
    clock = iter((100.0, 200.0, 300.0))
    runner = V2Maintenance(
        scope_key=SCOPE, user_model=UserModelV2Service(service_repo), repository=repository,
        config=V2MaintenanceConfig(minimum_interval_seconds=1, fit_interval_seconds=9999),
        monotonic=lambda: next(clock),
    )
    end = NOW + timedelta(seconds=300)
    first = runner.run_due(now=end, fit=False)
    labels = {target: service_repo.active[(SCOPE, "exp-1", target)] for target in Target}
    assert labels[Target.REPLY][0].status is LabelStatus.OBSERVED_NEGATIVE
    assert labels[Target.NEGATIVE][0].status is LabelStatus.OBSERVED_NEGATIVE
    assert labels[Target.CONTINUE][0].status is LabelStatus.UNKNOWN
    assert labels[Target.ACCEPTANCE][0].status is LabelStatus.UNKNOWN
    assert all(revision == 2 for _label, revision in labels.values())
    assert first.label_revisions_written == 4

    reply_label, revision = labels[Target.REPLY]
    repository.expectations = [DueExpectationV2(
        expectation=expectation(), label=reply_label, label_revision=revision
    )]
    second = runner.run_due(now=end, fit=False)
    third = runner.run_due(now=end, force=True, fit=False)
    assert second.expectation_revisions_written == 1
    assert third.expectation_revisions_written == 0
    assert len(repository.emotions) == 1
    assert repository.emotions[0].shadow is True
    assert repository.emotions[0].actual_outcome == 0.0


def test_fit_uses_active_labels_and_is_interval_limited():
    service_repo = FakeRepository()
    prepared = prepare(service_repo)
    reply = next(label for label in prepared.labels if label.target is Target.REPLY)
    observed = replace(
        reply, status=LabelStatus.OBSERVED_POSITIVE, value=True,
        observed_at=NOW + timedelta(seconds=10), updated_at=NOW + timedelta(seconds=10),
    )
    from companion_runtime.user_model_v2_service import ActiveTrainingRecordV2
    service_repo.training = [ActiveTrainingRecordV2(label=observed, features=prepared.features)]
    repository = MaintenanceRepository(service_repo)
    ticks = iter((100.0, 101.0, 120.0))
    runner = V2Maintenance(
        scope_key=SCOPE, user_model=UserModelV2Service(service_repo), repository=repository,
        config=V2MaintenanceConfig(
            minimum_interval_seconds=10, fit_interval_seconds=60, max_fit_targets=4
        ), monotonic=lambda: next(ticks),
    )
    first = runner.run_due(now=NOW + timedelta(hours=1))
    skipped = runner.run_due(now=NOW + timedelta(hours=1, seconds=1))
    no_refit = runner.run_due(now=NOW + timedelta(hours=1, seconds=20))
    assert first.fit_attempted and first.fit_activated
    assert set(repository.snapshots[0]["parameters"]) == {target.value for target in Target}
    assert repository.snapshots[0]["parameters"]["reply"]["training_exposure_ids"] == ["exp-1"]
    assert skipped.ran is False and skipped.reason == "interval"
    assert no_refit.ran and not no_refit.fit_attempted
    assert len(repository.snapshots) == 1
