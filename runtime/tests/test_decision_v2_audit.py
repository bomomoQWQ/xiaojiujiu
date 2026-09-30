"""Tests for the isolated decision-v2 audit recorder."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.decision_v2_audit import (
    DECISION_V2_AUDIT_CONTRACT_VERSION,
    CandidateAssessment,
    DecisionAuditError,
    DecisionAuditRecorder,
    DecisionRun,
    DecisionStage,
    DecisionStageError,
    IdempotencyConflict,
)

NOW = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)


def assessment(candidate_id: str = "candidate:follow-up") -> CandidateAssessment:
    return CandidateAssessment(
        candidate_id=candidate_id,
        prediction_snapshot_id="prediction:88",
        used_bounds={"reply_lower": 0.31, "negative_upper": 0.12},
        utility_terms={
            "care": 0.7,
            "expected_reply": 0.2,
            "repeat_cost": -0.15,
            "silence": -0.1,
        },
        repeat_key="unfinished:interview",
        reasons=("reply_used_lower_bound", "negative_used_upper_bound"),
    )


def run(**changes: object) -> DecisionRun:
    values: dict[str, object] = {
        "decision_id": "decision:1",
        "scope": "user:42/channel:direct",
        "policy_version": "decision-policy-v2.0",
        "contract_version": "decision-contract-v2.0",
        "feature_version": "features-v2.3",
        "parameter_version": "parameters:17",
        "D": 0.42,
        "lambda_rate": 0.0002,
        "delta_allowed_seconds": 300.0,
        "cumulative_lambda": 0.06,
        "trial_probability": 0.0582354664,
        "random_draw": 0.02,
        "chosen": "candidate:follow-up",
    }
    values.update(changes)
    return DecisionRun(**values)  # type: ignore[arg-type]


def recorder(**run_changes: object) -> DecisionAuditRecorder:
    return DecisionAuditRecorder(run(**run_changes), (assessment(),))


def record_send_success(item: DecisionAuditRecorder) -> None:
    stages = (
        DecisionStage.WAKE,
        DecisionStage.PERMISSIONS,
        DecisionStage.CANDIDATE_ELIGIBLE,
        DecisionStage.HAZARD_TRIAL_PERFORMED,
        DecisionStage.HAZARD_TRIAL_WON,
        DecisionStage.COMMITTED,
        DecisionStage.RENDERED,
        DecisionStage.SEND_ACK,
        DecisionStage.RECONCILED,
    )
    for index, stage in enumerate(stages):
        item.record(stage, occurred_at=NOW + timedelta(seconds=index), details={"n": index})


def test_complete_success_path_records_required_stages_and_json() -> None:
    item = recorder()
    record_send_success(item)

    payload = item.to_dict()
    assert payload["audit_contract_version"] == DECISION_V2_AUDIT_CONTRACT_VERSION
    assert [event["stage"] for event in payload["events"]] == [
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
    assert payload["run"] == {
        "decision_id": "decision:1",
        "scope": "user:42/channel:direct",
        "policy_version": "decision-policy-v2.0",
        "contract_version": "decision-contract-v2.0",
        "feature_version": "features-v2.3",
        "parameter_version": "parameters:17",
        "D": 0.42,
        "lambda": 0.0002,
        "delta_allowed_seconds": 300.0,
        "cumulative_lambda": 0.06,
        "trial_probability": 0.0582354664,
        "random_draw": 0.02,
        "chosen": "candidate:follow-up",
    }
    assert payload["assessments"][0]["prediction_snapshot_id"] == "prediction:88"
    assert payload["assessments"][0]["used_bounds"]["negative_upper"] == 0.12
    assert payload["assessments"][0]["utility_terms"]["repeat_cost"] == -0.15
    assert payload["assessments"][0]["repeat_key"] == "unfinished:interview"
    assert json.loads(json.dumps(payload))["events"][-1]["stage"] == "reconciled"


def test_send_failure_is_recorded_as_distinct_terminal_delivery_outcome() -> None:
    item = recorder()
    for index, stage in enumerate(
        (
            DecisionStage.WAKE,
            DecisionStage.PERMISSIONS,
            DecisionStage.CANDIDATE_ELIGIBLE,
            DecisionStage.HAZARD_TRIAL_PERFORMED,
            DecisionStage.HAZARD_TRIAL_WON,
            DecisionStage.COMMITTED,
            DecisionStage.RENDERED,
            DecisionStage.SEND_FAIL,
            DecisionStage.RECONCILED,
        )
    ):
        item.record(stage, occurred_at=NOW + timedelta(seconds=index))
    assert [event.stage for event in item.events][-2:] == [
        DecisionStage.SEND_FAIL,
        DecisionStage.RECONCILED,
    ]


def test_state_machine_rejects_out_of_order_and_send_without_commit_render() -> None:
    item = recorder()
    with pytest.raises(DecisionStageError, match="after 'start'"):
        item.record(DecisionStage.PERMISSIONS, occurred_at=NOW)

    item.record(DecisionStage.WAKE, occurred_at=NOW)
    item.record(DecisionStage.PERMISSIONS, occurred_at=NOW)
    item.record(DecisionStage.CANDIDATE_ELIGIBLE, occurred_at=NOW)
    with pytest.raises(DecisionStageError, match="expected hazard_trial_performed"):
        item.record(DecisionStage.SEND_ACK, occurred_at=NOW)

    # Even if a future transition table is relaxed, the explicit send invariant remains
    # covered by the public behavior today: neither outcome can jump commit/render.
    with pytest.raises(DecisionStageError):
        item.record(DecisionStage.SEND_FAIL, occurred_at=NOW)


def test_each_stage_is_idempotent_but_conflicting_retry_is_rejected() -> None:
    item = recorder()
    first = item.record(
        DecisionStage.WAKE,
        occurred_at=NOW,
        details={"source": "scheduler", "window": ["09:25", "09:30"]},
    )
    retry = item.record(
        "wake",
        occurred_at=NOW + timedelta(seconds=30),
        details={"source": "scheduler", "window": ["09:25", "09:30"]},
    )
    assert retry is first
    assert len(item.events) == 1

    with pytest.raises(IdempotencyConflict, match="different details"):
        item.record(
            DecisionStage.WAKE,
            occurred_at=NOW,
            details={"source": "user_message"},
        )


def test_trial_requires_recorded_draw_and_win_must_match_it() -> None:
    missing = recorder(trial_probability=None, random_draw=None)
    missing.record(DecisionStage.WAKE, occurred_at=NOW)
    missing.record(DecisionStage.PERMISSIONS, occurred_at=NOW)
    missing.record(DecisionStage.CANDIDATE_ELIGIBLE, occurred_at=NOW)
    with pytest.raises(DecisionStageError, match="random_draw"):
        missing.record(DecisionStage.HAZARD_TRIAL_PERFORMED, occurred_at=NOW)

    lost = recorder(trial_probability=0.4, random_draw=0.4)
    lost.record(DecisionStage.WAKE, occurred_at=NOW)
    lost.record(DecisionStage.PERMISSIONS, occurred_at=NOW)
    lost.record(DecisionStage.CANDIDATE_ELIGIBLE, occurred_at=NOW)
    lost.record(DecisionStage.HAZARD_TRIAL_PERFORMED, occurred_at=NOW)
    with pytest.raises(DecisionStageError, match="random_draw < trial_probability"):
        lost.record(DecisionStage.HAZARD_TRIAL_WON, occurred_at=NOW)
    lost.record(
        DecisionStage.RECONCILED,
        occurred_at=NOW,
        details={"outcome": "trial_lost"},
    )


def test_reconcile_can_close_non_sending_branches_but_is_terminal() -> None:
    item = recorder(chosen=None, trial_probability=None, random_draw=None)
    item.record(DecisionStage.WAKE, occurred_at=NOW)
    item.record(DecisionStage.PERMISSIONS, occurred_at=NOW)
    item.record(
        DecisionStage.RECONCILED,
        occurred_at=NOW,
        details={"outcome": "permissions_denied", "reason": "quiet_hours"},
    )
    with pytest.raises(DecisionStageError, match="no further stage"):
        item.record(DecisionStage.CANDIDATE_ELIGIBLE, occurred_at=NOW)


def test_candidate_and_run_contract_validation() -> None:
    with pytest.raises(DecisionAuditError, match="trial_probability and random_draw"):
        run(random_draw=None)
    with pytest.raises(DecisionAuditError, match=r"random_draw must be in \[0, 1\]"):
        run(random_draw=1.1)
    with pytest.raises(DecisionAuditError, match="finite"):
        run(cumulative_lambda=float("nan"))
    with pytest.raises(DecisionAuditError, match="chosen must identify"):
        DecisionAuditRecorder(run(chosen="candidate:missing"), (assessment(),))
    with pytest.raises(DecisionAuditError, match="unique candidate_id"):
        DecisionAuditRecorder(run(), (assessment(), assessment()))
    with pytest.raises(DecisionAuditError, match="reasons must not contain duplicates"):
        CandidateAssessment(
            candidate_id="candidate:x",
            prediction_snapshot_id="prediction:1",
            used_bounds={},
            utility_terms={},
            repeat_key="repeat:x",
            reasons=("same", "same"),
        )


def test_commit_requires_chosen_candidate_and_timestamps_are_monotonic() -> None:
    item = recorder(chosen=None)
    for stage in (
        DecisionStage.WAKE,
        DecisionStage.PERMISSIONS,
        DecisionStage.CANDIDATE_ELIGIBLE,
        DecisionStage.HAZARD_TRIAL_PERFORMED,
        DecisionStage.HAZARD_TRIAL_WON,
    ):
        item.record(stage, occurred_at=NOW)
    with pytest.raises(DecisionStageError, match="chosen candidate"):
        item.record(DecisionStage.COMMITTED, occurred_at=NOW)

    ordered = recorder()
    ordered.record(DecisionStage.WAKE, occurred_at=NOW)
    with pytest.raises(DecisionStageError, match="monotonic"):
        ordered.record(DecisionStage.PERMISSIONS, occurred_at=NOW - timedelta(seconds=1))


def test_candidate_mappings_are_defensively_copied() -> None:
    bounds = {"reply_lower": 0.3}
    terms = {"care": 0.5}
    item = CandidateAssessment(
        candidate_id="candidate:x",
        prediction_snapshot_id="prediction:x",
        used_bounds=bounds,
        utility_terms=terms,
        repeat_key="repeat:x",
        reasons=(),
    )
    bounds["reply_lower"] = 0.9
    terms["care"] = 99.0
    assert item.used_bounds == {"reply_lower": 0.3}
    assert item.utility_terms == {"care": 0.5}
    with pytest.raises(TypeError):
        item.used_bounds["reply_lower"] = 0.1  # type: ignore[index]
