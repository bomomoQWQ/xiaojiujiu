"""Versioned wire protocol between the thin AstrBot adapter and the Runtime.

This module is pure Python and must never import AstrBot, so the protocol can be
unit tested and reused without a running AstrBot instance.

Direction of travel:

* adapter -> Runtime: event reports, context fetches, lease requests,
  lease heartbeats, send authorizations, action results.
* Runtime -> adapter: the current prompt context, and leased actions
  (``render`` / ``send``).

Design rules encoded here:

* The Runtime is the single writer of cognition. The adapter never interprets
  psychological state, it only transports it.
* ``committed`` is not ``sent``: an action result always states explicitly what
  actually happened on the host side.
* Every request carries ``protocol_version`` so the Runtime can reject or adapt.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .coerce import as_bool, as_int, as_str

PROTOCOL_VERSION = "1"

#: Event kinds the adapter appends to the Runtime's immutable event log.
EVENT_USER_MESSAGE = "user_message"
EVENT_ASSISTANT_MESSAGE = "assistant_message"

#: Action types the adapter is able to execute.
ACTION_RENDER = "render"
ACTION_SEND = "send"
SUPPORTED_ACTION_TYPES: tuple[str, ...] = (ACTION_RENDER, ACTION_SEND)

#: Result statuses reported for a leased action.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"

#: Why a context snapshot was requested.
TRIGGER_LLM_REQUEST = "llm_request"
TRIGGER_MESSAGE = "message"

#: Length cap for error strings that travel to the Runtime or into a log line.
ERROR_CHAR_LIMIT = 400

#: Length cap for the text preview attached to a send authorization request.
PREVIEW_CHAR_LIMIT = 160


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def new_client_id(prefix: str) -> str:
    """Return a fresh client-side identifier such as ``evt_3f2a...``."""
    return f"{prefix}_{uuid.uuid4().hex}"


def truncate_error(error: BaseException | str, limit: int = ERROR_CHAR_LIMIT) -> str:
    """Return a log/report safe one-line description of an error.

    Args:
        error: The exception (or plain string) to describe.
        limit: Maximum length of the returned string.

    Returns:
        ``"ExceptionClass: message"`` collapsed to a single line and truncated.
        Tracebacks are never included: the Runtime does not need them and
        request bodies must not leak into reports.
    """
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


@dataclass
class EventRecord:
    """One observation appended to the Runtime's immutable event log."""

    kind: str
    session: str
    text: str = ""
    event_id: str = ""
    occurred_at: str = ""
    platform: str = ""
    message_type: str = ""
    sender_id: str = ""
    sender_name: str = ""
    self_id: str = ""
    group_id: str = ""
    message_id: str = ""
    wake: bool = False
    preempts_proactive: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = new_client_id("evt")
        if not self.occurred_at:
            self.occurred_at = utc_now_iso()

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-serializable representation sent to the Runtime."""
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "kind": self.kind,
            "session": self.session,
            "text": self.text,
            "occurred_at": self.occurred_at,
            "platform": self.platform,
            "message_type": self.message_type,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "self_id": self.self_id,
            "group_id": self.group_id,
            "message_id": self.message_id,
            "wake": self.wake,
            "preempts_proactive": self.preempts_proactive,
        }
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


@dataclass
class EventEnvelope:
    """A batch of event records posted to ``POST /v1/events``."""

    adapter_id: str
    events: list[EventRecord] = field(default_factory=list)
    protocol_version: str = PROTOCOL_VERSION

    def to_wire(self) -> dict[str, Any]:
        """Return the request body for the events endpoint."""
        return {
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "sent_at": utc_now_iso(),
            "events": [event.to_wire() for event in self.events],
        }


@dataclass
class ContextRequest:
    """Request for the Runtime's current prompt context (``POST /v1/context``).

    ``last_event_id`` tells the Runtime where this adapter's view of the session
    currently is, so a context built for a newer event is never applied to an
    older request.
    """

    adapter_id: str
    session: str
    trigger: str = TRIGGER_LLM_REQUEST
    platform: str = ""
    last_event_id: str | None = None
    protocol_version: str = PROTOCOL_VERSION

    def to_wire(self) -> dict[str, Any]:
        """Return the request body for the context endpoint."""
        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "session": self.session,
            "trigger": self.trigger,
            "platform": self.platform,
        }
        if self.last_event_id:
            payload["last_event_id"] = self.last_event_id
        return payload


@dataclass
class ContextSnapshot:
    """The Runtime's current context for one session.

    ``text`` is the Runtime's pre-composed injection block (preferred).
    ``sections`` is an optional structured alternative following the documented
    ``【段落】`` layout; the adapter only joins it, it never reinterprets it.
    """

    text: str = ""
    version: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    ttl_ms: int = 0

    @classmethod
    def from_wire(cls, data: Any) -> ContextSnapshot | None:
        """Parse a context payload; returns ``None`` when nothing usable arrived."""
        if not isinstance(data, Mapping):
            return None
        sections: dict[str, str] = {}
        raw_sections = data.get("sections")
        if isinstance(raw_sections, Mapping):
            for key, value in raw_sections.items():
                if isinstance(value, str) and value.strip() and str(key).strip():
                    sections[str(key).strip()] = value.strip()
        return cls(
            text=as_str(data.get("text")).strip(),
            version=as_str(data.get("version")).strip(),
            sections=sections,
            ttl_ms=max(0, as_int(data.get("ttl_ms"), 0)),
        )

    def is_empty(self) -> bool:
        """Whether the snapshot carries no injectable content."""
        return not self.text and not self.sections

    def render(self) -> str:
        """Return the snapshot as plain text for prompt injection."""
        if self.text:
            return self.text
        if not self.sections:
            return ""
        return "\n\n".join(f"【{key}】\n{value}" for key, value in self.sections.items())


@dataclass
class LeaseRequest:
    """Request for leased outbox actions (``POST /v1/outbox/lease``)."""

    adapter_id: str
    capabilities: tuple[str, ...] = SUPPORTED_ACTION_TYPES
    max_actions: int = 1
    lease_ttl_ms: int = 30000
    sessions: tuple[str, ...] = ()
    protocol_version: str = PROTOCOL_VERSION

    def to_wire(self) -> dict[str, Any]:
        """Return the request body for the lease endpoint."""
        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "capabilities": list(self.capabilities),
            "max_actions": max(1, as_int(self.max_actions, 1)),
            "lease_ttl_ms": max(1000, as_int(self.lease_ttl_ms, 30000)),
        }
        if self.sessions:
            payload["sessions"] = list(self.sessions)
        return payload


@dataclass
class LeasedAction:
    """An outbox action leased to this adapter for a bounded time."""

    action_id: str
    action_type: str
    lease_id: str
    session: str
    attempt_id: str = ""
    lease_ttl_ms: int = 0
    deadline_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, data: Any) -> LeasedAction | None:
        """Parse one leased action; returns ``None`` for unusable entries."""
        if not isinstance(data, Mapping):
            return None
        action_id = as_str(data.get("action_id")).strip()
        lease_id = as_str(data.get("lease_id")).strip()
        if not action_id or not lease_id:
            return None
        raw_payload = data.get("payload")
        return cls(
            action_id=action_id,
            action_type=as_str(data.get("action_type")).strip().lower(),
            lease_id=lease_id,
            session=as_str(data.get("session")).strip(),
            attempt_id=as_str(data.get("attempt_id")).strip(),
            lease_ttl_ms=max(0, as_int(data.get("lease_ttl_ms"), 0)),
            deadline_at=as_str(data.get("deadline_at")).strip(),
            payload=dict(raw_payload) if isinstance(raw_payload, Mapping) else {},
        )

    @property
    def key(self) -> tuple[str, str]:
        """Identity used for in-process duplicate suppression.

        A re-lease of the same attempt must not be executed twice; a genuine
        retry from the Runtime uses a new ``attempt_id`` and therefore a new key.
        """
        return (self.action_id, self.attempt_id or self.lease_id)


@dataclass
class LeaseHeartbeat:
    """Lease extension for a long running action (``POST /v1/outbox/{id}/heartbeat``)."""

    adapter_id: str
    action_id: str
    lease_id: str
    extend_ms: int
    protocol_version: str = PROTOCOL_VERSION

    def to_wire(self) -> dict[str, Any]:
        """Return the request body for the heartbeat endpoint."""
        return {
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "lease_id": self.lease_id,
            "extend_ms": max(1000, as_int(self.extend_ms, 30000)),
        }


@dataclass
class AuthorizeRequest:
    """Last-moment authorization gate before an irreversible send.

    Sent to ``POST /v1/actions/{action_id}/authorize`` immediately before
    delivery so the Runtime can re-coordinate concurrent activity (KEEP / MERGE /
    RERENDER / RESOLVED / ABORT) and refuse a message that is no longer valid.
    """

    adapter_id: str
    action_id: str
    lease_id: str
    session: str
    attempt_id: str = ""
    text_preview: str = ""
    text_sha256: str = ""
    protocol_version: str = PROTOCOL_VERSION

    def to_wire(self) -> dict[str, Any]:
        """Return the request body for the authorize endpoint."""
        return {
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "lease_id": self.lease_id,
            "session": self.session,
            "attempt_id": self.attempt_id,
            "text_preview": self.text_preview[:PREVIEW_CHAR_LIMIT],
            "text_sha256": self.text_sha256,
        }


@dataclass
class AuthorizeDecision:
    """The Runtime's answer to an authorization request."""

    authorized: bool = False
    reason: str = ""
    text: str = ""

    @classmethod
    def from_wire(cls, data: Any) -> AuthorizeDecision:
        """Parse an authorization response; anything unusable counts as denied."""
        if not isinstance(data, Mapping):
            return cls(authorized=False, reason="malformed_authorize_response")
        return cls(
            authorized=as_bool(data.get("authorized"), False),
            reason=as_str(data.get("reason")).strip(),
            text=as_str(data.get("text")).strip(),
        )

    @classmethod
    def denied(cls, reason: str) -> AuthorizeDecision:
        """Build an explicit denial."""
        return cls(authorized=False, reason=reason)


