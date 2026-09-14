"""In-memory fakes used by the adapter tests.

Deliberately hand written instead of ``unittest.mock`` so that every interaction
the consumer depends on is explicit and reviewable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from companion_runtime.protocol import (
    ActionReport,
    AuthorizeDecision,
    ContextSnapshot,
    LeasedAction,
)


class FakeClock:
    """Controllable monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class RecordingLog:
    """Logger-like recorder capturing ``(level, message)`` pairs."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        text = message % args if args else message
        self.records.append((level, text))

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("debug", message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("info", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("warning", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("error", message, *args, **kwargs)

    def levels(self, level: str) -> list[str]:
        """Return every message recorded at ``level``."""
        return [text for recorded_level, text in self.records if recorded_level == level]


class FakeTransport:
    """In-memory :class:`RuntimeTransport` with configurable failures."""

    def __init__(
        self,
        *,
        snapshot: ContextSnapshot | None = None,
        actions: list[LeasedAction] | None = None,
        authorize: AuthorizeDecision | None = None,
        context_delay_s: float = 0.0,
        context_error: Exception | None = None,
        lease_error: Exception | None = None,
        authorize_error: Exception | None = None,
        heartbeat_error: Exception | None = None,
        report_error: Exception | None = None,
        health: dict[str, Any] | None = None,
        health_error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.actions = list(actions or [])
        self.authorize_decision = authorize or AuthorizeDecision(authorized=True)
        self.context_delay_s = context_delay_s
        self.context_error = context_error
        self.lease_error = lease_error
        self.authorize_error = authorize_error
        self.heartbeat_error = heartbeat_error
        self.report_error = report_error
        #: Advisory health payload used by the status command. ``None`` means the
        #: Runtime either predates patch v0.2 or is unreachable. The default
        #: mirrors the real Runtime's field names so a stub can never hide a
        #: protocol mismatch behind a differently-named key.
        self.health = health if health is not None else {
            "semantic_provider": {"provider": "disabled", "available": False},
            "semantics": {"unresolved": 0},
        }
        self.health_error = health_error

        self.event_bodies: list[dict[str, Any]] = []
        self.context_requests: list[Any] = []
        self.lease_requests: list[Any] = []
        self.heartbeat_requests: list[Any] = []
        self.authorize_requests: list[Any] = []
        self.report_bodies: list[dict[str, Any]] = []
        self.health_calls = 0
        self.context_calls = 0
        self.closed = False

    async def fetch_health(self, *, timeout_s: float) -> dict[str, Any] | None:
        """Return the configured advisory health payload, or ``None``."""
        self.health_calls += 1
        if self.health_error is not None:
            raise self.health_error
        return self.health

    async def post_events(self, body: dict[str, Any], *, timeout_s: float) -> None:
        self.event_bodies.append(body)

    async def fetch_context(self, request: Any, *, timeout_s: float) -> ContextSnapshot | None:
        self.context_calls += 1
        self.context_requests.append(request)
        if self.context_delay_s:
            await asyncio.sleep(self.context_delay_s)
        if self.context_error is not None:
            raise self.context_error
        return self.snapshot

    async def lease_actions(self, request: Any, *, timeout_s: float) -> list[LeasedAction]:
        self.lease_requests.append(request)
        if self.lease_error is not None:
            raise self.lease_error
        leased, self.actions = self.actions, []
        return leased

    async def heartbeat_lease(self, request: Any, *, timeout_s: float) -> bool:
        self.heartbeat_requests.append(request)
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return True

    async def authorize_action(self, request: Any, *, timeout_s: float) -> AuthorizeDecision:
        self.authorize_requests.append(request)
        if self.authorize_error is not None:
            raise self.authorize_error
        return self.authorize_decision

    async def report_action(self, body: dict[str, Any], *, timeout_s: float) -> None:
        if self.report_error is not None:
            raise self.report_error
        self.report_bodies.append(body)

    async def aclose(self) -> None:
        self.closed = True


class FakeExecutor:
    """In-memory action executor with configurable results and failures."""

    def __init__(
        self,
        *,
        render_text: str = "rendered text",
        render_error: Exception | None = None,
        render_delay_s: float = 0.0,
        render_result: dict[str, Any] | None = None,
        send_result: dict[str, Any] | None = None,
        send_error: Exception | None = None,
        send_delay_s: float = 0.0,
    ) -> None:
        self.render_text = render_text
        self.render_error = render_error
        self.render_delay_s = render_delay_s
        self.render_result = render_result
        self.send_result = send_result if send_result is not None else {"sent": True}
        self.send_error = send_error
        self.send_delay_s = send_delay_s
        self.render_calls: list[LeasedAction] = []
        self.send_calls: list[tuple[LeasedAction, str]] = []

    async def render(self, action: LeasedAction) -> dict[str, Any]:
        self.render_calls.append(action)
        if self.render_delay_s:
            await asyncio.sleep(self.render_delay_s)
        if self.render_error is not None:
            raise self.render_error
        if self.render_result is not None:
            return dict(self.render_result)
        return {"text": self.render_text}

    async def send(self, action: LeasedAction, text: str) -> dict[str, Any]:
        self.send_calls.append((action, text))
        if self.send_delay_s:
            await asyncio.sleep(self.send_delay_s)
        if self.send_error is not None:
            raise self.send_error
        return dict(self.send_result)


class ReportCollector:
    """Reporter callable that records reports, optionally failing first."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.reports: list[ActionReport] = []
        self.attempts = 0

    async def __call__(self, report: ActionReport) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("report sink unavailable")
        self.reports.append(report)


async def wait_until(predicate: Any, *, timeout_s: float = 1.0) -> bool:
    """Poll ``predicate`` until it is true or the timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return bool(predicate())
