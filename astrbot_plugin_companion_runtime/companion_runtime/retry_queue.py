"""Bounded, fail-open local retry queue for outbound Runtime requests.

Everything the adapter sends to the Runtime is replay-safe and idempotent thanks
to client generated ids, so it can be queued instead of awaited. The queue is the
reason a Runtime restart never blocks AstrBot:

* :meth:`BoundedRetryQueue.put` never awaits and never raises.
* The queue is bounded; when it is full the *oldest* item is evicted, because
  recent observations matter more than stale ones.
* Items are given up on after ``max_attempts`` or ``max_age_s`` so a permanently
  broken payload cannot occupy the queue forever.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .protocol import new_client_id, truncate_error

#: How often the worker re-checks whether a retry became due.
MAX_WAIT_SLICE_S = 0.5


@dataclass
class QueueItem:
    """One outbound request waiting to be delivered."""

    idempotency_key: str
    payload: dict[str, Any]
    enqueued_at: float
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""

    def describe(self) -> str:
        """Return a short identifier for logs."""
        return f"{self.idempotency_key} (attempt {self.attempts})"


@dataclass
class QueueStats:
    """Counters exposed through the status command and debug logs."""

    accepted: int = 0
    deduplicated: int = 0
    delivered: int = 0
    retried: int = 0
    dropped_full: int = 0
    dropped_failed: int = 0
    dropped_expired: int = 0
    send_errors: int = 0

    def dropped(self) -> int:
        """Total number of items the queue gave up on."""
        return self.dropped_full + self.dropped_failed + self.dropped_expired


@dataclass
class _NullLog:
    """Fallback logger so core modules never import AstrBot's logger."""

    def debug(self, *args: Any, **kwargs: Any) -> None: ...

    def info(self, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, *args: Any, **kwargs: Any) -> None: ...


NULL_LOG = _NullLog()


class BoundedRetryQueue:
    """A bounded FIFO retry queue with exponential backoff."""

    def __init__(
        self,
        *,
        sender: Callable[[QueueItem], Awaitable[None]],
        max_items: int = 256,
        max_attempts: int = 6,
        max_age_s: float = 600.0,
        base_backoff_s: float = 0.5,
        max_backoff_s: float = 30.0,
        send_timeout_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
        log: Any = NULL_LOG,
    ) -> None:
        """Create the queue.

        Args:
            sender: Coroutine delivering one item; raising schedules a retry.
            max_items: Hard capacity; the oldest item is evicted beyond it.
            max_attempts: Delivery attempts before an item is given up on.
            max_age_s: Wall age after which an undelivered item is dropped.
            base_backoff_s: First retry delay.
            max_backoff_s: Backoff ceiling.
            send_timeout_s: Hard per-item delivery timeout.
            clock: Monotonic clock source (injectable for tests).
            random_source: Jitter source in ``[0, 1)``.
            log: Logger-like object.
        """
        self._sender = sender
        self._max_items = max(1, int(max_items))
        self._max_attempts = max(1, int(max_attempts))
        self._max_age_s = max(0.0, float(max_age_s))
        self._base_backoff_s = max(0.0, float(base_backoff_s))
        self._max_backoff_s = max(self._base_backoff_s, float(max_backoff_s))
        self._send_timeout_s = max(0.05, float(send_timeout_s))
        self._clock = clock
        self._random = random_source
        self._log = log
        self._items: OrderedDict[str, QueueItem] = OrderedDict()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.stats = QueueStats()

    def __len__(self) -> int:
        return len(self._items)

    def put(self, payload: dict[str, Any], *, key: str = "") -> bool:
        """Enqueue a payload without blocking.

        Args:
            payload: JSON-serializable request description.
            key: Idempotency key; an equal key already queued is ignored.

        Returns:
            ``True`` when the item was queued, ``False`` when it was rejected as
            a duplicate. Never raises.
        """
        try:
            item_key = key or new_client_id("q")
            if item_key in self._items:
                self.stats.deduplicated += 1
                return False
            while len(self._items) >= self._max_items:
                evicted_key, evicted = self._items.popitem(last=False)
                self.stats.dropped_full += 1
                self._log.debug(
                    "retry queue full (%d); dropped oldest item %s",
                    self._max_items,
                    evicted_key,
                )
                del evicted
            self._items[item_key] = QueueItem(
                idempotency_key=item_key,
                payload=payload,
                enqueued_at=self._clock(),
            )
            self.stats.accepted += 1
            self._wakeup.set()
            return True
        except Exception:  # pragma: no cover - the queue must never break callers
            self._log.warning("retry queue rejected an item", exc_info=True)
            return False

    def start(self) -> None:
        """Start the delivery worker. Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._worker(), name="companion-runtime-retry-queue")

    async def stop(self) -> None:
        """Stop the worker. Safe to call repeatedly or after a failed start."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def drain_once(self) -> int:
        """Deliver every currently due item once.

        Returns:
            The number of items examined. Failures reschedule or drop their item
            instead of raising, so the caller always makes progress.
        """
        now = self._clock()
        handled = 0
        for key, item in list(self._items.items()):
            if item.next_attempt_at > now:
                continue
            age = now - item.enqueued_at
            if self._max_age_s and age > self._max_age_s:
                self._items.pop(key, None)
                self.stats.dropped_expired += 1
                self._log.debug("retry queue dropped expired item %s", key)
                continue
            item.attempts += 1
            try:
                await asyncio.wait_for(self._sender(item), timeout=self._send_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.send_errors += 1
                item.last_error = truncate_error(exc)
                if item.attempts >= self._max_attempts:
                    self._items.pop(key, None)
                    self.stats.dropped_failed += 1
                    self._log.warning(
                        "retry queue gave up on %s after %d attempts: %s",
                        key,
                        item.attempts,
                        item.last_error,
                    )
                else:
                    item.next_attempt_at = self._clock() + self._backoff(item.attempts)
                    self.stats.retried += 1
                    self._log.debug(
                        "retry queue will retry %s in %.1fs: %s",
                        key,
                        item.next_attempt_at - self._clock(),
                        item.last_error,
                    )
            else:
                self._items.pop(key, None)
                self.stats.delivered += 1
            handled += 1
        return handled

    def _backoff(self, attempts: int) -> float:
        """Return the jittered backoff delay after ``attempts`` failures."""
        raw = self._base_backoff_s * (2 ** max(0, attempts - 1))
        capped = min(raw, self._max_backoff_s)
        # Full jitter keeps a fleet of adapters from retrying in lockstep.
        return capped * (0.5 + 0.5 * float(self._random()))

    def _next_due_delay(self) -> float:
        """Seconds until the next item becomes due, or ``0`` when one is due now."""
        if not self._items:
            return MAX_WAIT_SLICE_S
        now = self._clock()
        soonest = min(item.next_attempt_at for item in self._items.values())
        return max(0.0, min(soonest - now, MAX_WAIT_SLICE_S))

    async def _worker(self) -> None:
        """Deliver queued items until cancelled."""
        while True:
            try:
                delay = self._next_due_delay()
                if delay > 0:
                    await self._wait_for_work(delay)
                    continue
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive: keep the worker alive
                self._log.warning("retry queue worker error", exc_info=True)
                await asyncio.sleep(MAX_WAIT_SLICE_S)

    async def _wait_for_work(self, delay: float) -> None:
        """Sleep until ``delay`` elapses or an item is enqueued."""
        self._wakeup.clear()
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            pass
