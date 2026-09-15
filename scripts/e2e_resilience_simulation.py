#!/usr/bin/env python3
"""End-to-end resilience simulation for the companion Runtime sidecar.

This is not another unit test. It starts a **real** ``uvicorn`` HTTP server in
front of a **real** :class:`~companion_runtime.runtime.Runtime` over a **real file
backed SQLite database in WAL mode**, and then talks to it the way a deployment
does:

* the *host* side is a faithful stand-in for AstrBot. It uses the shipped
  adapter's own AstrBot-free core (``protocol`` / ``outbox`` / ``http_client`` /
  ``settings`` / ``bridge`` from ``astrbot_plugin_companion_runtime``) and only
  fakes the three public AstrBot APIs the shipped
  ``astrbot_executor.AstrBotActionExecutor`` calls: the session's current chat
  provider, ``llm_generate`` (the main LLM), and ``send_message`` (the platform);
* the *Runtime* side is the shipped ASGI app, unmodified, driven over
  ``/v1/*`` (the adapter contract) and the native routes where the adapter has no
  counterpart (``/rendered``, ``/outbox/claim``, ``/tick``, ``/schedule`` …).

Coverage (one printed phase per line, each check with PASS/FAIL and diagnostics):

1.  v1 event ingestion: batch accounting, ``event_id`` idempotency, replay of a
    *modified* duplicate (history is never rewritten).
2.  semantic settlement: an explicit event settles through the coarse rule table,
    an ambiguous one is deliberately left ``unresolved`` instead of guessed.
3.  the autonomous :class:`~companion_runtime.scheduler.Scheduler` running around
    the live Runtime (exactly as ``companion-runtime serve`` wires it) actually
    producing a round and an outbox row, with no request driving it.
4.  the full adapter contract: lease → heartbeat → render (slow main LLM) →
    authorize → send → result, plus lease-id staleness and the authorize gate.
5.  identity/replay: a replayed render report and a replayed delivery report
    converge; the daily contact counter moves exactly once.
6.  multi-session routing: a proactive action reaches the session its intention
    was formed in, an adapter that asks for another session is not given it, and
    a second session round-trips through the adapter's own polling loop.
7.  user reply attribution: a reply resolves the sent attempt exactly once.
8.  an *unavailable* authorize (transient outage) must return the row for retry
    instead of closing the intention terminally — and the message must not be
    sent (fail closed).
9.  queue depth > 100: ``POST /rendered`` still finds the render row for its
    attempt (``path == "outbox"``) instead of falling down the direct path.
10. lease expiry and attempt-budget exhaustion close the attempt and free the
    scheduler gate.
11. a delayed (out-of-order) timestamp never rewinds the clock, while the delayed
    event itself is still stored verbatim.
12. remote semantic provider *selection* from configuration using a harmless
    loopback endpoint and no API key: the provider reports itself unavailable and
    nothing is sent to the endpoint.
13. simulated restart: version, clock, refresh timestamp, raw history and the
    pending outbox row survive, and the surviving row can still be delivered.
14. concurrent duplicate v1 result reports apply at most once.

Design rules honoured here:

* **No real network.** Every socket is loopback: the Runtime binds
  ``127.0.0.1`` on an OS-assigned free port, and the "remote" provider endpoint is
  a loopback listener owned by this script.
* **No secrets.** No API key is ever configured, read, logged or written. The
  provider phase passes an explicit environment mapping without a key, and the
  script refuses to inherit a credential from its own environment.
* **Artifacts only under ``--base-dir``.** The script never writes next to the
  sources (``sys.dont_write_bytecode`` is set before any project import).
* **Everything is stopped.** Every server thread, scheduler, adapter event loop
  and HTTP listener is joined before the process exits.

Usage::

    python scripts/e2e_resilience_simulation.py --base-dir F:\\e2e-resilience
    python scripts/e2e_resilience_simulation.py --base-dir ... --only scheduler,restart
    python scripts/e2e_resilience_simulation.py --base-dir ... --quiet

Exit code is ``0`` only when every check passed, ``1`` when a check failed, and
``2`` when the script itself could not start (missing dependencies).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.machinery
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# Never drop bytecode next to the project sources: the only artifacts this run
# is allowed to leave behind live under --base-dir.
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = REPO_ROOT / "runtime" / "src"
PLUGIN_ROOT = REPO_ROOT / "astrbot_plugin_companion_runtime"
PLUGIN_CORE = PLUGIN_ROOT / "companion_runtime"

if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

HOST = "127.0.0.1"

#: Two distinct AstrBot-style origins (``platform:type:id``).
SESSION_A = "aiocqhttp:FriendMessage:10001"
SESSION_B = "webchat:GroupMessage:20002"
#: The Runtime's own default conversation, which a proactive action must *not*
#: silently fall back to in a multi-session deployment.
DEFAULT_SESSION = "default"

#: How long the simulated sidecar was "down" before the run starts. The Runtime's
#: creation epoch is moved this far into the past so that the character has a real
#: absence to reason about — which is what makes an endogenous round act instead of
#: waiting for a hazard that would only fire hours later. This is a controlled
#: state setup, not a change to any behaviour.
OFFLINE_WINDOW = timedelta(hours=96)
#: The simulated user message inside that offline window.
MESSAGE_AGE = timedelta(hours=72)
#: A user message that reliably produces an unfinished matter the Runtime will want
#: to follow up on.
MATTER_TEXT = "明天下午面试，结束告诉你结果。"
#: Deliberately ambiguous: no explicit anchor, so the coarse rule table must refuse
#: to settle it and leave it for a later deep refresh.
AMBIGUOUS_TEXT = "算了，也没什么。"
#: Explicit emotional statement, settled coarsely by the ingest path.
EXPLICIT_TEXT = "谢谢你，我今天真的被你安慰到了。"

OK_MARK = "[PASS]"
BAD_MARK = "[FAIL]"
NOTE_MARK = "  ·"


# --------------------------------------------------------------------------------------
# project imports (guarded, so a missing dependency is a clear message and not a traceback)
# --------------------------------------------------------------------------------------

IMPORT_ERROR = ""
try:
    import aiohttp as _aiohttp  # noqa: F401  (the adapter's HTTP transport needs it)

    from companion_runtime import action as action_module
    from companion_runtime import providers as providers_module
    from companion_runtime.api import create_app
    from companion_runtime.config import RuntimeConfig
    from companion_runtime.scheduler import Scheduler
    from companion_runtime.runtime import Runtime
    from companion_runtime.typing import CandidateIntent, OutboxItem, OutboxKind, new_id
except Exception as exc:  # noqa: BLE001 - reported to the operator as a setup failure
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

#: The shipped adapter package is imported under a private name: its inner package
#: is also called ``companion_runtime`` and would otherwise shadow the Runtime's.
ADAPTER_PACKAGE = "_e2e_adapter_core"
ADAPTER_MODULES: dict[str, Any] = {}


def load_adapter_modules() -> dict[str, Any]:
    """Import the shipped adapter's AstrBot-free core under a private name.

    The adapter is half of the contract this script verifies, so its *real* wire
    types, consumer and HTTP client are used rather than a re-implementation. The
    package's public ``__init__`` is skipped on purpose: it pulls in the aiohttp
    transport object eagerly and is not needed here.

    Returns:
        ``{"protocol": module, "outbox": module, ...}``.

    Raises:
        ImportError: When the adapter package is missing from the checkout.
    """
    if ADAPTER_MODULES:
        return ADAPTER_MODULES
    if not PLUGIN_CORE.is_dir():
        raise ImportError(f"adapter core package not found at {PLUGIN_CORE}")
    package = types.ModuleType(ADAPTER_PACKAGE)
    spec = importlib.machinery.ModuleSpec(ADAPTER_PACKAGE, None, is_package=True)
    spec.submodule_search_locations = [str(PLUGIN_CORE)]
    package.__spec__ = spec
    package.__path__ = [str(PLUGIN_CORE)]
    sys.modules[ADAPTER_PACKAGE] = package
    for name in ("protocol", "outbox", "http_client", "settings", "bridge", "retry_queue", "coerce"):
        ADAPTER_MODULES[name] = importlib.import_module(f"{ADAPTER_PACKAGE}.{name}")
    return ADAPTER_MODULES


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------


@dataclass
class RunState:
    """Facts about this process's run that later phases need to check against."""

    started_at: float = 0.0
    base_dir: Path = field(default_factory=lambda: Path("."))


RUN = RunState()


@dataclass
class Check:
    """One assertion, with the phase it belongs to."""

    phase: str
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Section:
    """A named group of checks."""

    title: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


