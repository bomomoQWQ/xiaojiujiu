"""MOT-10--12 acceptance tests for the isolated v2 repeat policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.repeat_v2 import (
    RepeatPolicyConfigV2,
    RepeatSubjectV2,
    SendAcknowledgedExposureV2,
    UserMatterEventKind,
    UserMatterEventV2,
    evaluate_repeat_v2,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SUBJECT = RepeatSubjectV2(concern_id="interview", action_goal_id="ask-result")


def sent(
    exposure_id: str,
    *,
    hours_ago: float,
    concern_id: str | None = "interview",
    action_goal_id: str | None = "ask-result",
) -> SendAcknowledgedExposureV2:
    return SendAcknowledgedExposureV2(
        exposure_id=exposure_id,
        acknowledged_at_utc=NOW - timedelta(hours=hours_ago),
        concern_id=concern_id,
        action_goal_id=action_goal_id,
    )


def config(**changes: object) -> RepeatPolicyConfigV2:
    values: dict[str, object] = {
        "contact_window": timedelta(hours=3),
        "matter_window": timedelta(hours=18),
        "contact_allowance": 1,
        "matter_repeat_allowance": 1,
        "contact_cost_per_excess": 0.25,
        "matter_cost_per_excess": 0.8,
        "hard_contact_limit": 3,
        "hard_matter_repeat_limit": 2,
    }
    values.update(changes)
    return RepeatPolicyConfigV2(**values)  # type: ignore[arg-type]


def test_mot_10_same_matter_without_progress_has_cost_and_hard_limit() -> None:
    result = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=[sent("old", hours_ago=10), sent("recent", hours_ago=1)],
        now_utc=NOW,
        config=config(),
    )

    assert result.recent_contact_count == 1
    assert result.same_matter_no_progress_count == 2
    assert result.short_contact_cost == pytest.approx(0.25)
    assert result.same_matter_cost == pytest.approx(1.6)
    assert result.total_cost == pytest.approx(1.85)
    assert result.blocked
    assert result.hard_limit_reasons == ("same_matter_no_progress_limit_exceeded",)


def test_mot_10_contact_load_and_same_matter_are_separate_components() -> None:
    history = [
        sent("same", hours_ago=2, concern_id="interview", action_goal_id="other-goal"),
        sent("other", hours_ago=1, concern_id="dinner", action_goal_id="eat"),
    ]
    result = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=history,
        now_utc=NOW,
        config=config(contact_allowance=2, hard_contact_limit=2),
    )

    assert result.recent_contact_count == 2
    assert result.same_matter_no_progress_count == 1
    assert result.short_contact_cost == pytest.approx(0.25)
    assert result.same_matter_cost == pytest.approx(0.8)
    assert result.hard_limit_reasons == ("short_contact_limit_exceeded",)


def test_mot_11_one_hundred_unsent_evaluations_do_not_change_cost() -> None:
    # Evaluation receives immutable send-acknowledged history and has no stateful counter.
    history = [sent("ack-1", hours_ago=1)]
    first = evaluate_repeat_v2(
        subject=SUBJECT, exposure_history=history, now_utc=NOW, config=config()
    )
    for _ in range(100):
        again = evaluate_repeat_v2(
            subject=SUBJECT, exposure_history=history, now_utc=NOW, config=config()
        )

    assert again == first
    assert again.recent_contact_count == 1
    assert again.same_matter_no_progress_count == 1


def test_mot_11_history_rejects_candidate_evaluation_records() -> None:
    @dataclass(frozen=True)
    class CandidateEvaluation:
        candidate_id: str
        evaluated_at_utc: datetime

    with pytest.raises(TypeError, match="only SendAcknowledgedExposureV2"):
        evaluate_repeat_v2(
            subject=SUBJECT,
            exposure_history=[CandidateEvaluation("candidate", NOW)],  # type: ignore[list-item]
            now_utc=NOW,
            config=config(),
        )


def test_mot_12_new_user_progress_resets_same_matter_but_not_contact_load() -> None:
    progress_at = NOW - timedelta(minutes=30)
    result = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=[sent("before-progress", hours_ago=1)],
        user_matter_events=[
            UserMatterEventV2(
                event_id="user-update",
                occurred_at_utc=progress_at,
                kind=UserMatterEventKind.PROGRESS,
                concern_id="interview",
            )
        ],
        now_utc=NOW,
        config=config(),
    )

    assert result.recent_contact_count == 1
    assert result.short_contact_cost == pytest.approx(0.25)
    assert result.same_matter_no_progress_count == 0
    assert result.same_matter_cost == 0.0
    assert result.latest_reset_kind is UserMatterEventKind.PROGRESS
    assert result.latest_reset_at_utc == progress_at
    assert not result.blocked


@pytest.mark.parametrize("identity", ["concern", "goal"])
def test_mot_12_user_reopen_resets_matching_identity(identity: str) -> None:
    event = UserMatterEventV2(
        event_id=f"reopen-{identity}",
        occurred_at_utc=NOW - timedelta(hours=1),
        kind=UserMatterEventKind.REOPEN,
        concern_id="interview" if identity == "concern" else None,
        action_goal_id="ask-result" if identity == "goal" else None,
    )
    result = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=[sent("before-reopen", hours_ago=4)],
        user_matter_events=[event],
        now_utc=NOW,
        config=config(),
    )

    assert result.same_matter_no_progress_count == 0
    assert result.same_matter_cost == 0.0
    assert result.latest_reset_kind is UserMatterEventKind.REOPEN


def test_progress_only_resets_sends_before_it() -> None:
    result = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=[sent("before", hours_ago=4), sent("after", hours_ago=0.5)],
        user_matter_events=[
            UserMatterEventV2(
                event_id="progress",
                occurred_at_utc=NOW - timedelta(hours=1),
                kind=UserMatterEventKind.PROGRESS,
                action_goal_id="ask-result",
            )
        ],
        now_utc=NOW,
        config=config(),
    )

    assert result.same_matter_no_progress_count == 1
    assert result.same_matter_cost == pytest.approx(0.8)


def test_windows_are_configurable_and_not_an_implicit_48_hours() -> None:
    exposure = sent("four-hours-old", hours_ago=4)
    short = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=[exposure],
        now_utc=NOW,
        config=config(contact_window=timedelta(hours=2), matter_window=timedelta(hours=2)),
    )
    long = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=[exposure],
        now_utc=NOW,
        config=config(contact_window=timedelta(hours=5), matter_window=timedelta(hours=5)),
    )

    assert (short.recent_contact_count, short.same_matter_no_progress_count) == (0, 0)
    assert (long.recent_contact_count, long.same_matter_no_progress_count) == (1, 1)


def test_all_times_must_be_explicit_utc() -> None:
    naive = NOW.replace(tzinfo=None)
    non_utc = NOW.astimezone(timezone(timedelta(hours=8)))

    with pytest.raises(ValueError, match="UTC"):
        SendAcknowledgedExposureV2("bad", naive)
    with pytest.raises(ValueError, match="UTC"):
        SendAcknowledgedExposureV2("bad", non_utc)
    with pytest.raises(ValueError, match="UTC"):
        evaluate_repeat_v2(
            subject=SUBJECT, exposure_history=[], now_utc=naive, config=config()
        )


def test_duplicate_delivery_replay_counts_once_and_conflicts_fail() -> None:
    exposure = sent("same-ack", hours_ago=1)
    result = evaluate_repeat_v2(
        subject=SUBJECT,
        exposure_history=[exposure, exposure],
        now_utc=NOW,
        config=config(),
    )
    assert result.recent_contact_count == 1
    assert result.same_matter_no_progress_count == 1

    with pytest.raises(ValueError, match="conflicting duplicate"):
        evaluate_repeat_v2(
            subject=SUBJECT,
            exposure_history=[exposure, sent("same-ack", hours_ago=2)],
            now_utc=NOW,
            config=config(),
        )
