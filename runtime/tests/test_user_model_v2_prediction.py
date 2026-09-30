"""Tests for the pure active-snapshot user-model v2 prediction service."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from companion_runtime.user_model_v2_estimator import GaussianPrior
from companion_runtime.user_model_v2_features import FeatureSnapshotV2
from companion_runtime.user_model_v2_prediction import (
    ActiveParameterSnapshotV2,
    UserModelV2PredictionService,
)
from companion_runtime.user_model_v2_types import SupportStatus, Target

NOW = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
SCOPE = "user:42/channel:direct"


class FakeRepository:
    def __init__(self, active=None):
        self.active = dict(active or {})
        self.calls = []

    def get_active_parameter_snapshot(self, *, scope_key, target):
        self.calls.append((scope_key, target))
        return self.active.get(target)


def complete_features() -> FeatureSnapshotV2:
    return FeatureSnapshotV2(
        scope_key=SCOPE,
        exposure_id="prediction-input",
        action_json={
            "proactive": True,
            "follow_up": False,
            "emotional_expression": False,
            "question": True,
            "topic_shift": False,
        },
        context_json={
            "busy_probability": 0.2,
            "recent_contact_count": 2,
            "hours_since_contact": 3.0,
            "user_active_now": False,
            "ever_boundary": False,
            "novelty": 0.8,
            "explicit_permission": True,
        },
        context_cutoff_at=NOW,
        created_at=NOW,
    )


def payload(features, target, *, beta=0.0, support="informative"):
    dimension = len(features.values)
    covariance = np.eye(dimension) * 0.25
    hessian = np.eye(dimension) * 4.0
    return {
        "target": target.value,
        "feature_version": features.feature_version,
        "feature_fingerprint": features.feature_fingerprint,
        "feature_names": list(features.spec.names),
        "map_parameters": [beta] + [0.0] * (dimension - 1),
        "hessian": hessian.tolist(),
        "covariance": covariance.tolist(),
        "precision_cholesky": np.linalg.cholesky(hessian).tolist(),
        "covariance_cholesky": np.linalg.cholesky(covariance).tolist(),
        "objective": 1.0,
        "support": support,
        "sample_count": 5,
        "weight_sum": 4.0,
        "converged": True,
        "optimizer_message": "ok",
        "iterations": 3,
    }


def snapshots(features):
    return {
        target: ActiveParameterSnapshotV2(
            parameter_snapshot_id=f"params-{target.value}-v{index + 1}",
            target=target,
            payload=payload(features, target, beta=float(index - 1)),
        )
        for index, target in enumerate(Target)
    }


def test_predicts_four_independent_heads_and_records_each_parameter_id() -> None:
    features = complete_features()
    repo = FakeRepository(snapshots(features))
    envelope = UserModelV2PredictionService(repo).predict(
        features=features, predicted_at=NOW, based_on_state_version=17
    )

    assert tuple(item.target for item in envelope.predictions) == tuple(Target)
    assert [item.point for item in envelope.predictions] == pytest.approx(
        [0.2689414214, 0.5, 0.7310585786, 0.8807970780]
    )
    assert all(item.lower < item.point < item.upper for item in envelope.predictions)
    assert all(item.support is SupportStatus.INFORMATIVE for item in envelope.predictions)
    assert dict(envelope.parameter_snapshot_ids) == {
        target: f"params-{target.value}-v{index + 1}"
        for index, target in enumerate(Target)
    }
    assert envelope.to_dict()["parameter_snapshot_ids"] == {
        target.value: f"params-{target.value}-v{index + 1}"
        for index, target in enumerate(Target)
    }
    assert repo.calls == [(SCOPE, target) for target in Target]


def test_no_active_snapshot_uses_registered_prior_or_marks_unavailable() -> None:
    features = complete_features()
    dimension = len(features.values)
    priors = {
        Target.REPLY: GaussianPrior(
            mean=np.zeros(dimension), precision=np.eye(dimension)
        )
    }
    envelope = UserModelV2PredictionService(
        FakeRepository(), registered_priors=priors
    ).predict(features=features, predicted_at=NOW, based_on_state_version=0)

    by_target = {item.target: item for item in envelope.predictions}
    assert by_target[Target.REPLY].support is SupportStatus.PRIOR_ONLY
    assert by_target[Target.REPLY].point == pytest.approx(0.5)
    for target in (Target.ACCEPTANCE, Target.CONTINUE, Target.NEGATIVE):
        prediction = by_target[target]
        assert prediction.support is SupportStatus.UNAVAILABLE
        assert prediction.point is prediction.lower is prediction.upper is None
        assert prediction.interval_level is prediction.interval_kind is None
    assert dict(envelope.parameter_snapshot_ids) == {target: None for target in Target}


def test_missing_features_never_silently_use_encoded_zero() -> None:
    missing = FeatureSnapshotV2(
        scope_key=SCOPE,
        exposure_id="missing",
        action_json={},
        context_json={},
        context_cutoff_at=NOW,
        created_at=NOW,
    )
    repo = FakeRepository(snapshots(missing))
    envelope = UserModelV2PredictionService(repo).predict(
        features=missing, predicted_at=NOW, based_on_state_version=1
    )
    assert any(missing.missing_mask)
    assert all(item.support is SupportStatus.UNAVAILABLE for item in envelope.predictions)
    assert all(item.point is None for item in envelope.predictions)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(feature_version="wrong"), "feature version"),
        (lambda data: data.update(feature_fingerprint="wrong"), "feature fingerprint"),
        (lambda data: data.update(map_parameters=[0.0]), "dimension"),
        (
            lambda data: data.update(
                covariance_cholesky=np.eye(len(data["map_parameters"])).tolist()
            ),
            "does not factor",
        ),
    ],
)
def test_rejects_incompatible_or_invalid_active_payload(mutation, message) -> None:
    features = complete_features()
    active = snapshots(features)
    broken = dict(active[Target.REPLY].payload)
    mutation(broken)
    active[Target.REPLY] = ActiveParameterSnapshotV2(
        parameter_snapshot_id="broken", target=Target.REPLY, payload=broken
    )
    with pytest.raises(ValueError, match=message):
        UserModelV2PredictionService(FakeRepository(active)).predict(
            features=features, predicted_at=NOW, based_on_state_version=1
        )


def test_rejects_snapshot_or_payload_for_the_wrong_target() -> None:
    features = complete_features()
    active = snapshots(features)
    active[Target.REPLY] = ActiveParameterSnapshotV2(
        parameter_snapshot_id="wrong-head",
        target=Target.ACCEPTANCE,
        payload=payload(features, Target.ACCEPTANCE),
    )
    with pytest.raises(ValueError, match="snapshot target mismatch"):
        UserModelV2PredictionService(FakeRepository(active)).predict(
            features=features, predicted_at=NOW, based_on_state_version=1
        )
