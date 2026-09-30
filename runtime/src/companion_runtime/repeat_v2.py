"""Isolated v2 repeat policy based only on acknowledged send exposures.

The policy deliberately has no Runtime, scheduler, candidate-pool, or Jev integration.  Its
history contract represents actions which the platform accepted (and which therefore may
have been seen).  Merely constructing, rendering, or evaluating a candidate cannot enter
that history and cannot increase either repeat cost.

All persisted/event timestamps are explicitly UTC.  Windows and limits are supplied by
:class:`RepeatPolicyConfigV2`; in particular, no 48-hour policy is hidden in the code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Sequence

REPEAT_POLICY_VERSION = "repeat-v2.0"


class UserMatterEventKind(str, Enum):
    """User-originated changes that can reset a same-matter repetition run."""

    PROGRESS = "progress"
    REOPEN = "reopen"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _optional_text(name: str, value: str | None) -> None:
    if value is not None:
        _require_text(name, value)


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC (offset +00:00)")


@dataclass(frozen=True, slots=True)
class SendAcknowledgedExposureV2:
    """One idempotent exposure created only after a send acknowledgement.

    ``acknowledged_at_utc`` is the ordering/window timestamp.  There is intentionally no
    candidate-evaluation constructor or counter in this module.
    """

    exposure_id: str
    acknowledged_at_utc: datetime
    concern_id: str | None = None
    action_goal_id: str | None = None

    def __post_init__(self) -> None:
        _require_text("exposure_id", self.exposure_id)
        _require_utc("acknowledged_at_utc", self.acknowledged_at_utc)
        _optional_text("concern_id", self.concern_id)
        _optional_text("action_goal_id", self.action_goal_id)


@dataclass(frozen=True, slots=True)
class UserMatterEventV2:
    """New user progress, or an explicit user-originated reopening of a matter."""

    event_id: str
    occurred_at_utc: datetime
    kind: UserMatterEventKind
    concern_id: str | None = None
    action_goal_id: str | None = None

    def __post_init__(self) -> None:
        _require_text("event_id", self.event_id)
        _require_utc("occurred_at_utc", self.occurred_at_utc)
        if not isinstance(self.kind, UserMatterEventKind):
            raise TypeError("kind must be a UserMatterEventKind")
        _optional_text("concern_id", self.concern_id)
        _optional_text("action_goal_id", self.action_goal_id)
        if self.concern_id is None and self.action_goal_id is None:
            raise ValueError("a user matter event needs concern_id or action_goal_id")


@dataclass(frozen=True, slots=True)
class RepeatSubjectV2:
    """Identity of the action currently being considered (not a history record)."""

    concern_id: str | None = None
    action_goal_id: str | None = None

    def __post_init__(self) -> None:
        _optional_text("concern_id", self.concern_id)
        _optional_text("action_goal_id", self.action_goal_id)


@dataclass(frozen=True, slots=True)
class RepeatPolicyConfigV2:
    """Fully testable policy configuration; durations are never implicit constants."""

    contact_window: timedelta = timedelta(hours=6)
    matter_window: timedelta = timedelta(hours=24)
    contact_allowance: int = 1
    matter_repeat_allowance: int = 1
    contact_cost_per_excess: float = 0.45
    matter_cost_per_excess: float = 0.75
    hard_contact_limit: int | None = 4
    hard_matter_repeat_limit: int | None = 3

    def __post_init__(self) -> None:
        for name in ("contact_window", "matter_window"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
        for name in ("contact_allowance", "matter_repeat_allowance"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("hard_contact_limit", "hard_matter_repeat_limit"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ValueError(f"{name} must be None or a positive integer")
        for name in ("contact_cost_per_excess", "matter_cost_per_excess"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class RepeatCostBreakdownV2:
    """Auditable component costs plus non-overridable limit reasons."""

    policy_version: str
    evaluated_at_utc: datetime
    recent_contact_count: int
    same_matter_no_progress_count: int
    short_contact_cost: float
    same_matter_cost: float
    total_cost: float
    hard_limit_reasons: tuple[str, ...]
    latest_reset_kind: UserMatterEventKind | None = None
    latest_reset_at_utc: datetime | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.hard_limit_reasons)


def _same_matter(
    *,
    left_concern_id: str | None,
    left_action_goal_id: str | None,
    right_concern_id: str | None,
    right_action_goal_id: str | None,
) -> bool:
    return bool(
        (left_concern_id is not None and left_concern_id == right_concern_id)
        or (left_action_goal_id is not None and left_action_goal_id == right_action_goal_id)
    )


def _deduplicate_exposures(
    history: Sequence[SendAcknowledgedExposureV2],
) -> tuple[SendAcknowledgedExposureV2, ...]:
    by_id: dict[str, SendAcknowledgedExposureV2] = {}
    for item in history:
        # Runtime checks are intentional: a candidate-evaluation object or an unacknowledged
        # attempt must fail closed instead of being duck-typed into a contact.
        if not isinstance(item, SendAcknowledgedExposureV2):
            raise TypeError("history accepts only SendAcknowledgedExposureV2 records")
        previous = by_id.get(item.exposure_id)
        if previous is not None and previous != item:
            raise ValueError(f"conflicting duplicate exposure_id: {item.exposure_id}")
        by_id[item.exposure_id] = item
    return tuple(by_id.values())


def evaluate_repeat_v2(
    *,
    subject: RepeatSubjectV2,
    exposure_history: Sequence[SendAcknowledgedExposureV2],
    now_utc: datetime,
    config: RepeatPolicyConfigV2,
    user_matter_events: Iterable[UserMatterEventV2] = (),
) -> RepeatCostBreakdownV2:
    """Evaluate prospective repeat pressure without mutating or recording a send.

    Window boundaries are inclusive.  Future records are rejected instead of silently
    producing negative ages.  A matching user ``progress`` or ``reopen`` event resets the
    same-matter run preceding it; it does not erase the global short-contact cost because
    that cost accounts for total contact load rather than topic staleness.
    """

    if not isinstance(subject, RepeatSubjectV2):
        raise TypeError("subject must be RepeatSubjectV2")
    if not isinstance(config, RepeatPolicyConfigV2):
        raise TypeError("config must be RepeatPolicyConfigV2")
    _require_utc("now_utc", now_utc)

    exposures = _deduplicate_exposures(exposure_history)
    for item in exposures:
        if item.acknowledged_at_utc > now_utc:
            raise ValueError("exposure history cannot contain future acknowledgements")

    events: list[UserMatterEventV2] = []
    seen_event_ids: set[str] = set()
    for event in user_matter_events:
        if not isinstance(event, UserMatterEventV2):
            raise TypeError("user_matter_events accepts only UserMatterEventV2 records")
        if event.occurred_at_utc > now_utc:
            raise ValueError("user matter events cannot occur in the future")
        if event.event_id not in seen_event_ids and _same_matter(
            left_concern_id=subject.concern_id,
            left_action_goal_id=subject.action_goal_id,
            right_concern_id=event.concern_id,
            right_action_goal_id=event.action_goal_id,
        ):
            events.append(event)
            seen_event_ids.add(event.event_id)

    latest_reset = max(events, key=lambda item: item.occurred_at_utc, default=None)
    contact_cutoff = now_utc - config.contact_window
    matter_cutoff = now_utc - config.matter_window

    recent_contacts = sum(
        item.acknowledged_at_utc >= contact_cutoff for item in exposures
    )
    matching = [
        item
        for item in exposures
        if item.acknowledged_at_utc >= matter_cutoff
        and _same_matter(
            left_concern_id=subject.concern_id,
            left_action_goal_id=subject.action_goal_id,
            right_concern_id=item.concern_id,
            right_action_goal_id=item.action_goal_id,
        )
        and (latest_reset is None or item.acknowledged_at_utc > latest_reset.occurred_at_utc)
    ]
    same_matter_count = len(matching)

    # Costs concern the prospective send: N prior contacts means this would be contact N+1.
    contact_excess = max(0, recent_contacts + 1 - config.contact_allowance)
    matter_excess = max(0, same_matter_count + 1 - config.matter_repeat_allowance)
    contact_cost = float(config.contact_cost_per_excess) * contact_excess
    matter_cost = float(config.matter_cost_per_excess) * matter_excess

    reasons: list[str] = []
    if (
        config.hard_contact_limit is not None
        and recent_contacts + 1 > config.hard_contact_limit
    ):
        reasons.append("short_contact_limit_exceeded")
    if (
        config.hard_matter_repeat_limit is not None
        and same_matter_count + 1 > config.hard_matter_repeat_limit
    ):
        reasons.append("same_matter_no_progress_limit_exceeded")

    return RepeatCostBreakdownV2(
        policy_version=REPEAT_POLICY_VERSION,
        evaluated_at_utc=now_utc,
        recent_contact_count=recent_contacts,
        same_matter_no_progress_count=same_matter_count,
        short_contact_cost=contact_cost,
        same_matter_cost=matter_cost,
        total_cost=contact_cost + matter_cost,
        hard_limit_reasons=tuple(reasons),
        latest_reset_kind=None if latest_reset is None else latest_reset.kind,
        latest_reset_at_utc=None if latest_reset is None else latest_reset.occurred_at_utc,
    )


# A concise public alias for callers that already live in a v2-only namespace.
evaluate_repeat = evaluate_repeat_v2


__all__ = [
    "REPEAT_POLICY_VERSION",
    "RepeatCostBreakdownV2",
    "RepeatPolicyConfigV2",
    "RepeatSubjectV2",
    "SendAcknowledgedExposureV2",
    "UserMatterEventKind",
    "UserMatterEventV2",
    "evaluate_repeat",
    "evaluate_repeat_v2",
]
