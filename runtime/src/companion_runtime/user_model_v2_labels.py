"""Pure target-by-target settlement for user-model v2 observations.

The functions in this module have no clock, database, runtime, or provider dependency.  Event
*time* decides whether evidence belongs to a fixed window; processing time is represented only
by the caller supplied ``as_of``.  Consequently a late-arriving event may produce a new label
revision without changing the original exposure or its window.

``negative=False`` means only that the contract's defined negative event was not observed in a
complete window.  It is deliberately not an assertion that the user was satisfied.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from .user_model_v2_types import (
    InteractionExposureV2,
    LabelStatus,
    Target,
    TargetLabelV2,
)


class ObservationOrigin(str, Enum):
    """Authority from which an observation came."""

    USER = "user"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetObservationV2:
    """One immutable piece of evidence proposed for target settlement.

    ``candidate_exposure_ids`` is the attribution result.  Exactly one candidate is required
    for settlement.  More than one candidate records ambiguity rather than duplicating the
    observation across exposures.  ``explicit`` is mandatory for acceptance and negative
    feedback; reply and continuation may be established by structural conversation events.
    """

    event_id: str
    target: Target
    occurred_at: datetime
    value: bool
    candidate_exposure_ids: tuple[str, ...]
    origin: ObservationOrigin = ObservationOrigin.USER
    explicit: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.target, Target):
            raise TypeError("target must be a Target")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if not isinstance(self.value, bool):
            raise TypeError("value must be a bool")
        if not isinstance(self.candidate_exposure_ids, tuple):
            raise TypeError("candidate_exposure_ids must be a tuple")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.candidate_exposure_ids
        ):
            raise ValueError("candidate_exposure_ids must contain non-empty strings")
        if len(set(self.candidate_exposure_ids)) != len(self.candidate_exposure_ids):
            raise ValueError("candidate_exposure_ids must not contain duplicates")
        if not isinstance(self.origin, ObservationOrigin):
            raise TypeError("origin must be an ObservationOrigin")


@dataclass(frozen=True, slots=True, kw_only=True)
class SettlementContextV2:
    """Caller-known observation coverage at a deterministic processing instant."""

    as_of: datetime
    observation_complete: bool = True
    collected_targets: frozenset[Target] = frozenset(Target)
    exposure_valid: bool = True

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(self.collected_targets, frozenset) or any(
            not isinstance(target, Target) for target in self.collected_targets
        ):
            raise TypeError("collected_targets must be a frozenset of Target values")


def settlement_key(
    scope_key: str,
    exposure_id: str,
    target: Target,
    label_revision: int,
) -> str:
    """Return a stable, delimiter-safe key for one logical label revision."""

    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key must be a non-empty string")
    if not isinstance(exposure_id, str) or not exposure_id.strip():
        raise ValueError("exposure_id must be a non-empty string")
    if not isinstance(target, Target):
        raise TypeError("target must be a Target")
    if (
        isinstance(label_revision, bool)
        or not isinstance(label_revision, int)
        or label_revision < 1
    ):
        raise ValueError("label_revision must be a positive integer")
    payload = "\0".join((scope_key, exposure_id, target.value, str(label_revision)))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"user-model-v2-label:{digest}"


def next_label_revision(
    current: TargetLabelV2 | None,
    candidate: TargetLabelV2,
    *,
    current_revision: int = 0,
) -> int:
    """Return the same revision for an idempotent replay, otherwise the next revision.

    Processing metadata (IDs, ``updated_at`` and source ordering) does not define label meaning.
    The source set is compared so newly discovered evidence can still create an auditable
    revision even when it supports the same value.
    """

    if (
        isinstance(current_revision, bool)
        or not isinstance(current_revision, int)
        or current_revision < 0
    ):
        raise ValueError("current_revision must be a non-negative integer")
    if current is None:
        return max(1, current_revision + 1)
    if current.exposure_id != candidate.exposure_id or current.target is not candidate.target:
        raise ValueError("current and candidate must identify the same exposure target")
    semantic_current = (
        current.status,
        current.value,
        current.observed_at,
        current.window_started_at,
        current.window_ends_at,
        current.horizon_seconds,
        frozenset(current.source_event_ids),
    )
    semantic_candidate = (
        candidate.status,
        candidate.value,
        candidate.observed_at,
        candidate.window_started_at,
        candidate.window_ends_at,
        candidate.horizon_seconds,
        frozenset(candidate.source_event_ids),
    )
    return current_revision if semantic_current == semantic_candidate else current_revision + 1


def _status_without_event(
    target: Target,
    *,
    exposure: InteractionExposureV2,
    context: SettlementContextV2,
    reply_observed: bool,
) -> LabelStatus:
    covered = target in context.collected_targets
    expired = context.as_of >= exposure.window_ends_at

    if not context.observation_complete:
        return LabelStatus.CENSORED
    if not covered:
        return LabelStatus.UNKNOWN if expired else LabelStatus.PENDING
    if not expired:
        return LabelStatus.PENDING
    if target is Target.REPLY:
        return LabelStatus.OBSERVED_NEGATIVE
    if target is Target.NEGATIVE:
        return LabelStatus.OBSERVED_NEGATIVE
    if target is Target.CONTINUE:
        return LabelStatus.OBSERVED_NEGATIVE if reply_observed else LabelStatus.UNKNOWN
    return LabelStatus.UNKNOWN  # acceptance has no implicit negative timeout


def settle_target_label(
    exposure: InteractionExposureV2,
    target: Target,
    observations: Iterable[TargetObservationV2],
    context: SettlementContextV2,
    *,
    label_id: str | None = None,
) -> TargetLabelV2:
    """Settle one target without reading or mutating external state.

    Only user-origin evidence occurring inside the exposure window is eligible.  Acceptance and
    negative feedback additionally require explicit evidence.  A continuation result is emitted
    only when reply was observed and the window/coverage is complete.
    """

    if not isinstance(target, Target):
        raise TypeError("target must be a Target")
    if context.as_of < exposure.occurred_at:
        raise ValueError("as_of must not precede the exposure")

    available = tuple(
        observation
        for observation in observations
        if observation.occurred_at <= context.as_of
    )
    in_window = tuple(
        observation
        for observation in available
        if observation.origin is ObservationOrigin.USER
        and exposure.window_started_at <= observation.occurred_at <= exposure.window_ends_at
    )

    def eligible(observation: TargetObservationV2) -> bool:
        if observation.target is not target:
            return False
        # Reply and negative targets describe occurrence events.  Their false value is
        # produced only by complete-window absence, never by a synthetic event.
        if target in {Target.REPLY, Target.NEGATIVE} and not observation.value:
            return False
        if target in {Target.ACCEPTANCE, Target.NEGATIVE} and not observation.explicit:
            return False
        return True

    relevant = tuple(observation for observation in in_window if eligible(observation))
    direct = tuple(
        observation
        for observation in relevant
        if observation.candidate_exposure_ids == (exposure.exposure_id,)
    )
    ambiguous = tuple(
        observation
        for observation in relevant
        if exposure.exposure_id in observation.candidate_exposure_ids
        and len(observation.candidate_exposure_ids) > 1
    )

    reply_direct = tuple(
        observation
        for observation in in_window
        if observation.target is Target.REPLY
        and observation.value
        and observation.candidate_exposure_ids == (exposure.exposure_id,)
    )
    reply_observed = bool(reply_direct)

    status: LabelStatus
    value: bool | None = None
    observed_at: datetime | None = None
    used: tuple[TargetObservationV2, ...] = ()

    if not context.exposure_valid:
        status = LabelStatus.INVALIDATED
    elif target is Target.CONTINUE and not reply_observed:
        if ambiguous:
            status = LabelStatus.UNATTRIBUTABLE
            used = ambiguous
        else:
            status = _status_without_event(
                target, exposure=exposure, context=context, reply_observed=False
            )
    elif direct and (
        target is not Target.CONTINUE
        or (context.observation_complete and context.as_of >= exposure.window_ends_at)
    ):
        # Conflicting direct evidence is not safely compressible into one binary label.
        values = {observation.value for observation in direct}
        if len(values) != 1:
            status = LabelStatus.UNKNOWN
            used = direct
        else:
            value = values.pop()
            status = LabelStatus.OBSERVED_POSITIVE if value else LabelStatus.OBSERVED_NEGATIVE
            observed_at = min(
                observation.occurred_at
                for observation in direct
                if observation.value == value
            )
            used = direct
    elif ambiguous:
        status = LabelStatus.UNATTRIBUTABLE
        used = ambiguous
    else:
        status = _status_without_event(
            target, exposure=exposure, context=context, reply_observed=reply_observed
        )
        if status is LabelStatus.OBSERVED_NEGATIVE:
            value = False
            observed_at = exposure.window_ends_at
        if target is Target.CONTINUE and reply_observed and not context.observation_complete:
            status = LabelStatus.CENSORED
            value = None
            observed_at = None

    source_ids = tuple(
        dict.fromkeys((*exposure.source_event_ids, *(item.event_id for item in used)))
    )
    return TargetLabelV2(
        label_id=label_id or f"{exposure.exposure_id}:{target.value}",
        exposure_id=exposure.exposure_id,
        scope_key=exposure.scope_key,
        target=target,
        status=status,
        value=value,
        observed_at=observed_at,
        window_started_at=exposure.window_started_at,
        window_ends_at=exposure.window_ends_at,
        horizon_seconds=exposure.horizon_seconds,
        created_at=exposure.created_at,
        updated_at=context.as_of,
        source_event_ids=source_ids,
        contract_version=exposure.contract_version,
        feature_version=exposure.feature_version,
        target_contract_version=exposure.target_contract_version,
    )


def settle_labels(
    exposure: InteractionExposureV2,
    observations: Iterable[TargetObservationV2],
    context: SettlementContextV2,
) -> tuple[TargetLabelV2, ...]:
    """Settle all targets independently in contract enum order."""

    materialized = tuple(observations)
    return tuple(
        settle_target_label(exposure, target, materialized, context) for target in Target
    )


__all__ = [
    "ObservationOrigin",
    "SettlementContextV2",
    "TargetObservationV2",
    "next_label_revision",
    "settle_labels",
    "settle_target_label",
    "settlement_key",
]
