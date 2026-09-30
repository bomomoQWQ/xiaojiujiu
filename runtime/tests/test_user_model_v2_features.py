"""Tests for the isolated action/context freeze and v2 feature encoder."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from companion_runtime.user_model_v2_features import (
    DEFAULT_FEATURE_SPEC_V2,
    FeatureSnapshotV2,
    FeatureSpecV2,
    V2_FEATURE_NAMES,
    canonical_json,
    encode_features_v2,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 10, 1, 11, 59, tzinfo=timezone.utc)


def snapshot(*, action: dict | None = None, context: dict | None = None) -> FeatureSnapshotV2:
    return FeatureSnapshotV2(
        scope_key="user:42/channel:direct",
        exposure_id="exp_1",
        action_json=action or {},
        context_json=context or {},
        context_cutoff_at=CUTOFF,
        created_at=NOW,
    )


def position(name: str) -> int:
    return V2_FEATURE_NAMES.index(name)


def test_spec_has_stable_names_version_and_sha256_fingerprint() -> None:
    first = FeatureSpecV2()
    second = FeatureSpecV2(names=tuple(V2_FEATURE_NAMES), version=first.version)

    assert first.names == V2_FEATURE_NAMES
    assert first.version == "user-model-v2.0"
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    int(first.fingerprint, 16)
    assert first.to_dict() == DEFAULT_FEATURE_SPEC_V2.to_dict()


def test_fingerprint_changes_with_order_names_or_version() -> None:
    baseline = FeatureSpecV2()
    assert FeatureSpecV2(names=tuple(reversed(baseline.names))).fingerprint != baseline.fingerprint
    assert FeatureSpecV2(names=baseline.names + ("future",)).fingerprint != baseline.fingerprint
    assert FeatureSpecV2(version="user-model-v2.1").fingerprint != baseline.fingerprint


def test_canonical_json_and_snapshot_are_independent_of_dict_insertion_order() -> None:
    left = {"z": [3, {"b": 2, "a": 1}], "a": {"two": 2, "one": 1}}
    right = {"a": {"one": 1, "two": 2}, "z": [3, {"a": 1, "b": 2}]}

    assert canonical_json(left) == canonical_json(right)
    assert json.loads(canonical_json(left)) == left

    one = snapshot(action={"type": "share", "proactive": True}, context={"novelty": 0.7})
    two = snapshot(action={"proactive": True, "type": "share"}, context={"novelty": 0.7})
    assert one.values == two.values
    assert one.missing_mask == two.missing_mask
    assert one.to_dict()["feature_fingerprint"] == two.to_dict()["feature_fingerprint"]


def test_snapshot_deep_freezes_action_and_context_and_serializes_safely() -> None:
    action = {"type": "contact", "meta": {"tags": ["original"]}}
    context = {"nested": [{"value": 3}], "recent_contact_count": 2}
    item = snapshot(action=action, context=context)

    action["type"] = "repair"
    action["meta"]["tags"].append("mutated")
    context["nested"][0]["value"] = 99
    context["recent_contact_count"] = 100

    assert item.action_json["type"] == "contact"
    assert item.action_json["meta"]["tags"] == ("original",)  # type: ignore[index]
    assert item.context_json["nested"][0]["value"] == 3  # type: ignore[index]
    assert item.values[position("recent_contact_count")] == 2.0
    with pytest.raises(TypeError):
        item.action_json["type"] = "reply"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        item.scope_key = "other"  # type: ignore[misc]

    payload = item.to_dict()
    assert payload["action_json"]["meta"]["tags"] == ["original"]
    assert json.loads(json.dumps(payload, allow_nan=False))["context_json"]["nested"] == [
        {"value": 3}
    ]


def test_missing_and_observed_zero_are_strictly_distinct() -> None:
    missing = encode_features_v2({}, {})
    observed_zero = encode_features_v2(
        {"proactive": False, "type": "contact"},
        {
            "busy_probability": 0.0,
            "recent_contact_count": 0,
            "hours_since_contact": 0.0,
            "user_active_now": False,
            "ever_boundary": False,
            "novelty": 0.0,
            "explicit_permission": False,
        },
    )

    for name in ("busy", "recent_contact_count", "hours_since_contact", "novelty"):
        index = position(name)
        assert missing.values[index] == observed_zero.values[index] == 0.0
        assert missing.missing_mask[index] is True
        assert observed_zero.missing_mask[index] is False
    assert missing.missing_mask[position("proactive")] is True
    assert observed_zero.missing_mask[position("proactive")] is False


def test_current_action_types_map_to_compatible_v2_features() -> None:
    item = snapshot(
        action={"type": "curious_question", "proactive": True},
        context={
            "busy_probability": 0.25,
            "recent_contact_count": 7,
            "hours_since_contact": 72,
            "user_active_now": True,
            "ever_boundary": False,
            "novelty": 0.5,
            "explicit_permission": True,
        },
    )
    by_name = dict(zip(item.spec.names, item.values, strict=True))

    assert by_name == {
        "bias": 1.0,
        "proactive": 1.0,
        "follow_up": 0.0,
        "emotional_expression": 0.0,
        "question": 1.0,
        "topic_shift": 1.0,
        "busy": 0.25,
        "recent_contact_count": 7.0,
        "hours_since_contact": 72.0,
        "collision": 1.0,
        "after_boundary": 0.0,
        "novelty": 0.5,
        "explicit_permission": 1.0,
    }
    assert not any(item.missing_mask)
    assert by_name["recent_contact_count"] > 1.0  # no legacy tolerance/clamp
    assert by_name["hours_since_contact"] > 24.0  # no legacy 24-hour saturation


def test_snapshot_and_direct_encoder_share_the_exact_same_function_result() -> None:
    action = {"type": "follow_up", "proactive": True, "question": False}
    context = {"busy_probability": 0.2, "hours_since_contact": 49.5}
    direct = encode_features_v2(action, context)
    frozen = snapshot(action=action, context=context)

    assert frozen.values == direct.values
    assert frozen.missing_mask == direct.missing_mask
    # An explicit action value overrides the backwards-compatible type derivation.
    assert frozen.values[position("question")] == 0.0
    assert frozen.missing_mask[position("question")] is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_data_is_rejected_everywhere(bad: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        snapshot(action={"score": bad})
    with pytest.raises(ValueError, match="finite"):
        encode_features_v2({}, {"busy_probability": bad})
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"value": bad})


@pytest.mark.parametrize("bad", [{"items": {1, 2}}, {1: "non-string-key"}, {"raw": object()}])
def test_non_json_data_is_rejected(bad: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        snapshot(context=bad)
    with pytest.raises((TypeError, ValueError)):
        canonical_json(bad)


def test_naive_datetimes_and_future_cutoff_are_rejected() -> None:
    naive = datetime(2026, 10, 1, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        FeatureSnapshotV2(
            scope_key="scope",
            exposure_id="exp",
            action_json={},
            context_json={},
            context_cutoff_at=naive,
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        FeatureSnapshotV2(
            scope_key="scope",
            exposure_id="exp",
            action_json={},
            context_json={},
            context_cutoff_at=CUTOFF,
            created_at=naive,
        )
    with pytest.raises(ValueError, match="must not be after"):
        FeatureSnapshotV2(
            scope_key="scope",
            exposure_id="exp",
            action_json={},
            context_json={},
            context_cutoff_at=NOW,
            created_at=CUTOFF,
        )
