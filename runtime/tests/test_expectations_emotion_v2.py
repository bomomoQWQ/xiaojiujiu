"""TIME-06..14 pure-logic acceptance tests for expectation/emotion v2."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.emotion_v2_interface import (
    EmotionDomainEventV2,
    EmotionEvidenceOriginV2,
    EmotionInputV2,
    expectation_emotion_input,
    role_internal_emotion_input,
)
from companion_runtime.expectations_v2 import (
    OutcomeCompletenessV2,
    SettlementDispositionV2,
    expectation_target_key,
    settle_expectation_target,
)
from companion_runtime.user_model_v2_types import (
    ExpectationV2,
    LabelStatus,
    PredictionEnvelopeV2,
    SupportStatus,
    Target,
    TargetLabelV2,
    TargetPredictionV2,
)

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
END = NOW + timedelta(hours=1)
SCOPE = "user:42/channel:direct"


def prediction(target: Target, point: float = 0.25, *, predicted_at: datetime = NOW):
    return TargetPredictionV2(
        prediction_id=f"pred:{target.value}:{predicted_at.isoformat()}",
        scope_key=SCOPE,
        target=target,
        point=point,
        lower=max(0.0, point - 0.2),
        upper=min(1.0, point + 0.2),
        interval_level=0.9,
        interval_kind="credible",
        support=SupportStatus.SPARSE,
        predicted_at=predicted_at,
        created_at=predicted_at,
        updated_at=predicted_at,
        source_event_ids=(f"fit:{predicted_at.isoformat()}",),
    )


def expectation(*, reply_point: float = 0.25) -> ExpectationV2:
    envelope = PredictionEnvelopeV2(
        envelope_id="env:before",
        scope_key=SCOPE,
        predictions=tuple(
            prediction(target, reply_point if target is Target.REPLY else 0.5)
            for target in Target
        ),
        predicted_at=NOW,
        based_on_state_version=7,
        created_at=NOW,
        updated_at=NOW,
        source_event_ids=("state:7",),
    )
    return ExpectationV2(
        expectation_id="expectation:1",
        exposure_id="exposure:1",
        envelope=envelope,
        scope_key=SCOPE,
        fixed_at=NOW,
        window_started_at=NOW,
        window_ends_at=END,
        horizon_seconds=3600,
        created_at=NOW,
        updated_at=NOW,
        source_event_ids=("delivery:1",),
    )


def label(
    status: LabelStatus,
    *,
    target: Target = Target.REPLY,
    value: bool | None = None,
    observed_at: datetime | None = None,
    source: str = "label-source:1",
) -> TargetLabelV2:
    return TargetLabelV2(
        label_id=f"label:{target.value}",
        exposure_id="exposure:1",
        scope_key=SCOPE,
        target=target,
        status=status,
        value=value,
        observed_at=observed_at,
        window_started_at=NOW,
        window_ends_at=END,
        horizon_seconds=3600,
        created_at=NOW,
        updated_at=observed_at or NOW,
        source_event_ids=(source,),
    )


def test_time_06_settlement_uses_saved_pre_outcome_prediction() -> None:
    saved = expectation(reply_point=0.2)
    observed = label(
        LabelStatus.OBSERVED_POSITIVE,
        value=True,
        observed_at=NOW + timedelta(minutes=10),
    )
    settlement = settle_expectation_target(saved, observed, label_revision=1)

    # A post-result prediction may now be .95, but it is not an input to settlement.
    assert settlement.expected_point == 0.2
    assert settlement.actual_outcome == 1.0
    assert settlement.residual == pytest.approx(0.8)


def test_time_07_and_08_wait_ticks_and_chunking_do_not_repeat_error() -> None:
    saved = expectation()
    pending = label(LabelStatus.PENDING)
    first = settle_expectation_target(saved, pending, label_revision=1)
    second = settle_expectation_target(saved, pending, label_revision=1, previous=first)

    assert first.residual is None
    assert second.disposition is SettlementDispositionV2.REPLAY
    assert second.settlement_key == first.settlement_key

    complete = label(LabelStatus.OBSERVED_NEGATIVE, value=False, observed_at=END)
    one_hour = settle_expectation_target(saved, complete, label_revision=2, previous=first)
    chunked = settle_expectation_target(saved, complete, label_revision=2, previous=one_hour)
    assert one_hour.disposition is SettlementDispositionV2.CORRECTION
    assert one_hour.residual == -0.25
    assert chunked.disposition is SettlementDispositionV2.REPLAY
    assert chunked.revision_key == one_hour.revision_key


def test_time_09_and_10_desire_and_wait_burden_cannot_create_user_fact() -> None:
    # Neither role desire nor waiting burden is accepted by the evidence-only contracts.
    settlement_names = {item.name for item in fields(type(settle_expectation_target(
        expectation(), label(LabelStatus.PENDING), label_revision=1
    )))}
    emotion_names = {item.name for item in fields(EmotionInputV2)}
    forbidden = {"desire", "promise", "user_rejects", "sadness_delta", "waiting_burden"}
    assert forbidden.isdisjoint(settlement_names)
    assert forbidden.isdisjoint(emotion_names)

    recovery = role_internal_emotion_input(
        event_key="wait:recovery:1",
        domain_event=EmotionDomainEventV2.TIME_OR_ATTENTION_RECOVERY,
        source_event_ids=("clock:1",),
    )
    assert recovery.origin is EmotionEvidenceOriginV2.ROLE_INTERNAL
    assert recovery.prior_expectation is None


def test_time_11_intervention_is_explicit_not_unrelated_censoring() -> None:
    item = settle_expectation_target(
        expectation(),
        label(LabelStatus.PENDING),
        label_revision=1,
        intervention_source_event_ids=("assistant-send:follow-up",),
    )
    assert item.completeness is OutcomeCompletenessV2.INTERVENED
    assert item.residual is None
    assert item.intervention_source_event_ids == ("assistant-send:follow-up",)


def test_time_12_internal_emotion_events_cannot_become_user_outcomes() -> None:
    send = role_internal_emotion_input(
        event_key="send:1",
        domain_event=EmotionDomainEventV2.SEND_RELIEF,
        source_event_ids=("assistant-send:1",),
    )
    assert send.shadow is True
    assert send.origin is EmotionEvidenceOriginV2.ROLE_INTERNAL

    with pytest.raises(ValueError, match="limited"):
        role_internal_emotion_input(
            event_key="bad:1",
            domain_event=EmotionDomainEventV2.USER_RESPONSE,
            source_event_ids=("internal-thought:1",),
        )


def test_four_domain_events_are_distinct_and_matter_resolution_is_supported() -> None:
    assert set(EmotionDomainEventV2) == {
        EmotionDomainEventV2.SEND_RELIEF,
        EmotionDomainEventV2.USER_RESPONSE,
        EmotionDomainEventV2.MATTER_RESOLUTION,
        EmotionDomainEventV2.TIME_OR_ATTENTION_RECOVERY,
    }
    matter = EmotionInputV2(
        event_key="matter:1",
        domain_event=EmotionDomainEventV2.MATTER_RESOLUTION,
        prior_expectation=None,
        actual_outcome=1.0,
        completeness=OutcomeCompletenessV2.OBSERVED,
        source_event_ids=("user-update:1",),
        support=SupportStatus.INFORMATIVE,
        origin=EmotionEvidenceOriginV2.USER_OR_WORLD,
    )
    assert matter.shadow is True


def test_time_13_unknown_is_not_neutral_or_zero() -> None:
    item = settle_expectation_target(
        expectation(), label(LabelStatus.UNKNOWN), label_revision=1
    )
    payload = expectation_emotion_input(item)
    assert payload.completeness is OutcomeCompletenessV2.UNKNOWN
    assert payload.actual_outcome is None
    assert item.residual is None


def test_unavailable_prediction_preserves_observed_negative_without_residual() -> None:
    saved = expectation()
    unavailable = replace(
        next(item for item in saved.envelope.predictions if item.target is Target.NEGATIVE),
        point=None,
        lower=None,
        upper=None,
        interval_level=None,
        interval_kind=None,
        support=SupportStatus.UNAVAILABLE,
    )
    saved = replace(
        saved,
        envelope=replace(
            saved.envelope,
            predictions=tuple(
                unavailable if item.target is Target.NEGATIVE else item
                for item in saved.envelope.predictions
            ),
        ),
    )
    negative = label(
        LabelStatus.OBSERVED_NEGATIVE,
        target=Target.NEGATIVE,
        value=False,
        observed_at=END,
    )
    settlement = settle_expectation_target(saved, negative, label_revision=1)
    assert settlement.actual_outcome == 0.0
    assert settlement.expected_point is None
    assert settlement.residual is None
    assert settlement.emits_prediction_error is False


def test_time_14_repeated_interpretation_does_not_add_independent_sample() -> None:
    saved = expectation()
    observed = label(
        LabelStatus.OBSERVED_POSITIVE,
        value=True,
        observed_at=NOW + timedelta(minutes=5),
        source="reply:1",
    )
    first = settle_expectation_target(saved, observed, label_revision=1)
    replay = settle_expectation_target(saved, observed, label_revision=1, previous=first)
    assert replay.disposition is SettlementDispositionV2.REPLAY
    assert replay.settlement_key == expectation_target_key(saved, Target.REPLY)

    revised = replace(observed, source_event_ids=("reply:1", "interpretation:v2"))
    correction = settle_expectation_target(saved, revised, label_revision=2, previous=first)
    assert correction.disposition is SettlementDispositionV2.CORRECTION
    assert correction.settlement_key == first.settlement_key
    assert correction.supersedes_revision_key == first.revision_key

    emotion = expectation_emotion_input(correction)
    assert emotion.supersedes_event_key == first.revision_key
    assert emotion.shadow is True
