"""Lease-based outbox consumer.

The Runtime owns intent; this adapter only executes two mechanical actions:

``render``
    Ask AstrBot's *current* chat provider for that session to render the prompt
    the Runtime composed, then report the rendered text back. The Runtime decides
    what the text means and whether it becomes a message.

``send``
    Deliver an already rendered message. Delivery is irreversible, so it is
    gated twice: the action must still hold a live lease, and the Runtime must
    authorize the send at the moment of delivery (KEEP / MERGE / RERENDER /
    RESOLVED / ABORT re-coordination). If authorization cannot be obtained, the
    message is *not* sent -- fail-open applies to observation, never to delivery.

Exactly-once-ish delivery is attempted with an in-process result cache: a
re-leased attempt replays its stored report instead of executing twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from .coerce import as_bool, as_str
from .protocol import (
    ACTION_RENDER,
    ACTION_SEND,
    PREVIEW_CHAR_LIMIT,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    SUPPORTED_ACTION_TYPES,
    ActionReport,
    AuthorizeDecision,
    AuthorizeRequest,
    LeaseHeartbeat,
    LeaseRequest,
    LeasedAction,
    RuntimeTransport,
    truncate_error,
)
from .retry_queue import NULL_LOG
from .settings import Settings

#: How many finished action reports are remembered for duplicate suppression.
COMPLETED_CACHE_SIZE = 256

#: Backoff ceiling after repeated lease failures.
MAX_POLL_BACKOFF_S = 30.0

#: Minimum interval between lease heartbeats.
MIN_HEARTBEAT_INTERVAL_S = 1.0


class ActionExecutor(Protocol):
    """Host-side execution of leased actions (implemented against AstrBot)."""

    async def render(self, action: LeasedAction) -> dict[str, Any]:
        """Render ``action`` and return a result mapping containing ``text``."""
        ...

    async def send(self, action: LeasedAction, text: str) -> dict[str, Any]:
        """Deliver ``text`` for ``action``; returns e.g. ``{"sent": bool}``."""
        ...


@dataclass
class OutboxStats:
    """Counters exposed through the status command."""

    polls: int = 0
    poll_errors: int = 0
    leased: int = 0
    rendered: int = 0
    sent: int = 0
    rejected: int = 0
    failed: int = 0
    skipped: int = 0
    replayed: int = 0
    duplicate_inflight: int = 0
    heartbeats: int = 0
    heartbeat_errors: int = 0
    authorize_errors: int = 0


class OutboxConsumer:
    """Polls the Runtime outbox under lease and executes what it is given."""

    def __init__(
        self,
        *,
        transport: RuntimeTransport,
        executor: ActionExecutor,
        reporter: Callable[[ActionReport], Awaitable[None]],
        settings: Settings,
        clock: Any = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        log: Any = NULL_LOG,
    ) -> None:
        """Create the consumer.

        Args:
            transport: Runtime HTTP client.
            executor: Host-side render/send executor.
            reporter: Coroutine reporting an :class:`ActionReport`; it owns
                retrying, and must not raise.
            settings: Normalized adapter settings.
            clock: Monotonic clock source (injectable for tests).
            sleep: Sleep function (injectable for tests).
            log: Logger-like object.
        """
        self._transport = transport
        self._executor = executor
        self._reporter = reporter
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._log = log
        self._semaphore = asyncio.Semaphore(max(1, settings.outbox_max_concurrency))
        self._completed: OrderedDict[tuple[str, str], ActionReport] = OrderedDict()
        self._inflight: set[tuple[str, str]] = set()
        self._consecutive_errors = 0
        self.stats = OutboxStats()

    async def run(self) -> None:
        """Poll the outbox until cancelled. Never raises on Runtime failure."""
        while True:
            try:
                leased = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.poll_errors += 1
                self._consecutive_errors += 1
                delay = self._poll_backoff()
                log_detail = self._log.warning if self._settings.debug else self._log.debug
                log_detail(
                    "outbox lease failed (%.1fs backoff): %s",
                    delay,
                    truncate_error(exc),
                )
            else:
                self._consecutive_errors = 0
                delay = self._settings.outbox_poll_interval_s
                if leased:
                    self._log.debug("outbox leased %d action(s)", leased)
            await self._sleep(delay)

    async def poll_once(self) -> int:
        """Lease one batch and execute it.

        Returns:
            The number of actions leased in this round.

        Raises:
            Exception: Whatever the lease call raised; :meth:`run` handles it.
        """
        request = LeaseRequest(
            adapter_id=self._settings.adapter_id,
            capabilities=SUPPORTED_ACTION_TYPES,
            max_actions=self._settings.outbox_batch,
            lease_ttl_ms=int(self._settings.outbox_lease_ttl_s * 1000),
        )
        actions = await self._transport.lease_actions(
            request,
            timeout_s=self._settings.request_timeout_s,
        )
        self.stats.polls += 1
        if not actions:
            return 0
        self.stats.leased += len(actions)
        await asyncio.gather(
            *(self._handle(action) for action in actions),
            return_exceptions=True,
        )
        return len(actions)

    async def _handle(self, action: LeasedAction) -> None:
        """Execute one leased action and report the outcome."""
        key = action.key
        previous = self._completed.get(key)
        if previous is not None:
            # The Runtime re-leased something we already answered: replay the
            # stored report instead of sending or rendering a second time.
            self._completed.move_to_end(key)
            self.stats.replayed += 1
            await self._safe_report(previous)
            return
        if key in self._inflight:
            self.stats.duplicate_inflight += 1
            self._log.debug(
                "duplicate lease for in-flight action %s ignored",
                action.action_id,
            )
            return
        self._inflight.add(key)
        try:
            async with self._semaphore:
                report = await self._execute(action)
        finally:
            self._inflight.discard(key)
        self._remember(key, report)
        await self._safe_report(report)

    async def _execute(self, action: LeasedAction) -> ActionReport:
        """Dispatch an action to its handler, never raising."""
        if not action.session:
            self.stats.skipped += 1
            return self._stub(action, STATUS_SKIPPED, error="missing_session")
        if action.action_type == ACTION_RENDER:
            return await self._render(action)
        if action.action_type == ACTION_SEND:
            return await self._send(action)
        self.stats.skipped += 1
        return self._stub(
            action,
            STATUS_SKIPPED,
            error=f"unsupported_action_type:{action.action_type or 'missing'}",
        )

    async def _render(self, action: LeasedAction) -> ActionReport:
        """Render with the session's current provider while holding the lease."""
        heartbeat = _LeaseHeartbeat(
            action,
            default_ttl_s=self._settings.outbox_lease_ttl_s,
            extend=self._extend_lease,
        )
        heartbeat.start()
        try:
            result = await asyncio.wait_for(
                self._executor.render(action),
                timeout=self._settings.render_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            self.stats.failed += 1
            return self._stub(
                action,
                STATUS_FAILED,
                error=f"render timed out after {self._settings.render_timeout_s:.0f}s",
            )
        except Exception as exc:
            self.stats.failed += 1
            return self._stub(action, STATUS_FAILED, error=truncate_error(exc))
        finally:
            await heartbeat.stop()
        payload = dict(result) if isinstance(result, dict) else {}
        if not as_str(payload.get("text")).strip():
            self.stats.failed += 1
            return self._stub(action, STATUS_FAILED, error="render produced no text", result=payload)
        self.stats.rendered += 1
        return self._report(action, STATUS_OK, result=payload)

    async def _send(self, action: LeasedAction) -> ActionReport:
        """Authorize, deliver, and report an irreversible proactive message."""
        text = as_str(action.payload.get("text")).strip()
        if not text:
            self.stats.failed += 1
            return self._stub(action, STATUS_FAILED, error="send action has no text")

        decision = await self._authorize(action, text)
        if not decision.authorized:
            self.stats.rejected += 1
            self._log.debug(
                "send for %s not authorized: %s",
                action.action_id,
                decision.reason or "not_authorized",
            )
            return self._report(
                action,
                STATUS_REJECTED,
                error=decision.reason or "not_authorized",
                result={"authorized": False},
            )

        final_text = decision.text.strip() or text
        try:
            result = await asyncio.wait_for(
                self._executor.send(action, final_text),
                timeout=self._settings.send_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            self.stats.failed += 1
            return self._stub(
                action,
                STATUS_FAILED,
                error=f"send timed out after {self._settings.send_timeout_s:.0f}s",
                result={"authorized": True},
            )
        except Exception as exc:
            self.stats.failed += 1
            return self._stub(
                action,
                STATUS_FAILED,
                error=truncate_error(exc),
                result={"authorized": True},
            )

        payload = dict(result) if isinstance(result, dict) else {}
        delivered = as_bool(payload.get("sent"), False)
        payload["authorized"] = True
        if not delivered:
            self.stats.failed += 1
            return self._report(
                action,
                STATUS_FAILED,
                error=as_str(payload.get("reason")) or "delivery_failed",
                result=payload,
            )
        self.stats.sent += 1
        return self._report(action, STATUS_OK, result=payload)

    async def _authorize(self, action: LeasedAction, text: str) -> AuthorizeDecision:
        """Ask the Runtime to authorize a send; anything unclear means "no"."""
        request = AuthorizeRequest(
            adapter_id=self._settings.adapter_id,
            action_id=action.action_id,
            lease_id=action.lease_id,
            session=action.session,
            attempt_id=action.attempt_id,
            text_preview=text[:PREVIEW_CHAR_LIMIT],
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        timeout_s = self._settings.request_timeout_s
        try:
            return await asyncio.wait_for(
                self._transport.authorize_action(request, timeout_s=timeout_s),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.authorize_errors += 1
            reason = f"authorize_unavailable:{truncate_error(exc, 120)}"
            self._log.warning(
                "send authorization for %s failed; message NOT sent: %s",
                action.action_id,
                reason,
            )
            return AuthorizeDecision.denied(reason)

    async def _safe_report(self, report: ActionReport) -> None:
        """Report an outcome, swallowing transport problems."""
        try:
            await self._reporter(report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - reporter owns retrying
            self._log.warning(
                "action report for %s failed: %s",
                report.action_id,
                truncate_error(exc),
            )

    def _remember(self, key: tuple[str, str], report: ActionReport) -> None:
        """Store a finished report for duplicate suppression."""
        self._completed[key] = report
        self._completed.move_to_end(key)
        while len(self._completed) > COMPLETED_CACHE_SIZE:
            self._completed.popitem(last=False)

    def _poll_backoff(self) -> float:
        """Return the backoff delay after consecutive lease failures."""
        interval = self._settings.outbox_poll_interval_s
        raw = interval * (2 ** min(self._consecutive_errors, 6))
        return min(max(interval, raw), MAX_POLL_BACKOFF_S)

    def _stub(
        self,
        action: LeasedAction,
        status: str,
        *,
        error: str = "",
        result: dict[str, Any] | None = None,
    ) -> ActionReport:
        """Build a report that carries no result payload."""
        return self._report(action, status, error=error, result=result)

    def _report(
        self,
        action: LeasedAction,
        status: str,
        *,
        error: str = "",
        result: dict[str, Any] | None = None,
    ) -> ActionReport:
        """Build an :class:`ActionReport` for a leased action."""
        return ActionReport(
            adapter_id=self._settings.adapter_id,
            action_id=action.action_id,
            lease_id=action.lease_id,
            status=status,
            action_type=action.action_type,
            attempt_id=action.attempt_id,
            session=action.session,
            result=dict(result) if result else {},
            error=error,
        )

    async def _extend_lease(self, action: LeasedAction) -> None:
        """Extend the lease of a long running action."""
        request = LeaseHeartbeat(
            adapter_id=self._settings.adapter_id,
            action_id=action.action_id,
            lease_id=action.lease_id,
            extend_ms=int(self._settings.outbox_lease_ttl_s * 1000),
        )
        timeout_s = self._settings.request_timeout_s
        try:
            await asyncio.wait_for(
                self._transport.heartbeat_lease(request, timeout_s=timeout_s),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.heartbeat_errors += 1
            self._log.debug(
                "lease heartbeat for %s failed: %s",
                action.action_id,
                truncate_error(exc),
            )
            return
        self.stats.heartbeats += 1


class _LeaseHeartbeat:
    """Background lease extension for one action; a no-op when TTL is unknown."""

    def __init__(
        self,
        action: LeasedAction,
        *,
        default_ttl_s: float,
        extend: Callable[[LeasedAction], Awaitable[None]],
    ) -> None:
        """Create the heartbeat helper.

        Args:
            action: The action whose lease should be extended.
            default_ttl_s: TTL to assume when the Runtime did not state one.
            extend: Callback performing the actual extension.
        """
        ttl_s = action.lease_ttl_ms / 1000.0 if action.lease_ttl_ms else 0.0
        if ttl_s <= 0:
            ttl_s = default_ttl_s
        self._extend = extend
        self._action = action
        self._interval_s = max(MIN_HEARTBEAT_INTERVAL_S, ttl_s / 3.0)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Schedule the heartbeat loop."""
        try:
            self._task = asyncio.create_task(
                self._loop(),
                name=f"companion-runtime-lease:{self._action.action_id}",
            )
        except RuntimeError:  # pragma: no cover - no running loop
            self._task = None

    async def stop(self) -> None:
        """Cancel the heartbeat loop."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        """Extend the lease until cancelled."""
        while True:
            await asyncio.sleep(self._interval_s)
            await self._extend(self._action)
