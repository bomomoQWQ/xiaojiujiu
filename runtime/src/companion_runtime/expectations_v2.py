"""Pure expectation and wait-result settlement for user-model v2.

This module deliberately has no clock, storage, scheduler, Jev, legacy emotion, or runtime
coupling.  Callers pass an expectation fixed before the outcome and the currently active label
revision.  A logical target has one stable key; a later active label revision produces a
superseding correction rather than a second independent outcome.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from .user_model_v2_types import (
    ExpectationV2,
    LabelStatus,
    SupportStatus,
    Target,
    TargetLabelV2,
)


class OutcomeCompletenessV2(str, Enum):
    """Whether a discrete outcome is available for expectation-error settlement."""

    OBSERVED = "observed"
    PENDING = "pending"
    CENSORED = "censored"
    UNKNOWN = "unknown"
    INTERVENED = "intervened"


class SettlementDispositionV2(str, Enum):
    """How this record relates to an earlier record for the same logical target."""

    INITIAL = "initial"
    REPLAY = "replay"
    CORRECTION = "correction"


_NON_OBSERVED_COMPLETENESS = {
    LabelStatus.PENDING: OutcomeCompletenessV2.PENDING,
    LabelStatus.CENSORED: OutcomeCompletenessV2.CENSORED,
    LabelStatus.UNKNOWN: OutcomeCompletenessV2.UNKNOWN,
    LabelStatus.UNATTRIBUTABLE: OutcomeCompletenessV2.UNKNOWN,
    LabelStatus.INVALIDATED: OutcomeCompletenessV2.UNKNOWN,
}


def _stable_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def expectation_target_key(expectation: ExpectationV2, target: Target) -> str:
    """Return the stable logical key settled at most once for one expectation target."""

    if not isinstance(expectation, ExpectationV2):
        raise TypeError("expectation must be an ExpectationV2")
    if not isinstance(target, Target):
        raise TypeError("target must be a Target")
    digest = _stable_digest(expectation.scope_key, expectation.expectation_id, target.value)
    return f"expectation-v2-target:{digest}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectationSettlementV2:
    """Auditable comparison between a pre-outcome prediction and one active label revision."""

    settlement_key: str
    revision_key: str
    expectation_id: str
    exposure_id: str
    target: Target
    label_id: str
    label_revision: int
    expected_point: float | None
    actual_outcome: float | None
    completeness: OutcomeCompletenessV2
    residual: float | None
    support: SupportStatus
    source_event_ids: tuple[str, ...]
    disposition: SettlementDispositionV2 = SettlementDispositionV2.INITIAL
    supersedes_revision_key: str | None = None
    intervention_source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("settlement_key", "revision_key", "expectation_id", "exposure_id", "label_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.target, Target):
            raise TypeError("target must be a Target")
        if isinstance(self.label_revision, bool) or not isinstance(self.label_revision, int) or self.label_revision < 1:
            raise ValueError("label_revision must be a positive integer")
        if not isinstance(self.completeness, OutcomeCompletenessV2):
            raise TypeError("completeness must be an OutcomeCompletenessV2")
        if not isinstance(self.support, SupportStatus):
            raise TypeError("support must be a SupportStatus")
        if not isinstance(self.disposition, SettlementDispositionV2):
            raise TypeError("disposition must be a SettlementDispositionV2")
        if not isinstance(self.intervention_source_event_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.intervention_source_event_ids
        ):
            raise ValueError("intervention_source_event_ids must contain non-empty strings")
        if len(set(self.intervention_source_event_ids)) != len(self.intervention_source_event_ids):
            raise ValueError("intervention_source_event_ids must not contain duplicates")
        if self.completeness is OutcomeCompletenessV2.INTERVENED:
            if not self.intervention_source_event_ids:
                raise ValueError("intervened settlements require an intervention source")
        elif self.intervention_source_event_ids:
            raise ValueError("intervention sources require intervened completeness")
        if self.expected_point is not None and not 0.0 <= self.expected_point <= 1.0:
            raise ValueError("expected_point must be None or in [0, 1]")
        if self.completeness is OutcomeCompletenessV2.OBSERVED:
            if self.actual_outcome is None:
                raise ValueError("observed settlements require actual_outcome")
            if self.expected_point is None and self.residual is not None:
                raise ValueError("unavailable predictions cannot emit a residual")
            if self.expected_point is not None and self.residual is None:
                raise ValueError("available observed predictions require a residual")
        elif self.actual_outcome is not None or self.residual is not None:
            raise ValueError("non-observed settlements cannot carry an outcome or residual")
        if self.disposition is SettlementDispositionV2.CORRECTION:
            if not self.supersedes_revision_key:
                raise ValueError("a correction must identify the superseded revision")
        elif self.supersedes_revision_key is not None:
            raise ValueError("only a correction may identify a superseded revision")

    @property
    def emits_prediction_error(self) -> bool:
        return self.residual is not None


def _prediction_for(expectation: ExpectationV2, target: Target):
    return next(item for item in expectation.envelope.predictions if item.target is target)


def _semantic_result(record: ExpectationSettlementV2) -> tuple[object, ...]:
    return (
        record.label_id,
        record.label_revision,
        record.expected_point,
        record.actual_outcome,
        record.completeness,
        record.residual,
        record.support,
        frozenset(record.source_event_ids),
    )


def settle_expectation_target(
    expectation: ExpectationV2,
    active_label: TargetLabelV2,
    *,
    label_revision: int,
    previous: ExpectationSettlementV2 | None = None,
    intervention_source_event_ids: tuple[str, ...] = (),
) -> ExpectationSettlementV2:
    """Compare one active target label with its saved, pre-outcome prediction.

    Pending, censored, unknown, unattributable, and invalidated labels are represented without a
    residual.  Replaying the same active revision is idempotent.  A changed revision retains the
    stable target key and explicitly supersedes the previous revision record.
    """

    if not isinstance(expectation, ExpectationV2):
        raise TypeError("expectation must be an ExpectationV2")
    if not isinstance(active_label, TargetLabelV2):
        raise TypeError("active_label must be a TargetLabelV2")
    if isinstance(label_revision, bool) or not isinstance(label_revision, int) or label_revision < 1:
        raise ValueError("label_revision must be a positive integer")
    if active_label.exposure_id != expectation.exposure_id:
        raise ValueError("label and expectation must identify the same exposure")
    if active_label.scope_key != expectation.scope_key:
        raise ValueError("label and expectation must use the same scope_key")

    prediction = _prediction_for(expectation, active_label.target)
    if prediction.predicted_at > expectation.fixed_at:
        raise ValueError("settlement requires a prediction saved before the expectation was fixed")

    if not isinstance(intervention_source_event_ids, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in intervention_source_event_ids
    ):
        raise ValueError("intervention_source_event_ids must contain non-empty strings")
    if len(set(intervention_source_event_ids)) != len(intervention_source_event_ids):
        raise ValueError("intervention_source_event_ids must not contain duplicates")

    if intervention_source_event_ids:
        completeness = OutcomeCompletenessV2.INTERVENED
        actual = None
        residual = None
    elif active_label.status in {LabelStatus.OBSERVED_POSITIVE, LabelStatus.OBSERVED_NEGATIVE}:
        completeness = OutcomeCompletenessV2.OBSERVED
        actual = float(active_label.value)  # validated by TargetLabelV2
        residual = None if prediction.point is None else actual - float(prediction.point)
    else:
        completeness = _NON_OBSERVED_COMPLETENESS[active_label.status]
        actual = None
        residual = None

    stable_key = expectation_target_key(expectation, active_label.target)
    revision_key = f"expectation-v2-revision:{_stable_digest(stable_key, str(label_revision))}"
    sources = tuple(
        dict.fromkeys(
            (
                *expectation.source_event_ids,
                *expectation.envelope.source_event_ids,
                *prediction.source_event_ids,
                *active_label.source_event_ids,
                *intervention_source_event_ids,
            )
        )
    )
    candidate = ExpectationSettlementV2(
        settlement_key=stable_key,
        revision_key=revision_key,
        expectation_id=expectation.expectation_id,
        exposure_id=expectation.exposure_id,
        target=active_label.target,
        label_id=active_label.label_id,
        label_revision=label_revision,
        expected_point=None if prediction.point is None else float(prediction.point),
        actual_outcome=actual,
        completeness=completeness,
        residual=residual,
        support=prediction.support,
        source_event_ids=sources,
        intervention_source_event_ids=intervention_source_event_ids,
    )
    if previous is None:
        return candidate
    if previous.settlement_key != stable_key:
        raise ValueError("previous must identify the same expectation target")
    if label_revision < previous.label_revision:
        raise ValueError("an active label revision cannot move backwards")
    if _semantic_result(previous) == _semantic_result(candidate):
        return ExpectationSettlementV2(
            **{
                name: getattr(candidate, name)
                for name in candidate.__dataclass_fields__
                if name not in {"disposition", "supersedes_revision_key"}
            },
            disposition=SettlementDispositionV2.REPLAY,
        )
    if label_revision == previous.label_revision:
        raise ValueError("changed label meaning requires a newer label_revision")
    return ExpectationSettlementV2(
        **{
            name: getattr(candidate, name)
            for name in candidate.__dataclass_fields__
            if name not in {"disposition", "supersedes_revision_key"}
        },
        disposition=SettlementDispositionV2.CORRECTION,
        supersedes_revision_key=previous.revision_key,
    )


__all__ = [
    "ExpectationSettlementV2",
    "OutcomeCompletenessV2",
    "SettlementDispositionV2",
    "expectation_target_key",
    "settle_expectation_target",
]
