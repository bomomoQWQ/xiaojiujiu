from __future__ import annotations

import pytest

from companion_runtime.actual_action_v21 import (
    ScopeDriftError,
    actual_action_for_exposure,
    build_actual_action_witness,
    enforce_plan_render_scope,
    verify_render_plan_v1,
)
from companion_runtime.render_plan_v1 import build_render_plan_v1, freeze_action_render_plan
from companion_runtime.runtime_v2 import CandidateV2
from companion_runtime.motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
from companion_runtime.repeat_v2 import RepeatSubjectV2
from companion_runtime.user_model_v2_features import FeatureSnapshotV2, V2_FEATURE_NAMES

from conftest import BASE_TIME


def plan(**overrides):
    value = {
        "asks_reply": False,
        "pressure_tier": "low",
        "completion_claim_intent": False,
        "commitment": False,
        "length_bucket": "short",
        "template_version": "share-2",
        "style_version": "warm-1",
    }
    value.update(overrides)
    return build_render_plan_v1(**value)


def review(**overrides):
    value = {
        "status": "approved",
        "reviewer": "test",
        "review_version": "gold-1",
        "asks_reply": False,
        "pressure": "low",
        "makes_commitment": False,
        "claims_task_completion": False,
    }
    value.update(overrides)
    return value


def witness(text: str, frozen_plan: dict, **review_overrides):
    return build_actual_action_witness(
        text=text,
        attempt_id="a",
        render_outbox_id="r",
        send_outbox_id="s",
        render_metadata={
            "template_version": frozen_plan["template_version"],
            "style_version": frozen_plan["style_version"],
            "render_plan_revision": frozen_plan["revision"],
        },
        semantic_review=review(**review_overrides),
    )


def snapshot(action):
    return FeatureSnapshotV2(
        scope_key="scope",
        exposure_id="exp",
        action_json=action,
        context_json={
            "busy_probability": 0.1,
            "recent_contact_count": 0,
            "hours_since_contact": 12,
            "user_active_now": False,
            "ever_boundary": False,
            "novelty": 0.5,
            "explicit_permission": True,
        },
        context_cutoff_at=BASE_TIME,
        created_at=BASE_TIME,
    )


def test_same_plan_different_actual_copy_stays_on_same_design_row() -> None:
    frozen = plan()
    action = {"type": "expression", "proactive": True, "render_plan_v1": frozen}
    first = actual_action_for_exposure(action, witness("今天看到一朵云。", frozen))
    second = actual_action_for_exposure(action, witness("刚才的晚霞很好看。", frozen))

    one = snapshot(first)
    two = snapshot(second)
    assert first["actual_action_revision"] != second["actual_action_revision"]
    assert one.action_json["render_plan_v1"]["revision"] == frozen["revision"]
    assert one.values == two.values
    assert one.missing_mask == two.missing_mask


def test_different_preregistered_plans_are_distinguishable() -> None:
    quiet = freeze_action_render_plan(
        {"type": "expression", "proactive": True, "render_plan_v1": plan()}
    )
    asking = freeze_action_render_plan(
        {
            "type": "expression",
            "proactive": True,
            "render_plan_v1": plan(
                asks_reply=True,
                pressure_tier="medium",
                commitment=True,
                length_bucket="long",
                template_version="ask-4",
            ),
        }
    )
    quiet_features = snapshot(quiet)
    asking_features = snapshot(asking)
    assert quiet["render_plan_v1"]["revision"] != asking["render_plan_v1"]["revision"]
    assert quiet_features.values != asking_features.values
    by_name = dict(zip(V2_FEATURE_NAMES, asking_features.values, strict=True))
    assert by_name["plan_asks_reply"] == 1.0
    assert by_name["plan_pressure_tier"] == 2.0
    assert by_name["plan_commitment"] == 1.0
    assert by_name["plan_length_bucket"] == 2.0


def test_actual_witness_text_and_review_cannot_leak_into_encoder() -> None:
    frozen = plan()
    action = {"type": "expression", "proactive": True, "render_plan_v1": frozen}
    baseline = snapshot(action)
    hostile_actual = actual_action_for_exposure(
        action,
        witness(
            "必须马上回复，我保证已经完成！",
            frozen,
            asks_reply=True,
            pressure="urgent",
            makes_commitment=True,
            claims_task_completion=True,
        ),
    )
    observed = snapshot(hostile_actual)
    assert observed.values == baseline.values
    assert observed.missing_mask == baseline.missing_mask


def test_drift_is_unattributable_and_requires_new_plan_revision() -> None:
    frozen = plan()
    action = {"type": "expression", "render_plan_v1": frozen}
    actual = witness("请回复。", frozen, asks_reply=True)
    attribution, drift = verify_render_plan_v1(planned_action=action, witness=actual)
    assert attribution == "UNATTRIBUTABLE"
    assert drift == ("asks_reply",)
    with pytest.raises(ScopeDriftError, match="new_render_plan_revision_required"):
        enforce_plan_render_scope(planned_action=action, witness=actual)

    revised = plan(asks_reply=True)
    revised_action = {"type": "expression", "render_plan_v1": revised}
    revised_actual = witness("请回复。", revised, asks_reply=True)
    assert revised["revision"] != frozen["revision"]
    assert verify_render_plan_v1(planned_action=revised_action, witness=revised_actual) == (
        "ATTRIBUTABLE",
        (),
    )


def test_candidate_freezes_plan_before_prediction_or_render() -> None:
    source = {"type": "share", "asks_reply": False, "template_version": "share-2"}
    candidate = CandidateV2(
        candidate_id="c",
        action=source,
        internal_utility=0.2,
        coefficients=UserUtilityCoefficientsV2(v_reply=1.0, v_continue=1.0, c_negative=1.0),
        repeat_subject=RepeatSubjectV2(),
        policy=CandidatePolicyV2(low_pressure=True, low_frequency=True, easy_to_ignore=True),
    )
    source["asks_reply"] = True
    assert candidate.action["render_plan_v1"]["asks_reply"] is False
    assert len(candidate.action["render_plan_v1"]["revision"]) == 64
