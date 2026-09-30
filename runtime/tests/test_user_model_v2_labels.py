"""OBS contract tests for pure user-model v2 target settlement."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.user_model_v2_labels import (
    ObservationOrigin,
    SettlementContextV2,
    TargetObservationV2,
    next_label_revision,
    settle_labels,
    settle_target_label,
    settlement_key,
)
from companion_runtime.user_model_v2_types import (
    DeliveryBasis,
    InteractionExposureV2,
    LabelStatus,
    Target,
)

START = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=6)


def exposure(**changes: object) -> InteractionExposureV2:
    values: dict[str, object] = {
        "exposure_id": "exp-1",
        "scope_key": "user:42/channel:direct",
        "occurred_at": START,
        "window_started_at": START,
        "window_ends_at": END,
        "horizon_seconds": 6 * 60 * 60,
        "delivery_basis": DeliveryBasis.DELIVERED,
        "created_at": START,
        "updated_at": START,
        "source_event_ids": ("delivery-1",),
    }
    values.update(changes)
    return InteractionExposureV2(**values)  # type: ignore[arg-type]


def context(at: datetime, **changes: object) -> SettlementContextV2:
    values: dict[str, object] = {"as_of": at}
    values.update(changes)
    return SettlementContextV2(**values)  # type: ignore[arg-type]


def observation(
    target: Target,
    *,
    event_id: str = "event-1",
    at: datetime = START + timedelta(hours=1),
    value: bool = True,
    candidates: tuple[str, ...] = ("exp-1",),
    explicit: bool = False,
    origin: ObservationOrigin = ObservationOrigin.USER,
) -> TargetObservationV2:
    return TargetObservationV2(
        event_id=event_id,
        target=target,
        occurred_at=at,
        value=value,
        candidate_exposure_ids=candidates,
        explicit=explicit,
        origin=origin,
    )


def test_obs_01_pending_before_fixed_reply_deadline() -> None:
    label = settle_target_label(exposure(), Target.REPLY, (), context(START + timedelta(hours=2)))
    assert label.status is LabelStatus.PENDING
    assert label.value is None


def test_obs_02_complete_window_without_reply_is_binary_zero_not_busy_pseudolabel() -> None:
    item = exposure(attributes=(("busy", 0.6),))
    label = settle_target_label(item, Target.REPLY, (), context(END))
    assert label.status is LabelStatus.OBSERVED_NEGATIVE
    assert label.value is False
    assert label.observed_at == END


def test_obs_03_attributable_reply_in_window_is_one_even_when_slow() -> None:
    reply = observation(Target.REPLY, at=END - timedelta(seconds=1))
    label = settle_target_label(exposure(), Target.REPLY, (reply,), context(END))
    assert label.status is LabelStatus.OBSERVED_POSITIVE
    assert label.value is True
    assert label.observed_at == reply.occurred_at


def test_obs_04_interrupted_partial_coverage_is_censored_not_zero() -> None:
    label = settle_target_label(
        exposure(),
        Target.REPLY,
        (),
        context(START + timedelta(hours=2), observation_complete=False),
    )
    assert label.status is LabelStatus.CENSORED
    assert label.value is None


def test_obs_05_reply_after_window_does_not_change_fixed_window_zero() -> None:
    late = observation(Target.REPLY, event_id="late", at=END + timedelta(seconds=1))
    label = settle_target_label(
        exposure(), Target.REPLY, (late,), context(END + timedelta(hours=1))
    )
    assert label.status is LabelStatus.OBSERVED_NEGATIVE
    assert label.value is False
    assert "late" not in label.source_event_ids


def test_obs_06_late_log_of_in_window_reply_creates_semantic_revision() -> None:
    item = exposure()
    original = settle_target_label(item, Target.REPLY, (), context(END))
    delayed_log = observation(Target.REPLY, event_id="reply-1", at=END - timedelta(minutes=1))
    corrected = settle_target_label(
        item,
        Target.REPLY,
        (delayed_log,),
        context(END + timedelta(hours=1)),
    )
    assert original.value is False
    assert corrected.value is True
    assert next_label_revision(None, original) == 1
    assert next_label_revision(original, corrected, current_revision=1) == 2
    assert next_label_revision(corrected, corrected, current_revision=2) == 2


def test_obs_07_uncollected_continue_and_acceptance_are_missing_not_false() -> None:
    reply = observation(Target.REPLY)
    coverage = frozenset({Target.REPLY, Target.NEGATIVE})
    labels = {
        target: settle_target_label(
            exposure(), target, (reply,), context(END, collected_targets=coverage)
        )
        for target in (Target.ACCEPTANCE, Target.CONTINUE)
    }
    assert labels[Target.ACCEPTANCE].status is LabelStatus.UNKNOWN
    assert labels[Target.CONTINUE].status is LabelStatus.UNKNOWN
    assert all(label.value is None for label in labels.values())


def test_continue_requires_reply_and_complete_observation() -> None:
    continued = observation(Target.CONTINUE)
    without_reply = settle_target_label(exposure(), Target.CONTINUE, (continued,), context(END))
    assert without_reply.status is LabelStatus.UNKNOWN
    assert without_reply.value is None

    reply = observation(Target.REPLY, event_id="reply")
    interrupted = settle_target_label(
        exposure(),
        Target.CONTINUE,
        (reply, continued),
        context(END, observation_complete=False),
    )
    assert interrupted.status is LabelStatus.CENSORED
    assert interrupted.value is None

    still_open = settle_target_label(
        exposure(),
        Target.CONTINUE,
        (reply, continued),
        context(START + timedelta(hours=2)),
    )
    assert still_open.status is LabelStatus.PENDING
    assert still_open.value is None

    complete = settle_target_label(exposure(), Target.CONTINUE, (reply, continued), context(END))
    assert complete.status is LabelStatus.OBSERVED_POSITIVE
    assert complete.value is True


def test_acceptance_requires_explicit_attributable_feedback() -> None:
    implicit = observation(Target.ACCEPTANCE, explicit=False)
    ambiguous = observation(
        Target.ACCEPTANCE,
        event_id="ambiguous",
        explicit=True,
        candidates=("exp-1", "exp-2"),
    )
    implicit_label = settle_target_label(
        exposure(), Target.ACCEPTANCE, (implicit,), context(END)
    )
    assert implicit_label.status is LabelStatus.UNKNOWN
    result = settle_target_label(exposure(), Target.ACCEPTANCE, (ambiguous,), context(END))
    assert result.status is LabelStatus.UNATTRIBUTABLE
    assert result.value is None


def test_obs_13_ambiguous_reply_is_not_duplicated_across_exposures() -> None:
    first = exposure()
    second = exposure(exposure_id="exp-2", source_event_ids=("delivery-2",))
    shared = observation(Target.REPLY, candidates=("exp-1", "exp-2"))
    for item in (first, second):
        label = settle_target_label(item, Target.REPLY, (shared,), context(END))
        assert label.status is LabelStatus.UNATTRIBUTABLE
        assert label.value is None


def test_obs_14_invalid_or_failed_delivery_cannot_become_no_reply_sample() -> None:
    label = settle_target_label(
        exposure(), Target.REPLY, (), context(END, exposure_valid=False)
    )
    assert label.status is LabelStatus.INVALIDATED
    assert label.value is None


def test_obs_15_repeated_delivery_is_idempotent_by_key_and_revision() -> None:
    reply = observation(Target.REPLY, event_id="same-event")
    first = settle_target_label(exposure(), Target.REPLY, (reply,), context(END))
    replay = settle_target_label(exposure(), Target.REPLY, (reply,) * 10, context(END))
    assert replay.source_event_ids == ("delivery-1", "same-event")
    assert next_label_revision(first, replay, current_revision=1) == 1
    assert settlement_key(first.scope_key, first.exposure_id, first.target, 1) == settlement_key(
        replay.scope_key, replay.exposure_id, replay.target, 1
    )
    assert settlement_key("a:b", "c", Target.REPLY, 1) != settlement_key(
        "a", "b:c", Target.REPLY, 1
    )


def test_obs_16_positive_acceptance_and_scoped_negative_are_independent() -> None:
    reply = observation(Target.REPLY, event_id="reply")
    accepted = observation(Target.ACCEPTANCE, event_id="thanks", explicit=True)
    negative = observation(Target.NEGATIVE, event_id="scope-limit", explicit=True)
    labels = {
        label.target: label
        for label in settle_labels(exposure(), (reply, accepted, negative), context(END))
    }
    assert labels[Target.ACCEPTANCE].status is LabelStatus.OBSERVED_POSITIVE
    assert labels[Target.NEGATIVE].status is LabelStatus.OBSERVED_POSITIVE
    assert labels[Target.ACCEPTANCE].source_event_ids[-1] == "thanks"
    assert labels[Target.NEGATIVE].source_event_ids[-1] == "scope-limit"


def test_obs_17_internal_rehearsals_never_settle_user_targets() -> None:
    internal = observation(
        Target.REPLY,
        event_id="rehearsal",
        origin=ObservationOrigin.INTERNAL,
    )
    label = settle_target_label(exposure(), Target.REPLY, (internal,) * 10, context(END))
    assert label.status is LabelStatus.OBSERVED_NEGATIVE
    assert label.value is False
    assert "rehearsal" not in label.source_event_ids


def test_obs_18_no_defined_negative_event_means_event_absence_only() -> None:
    label = settle_target_label(exposure(), Target.NEGATIVE, (), context(END))
    assert label.status is LabelStatus.OBSERVED_NEGATIVE
    assert label.value is False
    assert label.target is Target.NEGATIVE
    assert label.target is not Target.ACCEPTANCE


def test_conflicting_same_target_feedback_is_unknown_not_arbitrarily_dropped() -> None:
    positive = observation(Target.ACCEPTANCE, event_id="yes", explicit=True)
    negative = observation(Target.ACCEPTANCE, event_id="no", value=False, explicit=True)
    label = settle_target_label(exposure(), Target.ACCEPTANCE, (positive, negative), context(END))
    assert label.status is LabelStatus.UNKNOWN
    assert label.value is None
    assert label.source_event_ids[-2:] == ("yes", "no")


def test_revision_helper_ignores_processing_metadata_but_tracks_new_sources() -> None:
    reply = observation(Target.REPLY, event_id="reply")
    label = settle_target_label(exposure(), Target.REPLY, (reply,), context(END))
    replay = replace(label, label_id="another-id", updated_at=END + timedelta(hours=1))
    assert next_label_revision(label, replay, current_revision=3) == 3
    enriched = replace(replay, source_event_ids=(*replay.source_event_ids, "corroboration"))
    assert next_label_revision(label, enriched, current_revision=3) == 4


@pytest.mark.parametrize("revision", [0, -1, True])
def test_settlement_key_rejects_invalid_revisions(revision: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        settlement_key("scope", "exp", Target.REPLY, revision)  # type: ignore[arg-type]
