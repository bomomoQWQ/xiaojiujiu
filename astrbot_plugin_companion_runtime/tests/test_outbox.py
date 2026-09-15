"""Outbox consumer tests: leasing, render/send semantics, and safety gates."""

from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace

from companion_runtime.outbox import OutboxConsumer
from companion_runtime.protocol import (
    ACTION_RENDER,
    ACTION_SEND,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    AuthorizeDecision,
    LeasedAction,
)
from companion_runtime.settings import Settings
from tests.fakes import (
    FakeClock,
    FakeExecutor,
    FakeTransport,
    RecordingLog,
    ReportCollector,
    wait_until,
)


def _action(
    *,
    action_id: str = "act_1",
    action_type: str = ACTION_SEND,
    session: str = "webchat:FriendMessage:u1",
    attempt_id: str = "att_1",
    lease_ttl_ms: int = 30000,
    payload: dict | None = None,
) -> LeasedAction:
    return LeasedAction(
        action_id=action_id,
        action_type=action_type,
        lease_id=f"lease_{action_id}",
        session=session,
        attempt_id=attempt_id,
        lease_ttl_ms=lease_ttl_ms,
        payload=payload if payload is not None else {},
    )


class OutboxConsumerTests(unittest.IsolatedAsyncioTestCase):
    def _consumer(
        self,
        transport: FakeTransport,
        executor: FakeExecutor,
        reporter: ReportCollector | None = None,
        **overrides,
    ) -> tuple[OutboxConsumer, ReportCollector]:
        collector = reporter or ReportCollector()
        options = {
            "outbox_poll_interval_ms": 200,
            "outbox_max_actions_per_poll": 4,
            "outbox_lease_ttl_ms": 30000,
            "outbox_max_concurrency": 1,
            "render_timeout_ms": 1000,
            "send_timeout_ms": 1000,
            "request_timeout_ms": 500,
        }
        options.update(overrides)
        consumer = OutboxConsumer(
            transport=transport,
            executor=executor,
            reporter=collector,
            settings=Settings.from_mapping(options),
            clock=FakeClock(),
            sleep=_no_sleep,
            log=RecordingLog(),
        )
        return consumer, collector

    # -- render ------------------------------------------------------------

    async def test_render_reports_the_rendered_text(self) -> None:
        action = _action(action_type=ACTION_RENDER, payload={"prompt": "compose"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(render_text="今晚想问你面试怎么样")
        consumer, collector = self._consumer(transport, executor)

        self.assertEqual(await consumer.poll_once(), 1)

        self.assertEqual(len(executor.render_calls), 1)
        self.assertEqual(collector.reports[0].status, STATUS_OK)
        self.assertEqual(collector.reports[0].result["text"], "今晚想问你面试怎么样")
        self.assertEqual(collector.reports[0].action_type, ACTION_RENDER)
        self.assertEqual(consumer.stats.rendered, 1)
        self.assertEqual(transport.lease_requests[0].max_actions, 4)

    async def test_render_failure_is_reported_as_failed(self) -> None:
        action = _action(action_type=ACTION_RENDER, payload={"prompt": "compose"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(render_error=RuntimeError("no provider"))
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        report = collector.reports[0]
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("no provider", report.error)
        self.assertEqual(consumer.stats.failed, 1)

    async def test_render_timeout_is_bounded(self) -> None:
        action = _action(action_type=ACTION_RENDER, payload={"prompt": "compose"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(render_delay_s=0.5)
        collector = ReportCollector()
        # ``render_timeout_ms`` is clamped to >= 1s by Settings, so shorten the
        # already-normalized value here to keep the test fast.
        settings = replace(
            Settings.from_mapping({"render_timeout_ms": 1000}),
            render_timeout_s=0.05,
        )
        consumer = OutboxConsumer(
            transport=transport,
            executor=executor,
            reporter=collector,
            settings=settings,
            clock=FakeClock(),
            sleep=_no_sleep,
            log=RecordingLog(),
        )

        await consumer.poll_once()

        self.assertEqual(collector.reports[0].status, STATUS_FAILED)
        self.assertIn("timed out", collector.reports[0].error)

    async def test_empty_render_result_is_a_failure(self) -> None:
        action = _action(action_type=ACTION_RENDER, payload={"prompt": "compose"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(render_text="   ")
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(collector.reports[0].status, STATUS_FAILED)
        self.assertIn("no text", collector.reports[0].error)

    # -- send --------------------------------------------------------------

    async def test_authorized_send_is_delivered_and_reported(self) -> None:
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(len(transport.authorize_requests), 1)
        self.assertEqual(executor.send_calls[0][1], "在忙吗")
        report = collector.reports[0]
        self.assertEqual(report.status, STATUS_OK)
        self.assertTrue(report.result["sent"])
        self.assertEqual(consumer.stats.sent, 1)

    async def test_unauthorized_send_is_never_executed(self) -> None:
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(
            actions=[action],
            authorize=AuthorizeDecision(authorized=False, reason="resolved_by_user"),
        )
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls, [])
        report = collector.reports[0]
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertEqual(report.error, "resolved_by_user")
        self.assertEqual(consumer.stats.rejected, 1)
        # An explicit verdict must stay distinguishable from an outage.
        self.assertNotIn("authorize_unavailable", report.result)

    async def test_unavailable_authorize_leaves_the_action_retryable(self) -> None:
        """An outage is not a verdict, and the Runtime offers only one retry shape.

        Runtime contract this test mirrors, checked against the real Runtime
        (``api_v1._apply_action_report`` + ``Projections.claim`` /
        ``reclaim_expired``): reporting *any* non-``ok`` send result -- ``rejected``
        and ``failed`` alike -- drives ``mark_delivered(success=False)`` ->
        ``nack(terminal=True)``, after which the row is never handed out again.
        A row that is simply never reported goes ``leased`` -> (lease expiry) ->
        ``pending`` -> leased again with a fresh ``lease_id`` and ``attempts + 1``.

        So silence is the only shape of "retry this later" the contract has, and
        the adapter must not substitute a verdict-shaped result for it: doing so
        permanently drops a proactive message the Runtime was never asked about.
        """
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(
            actions=[action],
            authorize_error=RuntimeError("runtime unreachable"),
        )
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls, [], "fail-closed: nothing is sent")
        self.assertEqual(collector.reports, [], "no terminal outcome may be reported")
        self.assertEqual(transport.report_bodies, [])
        self.assertEqual(consumer.stats.deferred, 1)
        self.assertEqual(consumer.stats.authorize_errors, 1)
        self.assertEqual(consumer.stats.rejected, 0, "the Runtime never refused it")
        self.assertEqual(consumer.stats.failed, 0, "no outcome was recorded at all")
        self.assertFalse(
            consumer._completed,
            "a deferred action must not be cached as finished, or the Runtime's "
            "re-lease would replay silence instead of retrying",
        )
        # The silence has to be visible to an operator somewhere.
        self.assertTrue(
            any("authorize_unavailable" in text for text in consumer._log.levels("warning")),
        )

        # The Runtime reclaims the expired lease and hands the same attempt out
        # again with a new lease id; this time it can be asked for a verdict.
        transport.actions = [replace(action, lease_id="lease_act_1_2")]
        transport.authorize_error = None
        await consumer.poll_once()

        self.assertEqual(len(executor.send_calls), 1)
        self.assertEqual(collector.reports[0].status, STATUS_OK)
        self.assertEqual(collector.reports[0].lease_id, "lease_act_1_2")

    async def test_authorize_timeout_is_deferred_too(self) -> None:
        """A deadline miss is the same situation as an error: no verdict arrived."""
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(
            actions=[action],
            authorize_error=asyncio.TimeoutError(),
        )
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls, [])
        self.assertEqual(collector.reports, [])
        self.assertEqual(consumer.stats.deferred, 1)
        self.assertEqual(consumer.stats.authorize_errors, 1)

    async def test_authorization_can_amend_the_text(self) -> None:
        action = _action(payload={"text": "原措辞"})
        transport = FakeTransport(
            actions=[action],
            authorize=AuthorizeDecision(authorized=True, text="改口后的措辞"),
        )
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls[0][1], "改口后的措辞")
        self.assertEqual(collector.reports[0].status, STATUS_OK)

    async def test_delivery_without_a_matching_platform_is_a_failure(self) -> None:
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(send_result={"sent": False})
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        report = collector.reports[0]
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertEqual(report.error, "delivery_failed")
        self.assertEqual(consumer.stats.failed, 1)

    async def test_send_exception_is_reported_as_failed(self) -> None:
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(send_error=RuntimeError("platform exploded"))
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(collector.reports[0].status, STATUS_FAILED)
        self.assertIn("platform exploded", collector.reports[0].error)

    async def test_authorize_request_carries_a_text_digest(self) -> None:
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(actions=[action])
        consumer, _ = self._consumer(transport, FakeExecutor())

        await consumer.poll_once()

        request = transport.authorize_requests[0]
        self.assertEqual(len(request.text_sha256), 64)
        self.assertEqual(request.lease_id, action.lease_id)
        self.assertEqual(request.attempt_id, "att_1")

    # -- guard rails -------------------------------------------------------

    async def test_send_without_text_is_rejected_locally(self) -> None:
        action = _action(payload={})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls, [])
        self.assertEqual(transport.authorize_requests, [])
        self.assertEqual(collector.reports[0].status, STATUS_FAILED)
        self.assertIn("no text", collector.reports[0].error)

    async def test_missing_session_is_skipped(self) -> None:
        action = _action(session="", payload={"text": "hi"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls, [])
        self.assertEqual(collector.reports[0].status, STATUS_SKIPPED)
        self.assertEqual(collector.reports[0].error, "missing_session")
        self.assertEqual(consumer.stats.skipped, 1)

    async def test_unsupported_action_type_is_skipped(self) -> None:
        action = _action(action_type="teleport", payload={"text": "hi"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls, [])
        self.assertEqual(collector.reports[0].status, STATUS_SKIPPED)
        self.assertIn("unsupported_action_type", collector.reports[0].error)

    async def test_repeated_lease_replays_instead_of_resending(self) -> None:
        action = _action(payload={"text": "只发一次"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()
        # The lease expired before the Runtime saw the report, so it re-leases
        # the very same attempt.
        transport.actions = [action]
        await consumer.poll_once()

        self.assertEqual(len(executor.send_calls), 1)
        self.assertEqual(len(collector.reports), 2)
        self.assertEqual(consumer.stats.replayed, 1)

    async def test_duplicate_lease_is_not_executed_twice_in_one_batch(self) -> None:
        action = _action(payload={"text": "只发一次"})
        transport = FakeTransport(actions=[action, action])
        executor = FakeExecutor(send_delay_s=0.2)
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(len(executor.send_calls), 1)
        self.assertEqual(len(collector.reports), 1)
        self.assertEqual(consumer.stats.duplicate_inflight, 1)

    async def test_a_new_attempt_is_executed_again(self) -> None:
        first = _action(attempt_id="att_1", payload={"text": "第一次"})
        second = _action(attempt_id="att_2", payload={"text": "重试后"})
        transport = FakeTransport(actions=[first])
        executor = FakeExecutor()
        consumer, _ = self._consumer(transport, executor)

        await consumer.poll_once()
        transport.actions = [second]
        await consumer.poll_once()

        self.assertEqual(len(executor.send_calls), 2)
        self.assertEqual(executor.send_calls[1][1], "重试后")

    # -- reporting and polling --------------------------------------------

    async def test_report_is_deferred_when_the_sink_fails(self) -> None:
        action = _action(payload={"text": "hi"})
        transport = FakeTransport(actions=[action])
        collector = ReportCollector(fail_times=1)
        consumer, _ = self._consumer(transport, FakeExecutor(), collector)

        await consumer.poll_once()

        self.assertEqual(collector.attempts, 1)
        self.assertEqual(collector.reports, [])

    async def test_empty_lease_returns_zero(self) -> None:
        transport = FakeTransport(actions=[])
        consumer, _ = self._consumer(transport, FakeExecutor())
        self.assertEqual(await consumer.poll_once(), 0)
        self.assertEqual(consumer.stats.polls, 1)

    async def test_render_heartbeat_extends_a_long_lease(self) -> None:
        action = _action(
            action_type=ACTION_RENDER,
            lease_ttl_ms=1000,
            payload={"prompt": "slow"},
        )
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(render_delay_s=1.2)
        consumer, collector = self._consumer(transport, executor, render_timeout_ms=3000)

        await consumer.poll_once()

        self.assertEqual(collector.reports[0].status, STATUS_OK)
        self.assertGreaterEqual(len(transport.heartbeat_requests), 1)
        self.assertEqual(consumer.stats.heartbeats, len(transport.heartbeat_requests))

    async def test_lease_is_heartbeated_while_waiting_behind_the_semaphore(self) -> None:
        """An action waiting for a concurrency slot must already hold its lease.

        With the default concurrency of 1, the second action of a batch can wait a
        whole render for its turn. Leasing without heartbeating during that wait
        lets the lease expire, and the Runtime then hands the same action to a
        second worker -- one proactive message delivered twice.
        """
        first = _action(
            action_id="act_1",
            action_type=ACTION_RENDER,
            attempt_id="att_1",
            lease_ttl_ms=1000,
            payload={"prompt": "slow"},
        )
        second = _action(
            action_id="act_2",
            attempt_id="att_2",
            lease_ttl_ms=1000,
            payload={"text": "在忙吗"},
        )
        transport = FakeTransport(actions=[first, second])
        executor = FakeExecutor(render_delay_s=1.2)
        consumer, collector = self._consumer(transport, executor, render_timeout_ms=3000)

        await consumer.poll_once()

        waiting = [r for r in transport.heartbeat_requests if r.action_id == "act_2"]
        self.assertTrue(waiting, "the waiting action never heartbeated its lease")
        # Report order follows completion order, so only the statuses are pinned.
        self.assertEqual([report.status for report in collector.reports], [STATUS_OK] * 2)
        self.assertEqual(consumer.stats.sent, 1)
        self.assertEqual(consumer.stats.rendered, 1)

    async def test_refused_heartbeat_stops_the_work_and_is_not_counted_successful(self) -> None:
        """``extended=false`` means the lease is gone, so the work must stop.

        The Runtime refuses an extension when the action was re-leased, expired,
        or dropped, and it has already recorded that decision itself. Reporting a
        terminal ``failed`` here would close a row the Runtime intends to
        re-dispatch (an expired lease goes back to ``pending``), so the adapter
        stops, stays silent, and counts the refusal instead of a success.
        """
        action = _action(
            action_type=ACTION_RENDER,
            lease_ttl_ms=1000,
            payload={"prompt": "slow"},
        )
        transport = FakeTransport(actions=[action], heartbeat_extended=False)
        executor = FakeExecutor(render_delay_s=8.0)
        consumer, collector = self._consumer(transport, executor, render_timeout_ms=4000)

        started = time.monotonic()
        await consumer.poll_once()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0, "the refused lease must stop the render outright")
        self.assertEqual(collector.reports, [], "a lost lease is the Runtime's to record")
        self.assertEqual(consumer.stats.heartbeats, 0, "a refusal is not a success")
        self.assertEqual(consumer.stats.heartbeats_lost, 1)
        self.assertEqual(consumer.stats.deferred, 1)
        self.assertEqual(consumer.stats.rendered, 0)
        self.assertFalse(consumer._completed, "the action must stay retryable")
        self.assertTrue(
            any("no longer holds its lease" in text for text in consumer._log.levels("warning")),
        )

        # The Runtime re-leases the same attempt with a new lease id; the adapter
        # now holds a live lease again and finishes the render.
        transport.actions = [replace(action, lease_id="lease_act_1_2")]
        transport.heartbeat_extended = True
        executor.render_delay_s = 0.0
        await consumer.poll_once()

        self.assertEqual(collector.reports[0].status, STATUS_OK)
        self.assertEqual(consumer.stats.rendered, 1)

    async def test_transient_heartbeat_error_does_not_stop_the_work(self) -> None:
        """An unreachable Runtime is not a lost lease: the render keeps going."""
        action = _action(
            action_type=ACTION_RENDER,
            lease_ttl_ms=1000,
            payload={"prompt": "slow"},
        )
        transport = FakeTransport(actions=[action], heartbeat_error=RuntimeError("blip"))
        executor = FakeExecutor(render_delay_s=1.2)
        consumer, collector = self._consumer(transport, executor, render_timeout_ms=3000)

        await consumer.poll_once()

        self.assertEqual(collector.reports[0].status, STATUS_OK)
        self.assertEqual(consumer.stats.heartbeats, 0)
        self.assertEqual(consumer.stats.heartbeats_lost, 0)
        self.assertGreaterEqual(consumer.stats.heartbeat_errors, 1)

    async def test_lease_lost_mid_delivery_still_reports_the_delivery(self) -> None:
        """Silence is only safe while nothing has been delivered.

        If the lease disappears *after* the Runtime authorized the send, the
        message may already be on its way out. Staying silent there would let the
        Runtime hand the action out again and deliver the same message twice, so
        the delivery is allowed to settle and its real outcome is reported.
        """
        action = _action(payload={"text": "在忙吗"}, lease_ttl_ms=1000)
        transport = FakeTransport(actions=[action], heartbeat_extended=False)
        executor = FakeExecutor(send_delay_s=1.4)
        consumer, collector = self._consumer(transport, executor, send_timeout_ms=5000)

        await consumer.poll_once()

        self.assertEqual(len(executor.send_calls), 1)
        self.assertEqual(consumer.stats.heartbeats_lost, 1)
        self.assertEqual(consumer.stats.deferred, 0, "a delivery that happened must be reported")
        self.assertEqual(collector.reports[0].status, STATUS_OK)
        self.assertTrue(collector.reports[0].result["sent"])
        self.assertEqual(consumer.stats.sent, 1)

    async def test_cancelling_an_authorized_send_does_not_lose_the_report(self) -> None:
        """Cancellation after authorization must not orphan an irreversible send.

        The message is already on its way out when the Runtime authorizes it, so
        dropping the work at that point would leave a delivery the Runtime never
        hears about -- and it would hand the same action out again.
        """
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(send_delay_s=0.2)
        consumer, collector = self._consumer(transport, executor)

        polling = asyncio.ensure_future(consumer.poll_once())
        self.assertTrue(await wait_until(lambda: bool(executor.send_calls)))
        polling.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await polling

        self.assertTrue(await wait_until(lambda: bool(collector.reports), timeout_s=2.0))
        self.assertEqual(len(executor.send_calls), 1)
        self.assertEqual(collector.reports[0].status, STATUS_OK)
        self.assertTrue(collector.reports[0].result["sent"])

    async def test_request_stop_leases_nothing_new_and_reports_idleness(self) -> None:
        transport = FakeTransport(actions=[_action(payload={"text": "hi"})])
        consumer, collector = self._consumer(transport, FakeExecutor())

        consumer.request_stop()

        self.assertEqual(await consumer.poll_once(), 0)
        self.assertEqual(transport.lease_requests, [])
        self.assertEqual(collector.reports, [])
        self.assertTrue(await consumer.wait_idle(0.1))

    async def test_wait_idle_returns_false_while_an_action_is_still_running(self) -> None:
        action = _action(payload={"text": "hi"})
        transport = FakeTransport(actions=[action])
        executor = FakeExecutor(send_delay_s=0.3)
        consumer, _ = self._consumer(transport, executor)

        polling = asyncio.ensure_future(consumer.poll_once())
        self.assertTrue(await wait_until(lambda: bool(executor.send_calls)))

        self.assertFalse(await consumer.wait_idle(0.05))
        self.assertTrue(await consumer.wait_idle(2.0))
        await polling

    async def test_run_backs_off_after_lease_errors(self) -> None:
        transport = FakeTransport(lease_error=RuntimeError("runtime down"))
        consumer, _ = self._consumer(transport, FakeExecutor())
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)
            if len(sleeps) >= 4:
                raise _StopLoop

        consumer._sleep = sleep
        with self.assertRaises(_StopLoop):
            await consumer.run()

        self.assertEqual(consumer.stats.poll_errors, 4)
        self.assertEqual(sleeps, sorted(sleeps))
        self.assertGreater(sleeps[-1], sleeps[0])


class _StopLoop(Exception):
    """Sentinel used to break out of ``OutboxConsumer.run`` in tests."""


async def _no_sleep(delay: float) -> None:
    """Sleep stand-in so tests never wait on the poll interval."""
    del delay


if __name__ == "__main__":
    unittest.main()
