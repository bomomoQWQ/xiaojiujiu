"""Bounded retry queue tests: capacity, dedup, retry, and give-up policy."""

from __future__ import annotations

import asyncio
import unittest

from companion_runtime.retry_queue import BoundedRetryQueue
from tests.fakes import FakeClock, RecordingLog, wait_until


class RecordingSender:
    """Sender recording deliveries, optionally failing the first N attempts."""

    def __init__(self, *, fail_times: int = 0, error: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.error = error or RuntimeError("transport down")
        self.delivered: list[str] = []
        self.attempts = 0

    async def __call__(self, item) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.error
        self.delivered.append(item.idempotency_key)


class BoundedRetryQueueTests(unittest.IsolatedAsyncioTestCase):
    def _queue(self, sender, clock, **kwargs) -> BoundedRetryQueue:
        options = {
            "max_items": 4,
            "max_attempts": 3,
            "max_age_s": 600.0,
            "base_backoff_s": 1.0,
            "max_backoff_s": 8.0,
            "send_timeout_s": 1.0,
            "random_source": lambda: 0.0,
        }
        options.update(kwargs)
        return BoundedRetryQueue(sender=sender, clock=clock, log=RecordingLog(), **options)

    async def test_delivers_queued_items(self) -> None:
        clock = FakeClock()
        sender = RecordingSender()
        queue = self._queue(sender, clock)
        self.assertTrue(queue.put({"op": "events"}, key="a"))
        self.assertEqual(len(queue), 1)
        self.assertEqual(await queue.drain_once(), 1)
        self.assertEqual(sender.delivered, ["a"])
        self.assertEqual(len(queue), 0)
        self.assertEqual(queue.stats.delivered, 1)
        self.assertEqual(queue.stats.accepted, 1)

    async def test_duplicate_key_is_ignored(self) -> None:
        clock = FakeClock()
        queue = self._queue(RecordingSender(), clock)
        self.assertTrue(queue.put({"op": "events"}, key="dup"))
        self.assertFalse(queue.put({"op": "events"}, key="dup"))
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue.stats.deduplicated, 1)

    async def test_oldest_item_is_evicted_when_full(self) -> None:
        clock = FakeClock()
        sender = RecordingSender()
        queue = self._queue(sender, clock, max_items=2)
        queue.put({"n": 1}, key="one")
        queue.put({"n": 2}, key="two")
        queue.put({"n": 3}, key="three")
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue.stats.dropped_full, 1)
        await queue.drain_once()
        self.assertEqual(sender.delivered, ["two", "three"])

    async def test_failed_delivery_is_retried_after_backoff(self) -> None:
        clock = FakeClock()
        sender = RecordingSender(fail_times=1)
        queue = self._queue(sender, clock)
        queue.put({"n": 1}, key="a")
        await queue.drain_once()
        self.assertEqual(queue.stats.retried, 1)
        self.assertEqual(queue.stats.delivered, 0)
        self.assertEqual(queue.stats.send_errors, 1)
        # Not due yet.
        self.assertEqual(await queue.drain_once(), 0)
        clock.advance(0.5)  # base_backoff (1.0) * full jitter 0.5
        await queue.drain_once()
        self.assertEqual(sender.delivered, ["a"])
        self.assertEqual(queue.stats.delivered, 1)

    async def test_item_is_given_up_after_max_attempts(self) -> None:
        clock = FakeClock()
        sender = RecordingSender(fail_times=99)
        queue = self._queue(sender, clock, max_attempts=2)
        queue.put({"n": 1}, key="poison")
        await queue.drain_once()
        clock.advance(10.0)
        await queue.drain_once()
        self.assertEqual(len(queue), 0)
        self.assertEqual(queue.stats.dropped_failed, 1)
        self.assertEqual(queue.stats.dropped(), 1)

    async def test_item_is_dropped_once_it_is_too_old(self) -> None:
        clock = FakeClock()
        sender = RecordingSender(fail_times=99)
        queue = self._queue(sender, clock, max_age_s=5.0, max_attempts=50)
        queue.put({"n": 1}, key="stale")
        await queue.drain_once()
        clock.advance(6.0)
        await queue.drain_once()
        self.assertEqual(len(queue), 0)
        self.assertEqual(queue.stats.dropped_expired, 1)

    async def test_put_never_raises_on_bad_payload(self) -> None:
        queue = self._queue(RecordingSender(), FakeClock())
        self.assertTrue(queue.put({"op": "x"}, key=""))
        self.assertEqual(len(queue), 1)

    async def test_worker_delivers_and_stops_cleanly(self) -> None:
        clock = FakeClock()
        sender = RecordingSender()
        queue = self._queue(sender, clock, base_backoff_s=0.0)
        queue.start()
        queue.put({"op": "events"}, key="worker")
        self.assertTrue(await wait_until(lambda: sender.delivered == ["worker"]))
        queue.start()  # idempotent
        await queue.stop()
        await queue.stop()  # idempotent
        self.assertEqual(queue.stats.delivered, 1)

    async def test_slow_sender_is_timed_out_and_retried(self) -> None:
        clock = FakeClock()
        attempts = {"n": 0}

        async def slow_sender(item) -> None:
            attempts["n"] += 1
            await asyncio.sleep(0.2)

        queue = self._queue(slow_sender, clock, send_timeout_s=0.05, max_attempts=2)
        queue.put({"op": "events"}, key="slow")
        await queue.drain_once()
        self.assertEqual(attempts["n"], 1)
        self.assertEqual(queue.stats.send_errors, 1)
        self.assertEqual(queue.stats.retried, 1)


if __name__ == "__main__":
    unittest.main()
