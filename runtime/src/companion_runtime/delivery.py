"""Delivery service: claim -> render -> send -> observe.

This module is the operational shell around the outbox. It is what a worker
process (or the sidecar's own background task) calls in a loop:

1. ``claim`` the highest-priority ready outbox row with a lease;
2. for a ``render`` row, ask the host main LLM for the wording, then hand the text
   back through :meth:`Reducer.complete_render`, which moves the attempt to
   ``ready_to_send`` and enqueues a ``send`` row;
3. for a ``send`` row, authorize the message one last time and hand it to the host
   transport, then call :meth:`Reducer.mark_delivered`;
4. acknowledge or negatively acknowledge the lease.

The separation between ``committed``, ``rendered`` and ``sent`` is what makes a
user message arriving mid-render survivable: the user can be merged into, or beat,
an intention that has not left the Runtime yet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from .authorize import AuthorizeRequest, authorize
from .config import RuntimeConfig
from .reducer import Reducer, RenderResult
from .typing import AttemptState, OutboxItem, OutboxKind, OutboxStatus
from .utility import ensure_aware, isoformat, utcnow

LOGGER = logging.getLogger("companion_runtime.delivery")


class Renderer(Protocol):
    """Port for the host main LLM that produces the final wording."""

    def render(self, payload: Mapping[str, Any]) -> str:
        """Return the visible message text for ``payload``."""
        ...


class Transport(Protocol):
    """Port for the host message transport."""

    def send(self, text: str, conversation_id: str | None) -> Mapping[str, Any]:
        """Send ``text`` and return a result mapping with an optional ``message_id``."""
        ...


@dataclass(slots=True)
class DeliveryReport:
    """What one worker cycle did."""

    claimed: int = 0
    rendered: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "claimed": self.claimed,
            "rendered": self.rendered,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "details": list(self.details),
        }


class EchoRenderer:
    """Deterministic fallback renderer used when the host provides no main LLM.

    It produces a short, low-risk message built only from the intent, which keeps
    the Runtime fully functional in tests and in degradation Level 0.
    """

    def __init__(self, template: str | None = None) -> None:
        """Store an optional template with ``{intent}`` and ``{goal}`` placeholders."""
        self._template = template

    def render(self, payload: Mapping[str, Any]) -> str:
        """Return a short message derived from the intent."""
        if self._template:
            return self._template.format(
                intent=payload.get("intent", ""), goal=payload.get("goal", "")
            )
        intent = str(payload.get("intent") or "").strip()
        if not intent:
            return "在的，想和你说说话。"
        return f"（{intent}）"


class NullTransport:
    """Transport that accepts everything and reports success (dry-run mode)."""

    def __init__(self) -> None:
        """Record every sent message for inspection."""
        self.sent: list[dict[str, Any]] = []

    def send(self, text: str, conversation_id: str | None) -> Mapping[str, Any]:
        """Record the message and pretend it was delivered."""
        record = {"text": text, "conversation_id": conversation_id, "at": isoformat(utcnow())}
        self.sent.append(record)
        return {"ok": True, "message_id": f"null-{len(self.sent)}"}


class DeliveryService:
    """Claims outbox rows and drives them to completion."""

    def __init__(
        self,
        *,
        reducer: Reducer,
        config: RuntimeConfig,
        runtime: Any = None,
        renderer: Renderer | None = None,
        transport: Transport | None = None,
        owner: str = "delivery-worker",
    ) -> None:
        """Store the reducer, ports and the lease owner identifier."""
        self._reducer = reducer
        self._config = config
        self._runtime = runtime
        self._renderer = renderer or EchoRenderer()
        self._transport = transport or NullTransport()
        self._owner = owner

    def cycle(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
        kinds: Sequence[str] | None = None,
    ) -> DeliveryReport:
        """Process up to ``limit`` outbox rows once.

        Args:
            now: Reference time.
            limit: Maximum number of rows to claim.
            kinds: Optional restriction to ``render`` or ``send`` rows.

        Returns:
            A :class:`DeliveryReport`.
        """
        stamp = ensure_aware(now) or utcnow()
        batch = limit or self._config.outbox.max_batch
        items = self._reducer.claim_outbox(
            owner=self._owner, now=stamp, limit=batch, kinds=kinds
        )
        report = DeliveryReport(claimed=len(items))
        for item in items:
            try:
                detail = self._handle(item, now=stamp)
            except Exception as exc:  # noqa: BLE001 - a single row must not kill the worker
                LOGGER.exception("Outbox row %s failed", item.outbox_id)
                self._reducer.nack_outbox(item.outbox_id, error=str(exc))
                report.failed += 1
                report.details.append({"outbox_id": item.outbox_id, "error": str(exc)})
                continue
            report.details.append(detail)
            status = detail.get("status")
            if status in {"rendered"}:
                report.rendered += 1
            elif status in {"sent", "already_sent"}:
                report.sent += 1
            elif status in {"skipped"}:
                report.skipped += 1
            else:
                report.failed += 1
        return report

    def _handle(self, item: OutboxItem, *, now: datetime) -> dict[str, Any]:
        """Route one claimed row to its handler."""
        if item.kind == OutboxKind.RENDER.value:
            return self._handle_render(item, now=now)
        if item.kind == OutboxKind.SEND.value:
            return self._handle_send(item, now=now)
        self._reducer.nack_outbox(item.outbox_id, error=f"unknown kind {item.kind}", terminal=True)
        return {"outbox_id": item.outbox_id, "status": "failed", "error": "unknown_kind"}

    def _handle_render(self, item: OutboxItem, *, now: datetime) -> dict[str, Any]:
        """Render the intent into message text and queue it for sending."""
        attempt_id = str(item.payload.get("attempt_id") or "")
        try:
            text = self._renderer.render(item.payload)
        except Exception as exc:  # noqa: BLE001 - renderer is an external port
            self._reducer.fail_render(outbox_id=item.outbox_id, error=f"renderer_error:{exc}", now=now)
            return {"outbox_id": item.outbox_id, "status": "failed", "error": str(exc)}
        if not text or not text.strip():
            self._reducer.fail_render(
                outbox_id=item.outbox_id, error="renderer_returned_empty_text", now=now
            )
            return {"outbox_id": item.outbox_id, "status": "failed", "error": "empty_text"}

        result: RenderResult = self._reducer.complete_render(
            outbox_id=item.outbox_id, text=text, now=now
        )
        return {
            "outbox_id": item.outbox_id,
            "status": "rendered",
            "attempt_id": result.attempt_id,
            "attempt_state": result.state,
            "send_outbox_id": result.outbox_id,
            "reconciled": result.reconciled,
        }

    def _handle_send(self, item: OutboxItem, *, now: datetime) -> dict[str, Any]:
        """Authorize the message for the last time and deliver it."""
        attempt_id = str(item.payload.get("attempt_id") or "")
        text = str(item.payload.get("text") or "")
        request = AuthorizeRequest(
            action="send",
            attempt_id=attempt_id or None,
            text=text,
            is_proactive=True,
            now=now,
        )
        projections = self._reducer.projections
        verdict = authorize(
            request, projections=projections, config=self._config, now=now
        )
        if not verdict.allowed:
            if verdict.reason.startswith("attempt_terminal") or verdict.reason.startswith(
                "attempt_reconciled"
            ):
                # The attempt was already resolved or aborted mid-flight: the row
                # is stale, not failed.
                self._reducer.ack_outbox(item.outbox_id, now=now)
                return {
                    "outbox_id": item.outbox_id,
                    "status": "skipped",
                    "reason": verdict.reason,
                }
            self._reducer.nack_outbox(item.outbox_id, error=verdict.reason, terminal=True)
            return {"outbox_id": item.outbox_id, "status": "failed", "error": verdict.reason}

        try:
            outcome = self._transport.send(text, item.conversation_id)
        except Exception as exc:  # noqa: BLE001 - transport is an external port
            self._reducer.nack_outbox(item.outbox_id, error=f"transport_error:{exc}")
            return {"outbox_id": item.outbox_id, "status": "failed", "error": str(exc)}

        if outcome is not None and outcome.get("ok") is False:
            self._reducer.nack_outbox(
                item.outbox_id, error=str(outcome.get("error") or "transport_rejected")
            )
            return {"outbox_id": item.outbox_id, "status": "failed", "error": "transport_rejected"}

        result = self._reducer.mark_delivered(outbox_id=item.outbox_id, now=now)
        return {
            "outbox_id": item.outbox_id,
            "status": "sent",
            "attempt_id": result.get("attempt_id"),
            "message_id": (outcome or {}).get("message_id"),
            "version": result.get("version"),
        }

    # ------------------------------------------------------------------ helpers

    def pending_count(self) -> int:
        """Return how many outbox rows are waiting."""
        return len(self._reducer.projections.outbox.list_items(status=OutboxStatus.PENDING.value, limit=500))

    def stats(self) -> dict[str, int]:
        """Return per-status outbox counts."""
        return self._reducer.projections.outbox.stats()


def build_render_payload(item: OutboxItem, context_bundle: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge an outbox render payload with the transient context bundle.

    The renderer receives both the intent and the ephemeral psychological context,
    and must return only the visible message text.

    Args:
        item: The render outbox row.
        context_bundle: The temporary context bundle, when available.

    Returns:
        A merged payload mapping.
    """
    payload = dict(item.payload)
    if context_bundle:
        payload["runtime_context"] = dict(context_bundle)
    return payload


def is_sendable_state(state: str) -> bool:
    """Return whether an attempt in ``state`` may still be delivered."""
    return state == AttemptState.READY_TO_SEND.value
