"""Narrow, shadow-by-default input contract for a future six-state emotion engine.

The interface transports evidence, not an emotion appraisal or a user fact.  In particular it
contains no ``sadness +=`` style delta and no ``user_rejects`` conclusion.  Role-internal events
can describe relief or recovery, but cannot be converted into user-model labels here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .expectations_v2 import ExpectationSettlementV2, OutcomeCompletenessV2
from .user_model_v2_types import SupportStatus


class EmotionDomainEventV2(str, Enum):
    """Distinct causes which the emotion dynamics must not conflate."""

    SEND_RELIEF = "send_relief"
    USER_RESPONSE = "user_response"
    MATTER_RESOLUTION = "matter_resolution"
    TIME_OR_ATTENTION_RECOVERY = "time_or_attention_recovery"


class EmotionEvidenceOriginV2(str, Enum):
    """Whether evidence describes the user/world or only the role's internal process."""

    USER_OR_WORLD = "user_or_world"
    ROLE_INTERNAL = "role_internal"


@dataclass(frozen=True, slots=True, kw_only=True)
class EmotionInputV2:
    """Evidence-only payload consumable by six-state dynamics.

    ``prior_expectation`` and ``actual_outcome`` remain separate.  A consumer may derive an
    appraisal, but this boundary never emits a final emotion delta or a user-rejection fact.
    """

    event_key: str
    domain_event: EmotionDomainEventV2
    prior_expectation: float | None
    actual_outcome: float | None
    completeness: OutcomeCompletenessV2
    source_event_ids: tuple[str, ...]
    support: SupportStatus
    origin: EmotionEvidenceOriginV2
    shadow: bool = True
    supersedes_event_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_key, str) or not self.event_key.strip():
            raise ValueError("event_key must be a non-empty string")
        if not isinstance(self.domain_event, EmotionDomainEventV2):
            raise TypeError("domain_event must be an EmotionDomainEventV2")
        if not isinstance(self.completeness, OutcomeCompletenessV2):
            raise TypeError("completeness must be an OutcomeCompletenessV2")
        if not isinstance(self.support, SupportStatus):
            raise TypeError("support must be a SupportStatus")
        if not isinstance(self.origin, EmotionEvidenceOriginV2):
            raise TypeError("origin must be an EmotionEvidenceOriginV2")
        if not isinstance(self.shadow, bool):
            raise TypeError("shadow must be a bool")
        if not isinstance(self.source_event_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.source_event_ids
        ):
            raise ValueError("source_event_ids must be a tuple of non-empty strings")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids must not contain duplicates")
        for name in ("prior_expectation", "actual_outcome"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be None or a number in [0, 1]")
        if self.completeness is OutcomeCompletenessV2.OBSERVED:
            if self.actual_outcome is None:
                raise ValueError("an observed input requires actual_outcome")
        elif self.actual_outcome is not None:
            raise ValueError("an incomplete input cannot carry actual_outcome")
        if self.origin is EmotionEvidenceOriginV2.ROLE_INTERNAL and self.domain_event in {
            EmotionDomainEventV2.USER_RESPONSE,
            EmotionDomainEventV2.MATTER_RESOLUTION,
        }:
            raise ValueError("role-internal evidence cannot impersonate a user/world outcome")
        if self.supersedes_event_key is not None and (
            not isinstance(self.supersedes_event_key, str) or not self.supersedes_event_key.strip()
        ):
            raise ValueError("supersedes_event_key must be None or a non-empty string")


def expectation_emotion_input(
    settlement: ExpectationSettlementV2,
    *,
    shadow: bool = True,
) -> EmotionInputV2:
    """Adapt a settled response expectation without inventing an appraisal."""

    if not isinstance(settlement, ExpectationSettlementV2):
        raise TypeError("settlement must be an ExpectationSettlementV2")
    return EmotionInputV2(
        event_key=settlement.revision_key,
        domain_event=EmotionDomainEventV2.USER_RESPONSE,
        prior_expectation=settlement.expected_point,
        actual_outcome=settlement.actual_outcome,
        completeness=settlement.completeness,
        source_event_ids=settlement.source_event_ids,
        support=settlement.support,
        origin=EmotionEvidenceOriginV2.USER_OR_WORLD,
        shadow=shadow,
        supersedes_event_key=settlement.supersedes_revision_key,
    )


def role_internal_emotion_input(
    *,
    event_key: str,
    domain_event: EmotionDomainEventV2,
    source_event_ids: tuple[str, ...],
    completeness: OutcomeCompletenessV2 = OutcomeCompletenessV2.OBSERVED,
    actual_outcome: float | None = 1.0,
    support: SupportStatus = SupportStatus.INFORMATIVE,
    shadow: bool = True,
) -> EmotionInputV2:
    """Create send-relief or autonomous recovery evidence, never a user label."""

    if domain_event not in {
        EmotionDomainEventV2.SEND_RELIEF,
        EmotionDomainEventV2.TIME_OR_ATTENTION_RECOVERY,
    }:
        raise ValueError("role-internal inputs are limited to send relief or time/attention recovery")
    return EmotionInputV2(
        event_key=event_key,
        domain_event=domain_event,
        prior_expectation=None,
        actual_outcome=actual_outcome,
        completeness=completeness,
        source_event_ids=source_event_ids,
        support=support,
        origin=EmotionEvidenceOriginV2.ROLE_INTERNAL,
        shadow=shadow,
    )


__all__ = [
    "EmotionDomainEventV2",
    "EmotionEvidenceOriginV2",
    "EmotionInputV2",
    "expectation_emotion_input",
    "role_internal_emotion_input",
]
