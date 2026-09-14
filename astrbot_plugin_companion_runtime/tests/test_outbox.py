"""Outbox consumer tests: leasing, render/send semantics, and safety gates."""

from __future__ import annotations

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

    async def test_authorize_transport_failure_fails_closed(self) -> None:
        action = _action(payload={"text": "在忙吗"})
        transport = FakeTransport(
            actions=[action],
            authorize_error=RuntimeError("runtime unreachable"),
        )
        executor = FakeExecutor()
        consumer, collector = self._consumer(transport, executor)

        await consumer.poll_once()

        self.assertEqual(executor.send_calls, [])
        report = collector.reports[0]
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertIn("authorize_unavailable", report.error)
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
