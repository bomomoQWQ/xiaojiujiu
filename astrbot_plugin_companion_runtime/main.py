"""Thin AstrBot adapter for the endogenous companion Runtime.

This plugin is deliberately narrow. It does not hold psychology, memory, or
intent; the Runtime does. Its whole job is three mechanical things:

1. Observe messages and report them to the Runtime asynchronously (fail-open).
2. In ``on_llm_request``, fetch the Runtime's *current* context inside a strict
   short deadline and inject it as a temporary ``TextPart``
   (``mark_as_temp()``), so hidden psychological context never reaches permanent
   conversation history.
3. Consume the Runtime outbox under lease: ``render`` composes text with the
   session's current AstrBot provider, ``send`` delivers a message *only after*
   the Runtime authorizes it at the moment of delivery.

Failure policy: observation, injection, and reporting fail open -- a Runtime
outage must be invisible to AstrBot users. Delivery fails closed -- a message is
never sent without a live lease and a positive authorization.

No credential ships with this plugin. The optional shared token comes from the
plugin config or the ``COMPANION_RUNTIME_TOKEN`` environment variable, and
AstrBot's own provider API keys are never read, stored, or forwarded.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .astrbot_executor import AstrBotActionExecutor
from .companion_runtime.bridge import ContextBridge
from .companion_runtime.coerce import as_str
from .companion_runtime.http_client import (
    AiohttpRuntimeTransport,
    RuntimeTransportError,
    action_report_body,
)
from .companion_runtime.outbox import OutboxConsumer
from .companion_runtime.protocol import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_USER_MESSAGE,
    TRIGGER_LLM_REQUEST,
    TRIGGER_MESSAGE,
    ActionReport,
    ContextRequest,
    EventEnvelope,
    EventRecord,
    truncate_error,
)
from .companion_runtime.retry_queue import BoundedRetryQueue, QueueItem
from .companion_runtime.settings import OBSERVE_MODE_ALL, Settings

try:  # the documented import path for provider-facing content parts
    from astrbot.api.event.filter import CustomFilter
except ImportError:  # pragma: no cover - defensive
    from astrbot.core.star.filter.custom_filter import CustomFilter  # type: ignore

try:  # documented since AstrBot v4.24.0 (``TextPart.mark_as_temp``)
    from astrbot.core.agent.message import TextPart
except ImportError:  # pragma: no cover - defensive
    TextPart = None  # type: ignore[assignment]

#: Queue payload discriminators.
OP_EVENTS = "events"
OP_ACTION_RESULT = "action_result"

#: Bounded per-session memory of the last reported event id.
LAST_EVENT_ID_CACHE = 64

#: How many times a failing start is retried before the adapter gives up quietly.
MAX_START_ATTEMPTS = 3


class _ObservationScopeFilter(CustomFilter):
    """Pass only for messages AstrBot itself already treats as wake events.

    AstrBot sets ``is_at_or_wake_command`` exclusively for genuine wake
    conditions (wake prefix, @bot, @all, reply-to-bot, private chat). Plugin
    listeners never set it. Gating on it means this adapter can never turn an
    ordinary group message into a wake event, nor push non-wake traffic through
    the remaining pipeline stages.

    ``observe_all`` is flipped by the plugin instance when the operator explicitly
    opts in to observing every message; see ``observe_mode`` in ``README.md``.
    """

    observe_all: bool = False

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        """Return whether the adapter should observe this event."""
        if type(self).observe_all:
            return True
        return bool(getattr(event, "is_at_or_wake_command", False))


def _message_type_name(event: AstrMessageEvent) -> str:
    """Return a plain string message type such as ``group`` or ``private``."""
    try:
        message_type = event.get_message_type()
    except Exception:
        return ""
    value = getattr(message_type, "value", message_type)
    return as_str(value).lower()


class CompanionRuntimePlugin(Star):
    """Host-side thin adapter for the companion Runtime."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """Create the plugin; no background work starts until ``initialize``."""
        super().__init__(context)
        self.config = config or {}
        self._settings = Settings.from_mapping(self.config)
        self._started = False
        self._start_failures = 0
        self._transport: AiohttpRuntimeTransport | None = None
        self._queue: BoundedRetryQueue | None = None
        self._bridge: ContextBridge | None = None
        self._outbox: OutboxConsumer | None = None
        self._executor: AstrBotActionExecutor | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._last_event_ids: OrderedDict[str, str] = OrderedDict()
        self._injection_warnings: set[str] = set()
        for issue in self._settings.issues:
            self.logger.warning("companion_runtime config: %s", issue)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Start background workers (called by AstrBot after loading)."""
        self._start()

    async def terminate(self) -> None:
        """Stop background workers and release resources.

        Called when the plugin is disabled, unloaded, or reloaded. Idempotent, and
        safe to call even if :meth:`initialize` never ran.
        """
        self._started = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        queue, self._queue = self._queue, None
        bridge, self._bridge = self._bridge, None
        transport, self._transport = self._transport, None
        self._outbox = None
        self._executor = None
        self._last_event_ids.clear()

        try:
            if queue is not None:
                await queue.stop()
            if bridge is not None:
                await bridge.aclose()
            if transport is not None:
                await transport.aclose()
        except Exception:
            self.logger.warning("companion_runtime shutdown was not clean", exc_info=True)

    def _start(self) -> None:
        """Wire up workers. Synchronous and idempotent.

        Synchronous on purpose: observers and hooks must be able to lazily start
        the adapter without awaiting anything on the message path. Construction
        happens before any task is created, so a failure here leaks nothing.
        """
        if self._started:
            return
        self._started = True
        if not self._settings.usable:
            self.logger.info(
                "companion_runtime adapter is inactive (disabled or unusable config)",
            )
            return
        try:
            transport = AiohttpRuntimeTransport(settings=self._settings, log=self.logger)
            executor = AstrBotActionExecutor(context=self.context, log=self.logger)
            queue = BoundedRetryQueue(
                sender=self._deliver,
                max_items=self._settings.queue_max_items,
                max_attempts=self._settings.queue_max_attempts,
                max_age_s=self._settings.queue_max_age_s,
                base_backoff_s=self._settings.queue_base_backoff_s,
                max_backoff_s=self._settings.queue_max_backoff_s,
                send_timeout_s=self._settings.queue_send_timeout_s,
                log=self.logger,
            )
            bridge = ContextBridge(
                transport=transport,
                settings=self._settings,
                log=self.logger,
            )
            outbox = None
            if self._settings.outbox_enabled:
                outbox = OutboxConsumer(
                    transport=transport,
                    executor=executor,
                    reporter=self._report_action,
                    settings=self._settings,
                    log=self.logger,
                )
        except Exception:
            self._note_start_failure(
                "adapter could not be constructed; AstrBot behaviour is unchanged",
            )
            return

        self._transport = transport
        self._executor = executor
        self._queue = queue
        self._bridge = bridge
        self._outbox = outbox
        _ObservationScopeFilter.observe_all = self._settings.observe_mode == OBSERVE_MODE_ALL

        try:
            if outbox is not None:
                self._tasks.append(
                    asyncio.create_task(outbox.run(), name="companion-runtime-outbox"),
                )
            queue.start()
        except Exception:
            # Nothing is running yet, so dropping the wiring is enough cleanup.
            self._tasks.clear()
            self._transport = None
            self._executor = None
            self._queue = None
            self._bridge = None
            self._outbox = None
            self._note_start_failure("background workers could not be scheduled")
            return

        self.logger.info(
            "companion_runtime adapter started (adapter_id=%s, base_url=%s, "
            "observe_mode=%s, outbox=%s)",
            self._settings.adapter_id,
            self._settings.base_url,
            self._settings.observe_mode,
            "on" if outbox is not None else "off",
        )

    def _note_start_failure(self, message: str) -> None:
        """Record a failed start, giving up after a few attempts.

        Retrying forever would mean a broken adapter logging on every single
        message, so after ``MAX_START_ATTEMPTS`` the plugin stays quiet until
        AstrBot reloads it.
        """
        self._start_failures += 1
        if self._start_failures >= MAX_START_ATTEMPTS:
            self._started = True
            self.logger.error(
                "companion_runtime %s; adapter disabled after %d attempts "
                "(reload the plugin after fixing the config)",
                message,
                self._start_failures,
            )
            return
        self._started = False
        self.logger.warning(
            "companion_runtime %s (attempt %d/%d)",
            message,
            self._start_failures,
            MAX_START_ATTEMPTS,
        )

    # ------------------------------------------------------------------
    # observation
    # ------------------------------------------------------------------

    @filter.custom_filter(_ObservationScopeFilter)
    async def on_message_observed(self, event: AstrMessageEvent) -> None:
        """Report an observed user message to the Runtime.

        Runs on AstrBot's message path, so every failure is swallowed and only
        logged at debug level: a Runtime outage must never change how AstrBot
        handles the message.
        """
        try:
            self._start()
            queue = self._queue
            if queue is None:
                return
            wake = bool(getattr(event, "is_at_or_wake_command", False))
            if not wake and self._settings.observe_mode != OBSERVE_MODE_ALL:
                # Defence in depth: the scope filter already keeps non-wake
                # messages out, and this keeps the guarantee true even if the
                # host ever evaluates handler filters differently.
                return
            session = event.unified_msg_origin
            record = EventRecord(
                kind=EVENT_USER_MESSAGE,
                session=session,
                text=as_str(getattr(event, "message_str", "")),
                platform=as_str(event.get_platform_name()),
                message_type=_message_type_name(event),
                sender_id=as_str(event.get_sender_id()),
                sender_name=as_str(event.get_sender_name()),
                self_id=as_str(event.get_self_id()),
                group_id=as_str(event.get_group_id()),
                message_id=as_str(getattr(event.message_obj, "message_id", "")),
                wake=wake,
                # A user message immediately pauses endogenous dispatch on the
                # Runtime side (entry barrier), so the proactive system can never
                # speak before the Runtime has seen what the user just said.
                preempts_proactive=True,
            )
            self._remember_event_id(session, record.event_id)
            self._enqueue_event(record)
            bridge = self._bridge
            if bridge is not None:
                bridge.prefetch(self._context_request(event, trigger=TRIGGER_MESSAGE))
        except Exception:
            self.logger.debug("companion_runtime message observation failed", exc_info=True)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Report the message AstrBot actually delivered to the user."""
        try:
            self._start()
            if self._queue is None or not self._settings.report_assistant_messages:
                return
            text = self._result_text(event)
            if not text:
                return
            session = event.unified_msg_origin
            record = EventRecord(
                kind=EVENT_ASSISTANT_MESSAGE,
                session=session,
                text=text,
                platform=as_str(event.get_platform_name()),
                message_type=_message_type_name(event),
                sender_id=as_str(event.get_self_id()),
                sender_name="bot",
                self_id=as_str(event.get_self_id()),
                group_id=as_str(event.get_group_id()),
                wake=bool(getattr(event, "is_at_or_wake_command", False)),
            )
            self._remember_event_id(session, record.event_id)
            self._enqueue_event(record)
        except Exception:
            self.logger.debug("companion_runtime assistant report failed", exc_info=True)

    # ------------------------------------------------------------------
    # context injection
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Inject the Runtime's current context as a temporary content part.

        The Runtime's hidden context is explanatory, never authoritative: it is
        appended after the user's own words as an extra provider-facing content
        part, marked temporary so it is dropped instead of being persisted.
        """
        try:
            self._start()
            await self._inject_context(event, req)
        except Exception:
            self.logger.debug("companion_runtime context injection failed", exc_info=True)

    async def _inject_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Fetch and append the Runtime context block, fail-open on any problem."""
        bridge = self._bridge
        if bridge is None or not self._settings.inject_enabled:
            return
        if TextPart is None:
            self._warn_injection_once(
                "missing-textpart",
                "companion_runtime cannot inject context: astrbot.core.agent.message.TextPart "
                "is unavailable on this AstrBot version; injection is disabled",
            )
            return

        text = await bridge.text_for_llm_request(
            self._context_request(event, trigger=TRIGGER_LLM_REQUEST),
        )
        if not text:
            return

        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list):
            self._warn_injection_once(
                "unsupported-request",
                "companion_runtime cannot inject context: this AstrBot version exposes no "
                "ProviderRequest.extra_user_content_parts",
            )
            return

        try:
            part = TextPart(text=text)
            mark_as_temp = getattr(part, "mark_as_temp", None)
            if not callable(mark_as_temp):
                # Without mark_as_temp the hidden context could be written into
                # permanent history, which the architecture forbids. Skip instead.
                self._warn_injection_once(
                    "missing-mark-as-temp",
                    "companion_runtime cannot inject context: TextPart.mark_as_temp() is "
                    "unavailable (requires AstrBot >= 4.24); injection is disabled",
                )
                return
            parts.append(mark_as_temp())
        except Exception:
            self.logger.debug("companion_runtime could not append the context part", exc_info=True)

    # ------------------------------------------------------------------
    # Runtime calls
    # ------------------------------------------------------------------

    async def _deliver(self, item: QueueItem) -> None:
        """Deliver one queued request to the Runtime (called by the retry queue).

        Raises:
            RuntimeTransportError: When the request cannot be delivered, which
                makes the queue retry it with backoff.
        """
        transport = self._transport
        if transport is None:
            raise RuntimeTransportError("Runtime transport is not available")
        operation = as_str(item.payload.get("op"))
        body = item.payload.get("body")
        if not isinstance(body, dict):
            raise RuntimeTransportError(f"queue item {item.idempotency_key} carries no body")
        timeout_s = self._settings.request_timeout_s
        if operation == OP_EVENTS:
            await transport.post_events(body, timeout_s=timeout_s)
        elif operation == OP_ACTION_RESULT:
            await transport.report_action(body, timeout_s=timeout_s)
        else:
            raise RuntimeTransportError(f"unknown queue operation {operation!r}")

    async def _report_action(self, report: ActionReport) -> None:
        """Report an action outcome, deferring to the retry queue when needed.

        Never raises: a lost report is recovered by the bounded local queue, and
        ultimately by the Runtime's own lease expiry.
        """
        transport = self._transport
        if transport is None:
            return
        body = action_report_body(report)
        try:
            await transport.report_action(body, timeout_s=self._settings.request_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.debug("companion_runtime action report deferred: %s", truncate_error(exc))
            queue = self._queue
            if queue is not None:
                queue.put(
                    {"op": OP_ACTION_RESULT, "body": body},
                    key=(
                        f"result:{report.action_id}:"
                        f"{report.attempt_id or report.lease_id}:{report.status}"
                    ),
                )

    def _context_request(self, event: AstrMessageEvent, *, trigger: str) -> ContextRequest:
        """Build a context request for the event's session."""
        session = event.unified_msg_origin
        return ContextRequest(
            adapter_id=self._settings.adapter_id,
            session=session,
            trigger=trigger,
            platform=as_str(event.get_platform_name()),
            last_event_id=self._last_event_ids.get(session),
        )

    def _enqueue_event(self, record: EventRecord) -> None:
        """Queue one event report; dropping it is preferable to blocking."""
        queue = self._queue
        if queue is None:
            return
        envelope = EventEnvelope(adapter_id=self._settings.adapter_id, events=[record])
        queue.put(
            {"op": OP_EVENTS, "body": envelope.to_wire()},
            key=f"event:{record.event_id}",
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _remember_event_id(self, session: str, event_id: str) -> None:
        """Remember the newest reported event id for one session."""
        self._last_event_ids[session] = event_id
        self._last_event_ids.move_to_end(session)
        while len(self._last_event_ids) > LAST_EVENT_ID_CACHE:
            self._last_event_ids.popitem(last=False)

    def _warn_injection_once(self, key: str, message: str) -> None:
        """Log an injection problem once instead of on every LLM request."""
        if key in self._injection_warnings:
            return
        self._injection_warnings.add(key)
        self.logger.warning(message)

    @staticmethod
    def _result_text(event: AstrMessageEvent) -> str:
        """Return the plain text of the message AstrBot just sent, if any."""
        try:
            result = event.get_result()
        except Exception:
            return ""
        if result is None:
            return ""
        try:
            return as_str(result.get_plain_text()).strip()
        except Exception:
            return ""

    @filter.command("companion_runtime")
    async def companion_runtime_status(self, event: AstrMessageEvent):
        """查看陪伴 Runtime 适配器状态。"""
        yield event.plain_result(self._status_text())

    def _status_text(self) -> str:
        """Build the status report shown by the ``/companion_runtime`` command."""
        settings = self._settings
        lines = [
            "companion Runtime adapter",
            f"- state: {'running' if self._started and settings.usable else 'inactive'}",
            f"- adapter_id: {settings.adapter_id}",
            f"- runtime: {settings.base_url or '<unset>'}",
            f"- token: {'configured' if settings.token else 'not configured'}",
            f"- observe_mode: {settings.observe_mode}",
            f"- context deadline: {settings.context_timeout_s * 1000:.0f}ms"
            f" (ttl {settings.context_cache_ttl_s:.0f}s)",
            f"- outbox: {'on' if settings.outbox_enabled else 'off'}"
            f" every {settings.outbox_poll_interval_s:.1f}s"
            f" batch {settings.outbox_batch}",
        ]
        bridge = self._bridge
        if bridge is not None:
            stats = bridge.stats
            lines.append(
                "- context: "
                f"{stats.requests} requests, {stats.cache_hits} cache hits, "
                f"{stats.fetches} fetches, {stats.timeouts} timeouts, "
                f"{stats.errors} errors, {stats.stale_fallbacks} stale fallbacks",
            )
        outbox = self._outbox
        if outbox is not None:
            stats = outbox.stats
            lines.append(
                "- actions: "
                f"{stats.leased} leased, {stats.rendered} rendered, {stats.sent} sent, "
                f"{stats.rejected} rejected, {stats.failed} failed, "
                f"{stats.skipped} skipped, {stats.replayed} replayed",
            )
        queue = self._queue
        if queue is not None:
            stats = queue.stats
            lines.append(
                "- queue: "
                f"{len(queue)} pending, {stats.delivered} delivered, {stats.retried} retried, "
                f"{stats.dropped()} dropped (full {stats.dropped_full}, "
                f"failed {stats.dropped_failed}, expired {stats.dropped_expired})",
            )
        if settings.issues:
            lines.append("- config issues:")
            lines.extend(f"  · {issue}" for issue in settings.issues)
        return "\n".join(lines)