class Verifier:
    """Collects PASS/FAIL results, prints them, and writes the run report."""

    def __init__(self, *, quiet: bool = False, echo: bool = True) -> None:
        """Create the verifier.

        Args:
            quiet: Suppress the informational notes (checks are still printed).
            echo: Print to stdout as checks are recorded.
        """
        self.sections: list[Section] = []
        self.quiet = quiet
        self.echo = echo
        self._current: Section | None = None
        self._lines: list[str] = []

    # -- phase/section bookkeeping -----------------------------------------------------

    def phase(self, title: str) -> Section:
        """Start a new printed phase."""
        name = f"PHASE {len(self.sections) + 1}: {title}"
        section = Section(title=name)
        self.sections.append(section)
        self._current = section
        self._out("")
        self._out("=" * 78)
        self._out(name)
        self._out("=" * 78)
        return section

    @property
    def current_title(self) -> str:
        """Return the title of the phase currently being verified."""
        return self._current.title if self._current is not None else "(no phase)"

    def note(self, message: str) -> None:
        """Record and print an informational line."""
        if self._current is not None:
            self._current.notes.append(message)
        if not self.quiet:
            self._out(f"{NOTE_MARK} {message}")

    def diagnostics(self, title: str, payload: Any) -> None:
        """Record extra evidence (printed as an indented block)."""
        text = f"{title}: {_short_json(payload)}"
        if self._current is not None:
            self._current.diagnostics.append(text)
        if not self.quiet:
            self._out(f"    {text}")

    # -- assertions --------------------------------------------------------------------

    def check(self, label: str, condition: Any, detail: str = "") -> bool:
        """Record one PASS/FAIL assertion."""
        ok = bool(condition)
        check = Check(phase=self.current_title, label=label, ok=ok, detail=detail)
        if self._current is not None:
            self._current.checks.append(check)
        mark = OK_MARK if ok else BAD_MARK
        suffix = f"  [{detail}]" if detail else ""
        self._out(f"  {mark} {label}{suffix}")
        return ok

    def check_equal(self, label: str, actual: Any, expected: Any) -> bool:
        """Assert equality, reporting both sides when it fails."""
        return self.check(label, actual == expected, f"actual={_short(actual)} expected={_short(expected)}")

    # -- output ------------------------------------------------------------------------

    def line(self, text: str = "") -> None:
        """Print and remember one line."""
        self._lines.append(text)
        if self.echo:
            print(text, flush=True)

    def _out(self, line: str) -> None:
        """Internal alias used while a check is being recorded."""
        self.line(line)

    def totals(self) -> tuple[int, int]:
        """Return ``(passed, failed)`` over every recorded check."""
        checks = [check for section in self.sections for check in section.checks]
        passed = sum(1 for check in checks if check.ok)
        return passed, len(checks) - passed

    def failures(self) -> list[Check]:
        """Return every failed check, in order."""
        return [check for section in self.sections for check in section.checks if not check.ok]

    def summary(self) -> int:
        """Print the summary table and return the process exit code."""
        passed, failed = self.totals()
        self._out("")
        self._out("=" * 78)
        self._out("SUMMARY")
        self._out("=" * 78)
        for section in self.sections:
            section_passed = sum(1 for check in section.checks if check.ok)
            section_failed = len(section.checks) - section_passed
            mark = OK_MARK if section_failed == 0 else BAD_MARK
            self._out(f"  {mark} {section.title}: {section_passed}/{len(section.checks)} checks passed")
        self._out("")
        self._out(f"  checks passed: {passed}")
        self._out(f"  checks failed: {failed}")
        if failed:
            self._out("")
            self._out("FAILURES (with diagnostics)")
            self._out("-" * 78)
            for index, check in enumerate(self.failures(), start=1):
                self._out(f"  {index}. [{check.phase}] {check.label}")
                if check.detail:
                    self._out(f"       {check.detail}")
        return 1 if failed else 0

    def as_report(self) -> dict[str, Any]:
        """Return the JSON-serialisable run report."""
        return {
            "generated_at": utc_iso(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "repo_root": str(REPO_ROOT),
            "sections": [
                {
                    "title": section.title,
                    "notes": list(section.notes),
                    "diagnostics": list(section.diagnostics),
                    "checks": [
                        {"label": check.label, "ok": check.ok, "detail": check.detail}
                        for check in section.checks
                    ],
                }
                for section in self.sections
            ],
            "totals": dict(zip(("passed", "failed"), self.totals())),
            "failures": [
                {"phase": check.phase, "label": check.label, "detail": check.detail}
                for check in self.failures()
            ],
        }


V = Verifier()


class _MemoryLogHandler(logging.Handler):
    """Keeps the sidecar's own log lines for the diagnostics artifact."""

    def __init__(self, *, echo_level: int = logging.WARNING) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []
        self.echo_level = echo_level

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            line = self.format(record)
            self.records.append(line)
            if record.levelno >= self.echo_level and not V.quiet:
                print(f"    [runtime-log] {line}", flush=True)


LOG_HANDLER = _MemoryLogHandler()


def configure_script_logging() -> None:
    """Capture Runtime/uvicorn logs instead of letting them bury the report."""
    handler = LOG_HANDLER
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s :: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    with contextlib.suppress(Exception):
        root.handlers = [handler]
    for name in ("companion_runtime", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def utc_iso(moment: datetime | None = None) -> str:
    """Render a moment (default: now) as an ISO-8601 string."""
    return (moment or utc_now()).isoformat()


def parse_iso(text: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating ``Z`` and naive values."""
    if isinstance(text, datetime):
        value = text
    elif isinstance(text, str) and text.strip():
        try:
            value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _short(value: Any, limit: int = 160) -> str:
    """Render a value compactly for a check's detail field."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_json(payload: Any, limit: int = 900) -> str:
    """Render a diagnostic payload readably."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str, indent=None)
    except (TypeError, ValueError):
        text = repr(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def free_port() -> int:
    """Return a currently unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 0.1) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if predicate():
                return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------------------
# HTTP: the script's own synchronous client (loopback only)
# --------------------------------------------------------------------------------------


@dataclass
class Reply:
    """One HTTP reply, captured without raising."""

    status: int
    json: Any
    text: str
    elapsed_ms: float
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether the status is 2xx."""
        return 200 <= self.status < 300

    def field(self, *path: str, default: Any = None) -> Any:
        """Read a nested field (mapping keys or list indices) from a JSON body."""
        cursor: Any = self.json
        for key in path:
            if isinstance(cursor, Mapping) and key in cursor:
                cursor = cursor[key]
            elif isinstance(cursor, Sequence) and not isinstance(cursor, (str, bytes, bytearray)):
                try:
                    cursor = cursor[int(key)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return cursor

    def describe(self) -> str:
        """Return a compact one-line description for a failure detail."""
        if self.error:
            return f"status={self.status} error={self.error}"
        return f"status={self.status} body={_short(self.json, 300)}"


def http_call(
    base_url: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> Reply:
    """Perform one JSON request against a loopback server, never raising.

    Args:
        base_url: ``http://127.0.0.1:<port>``.
        method: HTTP method.
        path: Absolute path including the query string.
        body: JSON body, or ``None``.
        timeout: Per-request timeout in seconds.

    Returns:
        A :class:`Reply`; transport failures arrive as ``status=0`` with
        :attr:`Reply.error` set, so a caller can assert on them too.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "companion-runtime-e2e-resilience/1",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            raw = response.read().decode("utf-8", "replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:  # a 4xx/5xx is a result, not an error
        raw = exc.read().decode("utf-8", "replace")
        status = int(exc.code)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return Reply(
            status=0,
            json=None,
            text="",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = (time.perf_counter() - started) * 1000.0
    parsed: Any = None
    if raw.strip():
        with contextlib.suppress(ValueError):
            parsed = json.loads(raw)
    return Reply(status=status, json=parsed, text=raw, elapsed_ms=elapsed)


# --------------------------------------------------------------------------------------
# the Runtime server under test
# --------------------------------------------------------------------------------------


@dataclass
class Scenario:
    """A live Runtime sidecar: real uvicorn, real file SQLite/WAL, own port."""

    name: str
    directory: Path
    port: int
    base_url: str
    config: Any
    runtime: Any
    holder: dict[str, Any]
    thread: threading.Thread | None = None
    started_at: float = 0.0

    # -- convenience ---------------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> Reply:
        """GET against the live server."""
        return http_call(self.base_url, "GET", path, **kwargs)

    def post(self, path: str, body: Mapping[str, Any] | None = None, **kwargs: Any) -> Reply:
        """POST against the live server."""
        return http_call(self.base_url, "POST", path, body, **kwargs)

    def health(self) -> dict[str, Any]:
        """Return the health payload (``{}`` when unavailable)."""
        payload = self.get("/health").json
        return payload if isinstance(payload, dict) else {}

    def state(self) -> dict[str, Any]:
        """Return the runtime state projection (``{}`` when unavailable)."""
        payload = self.get("/state").json
        return payload if isinstance(payload, dict) else {}

    def outbox_row(self, outbox_id: str) -> Any:
        """Read one outbox row through the live Runtime's own projection.

        Used as an inspection aid: the public surface only lists pages, and a
        check about a *specific* row must not depend on where it lands in a page.
        """
        return self.runtime.projections.outbox.get(outbox_id)

    def attempt(self, attempt_id: str) -> Any:
        """Read one attempt through the live Runtime's own projection."""
        return self.runtime.projections.attempts.get(attempt_id)

    def scheduler(self) -> Any:
        """Return the Scheduler running on the server's loop, when there is one."""
        return self.holder.get("scheduler")


def prepare_directory(base_dir: Path, name: str) -> Path:
    """Create a clean directory for one scenario under ``--base-dir``."""
    directory = base_dir / "scenarios" / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_config(directory: Path, mutate: Callable[[Any], None] | None = None) -> Any:
    """Build a Runtime config rooted in ``directory``.

    No semantic provider is selected and the deep refresh is off by default: the
    phases that exercise a provider configure it explicitly, so a stray
    ``CR_SEMANTIC_*`` in the environment can never make an unrelated phase reach
    for the network.
    """
    config = RuntimeConfig()
    config.storage.database_path = str(directory / "runtime.sqlite3")
    config.storage.raw_log_path = str(directory / "raw_events.jsonl")
    config.storage.mirror_raw_events = True
    config.storage.wal = True
    config.semantic.provider = "disabled"
    config.semantic.deep_refresh_enabled = False
    config.conversation_id = DEFAULT_SESSION
    if mutate is not None:
        mutate(config)
    return config


def serve(
    config: Any,
    *,
    name: str,
    directory: Path,
    seed: int = 20260301,
    epoch_offset: timedelta = timedelta(0),
    with_scheduler: bool = False,
    round_callback: Callable[[], Any] | None = None,
    scheduler_gate: threading.Event | None = None,
    startup_timeout: float = 30.0,
) -> Scenario:
    """Start a real Runtime + uvicorn server and wait until it answers.

    The wiring mirrors ``companion_runtime.cli.cmd_serve``: one event loop owns the
    server, the scheduler and the final checkpoint-free shutdown.

    Args:
        config: Runtime configuration (storage paths already resolved).
        name: Scenario name, used for the port log and the artifact subdirectory.
        directory: Artifact directory for this scenario.
        seed: RNG seed, so a run is reproducible.
        epoch_offset: How far the Runtime's creation epoch is moved into the past.
        with_scheduler: Start the real endogenous Scheduler on the server's loop.
        round_callback: Callback for one round; defaults to ``endogenous_round``.
        scheduler_gate: When given, the Scheduler is created but only started once
            the gate is set. This lets a phase prepare state *before* the first
            autonomous round without changing what the Scheduler does.
        startup_timeout: Seconds to wait for ``/health`` to answer.

    Returns:
        A started :class:`Scenario`.

    Raises:
        RuntimeError: When the server never became reachable.
    """
    import uvicorn  # imported here so --help works without the dependency

    created_at = utc_now() - epoch_offset
    runtime = Runtime(config, seed=seed, created_at=created_at)
    app = create_app(runtime, config)
    port = free_port()
    holder: dict[str, Any] = {"name": name}
    ready = threading.Event()

    def _thread_main() -> None:
        """Own the loop: serve HTTP, run the scheduler, then shut down."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop

        async def _main() -> None:
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=HOST,
                    port=port,
                    log_level="info",
                    access_log=False,
                    log_config=None,
                )
            )
            holder["server"] = server
            scheduler = None
            starter: asyncio.Task[None] | None = None
            if with_scheduler:
                scheduler = Scheduler(
                    config=config,
                    round_callback=round_callback or runtime.endogenous_round,
                    rng=runtime.rng,
                    runtime=runtime,
                )
                holder["scheduler"] = scheduler
                if scheduler_gate is None:
                    await scheduler.start()
                else:

                    async def _wait_then_start() -> None:
                        """Start the loop only once the phase has prepared its state."""
                        while not scheduler_gate.is_set():
                            await asyncio.sleep(0.05)
                        await scheduler.start()

                    starter = asyncio.create_task(
                        _wait_then_start(), name="e2e-deferred-scheduler-start"
                    )
                    holder["scheduler_starter"] = starter
            ready.set()
            try:
                await server.serve()
            finally:
                if starter is not None and not starter.done():
                    starter.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await starter
                if scheduler is not None:
                    await scheduler.stop()

        try:
            loop.run_until_complete(_main())
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    thread = threading.Thread(target=_thread_main, name=f"e2e-runtime-{name}", daemon=True)
    thread.start()
    if not ready.wait(timeout=startup_timeout):
        raise RuntimeError(f"scenario {name}: the server loop never became ready")
    scenario = Scenario(
        name=name,
        directory=directory,
        port=port,
        base_url=f"http://{HOST}:{port}",
        config=config,
        runtime=runtime,
        holder=holder,
        thread=thread,
        started_at=time.monotonic(),
    )
    if not wait_until(lambda: scenario.get("/health", timeout=2.0).ok, timeout=startup_timeout):
        stop_scenario(scenario)
        raise RuntimeError(f"scenario {name}: /health never answered on {scenario.base_url}")
    return scenario


def stop_scenario(scenario: Scenario) -> None:
    """Stop the server, the scheduler and the Runtime, and join the thread."""
    server = scenario.holder.get("server")
    if server is not None:
        server.should_exit = True
    thread = scenario.thread
    if thread is not None:
        thread.join(timeout=20)
    scenario.thread = None
    with contextlib.suppress(Exception):
        scenario.runtime.close()
    if thread is not None and thread.is_alive():
        V.note(f"scenario {scenario.name}: server thread did not stop within 20s")


@contextlib.contextmanager
def scenario_session(
    base_dir: Path,
    name: str,
    *,
    mutate: Callable[[Any], None] | None = None,
    **kwargs: Any,
) -> Iterable[Scenario]:
    """Run one scenario and guarantee its threads are stopped afterwards."""
    directory = prepare_directory(base_dir, name)
    config = build_config(directory, mutate)
    scenario = serve(config, name=name, directory=directory, **kwargs)
    V.note(
        f"scenario {name}: real uvicorn on {scenario.base_url} "
        f"db={Path(config.storage.database_path).name} wal={config.storage.wal}"
    )
    try:
        yield scenario
    finally:
        stop_scenario(scenario)


# --------------------------------------------------------------------------------------
# state preparation helpers (all through the live HTTP surface unless stated)
# --------------------------------------------------------------------------------------


def post_v1_event(scenario: Scenario, record: Mapping[str, Any], *, adapter_id: str = "e2e-adapter") -> Reply:
    """Post one v1 event envelope."""
    return scenario.post(
        "/v1/events",
        {
            "protocol_version": "1",
            "adapter_id": adapter_id,
            "sent_at": utc_iso(),
            "events": [dict(record)],
        },
    )


def backfill_user_message(
    scenario: Scenario,
    *,
    session: str,
    text: str = MATTER_TEXT,
    age: timedelta = MESSAGE_AGE,
    event_id: str | None = None,
) -> Reply:
    """Deliver a user message that arrived while the sidecar was down."""
    return post_v1_event(
        scenario,
        {
            "event_id": event_id or new_id("evt"),
            "kind": "user_message",
            "session": session,
            "text": text,
            "occurred_at": utc_iso(utc_now() - age),
            "platform": session.split(":", 1)[0],
            "sender_id": "user-1",
            "sender_name": "阿岚",
            "wake": True,
            "preempts_proactive": True,
        },
    )


def force_endogenous_round(scenario: Scenario, *, now: datetime | None = None) -> dict[str, Any]:
    """Run one endogenous round through the public route and return its body."""
    reply = scenario.post(
        "/endogenous",
        {"now": utc_iso(now or utc_now()), "force": True, "create_attempt": True},
    )
    payload = reply.json
    return payload if isinstance(payload, dict) else {}


def open_matters(scenario: Scenario) -> list[dict[str, Any]]:
    """Return the Runtime's unfinished matters."""
    items = scenario.get("/unfinished").field("matters", default=[])
    return [item for item in items if isinstance(item, dict)]


def pending_rows(scenario: Scenario, *, kind: str | None = None) -> list[dict[str, Any]]:
    """Return pending outbox rows (optionally of one kind)."""
    items = scenario.get("/outbox", timeout=10.0).field("items", default=[])
    rows = [item for item in items if isinstance(item, dict)]
    return [row for row in rows if kind is None or row.get("kind") == kind]


def seed_committed_attempt(
    scenario: Scenario,
    *,
    session: str = SESSION_A,
    text: str = MATTER_TEXT,
) -> dict[str, Any]:
    """Prepare a committed proactive attempt with a pending render outbox row.

    The Runtime is given a real offline window (its creation epoch is moved into
    the past) and a user message from inside that window, so the endogenous round
    has a concrete reason to speak: an unfinished matter that has come due, plus a
    long absence. The round is *forced* only to skip the foreground-pause check —
    the motivational game itself still decides.

    Returns:
        ``{"event", "matter", "round", "attempt_id", "outbox_id"}``.
    """
    event_reply = backfill_user_message(scenario, session=session, text=text)
    outcome = event_reply.field("outcomes", default=[{}])
    outcome = outcome[0] if outcome else {}
    matters = open_matters(scenario)
    round_body = force_endogenous_round(scenario)
    return {
        "event": outcome,
        "matters": matters,
        "round": round_body,
        "attempt_id": round_body.get("attempt_id"),
        "outbox_id": round_body.get("outbox_id"),
        "decision": round_body.get("decision") or {},
    }


def enqueue_noise_rows(scenario: Scenario, count: int) -> list[str]:
    """Fill the outbox with unrelated, newer send rows.

    A white-box step by necessity: the public surface can queue work only through
    a commit, and the point of the check is a queue that is *not* about the attempt
    under test. The rows are newer on purpose — the outbox listing is
    ``ORDER BY created_at DESC``, so these are exactly the rows that used to push
    the real one off the first page.
    """
    runtime = scenario.runtime
    created_ids: list[str] = []
    base = utc_now()
    with runtime.db.transaction() as conn:
        for index in range(count):
            created_at = base + timedelta(milliseconds=index + 1)
            outbox_id = new_id("outbox")
            runtime.projections.outbox.enqueue(
                conn,
                OutboxItem(
                    outbox_id=outbox_id,
                    kind=OutboxKind.SEND.value,
                    payload={"attempt_id": f"att_noise_{index}", "text": "noise"},
                    created_at=created_at,
                    available_at=created_at + timedelta(days=1),
                ),
            )
            created_ids.append(outbox_id)
    return created_ids


def commit_attempt_without_render_row(scenario: Scenario, *, intent: str) -> str:
    """Commit an attempt that never queued a render row (white-box fixture).

    The public HTTP surface has no route that creates an attempt without queueing
    its render row, and the ``/rendered`` *direct* path exists precisely for such
    an attempt, so it is constructed against the live Runtime the way the delivery
    worker would.
    """
    runtime = scenario.runtime
    now = utc_now()
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=intent,
        goal="表达关心",
        sources=[],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        attempt = action_module.create_proposal(
            candidate=candidate, based_on_version=runtime.version(), now=now
        )
        action_module.commit(runtime.projections.attempts, conn, attempt, now=now)
    return attempt.attempt_id


def propose_attempt_only(scenario: Scenario, *, intent: str) -> str:
    """Store a *proposed* attempt that nothing ever committed (white-box fixture)."""
    runtime = scenario.runtime
    now = utc_now()
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent=intent,
        goal="表达关心",
        sources=[],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        attempt = action_module.create_proposal(
            candidate=candidate, based_on_version=runtime.version(), now=now
        )
        runtime.projections.attempts.upsert(conn, attempt)
    return attempt.attempt_id


# --------------------------------------------------------------------------------------
# the simulated AstrBot host: fake platform, fake main LLM, real adapter core
# --------------------------------------------------------------------------------------


@dataclass
class Delivery:
    """One message the fake platform accepted."""

    session: str
    text: str
    at: datetime


class FakePlatform:
    """Stand-in for AstrBot's platform adapters (an address book per session)."""

    def __init__(self) -> None:
        self._online: dict[str, bool] = {}
        self.deliveries: list[Delivery] = []

    def register(self, session: str, *, online: bool = True) -> None:
        """Make a session resolvable."""
        self._online[session] = online

    def resolve(self, session: str) -> bool:
        """Whether a session can currently be addressed."""
        return bool(self._online.get(session, False))

    def deliver(self, session: str, text: str) -> bool:
        """Deliver a message; ``False`` mirrors AstrBot's unmatched-session result."""
        if not self.resolve(session):
            return False
        self.deliveries.append(Delivery(session=session, text=text, at=utc_now()))
        return True

    def texts_for(self, session: str) -> list[str]:
        """Return every text delivered to one session."""
        return [item.text for item in self.deliveries if item.session == session]


class FakeMainLLM:
    """The host's main LLM, as used by the AstrBot render path."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    async def generate(self, *, provider_id: str, prompt: str, system_prompt: str | None = None) -> str:
        """Answer one render prompt deterministically from the intent it carries."""
        self.calls.append({"provider_id": provider_id, "prompt": prompt})
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return render_text_for(prompt)


def render_text_for(prompt: str) -> str:
    """Return the message a real main LLM would write for a render prompt.

    The prompt is the Runtime's, unmodified: this reads the intent line the Runtime
    composed, which is what makes the assertion on the delivered text meaningful.
    """
    intent = "你"
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("- 想做的事："):
            intent = stripped.split("：", 1)[1].strip() or intent
            break
    return f"突然想起{intent}，还顺利吗？"


class FakeAstrBotContext:
    """The three public AstrBot APIs ``astrbot_executor`` actually calls."""

    def __init__(self, *, platform: FakePlatform, llm: FakeMainLLM) -> None:
        self._platform = platform
        self._llm = llm

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        """Resolve the session's current chat provider (AstrBot raises when it cannot)."""
        if not self._platform.resolve(umo):
            raise RuntimeError(f"no chat provider for session {umo!r}")
        return f"e2e-provider::{umo}"

    async def llm_generate(self, *, chat_provider_id: str, prompt: str, system_prompt: str | None = None) -> Any:
        """Generate one completion through the session's provider."""
        text = await self._llm.generate(
            provider_id=chat_provider_id, prompt=prompt, system_prompt=system_prompt
        )
        return types.SimpleNamespace(completion_text=text)

    async def send_message(self, session: str, chain: Any) -> bool:
        """Deliver a message chain; returns whether a platform accepted it."""
        text = chain if isinstance(chain, str) else "".join(str(part) for part in chain)
        return self._platform.deliver(session, text)


class FakeHostActionExecutor:
    """Mirror of the shipped ``AstrBotActionExecutor`` on top of the fake host.

    Kept behaviourally identical on purpose: the same payload contract, the same
    error on a missing prompt, and the same ``{"sent": bool}`` shape, so a failure
    here would be a failure of the adapter contract rather than of this harness.
    """

    def __init__(self, *, context: FakeAstrBotContext, adapter: Any) -> None:
        self._context = context
        self._adapter = adapter

    async def render(self, action: Any) -> dict[str, Any]:
        """Render the Runtime's prompt with the session's current provider."""
        text_in = str(action.payload.get("prompt") or "").strip()
        if not text_in:
            raise ActionExecutionError("render action payload has no prompt")
        provider_id = await self._context.get_current_chat_provider_id(umo=action.session)
        response = await self._context.llm_generate(chat_provider_id=provider_id, prompt=text_in)
        text = str(getattr(response, "completion_text", "")).strip()
        result: dict[str, Any] = {"text": text, "provider_id": provider_id, "chars": len(text)}
        max_chars = int(action.payload.get("max_chars") or 0)
        if max_chars > 0 and len(text) > max_chars:
            result["text"] = text[:max_chars]
            result["truncated"] = True
        return result

    async def send(self, action: Any, text: str) -> dict[str, Any]:
        """Deliver an authorized message to the action's session."""
        body = (text or "").strip()
        if not body:
            raise ActionExecutionError("send action has no text")
        delivered = await self._context.send_message(action.session, body)
        return {"sent": bool(delivered), "chars": len(body)}


class ActionExecutionError(RuntimeError):
    """Mirrors the shipped executor's error type for host-side failures."""


class AdapterLoop:
    """A private asyncio loop for the fake host, like AstrBot's own runtime."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Run the loop until it is asked to stop."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Any, *, timeout: float = 60.0) -> Any:
        """Run a coroutine on the loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def spawn(self, coro: Any) -> Any:
        """Schedule a coroutine without waiting."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def close(self) -> None:
        """Cancel what is left and stop the loop, joining its thread."""
        if self.loop.is_closed():
            return

        async def _drain() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        with contextlib.suppress(Exception):
            self.call(_drain(), timeout=15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=15)
        with contextlib.suppress(Exception):
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        with contextlib.suppress(Exception):
            self.loop.close()


class _RecordingTransport:
    """Wraps the real transport and remembers every action it leased.

    The consumer never hands its leased action back to the caller, and a claim's
    identity (``lease_id``, which embeds the claim counter) is exactly what a
    re-lease check has to compare. Recording at the transport boundary is the one
    place the adapter's own path can be observed without changing it.
    """

    def __init__(self, inner: Any, *, sink: list[Any]) -> None:
        self._inner = inner
        self._sink = sink

    async def lease_actions(self, request: Any, *, timeout_s: float) -> Any:
        """Lease through the real transport and record what came back."""
        actions = await self._inner.lease_actions(request, timeout_s=timeout_s)
        self._sink.extend(actions)
        return actions

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else to the real transport."""
        return getattr(self._inner, name)


class FailingAuthorizeTransport:
    """Wraps the real transport and makes *only* the authorize call fail.

    This is exactly what the adapter sees when the Runtime cannot be reached for a
    verdict (a refused connection, a timeout, an unparseable body): the transport
    raises, so the consumer must distinguish "no answer" from "a refusal".
    """

    def __init__(self, inner: Any, *, error: str) -> None:
        self._inner = inner
        self.error = error

    async def authorize_action(self, request: Any, *, timeout_s: float) -> Any:
        """Raise instead of answering: the Runtime is unreachable."""
        raise RuntimeTransportError(self.error)

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else to the real transport."""
        return getattr(self._inner, name)


class AdapterHarness:
    """A fake AstrBot host running the shipped adapter core against a live Runtime."""

    def __init__(
        self,
        *,
        scenario: Scenario,
        platform: FakePlatform,
        llm: FakeMainLLM,
        adapter_id: str = "e2e-adapter",
        authorize_failure: str = "",
        settings_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        """Wire the adapter exactly as the plugin does, minus AstrBot itself."""
        self._adapter = load_adapter_modules()
        self.scenario = scenario
        self.adapter_id = adapter_id
        self.platform = platform
        self.llm = llm
        self.settings = adapter_settings(
            scenario.base_url, adapter_id, overrides=settings_overrides
        )
        self.loop = AdapterLoop(f"e2e-host-{adapter_id}")
        inner = self._adapter["http_client"].AiohttpRuntimeTransport(settings=self.settings)
        if authorize_failure:
            inner = FailingAuthorizeTransport(inner, error=authorize_failure)
        #: Every action this adapter leased, in order (see :class:`_RecordingTransport`).
        self.lease_log: list[Any] = []
        self.transport = _RecordingTransport(inner, sink=self.lease_log)
        self.context = FakeAstrBotContext(platform=platform, llm=llm)
        self.executor = FakeHostActionExecutor(context=self.context, adapter=self._adapter)
        self.bridge = self._adapter["bridge"].ContextBridge(
            transport=self.transport, settings=self.settings
        )
        self.reports: list[Any] = []
        self.consumer = self._adapter["outbox"].OutboxConsumer(
            transport=self.transport,
            executor=self.executor,
            reporter=self._report,
            settings=self.settings,
        )
        self._consumer_task: asyncio.Task[Any] | None = None

    # -- reporting -----------------------------------------------------------------

    async def _report(self, report: Any) -> None:
        """Record and deliver one action result, like the plugin's reporter."""
        self.reports.append(report)
        await self.transport.report_action(
            self._adapter["http_client"].action_report_body(report),
            timeout_s=self.settings.request_timeout_s,
        )

    def reports_with(self, *, status: str | None = None, action_type: str | None = None) -> list[Any]:
        """Filter the recorded reports."""
        return [
            report
            for report in self.reports
            if (status is None or report.status == status)
            and (action_type is None or report.action_type == action_type)
        ]

    def leases(self, *, action_id: str | None = None) -> list[Any]:
        """Return the actions this adapter leased, in order."""
        return [
            action
            for action in self.lease_log
            if action_id is None or action.action_id == action_id
        ]

    # -- direct adapter calls ------------------------------------------------------

    def lease(
        self,
        *,
        capabilities: Sequence[str] = ("render", "send"),
        max_actions: int = 1,
        sessions: Sequence[str] = (),
    ) -> list[Any]:
        """Lease actions through the adapter's own client."""
        return self.lease_request(
            capabilities=capabilities, sessions=sessions, max_actions=max_actions
        )

    def heartbeat(self, action: Any, *, extend_ms: int | None = None) -> bool:
        """Extend one lease through the adapter's own client."""
        request = self._adapter["protocol"].LeaseHeartbeat(
            adapter_id=self.adapter_id,
            action_id=action.action_id,
            lease_id=action.lease_id,
            extend_ms=extend_ms or int(self.settings.outbox_lease_ttl_s * 1000),
        )
        return bool(
            self.loop.call(
                self.transport.heartbeat_lease(request, timeout_s=self.settings.request_timeout_s)
            )
        )

    def heartbeat_with_lease_id(self, action_id: str, lease_id: str, *, extend_ms: int = 5000) -> bool:
        """Extend a lease using an arbitrary (possibly stale) lease id."""
        request = self._adapter["protocol"].LeaseHeartbeat(
            adapter_id=self.adapter_id,
            action_id=action_id,
            lease_id=lease_id,
            extend_ms=extend_ms,
        )
        return bool(
            self.loop.call(
                self.transport.heartbeat_lease(request, timeout_s=self.settings.request_timeout_s)
            )
        )

    def lease_request(
        self,
        *,
        capabilities: Sequence[str],
        sessions: Sequence[str] = (),
        max_actions: int = 1,
    ) -> list[Any]:
        """Lease through the adapter's client with an explicit request shape."""
        request = self._adapter["protocol"].LeaseRequest(
            adapter_id=self.adapter_id,
            capabilities=tuple(capabilities),
            max_actions=max_actions,
            sessions=tuple(sessions),
        )
        return self.loop.call(
            self.transport.lease_actions(request, timeout_s=self.settings.request_timeout_s)
        )

    def report_body(
        self,
        action: Any,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Build the wire body an adapter sends for one action result."""
        return self.wire_report_body(
            action_id=action.action_id,
            lease_id=action.lease_id,
            action_type=action.action_type,
            attempt_id=action.attempt_id,
            session=action.session,
            status=status,
            result=result,
            error=error,
        )

    def wire_report_body(
        self,
        *,
        action_id: str,
        lease_id: str,
        action_type: str,
        attempt_id: str,
        session: str,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Build one result body from explicit fields (used to replay exactly)."""
        report = self._adapter["protocol"].ActionReport(
            adapter_id=self.adapter_id,
            action_id=action_id,
            lease_id=lease_id,
            status=status,
            action_type=action_type,
            attempt_id=attempt_id,
            session=session,
            result=dict(result or {}),
            error=error,
        )
        return self._adapter["http_client"].action_report_body(report)

    def result_path(self, action_id: str) -> str:
        """Return the v1 result path for one action."""
        return f"/v1/outbox/{urllib.parse.quote(action_id, safe='')}/result"

    def authorize(self, action: Any, text: str, *, text_sha256: str | None = None) -> Any:
        """Ask for an authorization verdict through the adapter's own client."""
        import hashlib

        digest = (
            text_sha256
            if text_sha256 is not None
            else hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        request = self._adapter["protocol"].AuthorizeRequest(
            adapter_id=self.adapter_id,
            action_id=action.action_id,
            lease_id=action.lease_id,
            session=action.session,
            attempt_id=action.attempt_id,
            text_preview=text[:160],
            text_sha256=digest,
        )
        return self.loop.call(
            self.transport.authorize_action(request, timeout_s=self.settings.request_timeout_s)
        )

    def report(self, action: Any, status: str, *, result: Mapping[str, Any] | None = None, error: str = "") -> dict[str, Any]:
        """Report an action result through raw HTTP and return the answer body.

        The real adapter reports through its transport (which discards the body);
        posting the same wire body directly is what makes the Runtime's answer
        assertable without weakening the transport itself.
        """
        body = self.report_body(action, status, result=result, error=error)
        reply = self.scenario.post(self.result_path(action.action_id), body)
        payload = reply.json
        return payload if isinstance(payload, dict) else {"_http": reply.describe()}

    def context_text(self, session: str) -> str | None:
        """Fetch an injection block the way the host does on an LLM request."""
        request = self._adapter["protocol"].ContextRequest(
            adapter_id=self.adapter_id, session=session, trigger="llm_request"
        )
        return self.loop.call(
            self.bridge.text_for_llm_request(request)
        )

    def poll_once(self) -> int:
        """One adapter polling round, executed to completion on its own loop."""
        return int(self.loop.call(self.consumer.poll_once()))

    # -- background consumer loop --------------------------------------------------

    def start_consumer(self) -> None:
        """Start the adapter's own polling loop, as the plugin does on load."""

        async def _ensure() -> Any:
            task = asyncio.create_task(self.consumer.run(), name="e2e-outbox-consumer")
            self._consumer_task = task
            return task

        self.loop.call(_ensure())

    def stop_consumer(self, *, wait_idle_s: float = 8.0) -> bool:
        """Ask the polling loop to stop and wait for work already in flight."""
        self.consumer.request_stop()
        idle = bool(self.loop.call(self.consumer.wait_idle(wait_idle_s)))
        task = self._consumer_task
        if task is not None:

            async def _cancel() -> None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

            with contextlib.suppress(Exception):
                self.loop.call(_cancel(), timeout=15)
            self._consumer_task = None
        return idle

    def close(self) -> None:
        """Stop every task and thread this harness owns."""
        with contextlib.suppress(Exception):
            self.stop_consumer()
        with contextlib.suppress(Exception):
            self.loop.call(self.transport.aclose(), timeout=10)
        with contextlib.suppress(Exception):
            self.loop.call(self.bridge.aclose(), timeout=10)
        self.loop.close()


def adapter_settings(base_url: str, adapter_id: str, *, overrides: Mapping[str, Any] | None = None) -> Any:
    """Build adapter settings from a raw plugin-style config mapping.

    Values are given in the plugin's own units (milliseconds), so the adapter's
    normalization and clamping are exercised rather than bypassed.
    """
    raw: dict[str, Any] = {
        "enabled": True,
        "runtime_base_url": base_url,
        "runtime_token": "",
        "adapter_id": adapter_id,
        "observe_mode": "all",
        "report_assistant_messages": True,
        "inject_enabled": True,
        "request_timeout_ms": 5000,
        "context_timeout_ms": 500,
        "outbox_enabled": True,
        "outbox_poll_interval_ms": 250,
        "outbox_max_actions_per_poll": 1,
        "outbox_lease_ttl_ms": 1500,
        "outbox_max_concurrency": 1,
        "render_timeout_ms": 30000,
        "send_timeout_ms": 10000,
    }
    raw.update(dict(overrides or {}))
    return load_adapter_modules()["settings"].Settings.from_mapping(raw)


# --------------------------------------------------------------------------------------
# PHASE 1 + 2 — v1 ingestion, idempotency and settlement
# --------------------------------------------------------------------------------------


def phase_events(base_dir: Path) -> None:
    """Verify the v1 event contract: accounting, idempotency, and settlement."""
    V.phase("v1 event ingestion: batch accounting, idempotency, no rewriting")
    with scenario_session(base_dir, "p1_events") as sc:
        now = utc_now()
        explicit_id = "evt_e2e_explicit"
        ambiguous_id = "evt_e2e_ambiguous"
        assistant_id = "evt_e2e_assistant"
        records = [
            {
                "event_id": explicit_id,
                "kind": "user_message",
                "session": SESSION_A,
                "text": EXPLICIT_TEXT,
                "occurred_at": utc_iso(now),
                "platform": "aiocqhttp",
                "sender_id": "user-1",
                "wake": True,
                "preempts_proactive": True,
            },
            {
                "event_id": ambiguous_id,
                "kind": "user_message",
                "session": SESSION_A,
                "text": AMBIGUOUS_TEXT,
                "occurred_at": utc_iso(now + timedelta(seconds=1)),
                "platform": "aiocqhttp",
                "sender_id": "user-1",
                "wake": True,
                "preempts_proactive": True,
            },
            {
                "event_id": assistant_id,
                "kind": "assistant_message",
                "session": SESSION_A,
                "text": "我在呢。",
                "occurred_at": utc_iso(now + timedelta(seconds=2)),
                "platform": "aiocqhttp",
                "self_id": "bot-1",
            },
            {
                "event_id": "evt_e2e_unsupported",
                "kind": "tool_result",
                "session": SESSION_A,
                "text": "this kind is not part of the adapter contract",
                "occurred_at": utc_iso(now + timedelta(seconds=3)),
            },
        ]
        envelope = {"protocol_version": "1", "adapter_id": "e2e-adapter", "events": records}

        before_health = sc.health()
        first = sc.post("/v1/events", envelope)
        after_first_health = sc.health()
        V.check("the batch is accepted with HTTP 200", first.status == 200, first.describe())
        V.check_equal("the response declares protocol version 1", first.field("protocol_version"), "1")
        V.check_equal("three records were appended", first.field("accepted"), 3)
        V.check_equal("no record was reported as a duplicate", first.field("duplicates"), 0)
        V.check_equal("the unusable kind was rejected, not dropped silently", first.field("rejected"), 1)
        outcomes = first.field("outcomes", default=[])
        V.check_equal("one outcome per submitted record, in order", len(outcomes), 4)
        V.check(
            "the unsupported record explains itself",
            isinstance(outcomes[3], dict)
            and str(outcomes[3].get("reason", "")).startswith("unsupported_kind:"),
            _short(outcomes[3] if len(outcomes) > 3 else None),
        )
        V.check(
            "the appended assistant message is echoed back as a fact",
            isinstance(outcomes[2], dict) and outcomes[2].get("event", {}).get("event_id") == assistant_id,
            _short(outcomes[2] if len(outcomes) > 2 else None),
        )
        V.check_equal(
            "runtime_version matches the server's own version",
            first.field("runtime_version"),
            sc.health().get("state_version"),
        )

        # ---- replay: the same envelope must cost nothing -------------------------
        replayed = sc.post("/v1/events", envelope)
        V.check_equal("a replayed envelope appends nothing", replayed.field("accepted"), 0)
        V.check_equal("every known id is recognised as a duplicate", replayed.field("duplicates"), 3)
        V.check_equal("the unusable record is still rejected", replayed.field("rejected"), 1)
        V.check_equal(
            "a replay does not move the Runtime version",
            replayed.field("runtime_version"),
            first.field("runtime_version"),
        )
        V.check_equal(
            "a replay does not append raw history",
            sc.health().get("raw_events"),
            after_first_health.get("raw_events"),
        )
        V.note(
            "the batch appended "
            f"{int(after_first_health.get('raw_events') or 0) - int(before_health.get('raw_events') or 0)} "
            "raw events (the two user messages also carry the Runtime's own bookkeeping events)"
        )

        # ---- a duplicate id carrying *different* text --------------------------
        tampered = {
            "protocol_version": "1",
            "adapter_id": "e2e-adapter",
            "events": [dict(records[0]) | {"text": "这条文本绝不能覆盖原始事件。"}],
        }
        tampered_reply = sc.post("/v1/events", tampered)
        V.check_equal("a duplicate id is a duplicate even when its text differs", tampered_reply.field("duplicates"), 1)
        V.check_equal("nothing was appended for the tampered record", tampered_reply.field("accepted"), 0)
        stored = sc.get(f"/events/{explicit_id}")
        V.check_equal(
            "the stored event keeps its original text",
            stored.field("event", "content"),
            EXPLICIT_TEXT,
        )
        V.check_equal(
            "the stored event keeps its original conversation",
            stored.field("event", "conversation_id"),
            SESSION_A,
        )
        V.check_equal(
            "the tampered replay did not bump the version",
            tampered_reply.field("runtime_version"),
            first.field("runtime_version"),
        )

        # ---- PHASE 2: ambiguous vs explicit settlement ---------------------------
        V.phase("semantic settlement: explicit settles coarse, ambiguous is deferred")
        explicit = outcomes[0] if outcomes else {}
        ambiguous = outcomes[1] if len(outcomes) > 1 else {}
        V.check_equal("the explicit event is settled", explicit.get("semantic_status"), "resolved")
        V.check_equal("the settlement came from the coarse rule table", explicit.get("appraisal_source"), "coarse_rule")
        V.check(
            "the explicit event produced an emotional after-effect",
            bool(explicit.get("emotion_event_ids")),
            _short(explicit.get("emotion_event_ids")),
        )
        V.check_equal("the ambiguous event stays unresolved", ambiguous.get("semantic_status"), "unresolved")
        V.check_equal("the ambiguous event consulted nothing", ambiguous.get("appraisal_source"), "deferred")
        V.check(
            "no emotion was fabricated for the ambiguous event",
            ambiguous.get("emotion_event_ids") == [],
            _short(ambiguous.get("emotion_event_ids")),
        )
        V.check(
            "the ambiguous event carries a cheap relevance hint instead of a verdict",
            str(ambiguous.get("potential_relevance") or "") in {"low", "medium", "high"},
            _short(ambiguous.get("potential_relevance")),
        )

        backlog = sc.get("/cognition/backlog")
        items = backlog.field("items", default=[])
        V.check(
            "the deferred event is on the deep-refresh backlog",
            any(item.get("event_id") == ambiguous_id for item in items if isinstance(item, dict)),
            _short([item.get("event_id") for item in items if isinstance(item, dict)]),
        )
        matching = [item for item in items if isinstance(item, dict) and item.get("event_id") == ambiguous_id]
        V.check_equal(
            "the backlog preserves the raw text verbatim",
            matching[0].get("content") if matching else None,
            AMBIGUOUS_TEXT,
        )
        V.check_equal("exactly one event is waiting for a later reading", backlog.field("stats", "unresolved"), 1)
        V.check(
            "the settled event left the backlog",
            all(item.get("event_id") != explicit_id for item in items if isinstance(item, dict)),
            _short([item.get("event_id") for item in items if isinstance(item, dict)]),
        )
        still_there = sc.get(f"/events/{ambiguous_id}")
        V.check_equal(
            "the ambiguous raw event is byte-identical after being deferred",
            still_there.field("event", "content"),
            AMBIGUOUS_TEXT,
        )
        V.check(
            "history is byte-identical after the tampered replay",
            sc.get(f"/events/{explicit_id}").field("event", "content") == EXPLICIT_TEXT,
        )


# --------------------------------------------------------------------------------------
# PHASE 3 — the autonomous scheduler around the live Runtime
# --------------------------------------------------------------------------------------


def _scheduler_config(config: Any) -> None:
    """Compress the scheduler cadence so an autonomous round happens promptly."""
    config.scheduler.min_interval_seconds = 0.5
    config.scheduler.max_interval_seconds = 2.0
    config.scheduler.busy_poll_seconds = 0.5
    config.utility.min_sleep_seconds = 1.0
    config.utility.max_sleep_seconds = 30.0
    config.drive.cooldown_seconds = 0.0


def phase_scheduler(base_dir: Path) -> None:
    """Verify the Scheduler drives a round and produces outbox work on its own."""
    V.phase("autonomous Scheduler: a round and an outbox row with no request driving it")
    gate = threading.Event()
    with scenario_session(
        base_dir,
        "p3_scheduler",
        mutate=_scheduler_config,
        epoch_offset=OFFLINE_WINDOW,
        with_scheduler=True,
        scheduler_gate=gate,
    ) as sc:
        scheduler = sc.scheduler()
        V.check("a Scheduler is attached to the server's loop", scheduler is not None)
        V.check(
            "the Scheduler is held at the gate while the state is prepared",
            bool(scheduler is not None and not scheduler.status().get("running")),
            _short(scheduler.status() if scheduler else None),
        )

        message = backfill_user_message(sc, session=SESSION_A)
        V.check("the backfilled message was accepted", message.field("accepted") == 1, message.describe())
        matters = open_matters(sc)
        V.check(
            "the offline message left an unfinished matter behind",
            bool(matters),
            _short([(item.get("title"), item.get("status")) for item in matters]),
        )
        overdue = [
            item
            for item in matters
            if (parse_iso(item.get("waiting_until")) or utc_now() + timedelta(days=1)) < utc_now()
        ]
        V.check(
            "the matter is already overdue, so the character has a concrete reason to speak",
            bool(overdue),
            _short([(item.get("title"), item.get("status"), item.get("waiting_until")) for item in matters]),
        )

        # A second, deliberately future-dated obligation: it gives the scheduler a
        # real anchor in live state, which is exactly what "plan from state" means.
        anchor_at = utc_now() + timedelta(minutes=5)
        created = sc.post(
            "/unfinished",
            {
                "title": "下周的旅行安排",
                "waiting_until": utc_iso(anchor_at),
                "priority": 0.7,
                "source_event_ids": [],
            },
        )
        V.check("a future-dated obligation can be registered", created.status == 200, created.describe())
        pre_round = sc.get("/schedule").json or {}
        anchors = pre_round.get("anchors") or {}
        V.check(
            "before any round the plan is anchored on live Runtime state",
            bool(anchors.get("unfinished")) or bool(anchors.get("candidate")) or bool(anchors.get("maintenance")),
            _short(anchors),
        )
        V.check(
            "the future-dated obligation is the anchor it reports",
            parse_iso(anchors.get("unfinished")) == anchor_at,
            _short({"anchors": anchors.get("unfinished"), "expected": utc_iso(anchor_at)}),
        )
        V.check_equal(
            "nothing is in flight yet, so the dispatch gate is open",
            pre_round.get("dispatch_allowed"),
            True,
        )

        # Let the Scheduler go: from here on nothing but the loop itself runs rounds.
        gate.set()
        V.check(
            "the Scheduler is running once the gate opens",
            bool(scheduler is not None and wait_until(lambda: scheduler.status().get("running"), timeout=10.0)),
        )
        reached_round = wait_until(
            lambda: scheduler is not None and int(scheduler.status().get("rounds") or 0) >= 1,
            timeout=45.0,
            interval=0.2,
        )
        status = scheduler.status() if scheduler is not None else {}
        V.check(
            "the Scheduler completed at least one endogenous round unprompted",
            reached_round,
            _short(status),
        )

        produced = wait_until(
            lambda: bool(pending_rows(sc, kind="render")),
            timeout=45.0,
            interval=0.2,
        )
        rows = pending_rows(sc, kind="render")
        V.check(
            "the autonomous round queued render work in the outbox",
            produced,
            _short([(row.get("outbox_id"), row.get("kind"), row.get("conversation_id")) for row in rows]),
        )
        if not produced:
            V.diagnostics("scheduler status", status)
            V.diagnostics("schedule plan", sc.get("/schedule").json)
            V.diagnostics("attempts", sc.get("/attempts").field("attempts", default=[]))
            V.diagnostics("health", sc.health())
        else:
            row = rows[0]
            V.check_equal(
                "the proactive row is addressed to the session it was formed in",
                row.get("conversation_id"),
                SESSION_A,
            )
            attempt_id = str(row.get("payload", {}).get("attempt_id") or "")
            V.check("the proactive row names the attempt it belongs to", bool(attempt_id), _short(row.get("payload")))
            attempt = sc.attempt(attempt_id)
            V.check(
                "the attempt is committed (a decision, not a delivery)",
                attempt is not None and attempt.state in {"committed", "rendering"},
                _short(getattr(attempt, "state", None)),
            )
            V.check(
                "the attempt is grounded in the overdue obligation",
                bool(attempt is not None and (attempt.candidate_id or "")),
                _short(getattr(attempt, "candidate_id", None)),
            )
            after_state = sc.state()
            V.check(
                "the autonomous round advanced the Runtime version",
                int(after_state.get("version") or 0) > 1,
                _short(after_state.get("version")),
            )
            V.check(
                "the overdue obligation was consumed rather than left waiting",
                all(
                    item.get("status") != "waiting"
                    for item in open_matters(sc)
                    if (parse_iso(item.get("waiting_until")) or utc_now()) < utc_now()
                ),
                _short([(item.get("title"), item.get("status")) for item in open_matters(sc)]),
            )

        plan = status.get("plan") or {}
        V.check(
            "the loop published the wake plan it is waiting on",
            bool(plan.get("next_wake_at")),
            _short(plan),
        )
        V.check(
            "the plan explains itself with at least one reason",
            bool(plan.get("reasons")),
            _short(plan.get("reasons")),
        )
        after_round = sc.get("/schedule").json or {}
        V.check(
            "the schedule endpoint reports a dispatch verdict",
            "dispatch_allowed" in after_round,
            _short(after_round.get("dispatch_reason")),
        )
        V.check(
            "a committed-but-undelivered attempt closes the gate for new rounds",
            after_round.get("dispatch_allowed") is False
            and after_round.get("dispatch_reason") == "attempt_in_flight",
            _short({"allowed": after_round.get("dispatch_allowed"), "reason": after_round.get("dispatch_reason")}),
        )
        V.note(
            "the loop kept replanning without any request: "
            f"{int(status.get('rounds') or 0)} round(s) so far"
        )


# --------------------------------------------------------------------------------------
# PHASE 4 + 5 + 6 — the full adapter contract
# --------------------------------------------------------------------------------------


def _adapter_config(config: Any) -> None:
    """Config for the adapter phases: fast committed actions, no cooldown drift."""
    config.drive.cooldown_seconds = 0.0
    config.scheduler.foreground_pause_seconds = 0.0


def phase_adapter_contract(base_dir: Path) -> None:
    """Verify the adapter contract end to end, including replay and attribution."""
    V.phase("adapter contract: lease → heartbeat → render → authorize → send → result")
    with scenario_session(
        base_dir, "p4_adapter", mutate=_adapter_config, epoch_offset=OFFLINE_WINDOW
    ) as sc:
        platform = FakePlatform()
        platform.register(SESSION_A)
        llm = FakeMainLLM()
        host = AdapterHarness(scenario=sc, platform=platform, llm=llm)
        try:
            seed = seed_committed_attempt(sc, session=SESSION_A)
            attempt_id = seed["attempt_id"] or ""
            render_row_id = seed["outbox_id"] or ""
            V.check(
                "the Runtime committed an attempt for the offline message",
                bool(attempt_id),
                _short(seed["decision"]),
            )
            V.check("the commit queued a render outbox row", bool(render_row_id), _short(render_row_id))
            if not (attempt_id and render_row_id):
                V.diagnostics("round decision", seed["decision"])
                V.diagnostics("health", sc.health())
                return

            # ---- context injection -------------------------------------------------
            block = host.context_text(SESSION_A)
            V.check("the host can fetch an injection block for the session", bool(block), _short(block, 80))
            V.check(
                "the injected block is wrapped and version tagged",
                bool(block and block.startswith("<companion_runtime_context")),
                _short(block, 120),
            )
            V.check(
                "the block states the priority order rather than instructing",
                bool(block and "当前用户原话" in block),
                _short(block, 160),
            )

            # ---- render lease ------------------------------------------------------
            leased = host.lease(capabilities=("render",), max_actions=1)
            V.check_equal("the adapter leased exactly one action", len(leased), 1)
            if not leased:
                V.diagnostics("outbox row", _row_dict(sc, render_row_id))
                return
            render_action = leased[0]
            V.check_equal("the action is the attempt's render row", render_action.action_id, render_row_id)
            V.check_equal("the action type is render", render_action.action_type, "render")
            V.check_equal("the lease reports the originating session", render_action.session, SESSION_A)
            V.check_equal("the lease carries the attempt id", render_action.attempt_id, attempt_id)
            V.check(
                "the lease id encodes adapter, row and claim count",
                render_action.lease_id == f"{host.adapter_id}:{render_row_id}:1",
                _short(render_action.lease_id),
            )
            V.check(
                "the lease reports a positive remaining TTL",
                render_action.lease_ttl_ms > 0,
                _short(render_action.lease_ttl_ms),
            )
            prompt = str(render_action.payload.get("prompt") or "")
            V.check(
                "the Runtime composed the prompt (the adapter composes no semantics)",
                "【现在要写的话】" in prompt and "只输出要发送的消息正文本身" in prompt,
                _short(prompt, 200),
            )
            attempt_intent = str(getattr(sc.attempt(attempt_id), "intent", "") or "")
            V.check(
                "the prompt carries the intent the Runtime decided on",
                bool(attempt_intent) and attempt_intent in prompt,
                _short({"intent": attempt_intent, "prompt": prompt[:160]}),
            )

            # ---- heartbeat ---------------------------------------------------------
            before_lease = parse_iso(getattr(sc.outbox_row(render_row_id), "lease_expires_at", None))
            extended = host.heartbeat(render_action)
            after_row = sc.outbox_row(render_row_id)
            after_lease = parse_iso(getattr(after_row, "lease_expires_at", None))
            V.check("the Runtime confirmed the lease extension", extended is True)
            V.check(
                "the extension moved the deadline forward",
                bool(before_lease and after_lease and after_lease > before_lease),
                f"before={before_lease} after={after_lease}",
            )
            stale = host.heartbeat_with_lease_id(
                render_row_id, f"{host.adapter_id}:{render_row_id}:99"
            )
            V.check("a stale lease id is refused", stale is False, _short(stale))
            V.check_equal(
                "a refused heartbeat leaves the deadline alone",
                parse_iso(getattr(sc.outbox_row(render_row_id), "lease_expires_at", None)),
                after_lease,
            )

            # ---- render, report, verify the send row ------------------------------
            rendered = host.loop.call(host.executor.render(render_action))
            rendered_text = str(rendered.get("text") or "")
            V.check(
                "the fake main LLM produced the message from the Runtime's prompt",
                rendered_text.startswith("突然想起") and rendered_text.endswith("还顺利吗？"),
                _short(rendered_text),
            )
            V.check_equal(
                "the render reports which provider spoke",
                rendered.get("provider_id"),
                f"e2e-provider::{SESSION_A}",
            )
            render_report = host.report(render_action, "ok", result=rendered)
            V.check("the Runtime accepted the render report", render_report.get("ok") is True, _short(render_report))
            send_row_id = str(render_report.get("send_outbox_id") or "")
            V.check("the render queued a send row", bool(send_row_id), _short(render_report))
            V.check_equal(
                "the attempt reached ready_to_send",
                render_report.get("attempt_state"),
                "ready_to_send",
            )
            if send_row_id:
                send_row = sc.outbox_row(send_row_id)
                V.check_equal(
                    "the queued send carries the rendered text",
                    getattr(send_row, "payload", {}).get("text"),
                    rendered_text,
                )
                V.check_equal(
                    "the queued send stays in the originating session",
                    getattr(send_row, "conversation_id", None),
                    SESSION_A,
                )

            # ---- send lease, authorize, deliver, report ---------------------------
            send_actions = host.lease(capabilities=("send",), max_actions=1)
            V.check_equal("the adapter leased the send action", len(send_actions), 1)
            if not send_actions:
                return
            send_action = send_actions[0]
            V.check_equal("the send targets the same attempt", send_action.attempt_id, attempt_id)
            V.check_equal("the send carries the rendered text", send_action.payload.get("text"), rendered_text)

            verdict = host.authorize(send_action, rendered_text)
            V.check("the send is authorized", verdict.authorized is True, _short(verdict.reason))
            V.check_equal("the verdict names the permission", verdict.reason, "permitted")
            V.check(
                "the Runtime never rewrites the text (it owns no generator)",
                verdict.text == "",
                _short(verdict.text),
            )
            mismatched = host.authorize(send_action, rendered_text, text_sha256="0" * 64)
            V.check("a text digest mismatch fails closed", mismatched.authorized is False, _short(mismatched.reason))
            V.check_equal("and it says why", mismatched.reason, "text_sha256_mismatch")

            delivered = host.loop.call(host.executor.send(send_action, rendered_text))
            V.check("the fake platform accepted the message", delivered.get("sent") is True, _short(delivered))
            V.check_equal(
                "the platform received exactly the authorized text",
                platform.texts_for(SESSION_A),
                [rendered_text],
            )
            send_report = host.report(
                send_action, "ok", result=delivered | {"authorized": True}
            )
            V.check("the Runtime accepted the delivery report", send_report.get("ok") is True, _short(send_report))
            V.check("the Runtime recorded the delivery", send_report.get("delivered") is True, _short(send_report))
            attempt = sc.attempt(attempt_id)
            V.check_equal("the attempt is sent", getattr(attempt, "state", None), "sent")

            # ---- PHASE: replay and the daily counter ------------------------------
            V.phase("replay and counting: a repeated report converges, the day counts once")
            state_after_send = sc.state()
            V.check_equal(
                "the delivered message charged the daily counter exactly once",
                state_after_send.get("contact_count_today"),
                1,
            )
            replay_render = host.report(render_action, "ok", result=rendered)
            V.check(
                "a replayed render report is acknowledged as a duplicate",
                replay_render.get("ok") is True and replay_render.get("duplicate") is True,
                _short(replay_render),
            )
            replay_send = host.report(send_action, "ok", result=delivered | {"authorized": True})
            V.check(
                "a replayed delivery report is acknowledged as a duplicate",
                replay_send.get("ok") is True and replay_send.get("duplicate") is True,
                _short(replay_send),
            )
            sends = sc.runtime.projections.outbox.find_for_attempt(attempt_id, kind="send")
            V.check_equal("exactly one send row exists for the attempt", len(sends), 1)
            V.check_equal(
                "the replay did not count a second contact",
                sc.state().get("contact_count_today"),
                1,
            )
            V.check_equal(
                "the replay did not send a second message",
                len(platform.texts_for(SESSION_A)),
                1,
            )
            V.check_equal(
                "exactly one proactive_sent event was recorded",
                sc.runtime.events.count("proactive_sent"),
                1,
            )

            # ---- PHASE: the user reply -------------------------------------------
            V.phase("user reply: attributed to the sent attempt exactly once")
            reply = post_v1_event(
                sc,
                {
                    "event_id": "evt_e2e_reply",
                    "kind": "user_message",
                    "session": SESSION_A,
                    "text": "刚忙完，我看到你的消息啦，谢谢你。",
                    "occurred_at": utc_iso(),
                    "wake": True,
                    "preempts_proactive": True,
                },
            )
            outcome = (reply.field("outcomes", default=[{}]) or [{}])[0]
            V.check_equal(
                "the reply is attributed to the attempt that was awaiting one",
                outcome.get("attributed_attempt_id"),
                attempt_id,
            )
            V.check(
                "the reply produced an interaction observation",
                bool(outcome.get("observation_id")),
                _short(outcome.get("observation_id")),
            )
            V.check_equal(
                "the attempt is resolved by the attribution",
                getattr(sc.attempt(attempt_id), "state", None),
                "resolved",
            )
            V.check_equal(
                "the reply did not count a second contact",
                sc.state().get("contact_count_today"),
                1,
            )

            second = post_v1_event(
                sc,
                {
                    "event_id": "evt_e2e_reply_2",
                    "kind": "user_message",
                    "session": SESSION_A,
                    "text": "对了，晚上吃什么好呢。",
                    "occurred_at": utc_iso(),
                    "wake": True,
                    "preempts_proactive": True,
                },
            )
            second_outcome = (second.field("outcomes", default=[{}]) or [{}])[0]
            V.check(
                "a second message is not folded into the same attempt",
                not second_outcome.get("attributed_attempt_id"),
                _short(second_outcome.get("attributed_attempt_id")),
            )
            observations = sc.get("/observations").field("count", default=0)
            V.check_equal("the attempt carries exactly one observation", observations, 1)
            V.check_equal(
                "the daily counter is still one after the reply",
                sc.state().get("contact_count_today"),
                1,
            )
        finally:
            host.close()


def _short_lease_config(config: Any) -> None:
    """Adapter config with a short Runtime lease, so heartbeats are observable.

    A three second lease is a legitimate deployment choice, and it is also the only
    way to observe the adapter's heartbeat loop: the lease TTL drives the heartbeat
    interval, and the Runtime grants its own ``outbox.lease_seconds`` rather than
    whatever the adapter asked for.
    """
    _adapter_config(config)
    config.outbox.lease_seconds = 3.0


def phase_adapter_sessions(base_dir: Path) -> None:
    """Verify multi-session routing and the adapter's own polling loop."""
    V.phase("multi-session routing: the second session round-trips through the adapter loop")
    with scenario_session(
        base_dir, "p4b_sessions", mutate=_short_lease_config, epoch_offset=OFFLINE_WINDOW
    ) as sc:
        platform = FakePlatform()
        platform.register(SESSION_A)
        platform.register(SESSION_B)
        llm = FakeMainLLM(delay_s=2.5)
        host = AdapterHarness(scenario=sc, platform=platform, llm=llm, adapter_id="e2e-session-b")
        try:
            seed = seed_committed_attempt(sc, session=SESSION_B)
            attempt_id = seed["attempt_id"] or ""
            render_row_id = seed["outbox_id"] or ""
            V.check("the second session's intention committed", bool(attempt_id), _short(seed["decision"]))
            if not (attempt_id and render_row_id):
                V.diagnostics("round decision", seed["decision"])
                return
            row = sc.outbox_row(render_row_id)
            V.check_equal(
                "the proactive row belongs to the session it was formed in, not the process default",
                getattr(row, "conversation_id", None),
                SESSION_B,
            )
            V.check(
                "the process default conversation was not used",
                getattr(row, "conversation_id", None) != DEFAULT_SESSION,
                _short(getattr(row, "conversation_id", None)),
            )

            # An adapter that asked for another session must be told, not silently served.
            filtered = host.lease_request(capabilities=("render",), sessions=(SESSION_A,))
            V.check_equal("an adapter asking for the wrong session gets nothing", len(filtered), 0)
            nack = sc.post(f"/outbox/{render_row_id}/nack", {"error": "session_filter_probe"})
            V.check("the deliberately leased row can be released again", nack.ok, nack.describe())
            V.check_equal("the released row is claimable again", nack.field("status"), "pending")

            # The real adapter loop: poll, render slowly (heartbeating), send.
            host.start_consumer()
            delivered = wait_until(
                lambda: bool(platform.texts_for(SESSION_B)),
                timeout=40.0,
                interval=0.2,
            )
            host.stop_consumer()
            texts = platform.texts_for(SESSION_B)
            V.check(
                "the adapter's own polling loop delivered the proactive message",
                delivered,
                _short(texts),
            )
            V.check_equal(
                "the message reached the second session only",
                platform.texts_for(SESSION_A),
                [],
            )
            stats = host.consumer.stats
            V.check(
                "the slow render was kept alive by lease heartbeats",
                stats.heartbeats >= 1,
                _short(
                    {
                        "heartbeats": stats.heartbeats,
                        "lost": stats.heartbeats_lost,
                        "errors": stats.heartbeat_errors,
                        "render_delay_s": llm.delay_s,
                        "lease_seconds": sc.config.outbox.lease_seconds,
                    }
                ),
            )
            V.check(
                "the lease was never lost or allowed to lapse silently",
                stats.heartbeats_lost == 0,
                _short({"lost": stats.heartbeats_lost, "errors": stats.heartbeat_errors}),
            )
            V.check(
                "the consumer rendered and sent exactly one action each",
                stats.rendered == 1 and stats.sent == 1,
                _short({"rendered": stats.rendered, "sent": stats.sent, "failed": stats.failed}),
            )
            V.check(
                "the delivered text is the main LLM's rendering of the Runtime's prompt",
                bool(texts) and bool(llm.calls) and texts[0] == render_text_for(llm.calls[0]["prompt"]),
                _short({"texts": texts, "prompts": len(llm.calls)}),
            )
            V.check_equal(
                "the second session's contact was counted once",
                sc.state().get("contact_count_today"),
                1,
            )
            V.check_equal(
                "the attempt is sent after the consumer's report",
                getattr(sc.attempt(attempt_id), "state", None),
                "sent",
            )
            V.check(
                "the consumer recorded the render and delivery reports",
                len(host.reports_with(action_type="render")) == 1
                and len(host.reports_with(action_type="send")) == 1,
                _short([(report.action_type, report.status) for report in host.reports]),
            )
        finally:
            host.close()


# --------------------------------------------------------------------------------------
# PHASE 7 — authorize unavailable must stay recoverable, never terminal
# --------------------------------------------------------------------------------------


def _exercise_marker_branch(
    sc: Scenario,
    host: AdapterHarness,
    *,
    send_row_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Lease a send row, report an outage marker, and pin the retry branch's shape.

    The report is the shipped adapter shape: ``status=failed`` with
    ``result.authorize_unavailable=true``. The branch must answer with the
    current ``/v1`` result contract (``retryable`` as the primary field, mirrored
    by ``requeued``), return the row to the queue, and leave the intention
    completely untouched.

    Returns:
        ``{"action", "body", "answer", "row"}``, or ``{}`` when the row could not
        be leased (the caller then has nothing to verify).
    """
    actions = host.lease(capabilities=("send",), max_actions=1)
    V.check_equal("the send row was leased", len(actions), 1)
    if not actions:
        return {}
    action = actions[0]
    body = host.report_body(
        action,
        "failed",
        error="authorize_unavailable: simulated outage",
        result={"authorized": False, "authorize_unavailable": True},
    )
    reply = sc.post(host.result_path(action.action_id), body)
    answer = reply.json or {}
    V.check("the retry branch answers 200", reply.status == 200, reply.describe())
    V.check_equal("the branch reports ok", answer.get("ok"), True)
    V.check_equal("the branch reports the claim as retryable", answer.get("retryable"), True)
    V.check_equal(
        "requeued mirrors retryable",
        answer.get("requeued"),
        answer.get("retryable"),
    )
    V.check_equal("a first requeue is not a duplicate", answer.get("duplicate"), False)
    V.check_equal("the attempt state is reported untouched", answer.get("attempt_state"), "ready_to_send")
    V.check_equal("the answer names the row", answer.get("action_id"), send_row_id)
    V.check_equal("the answer names the kind", answer.get("action_type"), "send")
    V.check_equal("the answer carries the protocol version", answer.get("protocol_version"), "1")
    V.check(
        "a clean requeue carries no denial reason",
        "reason" not in answer,
        _short(answer.get("reason")),
    )
    V.check(
        "the answer states when the row became claimable again",
        parse_iso(answer.get("available_at")) is not None,
        _short(answer.get("available_at")),
    )
    V.check(
        "the answer echoes the reason the claim came back",
        "authorize_unavailable" in str(answer.get("error") or ""),
        _short(answer.get("error")),
    )

    row = sc.outbox_row(send_row_id)
    attempt = sc.attempt(attempt_id)
    V.check_equal("the row is pending again, not failed", getattr(row, "status", None), "pending")
    V.check_equal("the intention was never terminated", getattr(attempt, "state", None), "ready_to_send")
    V.check_equal("the release dropped the lease owner", getattr(row, "lease_owner", None), None)
    V.check(
        "the claim counter was not refunded (the documented lease-id trade-off)",
        int(getattr(row, "attempts", 0) or 0) >= 1,
        _short({"attempts": getattr(row, "attempts", None), "max_attempts": getattr(row, "max_attempts", None)}),
    )
    V.check(
        "the row records why it came back",
        "authorize_unavailable" in str(getattr(row, "last_error", "") or ""),
        _short(getattr(row, "last_error", None)),
    )
    V.check_equal("nothing was delivered", host.platform.deliveries, [])
    return {"action": action, "body": body, "answer": answer, "row": row}


def _seed_send_row(sc: Scenario, *, session: str, text: str) -> tuple[str, str] | None:
    """Prepare a committed attempt with a pending *send* row and return the ids."""
    seed = seed_committed_attempt(sc, session=session)
    attempt_id = seed["attempt_id"] or ""
    render_row_id = seed["outbox_id"] or ""
    if not (attempt_id and render_row_id):
        V.diagnostics("round decision", seed["decision"])
        return None
    attached = sc.post("/rendered", {"attempt_id": attempt_id, "text": text, "now": utc_iso()})
    if not attached.ok:
        V.diagnostics("render attach", attached.describe())
        return None
    rows = sc.runtime.projections.outbox.find_for_attempt(attempt_id, kind="send")
    if not rows:
        return None
    return attempt_id, rows[0].outbox_id


def _marker_config(config: Any) -> None:
    """Short leases plus a one-claim budget, to probe the outage path's rules."""
    _short_lease_config(config)
    # One claim allowed: an outage must still be able to return the row, because
    # "the Runtime could not be asked" is not a delivery attempt. If the requeue
    # path ever grew an exhaustion rule, this scenario would close the intention.
    config.outbox.max_attempts = 1


def _outage_config(config: Any) -> None:
    """A short lease, so the lease-expiry recovery is observable within a test.

    The shipped adapter deliberately reports *nothing* when it cannot obtain an
    authorization verdict, which means the Runtime's own ``reclaim_expired`` is the
    only thing that returns the row -- and that takes one full lease. One second
    keeps the scenario honest (the row really does expire) without stalling the run.
    """
    _adapter_config(config)
    config.outbox.lease_seconds = 1.0
    config.outbox.max_attempts = 3


def phase_authorize_unavailable(base_dir: Path) -> None:
    """Verify an unreachable authorize never closes an intention, on either side."""
    V.phase("authorize outage: no result at all, then lease expiry recovers the work")
    with scenario_session(
        base_dir,
        "p7_authorize_unavailable",
        mutate=_outage_config,
        epoch_offset=OFFLINE_WINDOW,
    ) as sc:
        platform = FakePlatform()
        platform.register(SESSION_A)
        prepared = _seed_send_row(sc, session=SESSION_A, text="突然想起你面试的事，还顺利吗？")
        V.check("an attempt with a queued send exists", prepared is not None)
        if prepared is None:
            return
        attempt_id, send_row_id = prepared
        send_text = str(getattr(sc.outbox_row(send_row_id), "payload", {}).get("text") or "")
        lease_seconds = float(sc.config.outbox.lease_seconds)

        outage = "simulated outage: connect to the Runtime refused"
        adapter_id = "e2e-outage"
        host = AdapterHarness(
            scenario=sc,
            platform=platform,
            llm=FakeMainLLM(),
            adapter_id=adapter_id,
            authorize_failure=outage,
        )
        try:
            host.poll_once()
            stats = host.consumer.stats
            V.check_equal("the adapter leased the action", len(host.leases()), 1)
            V.check_equal("nothing was delivered while the verdict was unknown", platform.deliveries, [])

            # ---- the contract: silence, not a verdict ------------------------------
            V.check_equal(
                "the adapter counted the authorize failure as an outage, not a refusal",
                stats.authorize_errors,
                1,
            )
            V.check_equal("the adapter did not record a refusal", stats.rejected, 0)
            V.check_equal(
                "the adapter posted no result at all for the action",
                host.reports,
                [],
            )
            V.check_equal(
                "the outage was parked for the Runtime's recovery, not counted as a failure",
                (int(getattr(stats, "deferred", 0) or 0), int(stats.failed)),
                (1, 0),
            )

            row = sc.outbox_row(send_row_id)
            attempt = sc.attempt(attempt_id)
            V.check_equal("the row is left leased, not settled", getattr(row, "status", None), "leased")
            V.check_equal(
                "the Runtime holds the row under the adapter's claim",
                getattr(row, "lease_owner", None),
                adapter_id,
            )
            V.check(
                "the Runtime was told nothing about the claim",
                getattr(row, "last_error", None) is None,
                _short(getattr(row, "last_error", None)),
            )
            V.check_equal(
                "the intention was not terminated",
                getattr(attempt, "state", None),
                "ready_to_send",
            )
            V.check_equal(
                "no failure reason was written onto the intention",
                getattr(attempt, "failure_reason", None),
                None,
            )
            V.check_equal(
                "no contact was counted for a message that never left",
                sc.state().get("contact_count_today"),
                0,
            )
            first_lease = host.leases()[0]
            V.check_equal("the deferred claim is the row under test", first_lease.action_id, send_row_id)
            V.check_equal(
                "the first lease id encodes the first claim",
                first_lease.lease_id,
                f"{adapter_id}:{send_row_id}:1",
            )
            V.note(
                "the shipped adapter stays silent on purpose (plugin README §4.5): the Runtime "
                "records every non-ok send result as terminal, so reporting one would drop the "
                "message over a blip the Runtime never saw"
            )

            # ---- the lease expires and the Runtime reclaims the work ----------------
            time.sleep(lease_seconds + 0.35)
            tick = sc.post("/tick", {"now": utc_iso()})
            V.check(
                "a plain tick reclaimed the expired lease",
                int(tick.field("released_leases") or 0) >= 1,
                _short(tick.json),
            )
            row = sc.outbox_row(send_row_id)
            V.check_equal("the row is pending again", getattr(row, "status", None), "pending")
            V.check_equal("the claim was released", getattr(row, "lease_owner", None), None)
            V.check_equal(
                "the reclaim charged the claim, not the delivery budget",
                int(getattr(row, "attempts", 0) or 0),
                1,
            )
            V.check_equal(
                "the intention survived the outage",
                getattr(sc.attempt(attempt_id), "state", None),
                "ready_to_send",
            )
            V.check(
                "the reclaim left the reason on the row",
                "lease expired" in str(getattr(row, "last_error", "") or ""),
                _short(getattr(row, "last_error", None)),
            )

            # ---- a healthy adapter re-leases it under a fresh claim ------------------
            healthy = AdapterHarness(
                scenario=sc, platform=platform, llm=FakeMainLLM(), adapter_id=adapter_id
            )
            try:
                healthy.poll_once()
            finally:
                healthy.close()
            V.check_equal("the recovered row was re-leased", len(healthy.leases()), 1)
            if healthy.leases():
                second_lease = healthy.leases()[0]
                V.check_equal("the re-lease is the same row", second_lease.action_id, send_row_id)
                V.check_equal(
                    "the re-lease carries the same intention",
                    second_lease.attempt_id,
                    attempt_id,
                )
                V.check(
                    "the re-lease carries a fresh lease id for the new claim",
                    second_lease.lease_id != first_lease.lease_id
                    and second_lease.lease_id == f"{adapter_id}:{send_row_id}:2",
                    _short({"before": first_lease.lease_id, "after": second_lease.lease_id}),
                )
            V.check_equal(
                "the retry delivered the message exactly once",
                platform.texts_for(SESSION_A),
                [send_text],
            )
            V.check_equal(
                "exactly one contact was counted for it",
                sc.state().get("contact_count_today"),
                1,
            )
            V.check_equal(
                "exactly one proactive_sent event exists",
                sc.runtime.events.count("proactive_sent"),
                1,
            )
            V.check_equal(
                "exactly one assistant message was recorded",
                sc.runtime.events.count("assistant_message"),
                1,
            )
            V.check_equal(
                "the attempt is sent after the retry",
                getattr(sc.attempt(attempt_id), "state", None),
                "sent",
            )
            V.check_equal(
                "the row ends delivered, not failed",
                getattr(sc.outbox_row(send_row_id), "status", None),
                "delivered",
            )
        finally:
            host.close()

    # ---- the runtime's retry branch for an explicit outage marker -----------------
    # No shipped adapter sends this: the plugin stays silent and lets the lease expire
    # (the phase above). The branch is the Runtime's compatibility path for an adapter
    # that *does* report the outage, and it must never close the intention either.
    V.phase("outage marker report (runtime compatibility): the retry branch shape")
    with scenario_session(
        base_dir,
        "p7b_authorize_marker",
        mutate=_short_lease_config,
        epoch_offset=OFFLINE_WINDOW,
    ) as sc:
        platform = FakePlatform()
        platform.register(SESSION_A)
        prepared = _seed_send_row(sc, session=SESSION_A, text="随口问一句，最近还好吗？")
        V.check("an attempt with a queued send exists", prepared is not None)
        if prepared is None:
            return
        attempt_id, send_row_id = prepared
        send_text = str(getattr(sc.outbox_row(send_row_id), "payload", {}).get("text") or "")
        host = AdapterHarness(scenario=sc, platform=platform, llm=FakeMainLLM(), adapter_id="e2e-old")
        try:
            # ---- 1. the shipped report shape: status=failed + result marker --------
            first = _exercise_marker_branch(
                sc, host, send_row_id=send_row_id, attempt_id=attempt_id
            )
            if not first:
                return
            action = first["action"]
            marker_body = first["body"]
            answer = first["answer"]
            V.check_equal(
                "the report uses the shipped shape (status failed + result marker)",
                (marker_body.get("status"), marker_body.get("result", {}).get("authorize_unavailable")),
                ("failed", True),
            )
            V.check(
                "the row is immediately claimable without a requested backoff",
                parse_iso(answer.get("available_at")) is not None
                and (parse_iso(answer.get("available_at")) - utc_now()).total_seconds() < 1.0,
                _short(answer.get("available_at")),
            )
            V.check_equal(
                "the outage report did not count a contact",
                sc.state().get("contact_count_today"),
                0,
            )

            # ---- 2. idempotency is keyed on the claim ------------------------------
            replay = sc.post(host.result_path(action.action_id), marker_body)
            replay_body = replay.json or {}
            V.check(
                "a replayed outage report is a no-op, not a second requeue",
                replay_body.get("ok") is True
                and replay_body.get("duplicate") is True
                and replay_body.get("retryable") is False
                and replay_body.get("requeued") is False,
                _short(replay_body),
            )
            V.check(
                "a no-op replay reports no new availability time",
                "available_at" not in replay_body,
                _short(replay_body.get("available_at")),
            )
            V.check_equal(
                "the replay did not touch the attempt",
                getattr(sc.attempt(attempt_id), "state", None),
                "ready_to_send",
            )

            # ---- 3. the adapter's requested backoff is honoured ---------------------
            again = host.lease(capabilities=("send",), max_actions=1)
            V.check_equal("the requeued row is claimable again", len(again), 1)
            post_backoff: list[Any] = []
            if again:
                delayed_body = host.report_body(
                    again[0],
                    "failed",
                    error="authorize_unavailable: backing off",
                    result={"authorize_unavailable": True, "retry_after_ms": 1500},
                )
                delayed = sc.post(host.result_path(again[0].action_id), delayed_body).json or {}
                V.check_equal("the delayed requeue is acknowledged", delayed.get("retryable"), True)
                available_at = parse_iso(delayed.get("available_at"))
                V.check(
                    "the requested retry_after_ms became the row's availability",
                    available_at is not None and 0.2 < (available_at - utc_now()).total_seconds() <= 2.0,
                    _short({"available_at": delayed.get("available_at")}),
                )
                V.check_equal(
                    "the delayed row is still pending, not failed",
                    getattr(sc.outbox_row(send_row_id), "status", None),
                    "pending",
                )
                V.check_equal(
                    "the row is withheld for the requested backoff",
                    len(host.lease(capabilities=("send",), max_actions=1)),
                    0,
                )
                time.sleep(1.7)
                post_backoff = host.lease(capabilities=("send",), max_actions=1)
                V.check_equal(
                    "the row becomes claimable once the backoff has elapsed",
                    len(post_backoff),
                    1,
                )

            # ---- 4. the marker is honoured at the top level as well ----------------
            # A fresh, live claim: the branch is about *this* claim, so reporting
            # against an already-released one is a no-op by design.
            holder = post_backoff[0] if post_backoff else action
            top_body = host.report_body(
                holder,
                "failed",
                error="authorize_unavailable: marker outside result",
            )
            top_body.pop("result", None)
            top_body["authorize_unavailable"] = True
            top = sc.post(host.result_path(holder.action_id), top_body).json or {}
            V.check_equal("a top-level marker reaches the same branch", top.get("retryable"), True)
            V.check_equal(
                "and it leaves the row pending",
                getattr(sc.outbox_row(send_row_id), "status", None),
                "pending",
            )

            # ---- 5. the requeued claim is real work: delivered once, later ---------
            healthy = AdapterHarness(
                scenario=sc, platform=platform, llm=FakeMainLLM(), adapter_id="e2e-old"
            )
            try:
                healthy.poll_once()
            finally:
                healthy.close()
            V.check_equal(
                "the requeued row was delivered once the outage cleared",
                platform.texts_for(SESSION_A),
                [send_text],
            )
            V.check_equal(
                "the recovery counted exactly one contact",
                sc.state().get("contact_count_today"),
                1,
            )
            V.check_equal(
                "the attempt is sent after the recovery",
                getattr(sc.attempt(attempt_id), "state", None),
                "sent",
            )

            # ---- 6. contrast: an explicit refusal *is* terminal --------------------
            direct_attempt = commit_attempt_without_render_row(sc, intent="显式拒绝校验")
            direct = sc.post(
                "/rendered", {"attempt_id": direct_attempt, "text": "这条永远不会被发出。", "now": utc_iso()}
            )
            V.check("a direct-path send row was queued", direct.field("applied") is True, direct.describe())
            direct_rows = sc.runtime.projections.outbox.find_for_attempt(direct_attempt, kind="send")
            V.check_equal("exactly one send row exists for it", len(direct_rows), 1)
            if direct_rows:
                denied_actions = host.lease(capabilities=("send",), max_actions=1)
                V.check_equal("the direct-path send row was leased", len(denied_actions), 1)
                if denied_actions:
                    V.check_equal(
                        "the lease is the row under test (nothing else is claimable)",
                        denied_actions[0].action_id,
                        direct_rows[0].outbox_id,
                    )
                    denial = host.report(
                        denied_actions[0],
                        "rejected",
                        error="aborted_by_user_message",
                        result={"authorized": False},
                    )
                    V.check("the Runtime acknowledged the refusal", denial.get("ok") is True, _short(denial))
                    V.check_equal(
                        "an explicit refusal is terminal for the row",
                        getattr(sc.outbox_row(direct_rows[0].outbox_id), "status", None),
                        "failed",
                    )
                    V.check(
                        "and it closes the attempt",
                        getattr(sc.attempt(direct_attempt), "state", None) in {"failed", "aborted"},
                        _short(getattr(sc.attempt(direct_attempt), "state", None)),
                    )
                    V.check_equal(
                        "a refused message was never delivered",
                        platform.texts_for(SESSION_A),
                        [send_text],
                    )
        finally:
            host.close()

    # ---- the same branch with the claim budget already spent ----------------------
    V.phase("outage marker (spent budget): the compatibility restore path still retries")
    with scenario_session(
        base_dir,
        "p7c_authorize_spent_budget",
        mutate=_marker_config,
        epoch_offset=OFFLINE_WINDOW,
    ) as sc:
        platform = FakePlatform()
        platform.register(SESSION_A)
        prepared = _seed_send_row(sc, session=SESSION_A, text="这条不该因为一次网络抖动被丢掉。")
        V.check("an attempt with a queued send exists", prepared is not None)
        if prepared is None:
            return
        attempt_id, send_row_id = prepared
        send_text = str(getattr(sc.outbox_row(send_row_id), "payload", {}).get("text") or "")
        row_before = sc.outbox_row(send_row_id)
        V.check_equal(
            "the row allows exactly one claim",
            int(getattr(row_before, "max_attempts", 0) or 0),
            1,
        )
        host = AdapterHarness(scenario=sc, platform=platform, llm=FakeMainLLM(), adapter_id="e2e-spent")
        try:
            result = _exercise_marker_branch(
                sc, host, send_row_id=send_row_id, attempt_id=attempt_id
            )
            if not result:
                return
            V.check_equal(
                "the claim consumed the whole budget",
                int(getattr(result["row"], "attempts", 0) or 0),
                int(getattr(result["row"], "max_attempts", 0) or 0),
            )
            V.note(
                "this is the path that used to close the row: an nack would fail it outright, so "
                "the branch restores the claim with the exhaustion-free requeue and still says retryable"
            )
            healthy = AdapterHarness(
                scenario=sc, platform=platform, llm=FakeMainLLM(), adapter_id="e2e-spent"
            )
            try:
                healthy.poll_once()
            finally:
                healthy.close()
            V.check_equal(
                "the restored claim is still deliverable",
                platform.texts_for(SESSION_A),
                [send_text],
            )
            V.check_equal(
                "and it counted exactly one contact",
                sc.state().get("contact_count_today"),
                1,
            )
            V.check_equal(
                "the attempt ends sent, not failed",
                getattr(sc.attempt(attempt_id), "state", None),
                "sent",
            )
        finally:
            host.close()


# --------------------------------------------------------------------------------------
# PHASE 8 — queue depth > 100 on the native /rendered path
# --------------------------------------------------------------------------------------


def phase_queue_depth(base_dir: Path, *, noise: int = 150) -> None:
    """Verify /rendered finds its row behind more than a hundred others."""
    V.phase(f"queue depth > {noise}: /rendered still finds the row for its attempt")
    with scenario_session(
        base_dir, "p8_queue_depth", mutate=_adapter_config, epoch_offset=OFFLINE_WINDOW
    ) as sc:
        seed = seed_committed_attempt(sc, session=SESSION_A)
        attempt_id = seed["attempt_id"] or ""
        render_row_id = seed["outbox_id"] or ""
        V.check("an attempt with a render row exists", bool(attempt_id and render_row_id), _short(seed["decision"]))
        if not (attempt_id and render_row_id):
            return
        enqueue_noise_rows(sc, noise)
        stats = sc.get("/outbox", body=None, **{"timeout": 10.0}).field("stats", default={})
        V.check(
            f"the outbox really holds more than {noise} rows",
            int(stats.get("pending") or 0) > noise,
            _short(stats),
        )

        text = "面试怎么样啦？"
        reply = sc.post("/rendered", {"attempt_id": attempt_id, "text": text, "now": utc_iso()})
        V.check("the native render report is accepted", reply.status == 200, reply.describe())
        V.check_equal(
            "it took the outbox path, not the direct path",
            reply.field("path"),
            "outbox",
        )
        V.check_equal("the report was applied, not merely noted", reply.field("applied"), True)
        V.check_equal("the attempt is ready to send", reply.field("state"), "ready_to_send")
        V.check(
            "the render row was consumed",
            getattr(sc.outbox_row(render_row_id), "status", None) == "delivered",
            _short(getattr(sc.outbox_row(render_row_id), "status", None)),
        )
        sends = sc.runtime.projections.outbox.find_for_attempt(attempt_id, kind="send")
        V.check_equal("exactly one send row was queued", len(sends), 1)
        V.check_equal(
            "the send row carries the reported text",
            sends[0].payload.get("text") if sends else None,
            text,
        )
        V.check_equal(
            "the send row is addressed to the originating session",
            sends[0].conversation_id if sends else None,
            SESSION_A,
        )
        V.note(f"the /rendered call took {reply.elapsed_ms:.1f} ms with {noise} newer rows in the queue")
        V.check(
            "the lookup is not a pathological scan",
            reply.elapsed_ms < 5000.0,
            f"{reply.elapsed_ms:.1f} ms",
        )

        # The direct path is for an attempt that never queued a render row.
        direct_attempt = commit_attempt_without_render_row(sc, intent="直接路径校验")
        direct = sc.post("/rendered", {"attempt_id": direct_attempt, "text": "直接路径的正文。", "now": utc_iso()})
        V.check_equal("an attempt without a render row takes the direct path", direct.field("path"), "direct")
        V.check_equal("the direct path queued its send", direct.field("applied"), True)
        direct_sends = sc.runtime.projections.outbox.find_for_attempt(direct_attempt, kind="send")
        V.check_equal("the direct path queued exactly one send", len(direct_sends), 1)

        proposed = propose_attempt_only(sc, intent="还没决定要做什么")
        refused = sc.post("/rendered", {"attempt_id": proposed, "text": "不该被接受。", "now": utc_iso()})
        V.check_equal("an uncommitted attempt is refused explicitly", refused.status, 409)
        V.check(
            "the refusal names the reason",
            "render_not_applicable" in str(refused.field("detail", default="")),
            _short(refused.field("detail")),
        )
        V.check_equal(
            "the refused attempt was not advanced",
            getattr(sc.attempt(proposed), "state", None),
            "proposed",
        )
        missing = sc.post("/rendered", {"attempt_id": "att_does_not_exist", "text": "x"})
        V.check_equal("an unknown attempt is a 404", missing.status, 404)


# --------------------------------------------------------------------------------------
# PHASE 9 — lease expiry and exhaustion
# --------------------------------------------------------------------------------------


def _lease_lifecycle_config(config: Any) -> None:
    """A one-second lease and a two-claim budget, so both endings are reachable."""
    _adapter_config(config)
    config.outbox.lease_seconds = 1.0
    config.outbox.max_attempts = 2
    config.outbox.retry_backoff_seconds = 0.0


def phase_lease_lifecycle(base_dir: Path) -> None:
    """Verify expired leases requeue, and budget exhaustion closes the attempt."""
    V.phase("lease lifecycle: expiry requeues, exhaustion closes the attempt")
    with scenario_session(
        base_dir,
        "p9_lease_lifecycle",
        mutate=_lease_lifecycle_config,
        epoch_offset=OFFLINE_WINDOW,
    ) as sc:
        seed = seed_committed_attempt(sc, session=SESSION_A)
        attempt_id = seed["attempt_id"] or ""
        render_row_id = seed["outbox_id"] or ""
        V.check("an attempt with a render row exists", bool(attempt_id and render_row_id), _short(seed["decision"]))
        if not (attempt_id and render_row_id):
            return

        first = sc.post("/outbox/claim", {"owner": "worker-a", "limit": 1, "kinds": ["render"]})
        V.check("the row is leased to worker-a", first.field("count") == 1, first.describe())
        V.check_equal("the row reports its owner", first.field("items", 0, "lease_owner"), "worker-a")
        V.check_equal("the claim consumed one attempt", first.field("items", 0, "attempts"), 1)

        foreign = sc.post(f"/outbox/{render_row_id}/nack", {"error": "steal", "owner": "worker-b"})
        V.check_equal("another worker cannot release it", foreign.status, 409)
        V.check_equal(
            "the row is untouched by the foreign nack",
            getattr(sc.outbox_row(render_row_id), "status", None),
            "leased",
        )

        time.sleep(1.4)
        tick = sc.post("/tick", {"now": utc_iso()})
        V.check(
            "the expiry returned the lease to the queue",
            int(tick.field("released_leases") or 0) >= 1,
            _short(tick.json),
        )
        row = sc.outbox_row(render_row_id)
        V.check_equal("the row is pending again", getattr(row, "status", None), "pending")
        V.check_equal("its owner was released", getattr(row, "lease_owner", None), None)
        V.check_equal("the attempt survived the expiry", getattr(sc.attempt(attempt_id), "state", None), "committed")

        second = sc.post("/outbox/claim", {"owner": "worker-a", "limit": 1, "kinds": ["render"]})
        V.check_equal("the row is claimable again", second.field("count"), 1)
        V.check_equal("the second claim consumed the last allowed attempt", second.field("items", 0, "attempts"), 2)

        time.sleep(1.4)
        tick2 = sc.post("/tick", {"now": utc_iso()})
        V.check(
            "the exhausted lease was reclaimed too",
            int(tick2.field("released_leases") or 0) >= 1,
            _short(tick2.json),
        )
        row = sc.outbox_row(render_row_id)
        V.check_equal(
            "a row out of attempts is failed instead of requeued",
            getattr(row, "status", None),
            "failed",
        )
        V.check(
            "the row records why it died",
            "lease expired" in str(getattr(row, "last_error", "") or ""),
            _short(getattr(row, "last_error", None)),
        )
        V.check_equal(
            "the attempt was closed with it",
            getattr(sc.attempt(attempt_id), "state", None),
            "failed",
        )
        V.check(
            "the failure reason is recorded on the attempt",
            bool(str(getattr(sc.attempt(attempt_id), "failure_reason", "") or "")),
            _short(getattr(sc.attempt(attempt_id), "failure_reason", None)),
        )
        aborted = sc.runtime.events.count("proactive_aborted")
        V.check("the closure is visible in the raw history", aborted >= 1, _short(aborted))
        V.check_equal(
            "the scheduler gate is no longer blocked by an in-flight attempt",
            sc.health().get("in_flight_attempts"),
            0,
        )
        schedule = sc.get("/schedule").json or {}
        V.check_equal(
            "the schedule endpoint agrees that dispatch is open again",
            schedule.get("dispatch_allowed"),
            True,
        )
        V.check(
            "no message was ever delivered for the closed attempt",
            getattr(sc.outbox_row(render_row_id), "acked_at", None) is None,
        )


# --------------------------------------------------------------------------------------
# PHASE 10 — a delayed timestamp never rewinds time
# --------------------------------------------------------------------------------------


def phase_monotone_clock(base_dir: Path) -> None:
    """Verify an out-of-order timestamp cannot undo a tick that already happened."""
    V.phase("delayed timestamps: the clock is monotone, the event is still stored")
    with scenario_session(base_dir, "p10_monotone_clock") as sc:
        base = utc_now()
        sc.post("/tick", {"now": utc_iso(base)})
        start = sc.state()
        V.check("the clock has a starting point", bool(start.get("last_tick_at")), _short(start.get("last_tick_at")))

        ahead = base + timedelta(seconds=600)
        forward = sc.post("/tick", {"now": utc_iso(ahead)})
        V.check(
            "a forward tick integrates its interval",
            float(forward.field("dt_seconds") or 0.0) > 500.0,
            _short(forward.field("dt_seconds")),
        )
        state_forward = sc.state()
        version_forward = state_forward.get("version")
        V.check_equal(
            "the clock moved forward",
            parse_iso(state_forward.get("last_tick_at")),
            ahead,
        )

        delayed = sc.post("/tick", {"now": utc_iso(ahead - timedelta(seconds=540))})
        V.check_equal(
            "a delayed tick integrates nothing",
            float(delayed.field("dt_seconds") or 0.0),
            0.0,
        )
        V.check_equal("and it reports no change", delayed.field("changed"), False)
        state_delayed = sc.state()
        V.check_equal(
            "the delayed tick did not rewind the clock",
            parse_iso(state_delayed.get("last_tick_at")),
            ahead,
        )
        V.check(
            "the delayed tick did not rewind the version counter",
            int(state_delayed.get("version") or 0) >= int(version_forward or 0),
            _short({"before": version_forward, "after": state_delayed.get("version")}),
        )
        V.note(
            "the version is a write counter: it advances on every persisted pass, so "
            "`changed` (false here) is what says whether the tick did any work"
        )

        # A newer message first, so the absence anchor has something to lose.
        newer_at = ahead + timedelta(seconds=120)
        post_v1_event(
            sc,
            {
                "event_id": "evt_e2e_newer",
                "kind": "user_message",
                "session": SESSION_A,
                "text": "我现在有空啦。",
                "occurred_at": utc_iso(newer_at),
                "wake": True,
            },
        )
        after_newer = sc.state()
        V.check_equal(
            "the newer message set the absence anchor",
            parse_iso(after_newer.get("last_user_message_at")),
            newer_at,
        )

        stale_at = ahead + timedelta(seconds=30)
        stale = post_v1_event(
            sc,
            {
                "event_id": "evt_e2e_stale",
                "kind": "user_message",
                "session": SESSION_A,
                "text": "（这条消息的时钟慢了十分钟才到）",
                "occurred_at": utc_iso(stale_at),
                "wake": True,
            },
        )
        V.check_equal("the delayed message was accepted, not rejected", stale.field("accepted"), 1)
        after_stale = sc.state()
        V.check_equal(
            "a delayed message cannot rewind the absence anchor",
            parse_iso(after_stale.get("last_user_message_at")),
            newer_at,
        )
        V.check(
            "the clock is still past the newest tick",
            (parse_iso(after_stale.get("last_tick_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= ahead,
            _short(after_stale.get("last_tick_at")),
        )
        stored = sc.get("/events/evt_e2e_stale")
        V.check_equal(
            "the late event keeps its own (older) timestamp in history",
            parse_iso(stored.field("event", "timestamp")),
            stale_at,
        )
        V.check(
            "the late event still did its work",
            int(after_stale.get("version") or 0) > int(after_newer.get("version") or 0),
            _short({"before": after_newer.get("version"), "after": after_stale.get("version")}),
        )


# --------------------------------------------------------------------------------------
# PHASE 11 — remote provider selection, no key, no network
# --------------------------------------------------------------------------------------


class _LoopbackProbe:
    """A loopback listener that counts connections and answers nothing useful.

    It stands in for a remote OpenAI-compatible endpoint so the script can *prove*
    that no request leaves the machine while the provider reports itself
    unavailable.
    """

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((HOST, 0))
        self._socket.listen(8)
        self._socket.settimeout(0.25)
        self.port = int(self._socket.getsockname()[1])
        self.connections = 0
        self.payloads: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="e2e-provider-probe", daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        """Return the OpenAI-compatible base URL of the probe."""
        return f"http://{HOST}:{self.port}/v1"

    def _serve(self) -> None:
        """Accept and record connections until stopped."""
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            with conn:
                self.connections += 1
                with contextlib.suppress(Exception):
                    conn.settimeout(0.2)
                    self.payloads.append(conn.recv(4096))
                with contextlib.suppress(Exception):
                    conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")

    def close(self) -> None:
        """Stop accepting and join the thread."""
        self._stop.set()
        with contextlib.suppress(Exception):
            self._socket.close()
        self._thread.join(timeout=5)


def phase_provider_config(base_dir: Path) -> None:
    """Verify provider selection from configuration, with no key and no request."""
    V.phase("remote provider selection: harmless endpoint, no key, no request sent")
    with scenario_session(base_dir, "p11_provider_config") as sc:
        probe = _LoopbackProbe()
        try:
            config = sc.config
            config.extras = {
                "semantic": {"base_url": probe.base_url, "model": "e2e-fake-model"},
            }
            config.semantic.provider = "remote_api"
            config.semantic.deep_refresh_enabled = True

            env = {
                "CR_SEMANTIC_PROVIDER": "remote_api",
                "CR_SEMANTIC_BASE_URL": probe.base_url,
                "CR_SEMANTIC_MODEL": "e2e-fake-model",
                # deliberately no CR_SEMANTIC_API_KEY
            }
            resolved = providers_module.resolve_provider_name(config, env)
            V.check_equal("the configured provider name resolves to remote_api", resolved, "remote_api")

            provider = providers_module.build_provider(config, env=env)
            V.check_equal("the factory built the remote provider", provider.name, "remote_api")
            health = provider.health()
            V.check_equal("the provider reports its endpoint", health.get("base_url"), probe.base_url)
            V.check(
                "the provider reports no key configured",
                health.get("api_key") == "not configured",
                _short(health.get("api_key")),
            )
            V.check(
                "the health payload reports the key state and nothing key-shaped",
                all(
                    not isinstance(value, str) or len(value) < 64
                    for key, value in health.items()
                    if "key" in key or "token" in key or "secret" in key
                ),
                _short(health),
            )
            V.check(
                "a keyless remote provider reports itself unavailable",
                health.get("available") is False,
                _short(health.get("reason") or health.get("available")),
            )
            V.check_equal(
                "a local name that no longer exists falls back to disabled",
                providers_module.resolve_provider_name(None, {"CR_SEMANTIC_PROVIDER": "local_cpu"}),
                "local_cpu",
            )
            fallback = providers_module.build_provider(None, env={"CR_SEMANTIC_PROVIDER": "local_cpu"})
            V.check_equal(
                "and the factory degrades it to the disabled provider",
                fallback.name,
                "disabled",
            )

            # Install it on the live Runtime: the HTTP surface must agree, and the
            # refresh must decline without touching the endpoint.
            sc.runtime.semantic_provider = provider
            served = sc.health().get("semantic_provider") or {}
            V.check_equal("the live health endpoint reports the provider", served.get("provider"), "remote_api")
            V.check_equal(
                "the live health endpoint reports the endpoint",
                served.get("base_url"),
                probe.base_url,
            )
            V.check_equal("the live health endpoint reports no key", served.get("api_key"), "not configured")
            V.check(
                "the live health endpoint reports it as unusable",
                served.get("available") is False,
                _short(served),
            )
            refresh = sc.post("/cognition/refresh", {"force": True})
            V.check_equal("a keyless provider declines the refresh", refresh.field("ran"), False)
            V.check_equal(
                "the decline is explained as provider_unavailable",
                refresh.field("reason"),
                "provider_unavailable",
            )
            time.sleep(0.3)
            V.check_equal(
                "not a single request reached the fake endpoint",
                probe.connections,
                0,
            )
            V.check_equal(
                "the probe never received a body",
                probe.payloads,
                [],
            )
        finally:
            probe.close()


# --------------------------------------------------------------------------------------
# PHASE 12 — simulated restart
# --------------------------------------------------------------------------------------


class _StubSemanticProvider:
    """An available provider that suggests nothing, used only to move a timestamp."""

    name = "e2e_stub"

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        """Report itself usable, so the Runtime records the refresh attempt."""
        return True

    def deep_refresh(self, request: Any, *, timeout_s: float | None = None) -> Any:
        """Return nothing: the point is the recorded attempt, not the suggestions."""
        self.calls += 1
        return None

    def explain_state(self, payload: Any, *, state_key: str = "") -> Any:
        """Return no explanation."""
        return None

    def health(self) -> dict[str, Any]:
        """Return a minimal self-description."""
        return {"provider": self.name, "available": True, "enabled": True, "reason": ""}


def phase_restart(base_dir: Path) -> None:
    """Verify a restart keeps durable state, the refresh timestamp, and usability."""
    V.phase("simulated restart: state, refresh timestamp and pending work survive")
    directory = prepare_directory(base_dir, "p12_restart")

    def _mutate(config: Any) -> None:
        _adapter_config(config)
        config.semantic.deep_refresh_enabled = True

    config = build_config(directory, _mutate)
    first = serve(config, name="p12_restart_before", directory=directory, epoch_offset=OFFLINE_WINDOW)
    snapshot: dict[str, Any] = {}
    try:
        seed = seed_committed_attempt(first, session=SESSION_A)
        attempt_id = seed["attempt_id"] or ""
        render_row_id = seed["outbox_id"] or ""
        V.check("an attempt with pending work exists before the restart", bool(attempt_id and render_row_id))
        added = post_v1_event(
            first,
            {
                "event_id": "evt_restart_marker",
                "kind": "user_message",
                "session": SESSION_A,
                "text": "我还要再说一句：路上小心。",
                "occurred_at": utc_iso(utc_now() - timedelta(hours=1)),
                "wake": True,
            },
        )
        V.check("a marker event was recorded before the restart", added.field("accepted") == 1, added.describe())

        # A durable deep-refresh timestamp: the provider is swapped in-process and
        # the refresh is asked to run, which records the attempt time *before*
        # spending (so even a provider that yields nothing leaves a durable mark).
        stub = _StubSemanticProvider()
        first.runtime.semantic_provider = stub
        refresh = first.post("/cognition/refresh", {"force": True})
        V.check("the forced refresh ran against the stub provider", refresh.status == 200, refresh.describe())
        V.check("the stub provider was actually consulted", stub.calls == 1, _short(stub.calls))

        before_state = first.state()
        before_health = first.health()
        journal = config.storage.database_path + "-wal"
        journal_row = first.runtime.db.query_one("PRAGMA journal_mode")
        journal_mode = str(journal_row[0]).lower() if journal_row is not None else ""
        V.check(
            "the database really is a file in WAL mode",
            journal_mode == "wal",
            _short(journal_mode),
        )
        V.check("the write-ahead log sits next to the database", Path(journal).exists(), _short(journal))
        V.check(
            "the raw event mirror is a real file artifact",
            Path(config.storage.raw_log_path).exists(),
            _short(config.storage.raw_log_path),
        )
        snapshot = {
            "state": before_state,
            "health": before_health,
            "render_row": _row_dict(first, render_row_id),
            "matters": open_matters(first),
            "refresh_at": (before_state.get("meta") or {}).get("last_deep_refresh_at"),
            "raw_events": before_health.get("raw_events"),
            "version": before_health.get("state_version"),
        }
        V.check(
            "the refresh attempt was recorded durably",
            bool(snapshot["refresh_at"]),
            _short(snapshot["refresh_at"]),
        )
    finally:
        stop_scenario(first)

    second = serve(config, name="p12_restart_after", directory=directory, epoch_offset=timedelta(0))
    try:
        after_state = second.state()
        after_health = second.health()
        V.check_equal(
            "the Runtime version survived",
            after_health.get("state_version"),
            snapshot.get("version"),
        )
        V.check_equal(
            "the runtime projection survived byte for byte",
            {key: after_state.get(key) for key in ("version", "last_tick_at", "contact_count_today", "epoch_at")},
            {key: (snapshot.get("state") or {}).get(key) for key in ("version", "last_tick_at", "contact_count_today", "epoch_at")},
        )
        V.check_equal(
            "the deep-refresh timestamp survived",
            (after_state.get("meta") or {}).get("last_deep_refresh_at"),
            snapshot.get("refresh_at"),
        )
        V.check_equal(
            "the raw history survived",
            after_health.get("raw_events"),
            snapshot.get("raw_events"),
        )
        V.check_equal(
            "the pending render row survived with its claim count",
            {key: value for key, value in _row_dict(second, str((snapshot.get("render_row") or {}).get("outbox_id"))).items() if key in {"status", "attempts", "conversation_id", "kind"}},
            {key: value for key, value in (snapshot.get("render_row") or {}).items() if key in {"status", "attempts", "conversation_id", "kind"}},
        )
        V.check_equal(
            "the unfinished matter is still open",
            [item.get("unfinished_id") for item in open_matters(second)],
            [item.get("unfinished_id") for item in (snapshot.get("matters") or [])],
        )
        verify = second.get("/maintenance/verify")
        V.check("the database passes its structural verification", verify.field("ok") is True, verify.describe())
        V.check_equal(
            "and the integrity check itself is clean",
            verify.field("integrity"),
            "ok",
        )

        # The surviving row must still be usable, not merely readable.
        platform = FakePlatform()
        platform.register(SESSION_A)
        host = AdapterHarness(scenario=second, platform=platform, llm=FakeMainLLM(), adapter_id="e2e-restart")
        try:
            render_row_id = str((snapshot.get("render_row") or {}).get("outbox_id") or "")
            actions = host.lease(capabilities=("render",), max_actions=1)
            V.check_equal("the surviving row can still be leased", len(actions), 1)
            if actions:
                V.check_equal("and it is the row that survived", actions[0].action_id, render_row_id)
                rendered = host.loop.call(host.executor.render(actions[0]))
                report = host.report(actions[0], "ok", result=rendered)
                V.check("the post-restart render was applied", report.get("ok") is True, _short(report))
                send_actions = host.lease(capabilities=("send",), max_actions=1)
                V.check_equal("a send row followed it", len(send_actions), 1)
                if send_actions:
                    text = str(send_actions[0].payload.get("text") or "")
                    verdict = host.authorize(send_actions[0], text)
                    V.check("the post-restart send is authorized", verdict.authorized is True, _short(verdict.reason))
                    delivered = host.loop.call(host.executor.send(send_actions[0], text))
                    final = host.report(send_actions[0], "ok", result=delivered | {"authorized": True})
                    V.check("the post-restart delivery is recorded", final.get("delivered") is True, _short(final))
                    V.check_equal(
                        "the platform received the post-restart message",
                        platform.texts_for(SESSION_A),
                        [text],
                    )
                    V.check_equal(
                        "the surviving contact was counted once",
                        second.state().get("contact_count_today"),
                        int((snapshot.get("state") or {}).get("contact_count_today") or 0) + 1,
                    )
        finally:
            host.close()
    finally:
        stop_scenario(second)


# --------------------------------------------------------------------------------------
# PHASE 13 — concurrent duplicate v1 result reports
# --------------------------------------------------------------------------------------


def _fire_concurrently(calls: Sequence[Callable[[], Any]], *, workers: int | None = None) -> list[Any]:
    """Run ``calls`` on real threads, as separate HTTP clients would."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers or len(calls)) as pool:
        return list(pool.map(lambda fn: fn(), calls))


def phase_concurrent_reports(base_dir: Path) -> None:
    """Verify concurrent duplicate v1 reports apply at most once."""
    V.phase("concurrent v1 results: a duplicated report applies exactly once")
    with scenario_session(
        base_dir, "p13_concurrent", mutate=_adapter_config, epoch_offset=OFFLINE_WINDOW
    ) as sc:
        seed = seed_committed_attempt(sc, session=SESSION_A)
        attempt_id = seed["attempt_id"] or ""
        render_row_id = seed["outbox_id"] or ""
        V.check("an attempt with a render row exists", bool(attempt_id and render_row_id), _short(seed["decision"]))
        if not (attempt_id and render_row_id):
            return

        host = AdapterHarness(scenario=sc, platform=FakePlatform(), llm=FakeMainLLM(), adapter_id="e2e-race")
        try:
            actions = host.lease(capabilities=("render",), max_actions=1)
            V.check_equal("the render row was leased once", len(actions), 1)
            if not actions:
                return
            render_action = actions[0]
            text = "面试结束了吧？我一直记着这件事。"
            body = host.report_body(render_action, "ok", result={"text": text})
            path = host.result_path(render_action.action_id)

            replies = _fire_concurrently([lambda: sc.post(path, body) for _ in range(6)])
            V.check(
                "every concurrent render report answered 200",
                all(reply.status == 200 for reply in replies),
                _short([reply.status for reply in replies]),
            )
            bodies = [reply.json or {} for reply in replies]
            V.check(
                "every concurrent render report was acknowledged",
                all(item.get("ok") is True for item in bodies),
                _short(bodies[:2]),
            )
            V.check_equal(
                "exactly one render report actually applied",
                sum(1 for item in bodies if not item.get("duplicate")),
                1,
            )
            V.check_equal(
                "no concurrent report raised an internal error",
                [item.get("reason") for item in bodies if str(item.get("reason") or "").startswith("report_error")],
                [],
            )
            sends = sc.runtime.projections.outbox.find_for_attempt(attempt_id, kind="send")
            V.check_equal("exactly one send row was queued by six reports", len(sends), 1)
            V.check_equal(
                "the render row was consumed once",
                getattr(sc.outbox_row(render_row_id), "status", None),
                "delivered",
            )

            send_actions = host.lease(capabilities=("send",), max_actions=1)
            V.check_equal("the send row was leased", len(send_actions), 1)
            if not send_actions:
                return
            send_action = send_actions[0]
            send_body = host.report_body(
                send_action,
                "ok",
                result={"sent": True, "chars": len(text), "authorized": True},
            )
            send_path = host.result_path(send_action.action_id)
            send_replies = _fire_concurrently([lambda: sc.post(send_path, send_body) for _ in range(6)])
            send_bodies = [reply.json or {} for reply in send_replies]
            V.check(
                "every concurrent delivery report was acknowledged",
                all(item.get("ok") is True for item in send_bodies),
                _short(send_bodies[:2]),
            )
            V.check_equal(
                "exactly one delivery report actually applied",
                sum(1 for item in send_bodies if not item.get("duplicate")),
                1,
            )
            V.check_equal(
                "the contact was counted exactly once",
                sc.state().get("contact_count_today"),
                1,
            )
            V.check_equal(
                "exactly one proactive_sent event exists",
                sc.runtime.events.count("proactive_sent"),
                1,
            )
            V.check_equal(
                "exactly one assistant message was recorded",
                sc.runtime.events.count("assistant_message"),
                1,
            )
            V.check_equal(
                "the attempt ended up sent",
                getattr(sc.attempt(attempt_id), "state", None),
                "sent",
            )
        finally:
            host.close()


# --------------------------------------------------------------------------------------
# helpers used by the phases
# --------------------------------------------------------------------------------------


def _row_dict(scenario: Scenario, outbox_id: str) -> dict[str, Any]:
    """Return one outbox row as a plain mapping (``{}`` when missing)."""
    row = scenario.outbox_row(outbox_id)
    if row is None:
        return {}
    payload = row.to_dict()
    return payload if isinstance(payload, dict) else {}


def phase_teardown(base_dir: Path) -> None:
    """Verify nothing was left running and nothing was written outside --base-dir."""
    V.phase("teardown: every thread stopped, artifacts confined to --base-dir")
    alive = [
        thread
        for thread in threading.enumerate()
        if thread is not threading.current_thread()
    ]
    ours = [
        thread
        for thread in alive
        if thread.name.startswith("e2e-") or thread.name.startswith("companion-runtime")
    ]
    V.check(
        "no server, scheduler or adapter thread is still running",
        not ours,
        _short([f"{thread.name} (alive={thread.is_alive()})" for thread in ours]),
    )
    V.note(
        "other threads alive at this point belong to the host process, not to this run: "
        + (", ".join(sorted(thread.name for thread in alive)) or "none")
    )

    started = RUN.started_at
    stray_pyc: list[str] = []
    for tree in (RUNTIME_SRC / "companion_runtime", PLUGIN_CORE):
        for cache in tree.glob("__pycache__/*.pyc"):
            with contextlib.suppress(OSError):
                if cache.stat().st_mtime > started:
                    stray_pyc.append(str(cache))
    V.check(
        "this process is configured never to write bytecode next to the sources",
        sys.dont_write_bytecode is True,
        _short(sys.dont_write_bytecode),
    )
    if stray_pyc:
        # Another process in the same checkout (a test run, an editor) can create
        # these too, so they are reported rather than attributed to this run.
        V.note(
            f"{len(stray_pyc)} .pyc file(s) appeared under the source trees while this run "
            "was in progress; another process in the same checkout is the likely author "
            f"(this run has dont_write_bytecode={sys.dont_write_bytecode}): {_short(stray_pyc[:3])}"
        )
    probe = base_dir / ".e2e-write-probe"
    writable = False
    with contextlib.suppress(OSError):
        probe.write_text("probe", encoding="utf-8")
        writable = probe.read_text(encoding="utf-8") == "probe"
        probe.unlink()
    V.check(
        "the chosen base directory is the writable artifact root",
        writable and base_dir.is_dir(),
        _short(str(base_dir)),
    )
    database_files = sorted(str(path.name) for path in (base_dir / "scenarios").glob("*/*.sqlite3*"))
    V.check(
        "every database the run created lives under --base-dir",
        bool(database_files),
        _short(database_files[:6]),
    )
    V.note(f"{len(database_files)} database artifact(s) kept under {base_dir / 'scenarios'}")


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------


PHASES: list[tuple[str, Callable[[Path], None]]] = [
    ("events", phase_events),
    ("scheduler", phase_scheduler),
    ("adapter", phase_adapter_contract),
    ("sessions", phase_adapter_sessions),
    ("authorize", phase_authorize_unavailable),
    ("queue", phase_queue_depth),
    ("lease", phase_lease_lifecycle),
    ("clock", phase_monotone_clock),
    ("provider", phase_provider_config),
    ("restart", phase_restart),
    ("concurrency", phase_concurrent_reports),
    ("teardown", phase_teardown),
]


def scrub_environment(v: Verifier) -> None:
    """Remove credential-shaped and provider-selecting variables from the process.

    The Runtime builds its provider from the process environment, and a deployment
    host may well have ``CR_SEMANTIC_*`` exported. A verification run must never
    inherit a credential or silently start talking to a real endpoint, so anything
    provider-shaped is removed for the duration of the run (and reported).
    """
    removed: list[str] = []
    for name in (
        "CR_SEMANTIC_PROVIDER",
        "CR_SEMANTIC_BASE_URL",
        "CR_SEMANTIC_API_KEY",
        "CR_SEMANTIC_MODEL",
        "CR_SEMANTIC_TIMEOUT_S",
        "CR_SEMANTIC_MAX_TOKENS",
        "COMPANION_RUNTIME_TOKEN",
    ):
        if name in os.environ:
            os.environ.pop(name, None)
            removed.append(name)
    if removed:
        v.note(f"removed provider/credential environment variables for this run: {', '.join(removed)}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="End-to-end resilience simulation for the companion Runtime sidecar",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-dir",
        default=str(Path(tempfile.gettempdir()) / "companion-runtime-e2e-resilience"),
        help="directory for every artifact this run writes (database, WAL, logs, report)",
    )
    parser.add_argument(
        "--only",
        default="",
        help=f"comma separated subset of phases to run: {', '.join(name for name, _ in PHASES)}",
    )
    parser.add_argument(
        "--list-phases",
        action="store_true",
        help="print the phase names and exit",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress informational notes")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run every phase, write the report, and return the exit code."""
    global V
    args = parse_args(argv)
    if args.list_phases:
        for name, function in PHASES:
            print(f"{name:12s} {function.__doc__.splitlines()[0] if function.__doc__ else ''}")
        return 0

    V = Verifier(quiet=bool(args.quiet))
    configure_script_logging()
    with contextlib.suppress(Exception):
        # A check's detail can quote user text; an odd console encoding must never
        # be the reason a verification run dies.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    base_dir = Path(args.base_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    RUN.started_at = time.time()
    RUN.base_dir = base_dir

    V.phase("setup: dependencies, artifacts and environment")
    V.note(f"repository: {REPO_ROOT}")
    V.note(f"python: {sys.version.split()[0]} ({sys.executable})")
    V.note(f"base directory (the only place this run writes): {base_dir}")
    if IMPORT_ERROR:
        V.check("the Runtime package imports", False, IMPORT_ERROR)
        V.check("the adapter package imports", False, "not attempted: the Runtime did not import")
        V.summary()
        _write_artifacts(base_dir, fatal=IMPORT_ERROR)
        return 2
    V.check("the Runtime package imports", True)
    V.note(f"sqlite, uvicorn and fastapi are importable; aiohttp {_aiohttp.__version__} backs the adapter")
    try:
        load_adapter_modules()
        V.check("the shipped adapter core imports under a private name", True)
    except Exception as exc:  # noqa: BLE001 - setup failure
        V.check("the shipped adapter core imports under a private name", False, f"{type(exc).__name__}: {exc}")
        V.summary()
        _write_artifacts(base_dir, fatal=f"adapter import failed: {exc}")
        return 2
    scrub_environment(V)

    selected = {name.strip() for name in args.only.split(",") if name.strip()}
    unknown = sorted(selected - {name for name, _ in PHASES})
    if unknown:
        V.check("every requested phase exists", False, f"unknown phases: {unknown}")
        V.summary()
        _write_artifacts(base_dir)
        return 2

    for name, function in PHASES:
        if selected and name not in selected:
            V.note(f"skipping phase {name} (not selected)")
            continue
        started = time.monotonic()
        try:
            function(base_dir)
        except Exception as exc:  # noqa: BLE001 - a phase fault is a FAIL, not a crash
            detail = f"{type(exc).__name__}: {exc}"
            V.check(f"phase {name} ran to completion", False, detail)
            V.diagnostics(
                f"phase {name} traceback",
                traceback.format_exc().splitlines()[-6:],
            )
        else:
            V.note(f"phase {name} finished in {time.monotonic() - started:.1f}s")

    exit_code = V.summary()
    V.line("")
    V.line(f"artifacts written under: {base_dir}")
    _write_artifacts(base_dir)
    return exit_code


def _write_artifacts(base_dir: Path, *, fatal: str = "") -> None:
    """Write the JSON report and the diagnostics log next to the scenario data."""
    report = V.as_report()
    if fatal:
        report["fatal"] = fatal
    with contextlib.suppress(Exception):
        (base_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with contextlib.suppress(Exception):
        lines = [f"# companion runtime e2e resilience simulation — {utc_iso()}", ""]
        lines.extend(V._lines)
        lines.append("")
        lines.append("## captured runtime log")
        lines.extend(LOG_HANDLER.records)
        (base_dir / "diagnostics.log").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
