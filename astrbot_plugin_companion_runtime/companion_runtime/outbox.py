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

Three further rules keep that cache meaningful:

* every leased action heartbeats from the moment it is handed to us -- including
  while it waits for a concurrency slot -- because a lease that expires before
  the work even starts is a lease the Runtime hands out a second time;
* a refused heartbeat means the lease is gone, so the work stops instead of
  finishing under a lease somebody else now owns;
* an action the Runtime did *not* make a decision about is reported as nothing at
  all. The Runtime records every non-``ok`` result as a terminal attempt, while a
  lease that simply expires is reclaimed to ``pending`` and handed out again, so
  silence is the only shape of "retry this later" the contract offers. See
  :meth:`OutboxConsumer._send` for the two cases that use it.
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
    """Lease extensions the Runtime actually confirmed."""
    heartbeats_lost: int = 0
    """Lease extensions the Runtime refused, i.e. the lease is no longer ours."""
    heartbeat_errors: int = 0
    """Lease extensions that could not be delivered at all (transient outage)."""
    authorize_errors: int = 0
    """Send authorizations that could not be obtained (never a Runtime verdict)."""
    deferred: int = 0
    """Actions intentionally left unreported, for the Runtime to re-dispatch."""


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
        #: Tasks currently executing a leased action, used by the shutdown path.
        self._active: set[asyncio.Task[Any]] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopping = False
        self._consecutive_errors = 0
        self.stats = OutboxStats()

    async def run(self) -> None:
        """Poll the outbox until cancelled or asked to stop.

        Never raises on Runtime failure: a Runtime outage must not take the
        plugin, let alone AstrBot, down with it.
        """
        while not self._stopping:
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
            if self._stopping:
                break
            await self._sleep(delay)

    def request_stop(self) -> None:
        """Ask the poll loop to stop leasing new actions.

        Called by the host while the plugin is unloaded or disabled. Actions that
        are already in flight are still executed and reported: they were leased
        from the Runtime, so cancelling them is the adapter's own bug, not the
        Runtime's problem.
        """
        self._stopping = True

    async def wait_idle(self, timeout_s: float) -> bool:
        """Wait for in-flight actions to finish, for at most ``timeout_s``.

        Args:
            timeout_s: Upper bound on the wait.

        Returns:
            ``True`` when nothing is in flight any more, ``False`` on timeout.
        """
        if not self._active:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=max(0.0, timeout_s))
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    async def poll_once(self) -> int:
        """Lease one batch and execute it.

        Returns:
            The number of actions leased in this round.

        Raises:
            Exception: Whatever the lease call raised; :meth:`run` handles it.
        """
        if self._stopping:
            # A stop arrived while we were waiting: lease nothing new, so
            # shutdown only has to drain the batch already in flight.
            return 0
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
        """Execute one leased action and report the outcome.

        The action runs in its own task, shielded from cancellation: a shutdown
        must not abandon a leased action half way, because the report the adapter
        owes the Runtime is the only way the Runtime learns what happened.
        """
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
        # Mark busy before the task can possibly finish, so the idle event can
        # never be set while work is still outstanding.
        self._idle.clear()
        work = asyncio.create_task(
            self._run_action(action),
            name=f"companion-runtime-action:{action.action_id}",
        )
        self._active.add(work)
        work.add_done_callback(self._mark_settled)
        try:
            await asyncio.shield(work)
        finally:
            self._inflight.discard(key)

    def _mark_settled(self, task: asyncio.Task[Any]) -> None:
        """Drop a finished action task and report idleness when none remain."""
        self._active.discard(task)
        if not self._active:
            self._idle.set()
        if not task.cancelled():
            # Retrieve the outcome, so a shutdown-raced action can never surface
            # later as "Task exception was never retrieved".
            task.exception()

    async def _run_action(self, action: LeasedAction) -> ActionReport | None:
        """Execute, remember, and report one leased action.

        The heartbeat starts here, before the concurrency slot is requested: with
        ``outbox_max_concurrency`` limiting parallel work, an action can spend
        real time waiting for its turn, and its lease must not expire while it
        waits -- that is exactly how the Runtime ends up handing the same action
        to a second worker.

        Args:
            action: The freshly leased action.

        Returns:
            The report that was sent to the Runtime, or ``None`` when the action
            was deliberately left unreported: a lease the Runtime has taken back
            (see below) or an authorization that never arrived (see
            :meth:`_send`). Both are cases the Runtime recovers from the lease
            deadline, and both are cases where nothing was delivered.
        """
        heartbeat = _LeaseHeartbeat(
            action,
            default_ttl_s=self._settings.outbox_lease_ttl_s,
            extend=self._extend_lease,
        )
        execution = asyncio.ensure_future(self._execute_guarded(action))
        heartbeat.stop_on_loss(execution)
        heartbeat.start()
        try:
            report = await execution
        except asyncio.CancelledError:
            if not heartbeat.lost:
                raise
            # The Runtime refused to extend the lease, so this action is no
            # longer ours: stop working on it and stay silent. The Runtime has
            # already recorded what it decided -- the row it took back is
            # ``pending`` again (or belongs to another adapter) -- and a terminal
            # result from here would close a row it means to re-dispatch.
            self.stats.deferred += 1
            self._log.warning(
                "stopped work on %s: the Runtime no longer holds its lease; "
                "leaving the row to the Runtime's own recovery",
                action.action_id,
            )
            report = None
        finally:
            await heartbeat.stop()
        if report is None:
            return None
        self._remember(action.key, report)
        await self._safe_report(report)
        return report

    async def _execute_guarded(self, action: LeasedAction) -> ActionReport | None:
        """Run one action under the configured concurrency limit."""
        async with self._semaphore:
            return await self._execute(action)

    async def _execute(self, action: LeasedAction) -> ActionReport | None:
        """Dispatch an action to its handler, never raising.

        Returns:
            The report to send, or ``None`` when the action must be left for the
            Runtime's lease-expiry recovery instead.
        """
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
        payload = dict(result) if isinstance(result, dict) else {}
        if not as_str(payload.get("text")).strip():
            self.stats.failed += 1
            return self._stub(action, STATUS_FAILED, error="render produced no text", result=payload)
        self.stats.rendered += 1
        return self._report(action, STATUS_OK, result=payload)

    async def _send(self, action: LeasedAction) -> ActionReport | None:
        """Authorize, deliver, and report an irreversible proactive message.

        Args:
            action: The leased ``send`` action.

        Returns:
            The report for the Runtime, or ``None`` when the action is left
            unreported on purpose so the Runtime can re-dispatch it.
        """
        text = as_str(action.payload.get("text")).strip()
        if not text:
            self.stats.failed += 1
            return self._stub(action, STATUS_FAILED, error="send action has no text")

        decision, unavailable = await self._authorize(action, text)
        if decision is None:
            # Fail closed on delivery, silent on reporting.
            #
            # The message is NOT sent, and nothing is reported either. The Runtime
            # records every non-``ok`` send result as a terminal attempt
            # (``mark_delivered(success=False)`` -> ``nack(terminal=True)``), so a
            # row reported as `failed` or `rejected` here would be dropped for
            # good over a network blip that the Runtime never even saw. Left
            # alone, the lease expires instead, and the Runtime's own recovery
            # (``reclaim_expired``) returns the row to ``pending`` so it is
            # re-leased with a fresh lease id while the attempt budget lasts.
            # Re-running it cannot duplicate anything: nothing was delivered.
            self.stats.deferred += 1
            self._log.warning(
                "send authorization for %s unavailable (%s); message NOT sent and "
                "left to the Runtime's lease-expiry recovery",
                action.action_id,
                unavailable,
            )
            return None
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
            result = await self._settle(
                asyncio.ensure_future(
                    asyncio.wait_for(
                        self._executor.send(action, final_text),
                        timeout=self._settings.send_timeout_s,
                    ),
                ),
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

    async def _authorize(
        self,
        action: LeasedAction,
        text: str,
    ) -> tuple[AuthorizeDecision | None, str]:
        """Ask the Runtime to authorize a send, keeping "no" and "no answer" apart.

        Args:
            action: The leased send action.
            text: The text that would be delivered, digested for the request.

        Returns:
            ``(decision, error)``. ``decision`` is ``None`` when the Runtime could
            not be asked at all (timeout, connection failure, unusable response),
            which is an outage rather than a verdict; ``error`` then carries the
            single-line reason and the decision is empty.
        """
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
            decision = await asyncio.wait_for(
                self._transport.authorize_action(request, timeout_s=timeout_s),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.authorize_errors += 1
            return None, f"authorize_unavailable:{truncate_error(exc, 120)}"
        return decision, ""

    async def _settle(self, task: asyncio.Task[Any]) -> Any:
        """Await an irreversible delivery, absorbing one cancellation.

        Once the Runtime has authorized a send, the message is on its way out.
        Abandoning it here -- plugin unload, lost lease -- would leave a message
        that may already have reached the user with no result report, and the
        Runtime only knows what this adapter tells it, so it would hand the same
        action out again. One cancellation is therefore absorbed until the
        delivery settles; a second one is honoured, which keeps shutdown bounded.
        A lease lost mid-delivery is the same situation: the message is out, so
        the honest report is worth more than an abandoned attempt.

        Args:
            task: The delivery task to settle.

        Returns:
            Whatever the delivery returned.

        Raises:
            asyncio.CancelledError: When the delivery itself was cancelled, or a
                further cancellation arrived while waiting for it.
        """
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            self._log.debug("delivery is still settling after a cancellation")
            return await asyncio.shield(task)

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

    async def _extend_lease(self, action: LeasedAction) -> bool:
        """Extend the lease of a running action.

        Args:
            action: The action whose lease should be extended.

        Returns:
            ``True`` to keep working, ``False`` when the Runtime says the lease is
            gone. A transient transport failure keeps the work going (the lease
            may well still be alive) but is never counted as a successful
            extension, so the counters keep telling the truth.
        """
        request = LeaseHeartbeat(
            adapter_id=self._settings.adapter_id,
            action_id=action.action_id,
            lease_id=action.lease_id,
            extend_ms=int(self._settings.outbox_lease_ttl_s * 1000),
        )
        timeout_s = self._settings.request_timeout_s
        try:
            extended = await asyncio.wait_for(
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
            return True
        if not extended:
            self.stats.heartbeats_lost += 1
            return False
        self.stats.heartbeats += 1
        return True


class _LeaseHeartbeat:
    """Background lease extension for one action; a no-op when TTL is unknown."""

    def __init__(
        self,
        action: LeasedAction,
        *,
        default_ttl_s: float,
        extend: Callable[[LeasedAction], Awaitable[bool]],
    ) -> None:
        """Create the heartbeat helper.

        Args:
            action: The action whose lease should be extended.
            default_ttl_s: TTL to assume when the Runtime did not state one.
            extend: Callback performing the actual extension; it returns ``False``
                when the Runtime no longer holds the lease.
        """
        ttl_s = action.lease_ttl_ms / 1000.0 if action.lease_ttl_ms else 0.0
        if ttl_s <= 0:
            ttl_s = default_ttl_s
        self._extend = extend
        self._action = action
        self._interval_s = max(MIN_HEARTBEAT_INTERVAL_S, ttl_s / 3.0)
        self._task: asyncio.Task[None] | None = None
        self._target: asyncio.Task[Any] | None = None
        self.lost = False
        """Whether the Runtime refused to extend the lease."""

    def stop_on_loss(self, target: asyncio.Task[Any]) -> None:
        """Cancel ``target`` as soon as the Runtime says the lease is gone."""
        self._target = target

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
        """Extend the lease until cancelled or refused.

        A refusal is not a transient failure: the Runtime has already handed the
        action to somebody else, or dropped it, so finishing the work would only
        produce a report nobody is waiting for -- and, for a send, a message the
        Runtime may simultaneously be having delivered elsewhere.
        """
        while True:
            await asyncio.sleep(self._interval_s)
            if not await self._extend(self._action):
                self.lost = True
                target = self._target
                if target is not None and not target.done():
                    target.cancel()
                return
