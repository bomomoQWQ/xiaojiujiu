"""A round whose render failed terminally must be settled, not left pending forever."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from companion_runtime import api_v1

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
SCOPE = "fixture:user:abandoned"


class FakeOutbox:
    def __init__(self, status: str) -> None:
        self.status = status
        self.asked: list[str] = []

    def get(self, outbox_id: str):
        self.asked.append(outbox_id)
        return SimpleNamespace(status=self.status)


class FakeCommitRepository:
    def __init__(self, terminal_ack_kind) -> None:
        self.terminal_ack_kind = terminal_ack_kind
        self.asked: list[str] = []

    def get(self, round_id: str):
        self.asked.append(round_id)
        return SimpleNamespace(terminal_ack_kind=self.terminal_ack_kind)


class FakeRunner:
    def __init__(self, terminal_ack_kind=None) -> None:
        self.repository = FakeCommitRepository(terminal_ack_kind)
        self.acks: list[object] = []

    def after_legacy_send_ack(self, ack, *, confirmed=True):
        self.acks.append((ack, confirmed))
        return ()


def row(engine: str = "langchao"):
    payload = {
        "engine": engine,
        "decision_id": "round:abandoned",
        "attempt_id": "attempt:1",
        "action": {"type": "expression"},
    }
    return SimpleNamespace(outbox_id="outbox:render", payload=payload)


class FakeAttempts:
    def get(self, attempt_id: str):
        return None


def runtime_with(*, status: str, runner: FakeRunner):
    outbox = FakeOutbox(status)
    return SimpleNamespace(
        projections=SimpleNamespace(outbox=outbox, attempts=FakeAttempts()),
        langchao_live_runner=runner,
        v2_coordinator=None,
    )


def test_a_terminally_failed_render_settles_its_round() -> None:
    runner = FakeRunner()
    runtime = runtime_with(status="failed", runner=runner)

    api_v1._settle_abandoned_round(runtime, row=row(), now=NOW)

    assert len(runner.acks) == 1, "the round must be closed as a non-delivery"
    ack, confirmed = runner.acks[0]
    assert ack.sent is False
    assert confirmed is True
    assert ack.decision_id == "round:abandoned"
    assert ack.attempt_id == "attempt:1"
    assert ack.send_outbox_id == "outbox:render"


def test_a_retryable_render_failure_leaves_the_round_open() -> None:
    """A row still queued may yet deliver the message, so nothing is settled."""
    runner = FakeRunner()
    runtime = runtime_with(status="pending", runner=runner)

    api_v1._settle_abandoned_round(runtime, row=row(), now=NOW)

    assert runner.acks == []
    assert runner.repository.asked == [], "no need to even read the commit"


def test_an_already_acknowledged_round_is_not_settled_twice() -> None:
    runner = FakeRunner(terminal_ack_kind="sent")
    runtime = runtime_with(status="failed", runner=runner)

    api_v1._settle_abandoned_round(runtime, row=row(), now=NOW)

    assert runner.acks == [], "a settled round must not be reopened as a non-delivery"
    assert runner.repository.asked == ["round:abandoned"]


def test_another_engines_render_failure_is_left_to_that_engine() -> None:
    runner = FakeRunner()
    runtime = runtime_with(status="failed", runner=runner)

    api_v1._settle_abandoned_round(runtime, row=row(engine="runtime_v2"), now=NOW)

    assert runner.acks == []
