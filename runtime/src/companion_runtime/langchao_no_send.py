"""Unified machine-readable reasons for a Langchao round that did not send.

The result is deliberately shared by adapter, numerical engine, permission gate and
live delivery.  ``stage`` and identifiers provide context, while ``reason`` remains a
closed taxonomy suitable for audit aggregation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


NO_SEND_RESULT_VERSION = "langchao.no-send-result.v1"


class NoSendReason(str, Enum):
    NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"
    DECISION_BUDGET_EXHAUSTED = "decision_budget_exhausted"
    COMPETITION_STALEMATE = "competition_stalemate"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_REVOKED = "permission_revoked"
    INVALIDATED = "invalidated"
    DISPATCH_FAILED = "dispatch_failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class NoSendResult:
    reason: NoSendReason
    stage: str
    round_id: str | None = None
    candidate_id: str | None = None
    permission_version: str | None = None
    details: tuple[tuple[str, str], ...] = ()
    result_version: str = NO_SEND_RESULT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.reason, NoSendReason):
            raise TypeError("reason must be NoSendReason")
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("stage must be a non-empty string")
        for name in ("round_id", "candidate_id", "permission_version"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        if not isinstance(self.details, tuple):
            raise TypeError("details must be a tuple")
        if any(not isinstance(item, tuple) or len(item) != 2 or not all(isinstance(v, str) for v in item)
               for item in self.details):
            raise TypeError("details must contain string pairs")
        if self.result_version != NO_SEND_RESULT_VERSION:
            raise ValueError(f"result_version must be {NO_SEND_RESULT_VERSION!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "stage": self.stage,
            "round_id": self.round_id,
            "candidate_id": self.candidate_id,
            "permission_version": self.permission_version,
            "details": dict(self.details),
            "result_version": self.result_version,
        }

    @classmethod
    def from_reason(cls, reason: str | NoSendReason, *, stage: str, **kwargs: Any) -> "NoSendResult":
        return cls(reason=reason if isinstance(reason, NoSendReason) else NoSendReason(reason),
                   stage=stage, **kwargs)


__all__ = ["NO_SEND_RESULT_VERSION", "NoSendReason", "NoSendResult"]
