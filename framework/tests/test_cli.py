"""Acceptance tests for the command line itself.

These drive ``cf`` as a real subprocess pair -- one ``cf run`` in the background,
then client commands against it -- because that is the workflow the framework
exists to support and the one thing unit tests cannot prove. A control plane that
works when called in-process but not through the CLI would satisfy every other
test in this suite and still be useless.

They are the slowest tests here (a full boot per test), so they are kept few and
focused on the contract: the CLI can move a *running* harness's clock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import FRAMEWORK_ROOT, PROGRAM_SRC, program_available

pytestmark = pytest.mark.skipif(
    not program_available(), reason="the program checkout or uvicorn is not available"
)

START = "2026-09-15T09:00:00Z"
BOOT_TIMEOUT = 60.0


def _env() -> dict[str, str]:
    """Environment for a subprocess that must import ``cf`` from the repo root."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{FRAMEWORK_ROOT}{os.pathsep}{existing}" if existing else str(FRAMEWORK_ROOT)
    return env


def _cf(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one ``cf`` client command."""
    return subprocess.run(
        [sys.executable, "-m", "cf", *args],
        cwd=FRAMEWORK_ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )


def _wait_ready(run_dir: Path, process: subprocess.Popen[str], timeout: float = BOOT_TIMEOUT) -> dict:
    """Wait until the trace names a control plane, then return that record."""
    trace = run_dir / "trace.jsonl"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(f"cf run exited early ({process.returncode})\n{stdout}\n{stderr}")
        if trace.exists():
            for line in trace.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("kind") == "harness_ready":
                    return record
        time.sleep(0.2)
    raise AssertionError(f"the harness never became ready; trace={trace.exists()}")


@pytest.fixture(scope="module")
def running_harness(tmp_path_factory):
    """A ``cf run`` subprocess, shut down through the CLI at the end."""
    run_dir = tmp_path_factory.mktemp("cf-cli")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cf",
            "run",
            "--run-dir",
            str(run_dir),
            "--program-src",
            str(PROGRAM_SRC),
            "--start-time",
            START,
            "--time-scale",
            "0",
            "--heartbeat-interval",
            "30",
            "--quiet",
        ],
        cwd=FRAMEWORK_ROOT,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    record = _wait_ready(run_dir, process)
    try:
        yield run_dir, record
    finally:
        if process.poll() is None:
            _cf("shutdown", "--run-dir", str(run_dir), check=False)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


class TestRunCommand:
    """``cf run`` boots everything and reports where it is."""

    def test_readiness_record_names_every_surface(self, running_harness) -> None:
        """The trace records the runtime, control and mock URLs and the provider."""
        _, record = running_harness
        assert record["base_url"].startswith("http://127.0.0.1:")
        assert record["control_url"].startswith("http://127.0.0.1:")
        assert record["mock_url"].startswith("http://127.0.0.1:")
        assert record["provider"] == "remote_api"

    def test_run_prints_the_same_urls(self, tmp_path) -> None:
        """The foreground banner tells the operator where things are.

        Boots a second harness because the banner is printed by the process that
        is still running under the shared fixture -- its stdout cannot be read
        until it exits.
        """
        run_dir = tmp_path / "banner"
        process = subprocess.Popen(
            [
                sys.executable, "-m", "cf", "run",
                "--run-dir", str(run_dir),
                "--program-src", str(PROGRAM_SRC),
                "--start-time", START,
                "--time-scale", "0",
                "--heartbeat-interval", "30",
                "--quiet",
            ],
            cwd=FRAMEWORK_ROOT,
            env=_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_ready(run_dir, process)
            _cf("shutdown", "--run-dir", str(run_dir))
            stdout, stderr = process.communicate(timeout=45)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        assert process.returncode == 0, stderr
        assert "control     : http://127.0.0.1:" in stdout
        assert "mock openai : http://127.0.0.1:" in stdout
        assert "Ctrl-C to stop." in stdout
        assert "stopped: beats=" in stdout


class TestTimeControl:
    """The headline capability: move a running harness's clock from the shell."""

    def test_status_reports_the_clock(self, running_harness) -> None:
        """``cf status`` reads the clock over HTTP."""
        run_dir, _ = running_harness
        result = _cf("status", "--run-dir", str(run_dir))
        assert START.replace("+00:00", "")[:19] in result.stdout
        assert "scale=0.0" in result.stdout
        assert "frozen=False" in result.stdout

    def test_advance_moves_the_running_clock(self, running_harness) -> None:
        """``cf time advance`` shifts the live harness, and the shift sticks."""
        run_dir, _ = running_harness
        _cf("time", "set", START, "--run-dir", str(run_dir))
        result = _cf("time", "advance", "8h", "--run-dir", str(run_dir))
        assert "2026-09-15T17:00:00" in result.stdout
        # A second call must see the moved clock, not the original one.
        again = _cf("status", "--run-dir", str(run_dir))
        assert "2026-09-15T17:00:00" in again.stdout

    def test_set_is_absolute(self, running_harness) -> None:
        """``cf time set`` jumps to the given instant."""
        run_dir, _ = running_harness
        result = _cf("time", "set", "2027-01-02T03:04:05Z", "--run-dir", str(run_dir))
        assert "2027-01-02T03:04:05" in result.stdout

    def test_freeze_and_unfreeze(self, running_harness) -> None:
        """``cf time freeze`` pins the clock and ``unfreeze`` releases it."""
        run_dir, _ = running_harness
        frozen = _cf("time", "freeze", "--run-dir", str(run_dir))
        assert '"frozen": true' in frozen.stdout
        status = _cf("status", "--run-dir", str(run_dir))
        assert "frozen=True" in status.stdout
        thawed = _cf("time", "unfreeze", "--run-dir", str(run_dir))
        assert '"frozen": false' in thawed.stdout

    def test_scale_changes_speed(self, running_harness) -> None:
        """``cf time scale`` sets virtual seconds per real second."""
        run_dir, _ = running_harness
        result = _cf("time", "scale", "60", "--run-dir", str(run_dir))
        assert '"scale": 60.0' in result.stdout
        _cf("time", "scale", "0", "--run-dir", str(run_dir))

    def test_bad_duration_exits_nonzero_with_a_reason(self, running_harness) -> None:
        """A typo is refused loudly rather than silently doing nothing."""
        run_dir, _ = running_harness
        result = _cf("time", "advance", "eight hours", "--run-dir", str(run_dir), check=False)
        assert result.returncode != 0
        assert "duration" in (result.stdout + result.stderr)

    def test_bad_time_exits_nonzero(self, running_harness) -> None:
        """A time that cannot be parsed is refused."""
        run_dir, _ = running_harness
        result = _cf("time", "set", "the day before yesterday", "--run-dir", str(run_dir), check=False)
        assert result.returncode != 0

    def test_missing_target_explains_itself(self) -> None:
        """With neither ``--control`` nor ``--run-dir``, the CLI says what to pass."""
        result = _cf("status", check=False)
        assert result.returncode != 0
        assert "--control" in (result.stdout + result.stderr)


class TestTicking:
    """The heartbeat is the thing time control acts on."""

    def test_tick_runs_a_beat_and_prints_variables(self, running_harness) -> None:
        """``cf tick`` records one heartbeat and shows the variables it saw."""
        run_dir, _ = running_harness
        _cf("time", "set", START, "--run-dir", str(run_dir))
        result = _cf("tick", "--by", "1h", "--run-dir", str(run_dir))
        assert "2026-09-15T10:00:00" in result.stdout
        for key in ("mood_valence", "pressure", "allow_proactive", "next_wake_at"):
            assert key in result.stdout, key

    def test_tick_is_recorded_in_the_trace(self, running_harness) -> None:
        """The beat reached the trace file, not just the terminal."""
        run_dir, _ = running_harness
        _cf("tick", "--run-dir", str(run_dir))
        time.sleep(0.5)
        trace = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        beats = [record for record in trace if record["kind"] == "heartbeat"]
        assert beats
        assert "pressure" in beats[-1]
        assert beats[-1]["virtual_now"]

    def test_endogenous_round_is_forced(self, running_harness) -> None:
        """``cf endogenous`` forces a decision round and prints its outcome."""
        run_dir, _ = running_harness
        result = _cf("endogenous", "--run-dir", str(run_dir))
        assert '"decision"' in result.stdout or '"acted"' in result.stdout or "{" in result.stdout


class TestTail:
    """The trace is the artefact a human reads afterwards."""

    def test_tail_prints_records(self, running_harness) -> None:
        """``cf tail`` renders the trace as readable lines."""
        run_dir, _ = running_harness
        result = _cf("tail", "--run-dir", str(run_dir))
        assert "harness_ready" in result.stdout
        assert "harness_start" in result.stdout

    def test_tail_filters_by_kind(self, running_harness) -> None:
        """``--kind`` selects just the records a question is about."""
        run_dir, _ = running_harness
        _cf("tick", "--run-dir", str(run_dir))
        result = _cf("tail", "--run-dir", str(run_dir), "--kind", "heartbeat")
        assert "heartbeat" in result.stdout
        assert "mock_openai_start" not in result.stdout

    def test_tail_json_is_machine_readable(self, running_harness) -> None:
        """``--json`` emits one parseable JSON object per line."""
        run_dir, _ = running_harness
        result = _cf("tail", "--run-dir", str(run_dir), "--json", "--limit", "5")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert lines
        for line in lines:
            record = json.loads(line)
            assert "kind" in record and "seq" in record

    def test_tail_on_a_missing_directory_says_so(self, tmp_path) -> None:
        """A wrong path is a clear message, not a traceback."""
        result = _cf("tail", "--run-dir", str(tmp_path / "nope"), check=False)
        assert result.returncode != 0
        assert "no trace at" in (result.stdout + result.stderr)


class TestProgramIO:
    """Feeding the program is a command-line job too, not just a Python one.

    Without these, the framework could move time from the shell but not put words
    in the user's mouth, which makes it useless for driving a scene interactively.
    """

    def test_say_appends_a_user_message(self, running_harness) -> None:
        """``cf say`` runs the program's real foreground path and reports the event."""
        run_dir, _ = running_harness
        result = _cf("say", "最近有点累，但也不知道该怎么说。", "--conversation", "c1", "--run-dir", str(run_dir))
        assert "event      : evt_" in result.stdout
        assert "duplicate  : False" in result.stdout

    def test_say_with_a_fixed_event_id_is_idempotent(self, running_harness) -> None:
        """Re-sending the same event id is a redelivery, not a second message."""
        run_dir, _ = running_harness
        args = ("say", "同一句话。", "--event-id", "evt_cli_idem", "--conversation", "c1", "--run-dir", str(run_dir))
        first = _cf(*args)
        assert "duplicate  : False" in first.stdout
        second = _cf(*args)
        assert "duplicate  : True" in second.stdout

    def test_backlog_shows_unresolved_events(self, running_harness) -> None:
        """``cf backlog`` reports what the program declined to interpret."""
        run_dir, _ = running_harness
        _cf("say", "算了，先这样吧，回头再说。", "--conversation", "c1", "--run-dir", str(run_dir))
        result = _cf("backlog", "--run-dir", str(run_dir))
        assert "unresolved" in result.stdout

    def test_refresh_hits_the_mock_endpoint(self, running_harness) -> None:
        """``cf refresh`` is the command that exercises the OpenAI-compatible path."""
        run_dir, _ = running_harness
        _cf("say", "有件小事，我说不好。", "--conversation", "c1", "--run-dir", str(run_dir))
        result = _cf("refresh", "--at", "2026-09-20T09:00:00Z", "--major-event", "--run-dir", str(run_dir))
        assert "ran        : True" in result.stdout
        assert "provider='remote_api'" in result.stdout
        assert "violations : []" in result.stdout

    def test_refresh_on_a_dead_runtime_explains_itself(self) -> None:
        """Pointing a client command at nothing is a clear error, not a traceback."""
        result = _cf("backlog", "--runtime", "http://127.0.0.1:1", check=False)
        assert result.returncode != 0
        assert "unreachable" in (result.stdout + result.stderr)


class TestShutdown:
    """The CLI can stop what it started."""

    def test_shutdown_ends_the_process(self, tmp_path) -> None:
        """``cf shutdown`` makes ``cf run`` exit cleanly."""
        run_dir = tmp_path / "shutdown"
        process = subprocess.Popen(
            [
                sys.executable, "-m", "cf", "run",
                "--run-dir", str(run_dir),
                "--program-src", str(PROGRAM_SRC),
                "--start-time", START,
                "--time-scale", "0",
                "--heartbeat-interval", "30",
                "--quiet",
            ],
            cwd=FRAMEWORK_ROOT,
            env=_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_ready(run_dir, process)
            _cf("shutdown", "--run-dir", str(run_dir))
            stdout, stderr = process.communicate(timeout=45)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        assert process.returncode == 0, stderr
        assert "stopped: beats=" in stdout
        trace = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        stop = [record for record in trace if record["kind"] == "harness_stop"]
        assert stop, "teardown was not recorded"
        assert stop[-1]["program_source_untouched"]["untouched"] is True
