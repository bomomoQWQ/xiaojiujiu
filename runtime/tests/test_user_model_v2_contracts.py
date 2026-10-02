"""Contract tests for the isolated user-model v2 record skeleton."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.user_model_v2_types import (
    DeliveryBasis,
    ExpectationV2,
    InteractionExposureV2,
    LabelStatus,
    PredictionEnvelopeV2,
    SupportStatus,
    Target,
    TargetLabelV2,
    TargetPredictionV2,
    USER_MODEL_V2_CONTRACT_VERSION,
    USER_MODEL_V2_FEATURE_VERSION,
    USER_MODEL_V2_FEATURE_VERSIONS,
    USER_MODEL_V2_TARGET_CONTRACT_VERSION,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
DEFAULT_HORIZON = 6 * 60 * 60
WINDOW_END = NOW + timedelta(seconds=DEFAULT_HORIZON)
SOURCES = ("evt_delivery",)


def exposure(**changes: object) -> InteractionExposureV2:
    values: dict[str, object] = {
        "exposure_id": "exp_1",
        "scope_key": "user:42/channel:direct",
        "occurred_at": NOW,
        "window_started_at": NOW,
        "window_ends_at": WINDOW_END,
        "horizon_seconds": DEFAULT_HORIZON,
        "delivery_basis": DeliveryBasis.DELIVERED,
        "created_at": NOW,
        "updated_at": NOW,
        "source_event_ids": SOURCES,
        "attributes": (("behaviour", "follow_up"), ("proactive", True)),
    }
    values.update(changes)
    return InteractionExposureV2(**values)  # type: ignore[arg-type]


def label(target: Target, **changes: object) -> TargetLabelV2:
    values: dict[str, object] = {
        "label_id": f"lbl_{target.value}",
        "exposure_id": "exp_1",
        "scope_key": "user:42/channel:direct",
        "target": target,
        "status": LabelStatus.PENDING,
        "value": None,
        "window_started_at": NOW,
        "window_ends_at": WINDOW_END,
        "horizon_seconds": DEFAULT_HORIZON,
        "created_at": NOW,
        "updated_at": NOW,
        "source_event_ids": SOURCES,
    }
    values.update(changes)
    return TargetLabelV2(**values)  # type: ignore[arg-type]


def prediction(target: Target, **changes: object) -> TargetPredictionV2:
    values: dict[str, object] = {
        "prediction_id": f"pred_{target.value}",
        "scope_key": "user:42/channel:direct",
        "target": target,
        "point": 0.5,
        "lower": 0.2,
        "upper": 0.8,
        "interval_level": 0.9,
        "interval_kind": "credible",
        "support": SupportStatus.PRIOR_ONLY,
        "predicted_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
        "source_event_ids": SOURCES,
    }
    values.update(changes)
    return TargetPredictionV2(**values)  # type: ignore[arg-type]


def envelope(**changes: object) -> PredictionEnvelopeV2:
    values: dict[str, object] = {
        "envelope_id": "env_1",
        "scope_key": "user:42/channel:direct",
        "predictions": tuple(prediction(target) for target in Target),
        "predicted_at": NOW,
        "based_on_state_version": 17,
        "created_at": NOW,
        "updated_at": NOW,
        "source_event_ids": SOURCES,
    }
    values.update(changes)
    return PredictionEnvelopeV2(**values)  # type: ignore[arg-type]


def test_missing_is_never_encoded_as_false() -> None:
    for status in (
        LabelStatus.PENDING,
        LabelStatus.CENSORED,
        LabelStatus.UNKNOWN,
        LabelStatus.UNATTRIBUTABLE,
        LabelStatus.INVALIDATED,
    ):
        item = label(Target.REPLY, status=status)
        assert item.value is None
        assert item.to_dict()["value"] is None

    with pytest.raises(ValueError, match="must not carry a value"):
        label(Target.REPLY, status=LabelStatus.UNKNOWN, value=False)


def test_each_target_keeps_an_independent_status_and_window() -> None:
    labels = {
        Target.REPLY: label(
            Target.REPLY,
            status=LabelStatus.OBSERVED_POSITIVE,
            value=True,
            observed_at=NOW + timedelta(minutes=4),
            source_event_ids=("evt_delivery", "evt_reply"),
        ),
        Target.ACCEPTANCE: label(Target.ACCEPTANCE, status=LabelStatus.UNKNOWN),
        Target.CONTINUE: label(
            Target.CONTINUE,
            status=LabelStatus.PENDING,
            horizon_seconds=1800,
            window_ends_at=NOW + timedelta(seconds=1800),
        ),
        Target.NEGATIVE: label(
            Target.NEGATIVE,
            status=LabelStatus.CENSORED,
            horizon_seconds=86400,
            window_ends_at=NOW + timedelta(seconds=86400),
        ),
    }
    assert labels[Target.REPLY].value is True
    assert len({item.horizon_seconds for item in labels.values()}) == 3
    assert {item.status for item in labels.values()} == {
        LabelStatus.OBSERVED_POSITIVE,
        LabelStatus.UNKNOWN,
        LabelStatus.PENDING,
        LabelStatus.CENSORED,
    }


def test_explicit_versions_and_horizon_are_json_safe() -> None:
    item = exposure()
    payload = item.to_dict()
    assert payload["horizon_seconds"] == DEFAULT_HORIZON
    assert payload["contract_version"] == USER_MODEL_V2_CONTRACT_VERSION
    assert payload["feature_version"] == USER_MODEL_V2_FEATURE_VERSION
    assert payload["target_contract_version"] == USER_MODEL_V2_TARGET_CONTRACT_VERSION
    assert payload["delivery_basis"] == "delivered"
    assert json.loads(json.dumps(payload))["source_event_ids"] == ["evt_delivery"]

    with pytest.raises(FrozenInstanceError):
        item.scope_key = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="duration"):
        exposure(window_ends_at=WINDOW_END + timedelta(seconds=1))
    with pytest.raises(ValueError, match="contract_version"):
        exposure(contract_version="3")
    with pytest.raises(ValueError, match="feature_version"):
        exposure(feature_version="latest")
    with pytest.raises(ValueError, match="target_contract_version"):
        exposure(target_contract_version="latest")


def test_historical_feature_versions_stay_readable_but_are_not_the_latest() -> None:
    """Persisted rows must survive a feature-definition bump.

    A repository that refuses yesterday's rows loses the ability to reconcile or retire
    them, and the failure shows up somewhere unrelated (a scheduler round, a late ACK).
    Historical versions therefore stay readable while learning keeps comparing against
    the current definition.
    """
    historical = exposure(feature_version="user-model-v2.0")
    assert historical.feature_version == "user-model-v2.0"
    assert "user-model-v2.0" in USER_MODEL_V2_FEATURE_VERSIONS
    assert USER_MODEL_V2_FEATURE_VERSION in USER_MODEL_V2_FEATURE_VERSIONS
    assert USER_MODEL_V2_FEATURE_VERSION != "user-model-v2.0"


def test_prediction_envelope_has_intervals_and_one_row_per_target() -> None:
    item = envelope()
    payload = item.to_dict()
    assert [row["target"] for row in payload["predictions"]] == [
        "reply",
        "acceptance",
        "continue",
        "negative",
    ]
    assert {
        "point": 0.5,
        "lower": 0.2,
        "upper": 0.8,
        "interval_level": 0.9,
        "interval_kind": "credible",
        "support": "prior_only",
    }.items() <= payload["predictions"][0].items()
    assert "uncertainty" not in payload["predictions"][0]
    json.dumps(payload)

    with pytest.raises(ValueError, match="exactly one"):
        envelope(predictions=tuple(prediction(Target.REPLY) for _ in Target))
    with pytest.raises(ValueError, match="scope_key"):
        envelope(
            predictions=tuple(
                prediction(target, scope_key="other") if target is Target.REPLY else prediction(target)
                for target in Target
            )
        )


def test_expectation_fixes_envelope_with_an_explicit_horizon() -> None:
    item = ExpectationV2(
        expectation_id="expect_1",
        exposure_id="exp_1",
        envelope=envelope(),
        scope_key="user:42/channel:direct",
        fixed_at=NOW,
        window_started_at=NOW,
        window_ends_at=WINDOW_END,
        horizon_seconds=DEFAULT_HORIZON,
        created_at=NOW,
        updated_at=NOW,
        source_event_ids=SOURCES,
    )
    payload = item.to_dict()
    assert payload["fixed_at"] == NOW.isoformat()
    assert payload["envelope"]["predicted_at"] == NOW.isoformat()
    assert payload["horizon_seconds"] == DEFAULT_HORIZON
    json.dumps(payload)

    future = NOW + timedelta(seconds=1)
    future_envelope = envelope(
        predicted_at=future,
        predictions=tuple(prediction(target, predicted_at=future) for target in Target),
    )
    with pytest.raises(ValueError, match="after it was fixed"):
        replace(item, envelope=future_envelope)


def test_invalid_label_combinations_are_rejected() -> None:
    with pytest.raises(ValueError, match="require a bool or float"):
        label(Target.REPLY, status=LabelStatus.OBSERVED_POSITIVE)
    with pytest.raises(ValueError, match="requires a value greater"):
        label(
            Target.REPLY,
            status=LabelStatus.OBSERVED_POSITIVE,
            value=False,
            observed_at=NOW,
        )
    with pytest.raises(ValueError, match="false/zero"):
        label(Target.REPLY, status=LabelStatus.OBSERVED_NEGATIVE, value=True, observed_at=NOW)
    with pytest.raises(ValueError, match="inside"):
        label(
            Target.REPLY,
            status=LabelStatus.OBSERVED_POSITIVE,
            value=True,
            observed_at=WINDOW_END + timedelta(microseconds=1),
        )
    with pytest.raises(ValueError, match="must not carry observed_at"):
        label(Target.REPLY, observed_at=NOW)


def test_invalid_prediction_window_and_provenance_are_rejected() -> None:
    with pytest.raises(ValueError, match="lower <= point <= upper"):
        prediction(Target.REPLY, point=0.1, lower=0.2)
    with pytest.raises(ValueError, match="unavailable"):
        prediction(Target.REPLY, support=SupportStatus.UNAVAILABLE)
    with pytest.raises(ValueError, match="timezone-aware"):
        prediction(Target.REPLY, predicted_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="positive"):
        exposure(horizon_seconds=0, window_ends_at=NOW)
    with pytest.raises(ValueError, match="duplicates"):
        exposure(source_event_ids=("evt_1", "evt_1"))
    with pytest.raises(TypeError, match="tuple"):
        exposure(source_event_ids=["evt_1"])
