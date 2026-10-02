from __future__ import annotations

import hashlib

from companion_runtime.actual_action_v21 import (
    ScopeDriftError,
    actual_action_for_exposure,
    build_actual_action_witness,
    enforce_plan_render_scope,
)
from companion_runtime.typing import CandidateIntent
from companion_runtime.user_model_v2_features import FeatureSnapshotV2

from conftest import BASE_TIME


def review(**overrides):
    value = {
        "status": "approved",
        "reviewer": "semantic-gate:test",
        "review_version": "gold-v1",
        "asks_reply": False,
        "pressure": "low",
        "makes_commitment": False,
        "claims_task_completion": False,
    }
    value.update(overrides)
    return value


def witness(text: str, **kwargs):
    return build_actual_action_witness(
        text=text,
        attempt_id="attempt-1",
        render_outbox_id="render-1",
        send_outbox_id="send-1",
        render_metadata={
            "render_version": "renderer-7",
            "template_version": "low-pressure-3",
            "encoder_version": "encoder-21",
        },
        semantic_review=kwargs.get("semantic_review", review()),
    )


def test_same_plan_different_final_text_has_different_actual_action_witness() -> None:
    first = witness("只是想分享一下今天看到的云。")
    second = witness("只是想分享一下今天看到的晚霞。")

    assert first["rendered_text_sha256"] == hashlib.sha256(
        "只是想分享一下今天看到的云。".encode("utf-8")
    ).hexdigest()
    assert first["rendered_text_sha256"] != second["rendered_text_sha256"]
    assert first["actual_action_revision"] != second["actual_action_revision"]
    assert first["normalized"]["length_codepoints"] != second["normalized"]["length_codepoints"]
    assert first["attempt_id"] == second["attempt_id"] == "attempt-1"
    assert first["render_outbox_id"] == "render-1"
    assert first["send_outbox_id"] == "send-1"


def test_unknown_semantics_are_explicit_and_unattributable_not_guessed_from_text() -> None:
    item = witness("你必须马上回复我！", semantic_review=None)
    assert item["semantic_review"]["status"] == "unknown"
    assert item["semantic_review"]["asks_reply"] == "unknown"
    assert item["semantic_review"]["pressure"] == "unknown"
    assert item["attribution"] == "UNATTRIBUTABLE"

    action = actual_action_for_exposure({"type": "expression", "asks_reply": False}, item)
    assert action["asks_reply"] is False
    assert action["actual_action_attribution"] == "UNATTRIBUTABLE"
    assert action["actual_action_revision"] == item["actual_action_revision"]


def test_post_treatment_witness_does_not_change_prediction_vector() -> None:
    planned = {"type": "expression", "proactive": True, "asks_reply": False}
    actual = actual_action_for_exposure(planned, witness("只是分享一下。"))
    context = {"busy_probability": 0.1, "recent_contact_count": 0}
    baseline = FeatureSnapshotV2(
        scope_key="scope", exposure_id="prediction", action_json=planned,
        context_json=context, context_cutoff_at=BASE_TIME, created_at=BASE_TIME,
    )
    observed = FeatureSnapshotV2(
        scope_key="scope", exposure_id="training", action_json=actual,
        context_json=context, context_cutoff_at=BASE_TIME, created_at=BASE_TIME,
    )
    assert observed.values == baseline.values
    assert observed.missing_mask == baseline.missing_mask


def test_approved_reply_pressure_drift_is_blocked_without_reauthorization() -> None:
    item = witness(
        "马上回复我。",
        semantic_review=review(asks_reply=True, pressure="urgent"),
    )
    plan = {"type": "expression", "asks_reply": False, "pressure": "low"}
    try:
        enforce_plan_render_scope(planned_action=plan, witness=item)
    except ScopeDriftError as exc:
        assert "asks_reply" in str(exc) and "pressure" in str(exc)
    else:
        raise AssertionError("scope expansion must be blocked")

    enforce_plan_render_scope(planned_action=plan, witness=item, reauthorized=True)


def test_positive_control_equivalent_low_pressure_render_is_allowed() -> None:
    item = witness("刚看到一朵很好看的云，想和你分享。")
    enforce_plan_render_scope(
        planned_action={"type": "expression", "asks_reply": False, "pressure": "low"},
        witness=item,
    )


def test_reducer_binds_witness_to_send_outbox_and_blocks_pressure_drift(runtime) -> None:
    candidate = CandidateIntent(
        candidate_id="actual-action-candidate",
        type="share",
        intent="分享一件小事",
        goal="轻量表达",
        sources=[],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        attempt_id, render_id = runtime._commit_attempt(
            conn,
            chosen=candidate,
            state=runtime.projections.runtime.ensure(),
            now=BASE_TIME,
        )
        render_row = runtime.projections.outbox.get(render_id)
        render_row.payload = {
            **render_row.payload,
            "action": {"type": "expression", "asks_reply": False, "pressure": "low"},
        }
        runtime.projections.outbox.enqueue(conn, render_row)

    blocked = runtime.reducer.complete_render(
        outbox_id=render_id,
        text="马上回复我。",
        now=BASE_TIME,
        semantic_review=review(asks_reply=True, pressure="urgent"),
    )
    assert blocked.outbox_id is None
    assert runtime.projections.attempts.get(attempt_id).state == "failed"
    assert not runtime.projections.outbox.find_for_attempt(attempt_id, kind="send")


def test_reducer_positive_control_send_contains_frozen_actual_witness(runtime) -> None:
    candidate = CandidateIntent(
        candidate_id="actual-action-positive",
        type="share",
        intent="分享一件小事",
        goal="轻量表达",
        sources=[],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        attempt_id, render_id = runtime._commit_attempt(
            conn,
            chosen=candidate,
            state=runtime.projections.runtime.ensure(),
            now=BASE_TIME,
        )
        render_row = runtime.projections.outbox.get(render_id)
        render_row.payload = {
            **render_row.payload,
            "action": {"type": "expression", "asks_reply": False, "pressure": "low"},
        }
        runtime.projections.outbox.enqueue(conn, render_row)

    rendered = runtime.reducer.complete_render(
        outbox_id=render_id,
        text="刚看到一朵很好看的云，想和你分享。",
        now=BASE_TIME,
        render_metadata={"render_version": "r1", "template_version": "lp1", "encoder_version": "e21"},
        semantic_review=review(),
    )
    assert rendered.outbox_id
    send = runtime.projections.outbox.get(rendered.outbox_id)
    actual = send.payload["actual_action_witness"]
    assert actual["attempt_id"] == attempt_id
    assert actual["render_outbox_id"] == render_id
    assert actual["send_outbox_id"] == send.outbox_id
    assert actual["semantic_review"]["asks_reply"] is False
    assert actual["attribution"] == "ATTRIBUTABLE"
