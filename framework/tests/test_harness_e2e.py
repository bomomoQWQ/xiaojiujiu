"""End-to-end tests: the framework driving the real program.

These need the program checkout and uvicorn, so they skip cleanly when it is
absent -- the pure-logic tests must still pass on a machine that has only the
framework.

Cost is dominated by booting uvicorn once, so the harness is module-scoped and
the tests share one run. That means they must not depend on each other's
ordering; each asserts its own effect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cf.clock import parse_duration, parse_when
from cf.harness import Harness, HarnessConfig
from cf.mock_openai import MockReply, MockScript
from conftest import PROGRAM_SRC, program_available

pytestmark = pytest.mark.skipif(
    not program_available(), reason="the program checkout or uvicorn is not available"
)

START = "2026-09-15T09:00:00Z"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """One booted harness shared by the module."""
    run_dir = tmp_path_factory.mktemp("framework-e2e")
    config = HarnessConfig(
        run_dir=run_dir,
        program_src=PROGRAM_SRC,
        start_time=START,
        heartbeat_interval_s=30.0,  # beats are driven explicitly, not by the clock
        # Virtual time must not drift on its own here: these tests assert exact
        # instants, and a clock running at real speed moves between the write and
        # the read. That the clock *can* run is tested in test_clock.py.
        time_scale=0.0,
        echo_logs=False,
    )
    instance = Harness(config)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


class TestWiring:
    """The program is pointed at the framework without being edited."""

    def test_strong_semantics_use_the_mock_endpoint(self, harness) -> None:
        """The provider is ``remote_api`` and it is actually usable.

        ``available()`` requires base_url, model and key together, so this also
        asserts the key was supplied -- without it the provider reports
        ``disabled``-like unavailability and no deep refresh ever happens.
        """
        status = harness.status()
        assert status["provider"] == "remote_api"
        assert status["provider_health"]["available"] is True
        assert status["provider_health"]["base_url"].startswith("http://127.0.0.1:")

    def test_epoch_is_the_declared_start_instant(self, harness) -> None:
        """The program's creation epoch is the declared start, not boot-finish time.

        The program clamps ``lazy_tick`` to be no earlier than its epoch, so if
        the epoch drifted forward by the boot duration a scenario stamping its
        opening line at ``--start-time`` would be told its own first event
        predates the world.
        """
        assert harness.epoch == parse_when(START)
        assert harness.status()["epoch"] == parse_when(START).isoformat()

    def test_program_source_is_never_written(self, tmp_path) -> None:
        """The framework's central promise: importing is not editing.

        Boots its own short-lived harness because the check is a before/after
        comparison taken across one run's lifetime -- the module-scoped fixture's
        answer only exists after its teardown, which is too late to assert on.
        """
        instance = Harness(
            HarnessConfig(
                run_dir=tmp_path / "source-check",
                program_src=PROGRAM_SRC,
                start_time=START,
                heartbeat_interval_s=30.0,
                echo_logs=False,
            )
        )
        instance.start()
        try:
            instance.program.say("你好。", conversation_id="c1", timestamp=START)
            instance.beat()
        finally:
            summary = instance.stop()
        check = summary["program_source_untouched"]
        assert check["checked"] is True
        assert check["files"] >= 20, check
        assert check["created"] == []
        assert check["changed"] == []
        assert check["deleted"] == []
        assert check["untouched"] is True


class TestStrongSemanticsRoundTrip:
    """A user message becomes an unresolved event, and a refresh resolves it."""

    def test_unresolved_backlog_accumulates(self, harness) -> None:
        """Ambiguous messages are deferred rather than guessed at."""
        harness.program.say("最近有点累，但也不知道该怎么说。", conversation_id="c1", timestamp="2026-09-15T09:00:00Z")
        harness.program.say("算了，先这样吧，回头再说。", conversation_id="c1", timestamp="2026-09-15T09:05:00Z")
        backlog = harness.program.backlog()
        assert backlog["stats"]["by_status"].get("unresolved", 0) >= 1

    def test_deep_refresh_goes_through_the_mock_and_applies(self, harness) -> None:
        """The whole point: the framework's endpoint feeds the program's cognition.

        Asserts three things that could each fail independently -- the mock was
        called, its suggestions survived the program's grounding pass
        (``violations`` empty), and at least one operation reached state.
        """
        before = harness.mock.calls_made
        # ``major_event`` is a top-level signal, and the gap must clear the
        # provider's 1h minimum interval -- the first refresh in this module
        # happens at 09:00-era time, so 12:00 is far enough.
        result = harness.program.refresh(now="2026-09-15T12:00:00Z", major_event=True)
        assert harness.mock.calls_made == before + 1, "the program never called the mock endpoint"
        assert result["ran"] is True
        assert result["provider"] == "remote_api"
        assert result["violations"] == []
        assert sum(result["applied"].values()) >= 1, result

    def test_mock_call_records_the_variables_the_program_sent(self, harness) -> None:
        """The request the program made is in the trace, with its variables."""
        calls = harness.logbook.read_trace(kinds=("mock_openai_call",))
        assert calls, "no mock call was traced"
        call = calls[-1]
        assert call["prompt_kind"] == "deep_refresh"
        assert call["auth_present"] is True
        assert "mood" in call["request"]
        assert isinstance(call["reply"], dict)


class TestTimeControl:
    """Time is the framework's to move, and moving it is visible everywhere."""

    def test_advance_moves_the_program(self, harness) -> None:
        """A clock jump changes what the program reports as now."""
        harness.clock.set(parse_when(START))
        harness.clock.advance(parse_duration("8h"))
        assert harness.clock.now() == parse_when("2026-09-15T17:00:00Z")

    def test_beat_ticks_and_logs_variables(self, harness) -> None:
        """One beat ticks the program and writes the variable block."""
        before = len(harness.logbook.read_trace(kinds=("heartbeat",)))
        variables = harness.beat()
        records = harness.logbook.read_trace(kinds=("heartbeat",))
        assert len(records) == before + 1
        record = records[-1]
        for key in (
            "mood_valence",
            "mood_arousal",
            "mood_stability",
            "impulse",
            "restraint",
            "pressure",
            "allow_proactive",
            "unfinished_open",
            "candidates_active",
            "next_wake_at",
            "next_wake_reasons",
            "quiet_hours",
        ):
            assert key in record, f"{key} missing from the heartbeat trace"
        assert variables["next_wake_at"], "the program returned no wake plan"

    def test_beat_variables_match_the_program(self, harness) -> None:
        """The logged variables are the program's, not the framework's invention.

        The clock is frozen first so nothing time-driven can move between the beat
        and the read: the program's own Scheduler also wakes on real time, and
        comparing two live readings of a running system is how a test becomes
        intermittently red for no reason.
        """
        harness.clock.freeze()
        try:
            variables = harness.beat()
            state = harness.program.state()
        finally:
            harness.clock.unfreeze()
        assert variables["mood_valence"] == pytest.approx(state["mood"]["valence"], abs=1e-6)
        assert variables["mood_arousal"] == pytest.approx(state["mood"]["arousal"], abs=1e-6)
        assert variables["pressure"] == pytest.approx(state["drive"]["pressure"], abs=1e-6)
        assert variables["impulse"] == pytest.approx(state["drive"]["approach_impulse"], abs=1e-6)
        assert variables["allow_proactive"] == state["allow_proactive"]

    def test_variables_are_in_their_documented_ranges(self, harness) -> None:
        """Every logged number is a number, in range, and parseable as a time."""
        from datetime import datetime

        variables = harness.beat()
        assert -1.0 <= variables["mood_valence"] <= 1.0
        assert -1.0 <= variables["mood_arousal"] <= 1.0
        assert 0.0 <= variables["mood_stability"] <= 1.0
        for key in ("impulse", "restraint", "pressure"):
            assert 0.0 <= variables[key] <= 1.0, key
        assert isinstance(variables["allow_proactive"], bool)
        assert isinstance(variables["contact_count_today"], int)
        assert isinstance(variables["next_wake_reasons"], list)
        datetime.fromisoformat(variables["next_wake_at"])

    def test_freeze_pins_the_program(self, harness) -> None:
        """A frozen clock does not advance the program's time."""
        harness.clock.freeze()
        first = harness.program.state()["last_tick_at"]
        harness.beat()
        assert harness.clock.frozen is True
        assert harness.program.state()["last_tick_at"] is not None
        harness.clock.unfreeze()
        assert harness.clock.frozen is False
        assert first is not None

    def test_control_plane_moves_time_over_http(self, harness) -> None:
        """The CLI's channel works: a control request changes the clock."""
        import urllib.request

        body = json.dumps({"by": "3h"}).encode("utf-8")
        request = urllib.request.Request(
            f"{harness.control.base_url}/control/time/advance",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        before = harness.clock.now()
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert harness.clock.now() == before + parse_duration("3h")

    def test_control_plane_rejects_bad_input(self, harness) -> None:
        """A malformed duration is a 400 with a reason, not a silent no-op."""
        import urllib.error
        import urllib.request

        body = json.dumps({"by": "eight hours"}).encode("utf-8")
        request = urllib.request.Request(
            f"{harness.control.base_url}/control/time/advance",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)  # noqa: S310
        assert excinfo.value.code == 400
        assert "duration" in json.loads(excinfo.value.read().decode("utf-8"))["error"]


class TestFaultInjection:
    """The endpoint can be made to fail, and the program must survive it."""

    def test_http_500_degrades_instead_of_breaking(self, harness) -> None:
        """A failing endpoint yields no strong semantics, not a broken Runtime.

        This is the program's documented fail-open contract, and it is the one
        behaviour a mock that can only succeed could never test. The exact shape
        is asserted -- ``ran: false`` with ``degraded: true`` and nothing applied
        -- because "it did not crash" would also be true of a refresh that
        silently did the wrong thing.
        """
        original = harness.mock.script
        harness.mock.script = MockScript([MockReply.http_error(500)])
        before = harness.mock.calls_made
        try:
            result = harness.program.refresh(now="2026-09-15T20:00:00Z", major_event=True)
        finally:
            harness.mock.script = original
        assert harness.mock.calls_made == before + 1, "the failing endpoint was never reached"
        assert result["ran"] is False
        assert result["degraded"] is True
        assert result["operations"] == 0
        assert result["applied"] == {}
        # And the program is still alive and answering.
        assert harness.program.health()["status"] == "ok"

    def test_malformed_reply_degrades(self, harness) -> None:
        """Non-JSON content is rejected without taking the Runtime down."""
        original = harness.mock.script
        harness.mock.script = MockScript([MockReply.malformed("这不是 JSON，只是一句话。")])
        before = harness.mock.calls_made
        try:
            result = harness.program.refresh(now="2026-09-16T04:00:00Z", major_event=True)
        finally:
            harness.mock.script = original
        assert harness.mock.calls_made == before + 1, "the endpoint was never reached"
        assert result["ran"] is False
        assert result["degraded"] is True
        assert result["operations"] == 0
        assert harness.program.health()["status"] == "ok"

    def test_degradation_is_counted_in_provider_health(self, harness) -> None:
        """The provider's own stats record the degradation, so it is observable."""
        original = harness.mock.script
        harness.mock.script = MockScript([MockReply.http_error(503)])
        try:
            harness.program.refresh(now="2026-09-16T12:00:00Z", major_event=True)
        finally:
            harness.mock.script = original
        stats = harness.status()["provider_health"]["stats"]
        assert stats["deep_refresh_degraded"] >= 1
        assert stats["deep_refresh_ok"] >= 1, "the successful refresh earlier in the module should be counted"


class TestTraceIntegrity:
    """The trace is what a human reads afterwards, so its shape is asserted."""

    def test_program_logs_are_captured(self, harness) -> None:
        """The program's own log records land in the run's trace and log file."""
        records = harness.logbook.read_trace(kinds=("program_log",))
        assert records, "the program's own log lines were not captured"
        assert all("program_message" in record for record in records)

    def test_trace_and_rolling_log_are_both_written(self, harness) -> None:
        """Both artefacts exist and are non-empty."""
        assert harness.logbook.trace_path.exists()
        assert harness.logbook.log_path.exists()
        assert harness.logbook.trace_path.stat().st_size > 0
        assert harness.logbook.log_path.stat().st_size > 0

    def test_every_record_has_routing_fields(self, harness) -> None:
        """No record can be missing the fields the CLI filters on."""
        for record in harness.logbook.read_trace():
            assert isinstance(record.get("kind"), str) and record["kind"]
            assert isinstance(record.get("seq"), int)
            assert "wall_now" in record