@dataclass
class ActionReport:
    """The outcome of a leased action, posted to ``POST /v1/outbox/{id}/result``."""

    adapter_id: str
    action_id: str
    lease_id: str
    status: str
    action_type: str = ""
    attempt_id: str = ""
    session: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    reported_at: str = ""
    protocol_version: str = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.reported_at:
            self.reported_at = utc_now_iso()
        if self.error:
            self.error = truncate_error(self.error)

    def to_wire(self) -> dict[str, Any]:
        """Return the request body for the action result endpoint."""
        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "adapter_id": self.adapter_id,
            "lease_id": self.lease_id,
            "action_type": self.action_type,
            "status": self.status,
            "attempt_id": self.attempt_id,
            "session": self.session,
            "reported_at": self.reported_at,
        }
        if self.result:
            payload["result"] = dict(self.result)
        if self.error:
            payload["error"] = self.error
        return payload

    def summary(self) -> str:
        """Return a compact one-line summary for logs."""
        detail = self.error or as_str(self.result.get("reason"))
        suffix = f" ({detail})" if detail else ""
        return (
            f"{self.action_type or 'action'}:{self.action_id} -> {self.status}{suffix}"
        )


@runtime_checkable
class RuntimeTransport(Protocol):
    """Everything the adapter needs from the Runtime over the wire.

    Implementations raise on failure; deciding how to degrade is the caller's
    job, because ``report`` and ``observe`` paths fail open while ``send``
    authorization fails closed.
    """

    async def post_events(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Append an event envelope to the Runtime event log."""
        ...

    async def fetch_context(
        self, request: ContextRequest, *, timeout_s: float
    ) -> ContextSnapshot | None:
        """Fetch the Runtime's current context for one session."""
        ...

    async def lease_actions(
        self, request: LeaseRequest, *, timeout_s: float
    ) -> list[LeasedAction]:
        """Lease up to ``max_actions`` outbox actions."""
        ...

    async def heartbeat_lease(self, request: LeaseHeartbeat, *, timeout_s: float) -> bool:
        """Extend a lease; returns whether the Runtime confirmed the extension."""
        ...

    async def authorize_action(
        self, request: AuthorizeRequest, *, timeout_s: float
    ) -> AuthorizeDecision:
        """Ask the Runtime whether an irreversible send may proceed right now."""
        ...

    async def report_action(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Report the outcome of a leased action."""
        ...

    async def aclose(self) -> None:
        """Release any underlying connection resources."""
        ...
