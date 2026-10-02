"""Event-time permission projection used by Langchao state and candidates."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable

PERMISSION_EVENT_VERSION = "langchao.permission-event.v1"
PERMISSION_PROJECTION_VERSION = "langchao.permission-projection.v1"


class PermissionVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


def _utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionEvent:
    event_id: str
    scope_key: str
    verdict: PermissionVerdict
    occurred_at: datetime
    ingested_at: datetime
    revision: int = 1
    event_version: str = PERMISSION_EVENT_VERSION

    def __post_init__(self) -> None:
        for name in ("event_id", "scope_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.verdict, PermissionVerdict):
            raise TypeError("verdict must be PermissionVerdict")
        _utc("occurred_at", self.occurred_at)
        _utc("ingested_at", self.ingested_at)
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if self.event_version != PERMISSION_EVENT_VERSION:
            raise ValueError(f"event_version must be {PERMISSION_EVENT_VERSION!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "scope_key": self.scope_key,
                "verdict": self.verdict.value, "occurred_at": self.occurred_at.isoformat(),
                "ingested_at": self.ingested_at.isoformat(), "revision": self.revision,
                "event_version": self.event_version}


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionProjection:
    scope_key: str
    verdict: PermissionVerdict
    permission_version: str
    event_id: str
    occurred_at: datetime
    ingested_at: datetime
    revision: int
    projection_version: str = PERMISSION_PROJECTION_VERSION

    @property
    def allowed(self) -> bool:
        return self.verdict is PermissionVerdict.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {"scope_key": self.scope_key, "verdict": self.verdict.value,
                "allowed": self.allowed, "permission_version": self.permission_version,
                "event_id": self.event_id, "occurred_at": self.occurred_at.isoformat(),
                "ingested_at": self.ingested_at.isoformat(), "revision": self.revision,
                "projection_version": self.projection_version}


def project_current_permission(events: Iterable[PermissionEvent], *, scope_key: str) -> PermissionProjection:
    """Select the temporal successor, independently of event ingestion order.

    ``occurred_at`` is authoritative. ``ingested_at`` then resolves equal event time,
    and revision resolves re-ingestion/correction at both equal timestamps. Event id is
    the final deterministic tie-break, preventing iteration order from becoming policy.
    """
    scoped = tuple(event for event in events if event.scope_key == scope_key)
    if not scoped:
        raise ValueError("permission history has no event for scope")
    current = max(scoped, key=lambda item: (
        item.occurred_at, item.ingested_at, item.revision, item.event_id,
    ))
    identity = json.dumps({
        "scope_key": scope_key, "event_id": current.event_id,
        "verdict": current.verdict.value, "occurred_at": current.occurred_at.isoformat(),
        "ingested_at": current.ingested_at.isoformat(), "revision": current.revision,
        "projection_version": PERMISSION_PROJECTION_VERSION,
    }, sort_keys=True, separators=(",", ":"))
    version = "permission:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return PermissionProjection(scope_key=scope_key, verdict=current.verdict,
        permission_version=version, event_id=current.event_id,
        occurred_at=current.occurred_at, ingested_at=current.ingested_at,
        revision=current.revision)


__all__ = ["PERMISSION_EVENT_VERSION", "PERMISSION_PROJECTION_VERSION", "PermissionEvent",
           "PermissionProjection", "PermissionVerdict", "project_current_permission"]
