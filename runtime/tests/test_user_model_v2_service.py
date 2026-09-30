"""Application-service tests for the isolated user-model v2 orchestration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np

from companion_runtime.user_model_v2_estimator import GaussianPrior
from companion_runtime.user_model_v2_features import FeatureSnapshotV2
from companion_runtime.user_model_v2_labels import SettlementContextV2, TargetObservationV2
from companion_runtime.user_model_v2_service import (
    ActiveTrainingRecordV2,
    PreparedExposureV2,
    UserModelV2Service,
)
from companion_runtime.user_model_v2_types import LabelStatus, Target, TargetLabelV2

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SCOPE = "user:42/channel:direct"
HORIZONS = {
    Target.REPLY: 60,
    Target.ACCEPTANCE: 120,
    Target.CONTINUE: 180,
    Target.NEGATIVE: 240,
}


class FakeRepository:
    def __init__(self) -> None:
        self.prepared: dict[tuple[str, str], PreparedExposureV2] = {}
        self.active: dict[tuple[str, str, Target], tuple[TargetLabelV2, int]] = {}
        self.training: list[ActiveTrainingRecordV2] = []
        self.get_prepared_calls = 0
        self.put_calls = 0
        self.cas_calls = 0

    def get_prepared_exposure(self, *, scope_key, idempotency_key):
        self.get_prepared_calls += 1
        return self.prepared.get((scope_key, idempotency_key))

    def put_prepared_exposure(self, *, prepared, idempotency_key):
        self.put_calls += 1
        key = (prepared.exposure.scope_key, idempotency_key)
        winner = self.prepared.setdefault(key, prepared)
        for label in winner.labels:
            self.active.setdefault(
                (label.scope_key, label.exposure_id, label.target), (label, 1)
            )
        return winner

    def get_active_label(self, *, scope_key, exposure_id, target):
        return self.active.get((scope_key, exposure_id, target))

    def compare_and_swap_active_label(
        self, *, label, revision, expected_revision, idempotency_key
    ):
        self.cas_calls += 1
        key = (label.scope_key, label.exposure_id, label.target)
        current = self.active.get(key)
        if current is None or current[1] != expected_revision:
            return False
        self.active[key] = (label, revision)
        return True

    def list_active_training_records(self, *, scope_key, target):
        return tuple(self.training)


def prepare(repo: FakeRepository, *, key: str = "delivery-1") -> PreparedExposureV2:
    result = UserModelV2Service(repo).prepare_exposure(
        scope_key=SCOPE,
        exposure_id="exp-1",
        idempotency_key=key,
        occurred_at=NOW,
        action={"proactive": True, "type": "follow_up"},
        context_provider=lambda: {"busy_probability": 0.2},
        delivery_confirmed=True,
        horizons=HORIZONS,
        source_event_ids=("delivery-1",),
    )
    assert result is not None
    return result


def test_prepare_exposure_failed_send_has_no_context_or_repository_effect() -> None:
    repo = FakeRepository()
    context_calls = 0

    def context():
        nonlocal context_calls
        context_calls += 1
        return {"busy_probability": 0.3}

    result = UserModelV2Service(repo).prepare_exposure(
        scope_key=SCOPE,
        exposure_id="exp-failed",
        idempotency_key="failed",
        occurred_at=NOW,
        action={},
        context_provider=context,
        delivery_confirmed=False,
        horizons=HORIZONS,
    )
    assert result is None
    assert context_calls == repo.get_prepared_calls == repo.put_calls == 0


def test_prepare_exposure_replay_returns_winner_without_recomputing_context() -> None:
    repo = FakeRepository()
    service = UserModelV2Service(repo)
    contexts = iter(({"busy_probability": 0.25}, {"busy_probability": 0.99}))
    context_calls = 0

    def context():
        nonlocal context_calls
        context_calls += 1
        return next(contexts)

    kwargs = dict(
        scope_key=SCOPE,
        exposure_id="exp-1",
        idempotency_key="confirmed-send-1",
        occurred_at=NOW,
        action={"proactive": True},
        context_provider=context,
        delivery_confirmed=True,
        horizons=HORIZONS,
    )
    first = service.prepare_exposure(**kwargs)
    second = service.prepare_exposure(**kwargs)
    assert first is second
    assert context_calls == 1
    assert repo.put_calls == 1
    assert first is not None and first.features.context_json["busy_probability"] == 0.25


def test_prepare_creates_four_independent_pending_target_windows() -> None:
    item = prepare(FakeRepository())
    assert tuple(label.target for label in item.labels) == tuple(Target)
    assert all(label.status is LabelStatus.PENDING for label in item.labels)
    assert {
        label.target: int((label.window_ends_at - label.window_started_at).total_seconds())
        for label in item.labels
    } == HORIZONS


def test_settlement_is_per_target_cas_and_replay_is_idempotent() -> None:
    repo = FakeRepository()
    item = prepare(repo)
    service = UserModelV2Service(repo)
    reply = TargetObservationV2(
        event_id="reply-1",
        target=Target.REPLY,
        occurred_at=NOW + timedelta(seconds=30),
        value=True,
        candidate_exposure_ids=("exp-1",),
    )
    first = service.settle_observation(
        prepared=item,
        observations=(reply,),
        context=SettlementContextV2(as_of=NOW + timedelta(seconds=70)),
        target=Target.REPLY,
    )
    second = service.settle_observation(
        prepared=item,
        observations=(reply,),
        context=SettlementContextV2(as_of=NOW + timedelta(seconds=70)),
        target=Target.REPLY,
    )
    assert first[0].status is LabelStatus.OBSERVED_POSITIVE
    assert second == first
    assert repo.cas_calls == 1
    assert repo.active[(SCOPE, "exp-1", Target.REPLY)][1] == 2
    assert repo.active[(SCOPE, "exp-1", Target.ACCEPTANCE)][0].status is LabelStatus.PENDING


def observed_label(item: PreparedExposureV2, target: Target, value: bool) -> TargetLabelV2:
    original = next(label for label in item.labels if label.target is target)
    return replace(
        original,
        status=LabelStatus.OBSERVED_POSITIVE if value else LabelStatus.OBSERVED_NEGATIVE,
        value=value,
        observed_at=NOW + timedelta(seconds=10),
        updated_at=NOW + timedelta(seconds=10),
    )


def test_build_fit_dataset_only_accepts_active_observed_labels_for_requested_target() -> None:
    repo = FakeRepository()
    item = prepare(repo)
    good = ActiveTrainingRecordV2(
        label=observed_label(item, Target.REPLY, True), features=item.features
    )
    pending = ActiveTrainingRecordV2(
        label=next(label for label in item.labels if label.target is Target.REPLY),
        features=item.features,
    )
    other_target = ActiveTrainingRecordV2(
        label=observed_label(item, Target.NEGATIVE, False), features=item.features
    )
    wrong_scope_feature = FeatureSnapshotV2(
        scope_key="other",
        exposure_id="exp-1",
        action_json={},
        context_json={},
        context_cutoff_at=NOW,
        created_at=NOW,
    )
    mismatched = ActiveTrainingRecordV2(
        label=observed_label(item, Target.REPLY, False), features=wrong_scope_feature
    )
    repo.training = [pending, other_target, mismatched, good]

    dataset = UserModelV2Service(repo).build_fit_dataset(
        scope_key=SCOPE, target=Target.REPLY
    )
    assert dataset.target is Target.REPLY
    assert dataset.exposure_ids == ("exp-1",)
    assert dataset.labels == (1.0,)
    assert len(dataset.design_rows) == 1


def test_target_datasets_and_fits_are_independent_and_payload_is_persistable() -> None:
    repo = FakeRepository()
    item = prepare(repo)
    reply_records = (
        ActiveTrainingRecordV2(
            label=observed_label(item, Target.REPLY, True), features=item.features
        ),
    )
    negative_records = (
        ActiveTrainingRecordV2(
            label=observed_label(item, Target.NEGATIVE, False), features=item.features
        ),
    )
    service = UserModelV2Service(repo)
    replies = service.build_fit_dataset(
        scope_key=SCOPE, target=Target.REPLY, records=reply_records
    )
    negatives = service.build_fit_dataset(
        scope_key=SCOPE, target=Target.NEGATIVE, records=negative_records
    )
    prior = GaussianPrior(
        mean=np.zeros(len(item.features.values)),
        precision=np.eye(len(item.features.values)),
    )
    reply_fit = service.fit_target(
        dataset=replies,
        prior=prior,
        fit_time=NOW + timedelta(hours=1),
        half_life_seconds=3600,
    )
    negative_fit = service.fit_target(
        dataset=negatives,
        prior=prior,
        fit_time=NOW + timedelta(hours=1),
        half_life_seconds=3600,
    )
    assert reply_fit.payload["target"] == "reply"
    assert negative_fit.payload["target"] == "negative"
    assert reply_fit.fit.map_parameters[0] > 0
    assert negative_fit.fit.map_parameters[0] < 0
    assert reply_fit.payload["training_exposure_ids"] == ["exp-1"]
    assert isinstance(reply_fit.payload["covariance"], list)
