"""Assemble a running, externally-controllable Runtime.

This is the piece that ties the four capabilities together:

* the program is **imported, never edited** -- the only thing the framework
  writes is its own run directory, and every program file is left alone (the
  shipped simulations assert the same property, and so does this framework's
  teardown check);
* the program's clock is replaced process-wide, so virtual time drives
  ``lazy_tick``, the Scheduler, attempt bookkeeping and the adapter alike;
* the program's strong-semantics port is pointed at the framework's own
  OpenAI-compatible endpoint through three environment variables;
* a heartbeat makes time actually pass, and each beat logs the variables it saw.

Why the heartbeat exists at all
------------------------------
The Runtime "deliberately does not run a fixed one-minute heartbeat": time only
passes when something happens, and the Scheduler's job is to choose the *next*
wake-up. That is the right design for production and an awkward one for a test
rig, because a scaled virtual clock and the Scheduler's real-time ``asyncio.sleep``
disagree about how fast time moves. So the framework supplies the missing beat:
every ``heartbeat_interval_s`` of real time it advances the clock (by ``step``,
when configured) and calls ``lazy_tick``, then records what changed. The
program's own Scheduler is still wired exactly as ``serve`` wires it, so its
decisions are exercised -- it is simply no longer the only thing driving time.

Nothing here imports ``companion_runtime`` at module import time. The program
path is resolved and imported in :meth:`Harness.start`, so a broken program
checkout produces a readable setup error instead of an import traceback.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .clock import ControllableClock, install_process_clock, parse_duration, parse_when
from .control import ControlServer
from .host import DEFAULT_PLUGIN_ROOT, AstrBotHost, Platform, SESSION_DEFAULT
from .logbook import LogBridge, Logbook
from .main_llm import (
    BASE_URL_ENV,
    MODEL_ENV,
    MainLLM,
    OpenAICompatibleMainLLM,
    ScriptedMainLLM,
)
from .mock_openai import MockOpenAIServer, MockReply, MockScript
from .program import ProgramClient
from .variables import VariableProbe

LOGGER = logging.getLogger("cf.harness")

#: Default location of the program's ``src`` directory, relative to the framework
#: directory. The framework sits beside ``runtime/`` inside the project, so this
#: is a single hop up. Overridable with ``--program-src``.
DEFAULT_PROGRAM_SRC = "../runtime/src"

#: A placeholder bearer token. The program refuses to use ``remote_api`` at all
#: unless a key is configured, and the mock accepts any value -- but the token is
#: never written to the trace, only whether one was present. This is a *test*
#: value, not a secret, and it is not read from the environment.
MOCK_API_KEY = "framework-mock-key"

#: Environment variables that point the program at the mock endpoint.
SEMANTIC_ENV = {
    "CR_SEMANTIC_PROVIDER": "remote_api",
    "CR_SEMANTIC_MODEL": "framework-mock",
}


@dataclass
class HarnessConfig:
    """Everything the harness needs to boot.

    Args:
        run_dir: Where the framework writes logs and traces.
        program_src: The program's ``src`` directory.
        base_dir: Where the program writes its SQLite database and JSONL mirror.
        host: Address the program's HTTP server binds.
        port: Port for the program's HTTP server; 0 asks the OS.
        start_time: Virtual instant the run starts at. Defaults to real now.
        time_scale: Virtual seconds per real second.
        step: Virtual duration added by every heartbeat. ``None`` lets the clock
            run on its own; a value makes stepping deterministic.
        heartbeat_interval_s: Real seconds between heartbeats.
        seed: RNG seed for the program; fixed by default so runs are comparable.
        use_mock_semantics: Point the program's strong-semantics port at the mock.
        config_path: Optional program config file (TOML/JSON).
        echo_logs: Also echo the rolling log to stderr.
    """

    run_dir: Path
    program_src: Path = field(default_factory=lambda: Path(DEFAULT_PROGRAM_SRC))
    base_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 0
    start_time: str | None = None
    time_scale: float = 1.0
    step: str | None = None
    heartbeat_interval_s: float = 1.0
    seed: int | None = 20260915
    use_mock_semantics: bool = True
    config_path: str | None = None
    echo_logs: bool = True
    #: The acting layer. Empty values fall back to the ``CF_MAIN_LLM_*``
    #: environment variables; with neither set, a deterministic stand-in is used
    #: so the chat still runs end to end without any endpoint.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_system_prompt: str = ""
    llm_temperature: float = 0.8
    #: Partial overrides of the Runtime's 8 value axes -- the personality compiled
    #: into dynamics. Applied when the runtime row is created, so it only takes
    #: effect on a fresh run directory (see :meth:`Harness._apply_values`).
    values: dict[str, float] = field(default_factory=dict)
    #: The AstrBot plugin checkout, loaded against its own stubs.
    plugin_root: Path = field(default_factory=lambda: Path(DEFAULT_PLUGIN_ROOT))
    #: Boot the AstrBot host at all. Off when only the Runtime is wanted.
    use_host: bool = True
    #: Point the program's own strong semantics at the *main* LLM instead of the
    #: mock endpoint. Off by default: the mock is grounded, deterministic and free.
    semantic_from_main_llm: bool = False

    def resolved_base_dir(self) -> Path:
        """Return the program's data directory, defaulting to ``run_dir/program``."""
        return Path(self.base_dir) if self.base_dir is not None else Path(self.run_dir) / "program"


class Harness:
    """A booted, controllable Runtime plus its fake semantic endpoint.

    Args:
        config: Harness configuration.

    Raises:
        RuntimeError: When the program cannot be imported or never becomes
            healthy. The message names the concrete cause.
    """

    def __init__(self, config: HarnessConfig) -> None:
        """Store the configuration; call :meth:`start` to boot."""
        self.config = config
        self.run_dir = Path(config.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logbook = Logbook(self.run_dir, echo=config.echo_logs)
        self.clock = ControllableClock(
            parse_when(config.start_time) if config.start_time else datetime.now(timezone.utc),
            scale=config.time_scale,
        )
        self.mock: MockOpenAIServer | None = None
        self.control: ControlServer | None = None
        self.runtime: Any = None
        self.app: Any = None
        self.scheduler: Any = None
        self._server: Any = None
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error = ""
        self.base_url = ""
        self.port = 0
        self._beats = 0
        self._probe: VariableProbe | None = None
        self.program: ProgramClient | None = None
        self._rebound = 0
        self._last_variables: dict[str, Any] = {}
        #: The instant the world begins: the declared ``--start-time``, else the
        #: clock's reading when :meth:`start` was called. The program's creation
        #: epoch is set from this, so scenario timestamps can be compared to it.
        self.epoch: datetime | None = None
        self._log_bridge: LogBridge | None = None
        #: Program source tree, snapshotted at boot and compared at teardown.
        self._program_src: Path | None = None
        self._source_before: dict[str, tuple[int, float]] = {}
        self.platform: Platform | None = None
        self.llm: MainLLM | None = None
        self.host: AstrBotHost | None = None

    # ------------------------------------------------------------------- boot

    def start(self, *, startup_timeout: float = 30.0) -> None:
        """Boot the mock endpoint, the program, the control plane and the beat.

        Args:
            startup_timeout: Seconds to wait for the program's ``/health``.

        Raises:
            RuntimeError: On any setup failure, naming the cause.
        """
        # The epoch is fixed *before* anything boots, and it is the declared start
        # instant rather than "whatever the clock reads once startup finished".
        # The two differ by however long booting took, and the program clamps
        # ``lazy_tick`` to be no earlier than its creation epoch -- so a scenario
        # that stamps its opening line at exactly ``--start-time`` would otherwise
        # be told its own first event predates the world.
        self.epoch = parse_when(self.config.start_time) if self.config.start_time else self.clock.now()
        self.logbook.event(
            "harness_start",
            {
                "program_src": str(self.config.program_src),
                "base_dir": str(self.config.resolved_base_dir()),
                "epoch": self.epoch.isoformat(),
                "clock_now": self.clock.now().isoformat(),
                "time_scale": self.config.time_scale,
                "step": self.config.step,
                "heartbeat_interval_s": self.config.heartbeat_interval_s,
                "seed": self.config.seed,
            },
        )
        self._import_program()
        # Snapshot the program tree before anything runs, so teardown can report
        # exactly which files this run touched rather than which files happen to
        # have a recent mtime.
        self._source_before = _snapshot_sources(self._program_src)
        # Attach after the import so the program's loggers exist, and before the
        # boot so a failure during startup is captured too.
        # The adapter reports its swallowed failures at DEBUG; without that
        # level a plugin that never reports anything looks identical to one
        # that is working silently.
        self._log_bridge = LogBridge(
            self.logbook, per_logger_levels={'astrbot': logging.DEBUG}
        )
        self._rebound = install_process_clock(self.clock)
        self.logbook.event("clock_installed", {"rebound": self._rebound}, message=f"[clock] rebound {self._rebound} binding(s)")
        self._build_llm()
        self._start_mock()
        self._build_runtime()
        self._start_http(startup_timeout)
        self._start_scheduler()
        # Late imports bind the real utcnow; re-install now that everything the
        # boot path touches has been imported.
        self._rebound += install_process_clock(self.clock)
        self._probe = VariableProbe(self.base_url, clock=self.clock)
        self.program = ProgramClient(self.base_url)
        self._start_control()
        self._start_host()
        self._start_heartbeat()
        self.logbook.event(
            "harness_ready",
            {
                "base_url": self.base_url,
                "control_url": self.control.base_url if self.control else "",
                "mock_url": self.mock.base_url if self.mock else "",
                "provider": getattr(getattr(self.runtime, "semantic_provider", None), "name", "unknown"),
                "mock_calls": self.mock.calls_made if self.mock else 0,
                "llm": self.llm.describe() if self.llm is not None else {},
                "host": self.host.stats() if self.host is not None else {},
            },
            message=(
                f"[ready] runtime={self.base_url} control={self.control.base_url if self.control else '-'} "
                f"mock={self.mock.base_url if self.mock else '-'}"
            ),
        )

    def _import_program(self) -> None:
        """Put the program on ``sys.path`` and import what the harness needs."""
        src = Path(self.config.program_src).expanduser().resolve()
        if not (src / "companion_runtime").is_dir():
            raise RuntimeError(
                f"no companion_runtime package under {src}; pass --program-src pointing at the program's src directory"
            )
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        try:
            from companion_runtime.api import create_app  # noqa: F401
            from companion_runtime.config import load_config, resolve_paths  # noqa: F401
            from companion_runtime.runtime import Runtime  # noqa: F401
            from companion_runtime.scheduler import Scheduler  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - reported as a setup failure
            raise RuntimeError(f"cannot import the program from {src}: {type(exc).__name__}: {exc}") from exc
        self._program_src = src

    def _start_mock(self) -> None:
        """Start the OpenAI-compatible endpoint and point the program at it.

        Three of the four settings are read straight from ``os.environ`` by the
        program's provider, so environment variables are the right channel for
        them. The *switch* is not: see :meth:`_build_runtime`.
        """
        if not self.config.use_mock_semantics:
            return
        if self.config.semantic_from_main_llm:
            llm = self.llm
            if llm is not None and getattr(llm, "configured", False):
                # The same endpoint serves both halves of the architecture: the
                # main LLM does the moment-to-moment acting, and the Runtime's
                # low-frequency strong semantics call the same model for the jobs
                # only a model can do (re-reading old events, naming what is
                # unfinished). One endpoint, one key, two very different cadences.
                os.environ["CR_SEMANTIC_BASE_URL"] = llm.base_url
                os.environ["CR_SEMANTIC_MODEL"] = llm.model
                os.environ["CR_SEMANTIC_API_KEY"] = getattr(llm, "_api_key", "") or MOCK_API_KEY
                self.logbook.event(
                    "semantic_wired",
                    {
                        "base_url": llm.base_url,
                        "model": llm.model,
                        "provider": "remote_api",
                        "source": "main_llm",
                    },
                    message=f"[semantic] 复用主 LLM 端点 {llm.model} @ {llm.base_url}",
                )
                return
            # Asked for the real model but none is configured: fall back rather
            # than refuse to start. A chat that cannot boot because of a missing
            # optional accelerator would be a worse failure than a mock.
            self.logbook.warn(
                "semantic_wired",
                {"source": "mock", "reason": "main LLM endpoint is not configured"},
                message="[semantic] 想要真模型但没配端点，回落到 mock",
            )
        self.mock = MockOpenAIServer(self.logbook, model_name=SEMANTIC_ENV["CR_SEMANTIC_MODEL"])
        base_url = self.mock.start()
        os.environ["CR_SEMANTIC_BASE_URL"] = base_url
        os.environ["CR_SEMANTIC_MODEL"] = SEMANTIC_ENV["CR_SEMANTIC_MODEL"]
        os.environ["CR_SEMANTIC_API_KEY"] = MOCK_API_KEY
        self.logbook.event("semantic_wired", {"base_url": base_url, "provider": "remote_api"})

    def _build_runtime(self) -> None:
        """Build the program's configuration and Runtime object."""
        from companion_runtime.config import load_config, resolve_paths
        from companion_runtime.runtime import Runtime

        base_dir = self.config.resolved_base_dir()
        base_dir.mkdir(parents=True, exist_ok=True)
        config = load_config(self.config.config_path)
        resolve_paths(config, base_dir)
        if self.config.use_mock_semantics:
            # The switch has to be set on the config object, not the environment.
            # ``providers.resolve_provider_name`` prefers ``config.semantic.provider``
            # over ``CR_SEMANTIC_PROVIDER``, and ``load_config`` always fills that
            # field with its ``"disabled"`` default -- so the single-underscore
            # environment variable the README documents is dead here (it is only
            # consulted when no config object carries the field at all).
            # ``load_config`` also rejects it as an unknown override:
            # "Ignoring unknown environment override: CR_SEMANTIC_PROVIDER".
            # Setting the field directly is immune to that spelling trap.
            config.semantic.provider = "remote_api"
        else:
            config.semantic.provider = "disabled"
        self._apply_values(config)
        self._runtime_config = config
        self.runtime = Runtime(config, seed=self.config.seed, created_at=getattr(self, "epoch", None) or self.clock.now())
        self.logbook.event(
            "runtime_built",
            {
                "database": config.storage.database_path,
                "raw_log": config.storage.raw_log_path,
                "wal": config.storage.wal,
                "provider": getattr(self.runtime.semantic_provider, "name", "unknown"),
                "provider_available": bool(self.runtime.semantic_provider.available())
                if hasattr(self.runtime.semantic_provider, "available")
                else False,
                "created_at": self.clock.now().isoformat(),
            },
        )

    def _apply_values(self, config: Any) -> None:
        """Fold the configured value axes into the Runtime's personality.

        The eight axes are the character: they are read by the emotion dynamics,
        the motivational game, memory salience and boundary enforcement, so they
        decide how easily this character is moved, how much it holds back, and
        whether a due matter is enough to make it speak. Leaving them at the
        library defaults is a legitimate choice, but it is a choice -- and an
        invisible one, because nothing in the run says "generic personality".

        They are seeded only when the runtime row is *created*, so a run directory
        that already holds a database keeps its original profile. Saying that out
        loud beats letting an operator change a number and see nothing happen.
        """
        from companion_runtime.typing import ValueProfile

        defaults = ValueProfile().to_dict()
        requested = {str(k): float(v) for k, v in (self.config.values or {}).items()}
        unknown = sorted(set(requested) - set(defaults))
        if unknown:
            raise RuntimeError(
                f"unknown value axe(s): {', '.join(unknown)}; known: {', '.join(sorted(defaults))}"
            )
        effective = {**defaults, **requested}
        config.values = ValueProfile.from_mapping(effective)

        database = Path(str(config.storage.database_path))
        existed = database.exists()
        self.logbook.event(
            "values_configured",
            {
                "effective": effective,
                "overridden": requested,
                "database_existed": existed,
                "note": "values are seeded only when the runtime row is created"
                if not existed
                else "database already existed; the stored profile is unchanged",
            },
            message=(
                f"[values] {len(requested)} axe(s) overridden" if requested else "[values] library defaults"
            )
            + (" | DB 已存在，本次覆盖不会生效" if existed and requested else ""),
        )
        if existed and requested:
            self.logbook.warn(
                "values_ignored",
                {"database": str(database), "overridden": requested},
                message=(
                    f"[values] {database} 已存在：价值观只在创建运行时那一行时写入，"
                    "改动不会生效。换一个空的 --run-dir 才能看到效果。"
                ),
            )

    def _start_http(self, startup_timeout: float) -> None:
        """Serve the program's app with uvicorn in a background thread."""
        import asyncio
        import socket

        import uvicorn

        from companion_runtime.api import create_app

        self.app = create_app(self.runtime, self._runtime_config)
        self.port = self.config.port or _free_port()
        self.base_url = f"http://{self.config.host}:{self.port}"
        holder: dict[str, Any] = {}

        def _main() -> None:
            """Own the event loop that serves HTTP and runs the Scheduler."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder["loop"] = loop

            async def _serve() -> None:
                server = uvicorn.Server(
                    uvicorn.Config(
                        self.app,
                        host=self.config.host,
                        port=self.port,
                        log_level="warning",
                        access_log=False,
                        log_config=None,
                    )
                )
                holder["server"] = server
                self._ready.set()
                try:
                    await server.serve()
                finally:
                    loop.stop()

            try:
                loop.run_until_complete(_serve())
            except Exception as exc:  # noqa: BLE001 - surfaced through health wait
                self._error = f"{type(exc).__name__}: {exc}"
                self._ready.set()
            finally:
                holder["loop"] = None

        self._thread = threading.Thread(target=_main, name="cf-runtime-http", daemon=True)
        self._thread.start()
        self._thread_ref = holder
        if not self._ready.wait(timeout=startup_timeout):
            raise RuntimeError("the program's HTTP server never started")
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self._health_ok():
                return
            if self._error:
                raise RuntimeError(f"the program's HTTP server failed: {self._error}")
            time.sleep(0.05)
        raise RuntimeError(f"the program never became healthy at {self.base_url}/health")

    def _start_scheduler(self) -> None:
        """Wire the program's own adaptive Scheduler, exactly as ``serve`` does."""
        import asyncio

        from companion_runtime.scheduler import Scheduler

        loop = self._thread_ref.get("loop")
        if loop is None:
            return
        self.scheduler = Scheduler(
            config=self._runtime_config,
            round_callback=lambda: self.runtime.endogenous_round(now=self.clock.now()),
            rng=random.Random(self.config.seed or 0),
            runtime=self.runtime,
        )
        # The Scheduler owns asyncio tasks, so it must be started from its loop.
        asyncio.run_coroutine_threadsafe(self.scheduler.start(), loop).result(timeout=10)

    def _start_control(self) -> None:
        """Start the loopback control plane."""
        self.control = ControlServer(
            self.clock,
            hooks={"tick": self.tick, "endogenous": self.endogenous, "status": self.status, "shutdown": self.request_stop},
            logbook=self.logbook,
        )
        self.control.start()

    def _start_heartbeat(self) -> None:
        """Start the beat that makes time pass and records the variables.

        A non-positive interval disables the background beat entirely, which is
        what a caller driving the clock by hand wants. Every ``lazy_tick``
        *consumes* the elapsed time it integrates over, so a background beat
        running between two manual steps leaves the next ``endogenous_round`` with
        ``delta_t ~ 0`` -- and the hazard draw then has probability ~0 no matter how
        long the scene is supposed to have lasted. That failure looks exactly like
        "the character never wants to speak", which is why it is worth a switch
        rather than a footnote.
        """
        if self.config.heartbeat_interval_s <= 0:
            self.logbook.event(
                "heartbeat_disabled",
                {"reason": "heartbeat_interval_s <= 0"},
                message="[beat] 后台心跳已关闭（由调用方手动推进）",
            )
            return
        self._heartbeat = threading.Thread(target=self._beat_loop, name="cf-heartbeat", daemon=True)
        self._heartbeat.start()

    # -------------------------------------------------------------- heartbeat

    def _beat_loop(self) -> None:
        """Advance the clock and tick until asked to stop."""
        step = parse_duration(self.config.step) if self.config.step else None
        while not self._stop.wait(self.config.heartbeat_interval_s):
            try:
                self.beat(step)
            except Exception as exc:  # noqa: BLE001 - one bad beat must not end the run
                self.logbook.warn("heartbeat_error", {"error": f"{type(exc).__name__}: {exc}"})

    def beat(self, step: Any = None) -> dict[str, Any]:
        """Run one heartbeat: move the clock, tick the program, log the variables.

        Args:
            step: Duration to add before ticking; ``None`` uses the clock's own speed.

        Returns:
            The variables observed after the tick.
        """
        if step is not None:
            self.clock.advance(step)
        self._rebound += install_process_clock(self.clock)
        self._beats += 1
        moment = self.clock.now()
        report = self.runtime.lazy_tick(moment).to_dict()
        snapshot = self._probe.snapshot() if self._probe is not None else None
        variables = dict(snapshot.variables) if snapshot is not None else {}
        self._last_variables = variables
        self.logbook.event(
            "heartbeat",
            {
                "virtual_now": moment.isoformat(),
                "beat": self._beats,
                "clock_scale": self.clock.scale,
                "clock_frozen": self.clock.frozen,
                "tick": report,
                **variables,
                "detail": snapshot.raw if snapshot is not None else {},
                "probe_errors": snapshot.errors if snapshot is not None else {},
            },
            message=(
                f"[beat {self._beats}] t={moment.isoformat()} "
                f"mood={variables.get('mood_valence', 0):+.2f}/{variables.get('mood_arousal', 0):+.2f} "
                f"I/R/P={variables.get('impulse', 0):.2f}/{variables.get('restraint', 0):.2f}/{variables.get('pressure', 0):.2f} "
                f"proactive={variables.get('allow_proactive')} "
                f"unfinished={variables.get('unfinished_open')} candidates={variables.get('candidates_active')} "
                f"next_wake_in={variables.get('next_wake_in_s')}s {variables.get('next_wake_reasons') or ''}"
            ),
        )
        return variables


    def _build_llm(self) -> None:
        """Build the acting layer: a real endpoint when configured, else a stand-in."""
        configured = OpenAICompatibleMainLLM.from_env(
            logbook=self.logbook,
            base_url=self.config.llm_base_url or None,
            model=self.config.llm_model or None,
            system_prompt=self.config.llm_system_prompt or None,
            temperature=self.config.llm_temperature,
        )
        if configured.configured:
            self.llm = configured
            self.logbook.event(
                "main_llm_wired",
                {"mode": "openai_compatible", **configured.describe()},
                message=f"[llm] {configured.model} @ {configured.base_url}",
            )
        else:
            self.llm = ScriptedMainLLM(logbook=self.logbook)
            self.logbook.warn(
                "main_llm_wired",
                {
                    "mode": "scripted",
                    "hint": f"set {BASE_URL_ENV} and {MODEL_ENV} for a real acting layer",
                },
                message="[llm] 没有配置主 LLM 端点，使用确定性替身（回复不代表真模型）",
            )

    def _start_host(self) -> None:
        """Load the shipped AstrBot plugin so there is something to talk to."""
        if not self.config.use_host:
            return
        self.platform = Platform()
        self.host = AstrBotHost(
            runtime_base_url=self.base_url,
            platform=self.platform,
            llm=self.llm,
            clock=self.clock,
            logbook=self.logbook,
            plugin_root=self.config.plugin_root,
        )
        self.host.start()
        self.platform.open(SESSION_DEFAULT)

    def user_turn(self, text: str, session: str = SESSION_DEFAULT) -> str:
        """Send one user message through the host and return the reply.

        This is the whole deployed path: platform -> plugin hooks -> Runtime
        context injection -> main LLM -> delivery report.
        """
        if self.host is None:
            raise RuntimeError("the AstrBot host is not running (use_host=False?)")
        return self.host.user_turn(text, session=session, at=self.clock.now())

    def last_variables(self) -> dict[str, Any]:
        """Return the most recent variable snapshot, for a status line."""
        return dict(self._last_variables)

    def beat_now(self, step: Any = None) -> dict[str, Any]:
        """Run one heartbeat immediately (alias of :meth:`beat`)."""
        return self.beat(step)

    # ------------------------------------------------------------------ hooks

    def tick(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Force one heartbeat now (used by the control plane)."""
        body = dict(payload or {})
        step = None
        if body.get("by"):
            step = parse_duration(str(body["by"]))
        variables = self.beat(step)
        return {"beat": self._beats, "virtual_now": self.clock.now().isoformat(), "variables": variables}

    def endogenous(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Force one endogenous decision round now (never touches state otherwise)."""
        body = dict(payload or {})
        when = parse_when(str(body["when"])) if body.get("when") else self.clock.now()
        outcome = self.runtime.endogenous_round(
            now=when,
            force=bool(body.get("force", True)),
            create_attempt=bool(body.get("create_attempt", True)),
        )
        result = outcome.to_dict()
        self.logbook.event("endogenous", {"virtual_now": when.isoformat(), "outcome": result})
        return result

    def status(self) -> dict[str, Any]:
        """Return a snapshot of the whole harness."""
        return {
            "virtual_now": self.clock.now().isoformat(),
            "epoch": self.epoch.isoformat() if self.epoch else None,
            "clock": self.clock.state().to_dict(),
            "beats": self._beats,
            "base_url": self.base_url,
            "control_url": self.control.base_url if self.control else "",
            "mock_url": self.mock.base_url if self.mock else "",
            "mock_calls": self.mock.calls_made if self.mock else 0,
            "mock_stats": self.mock.stats() if self.mock else {},
            "provider": getattr(getattr(self.runtime, "semantic_provider", None), "name", "unknown"),
            "provider_health": _safe_health(self.runtime),
            "llm": self.llm.describe() if self.llm is not None else {},
            "llm_stats": self.llm.stats() if hasattr(self.llm, "stats") else {},
            "host": self.host.stats() if self.host is not None else {},
            "variables": dict(self._last_variables),
            "rebound_bindings": self._rebound,
            "run_dir": str(self.run_dir),
            "trace": str(self.logbook.trace_path),
        }

    def script(self, *replies: MockReply) -> None:
        """Replace the mock endpoint's script."""
        if self.mock is None:
            raise RuntimeError("the mock endpoint is disabled (use_mock_semantics=False)")
        self.mock.script = MockScript(list(replies))
        self.logbook.event("mock_script", {"replies": len(replies)})

    def _health_ok(self) -> bool:
        """Return whether the program answers ``/health``."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=2.0) as response:  # noqa: S310
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError):
            return False

    # ------------------------------------------------------------------- stop

    def request_stop(self) -> dict[str, Any]:
        """Ask the harness to stop (callable from the control plane)."""
        self._stop.set()
        return {"stopping": True}

    def stop(self, timeout: float = 15.0) -> dict[str, Any]:
        """Stop the heartbeat, the program and every server. Idempotent.

        Returns:
            A teardown summary, including whether any program source file was
            created or modified (it must not be -- that is the framework's
            central promise).
        """
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=timeout)
            self._heartbeat = None
        if self.scheduler is not None:
            loop = self._thread_ref.get("loop") if hasattr(self, "_thread_ref") else None
            if loop is not None:
                import asyncio

                try:
                    asyncio.run_coroutine_threadsafe(self.scheduler.stop(), loop).result(timeout=10)
                except Exception:  # noqa: BLE001 - stopping must not raise
                    LOGGER.debug("scheduler stop failed", exc_info=True)
        server = self._thread_ref.get("server") if hasattr(self, "_thread_ref") else None
        if server is not None:
            server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self.host is not None:
            self.host.stop()
            self.host = None
        if self._log_bridge is not None:
            self._log_bridge.detach()
            self._log_bridge = None
        if self.control is not None:
            self.control.stop()
            self.control = None
        if self.mock is not None:
            self.mock.stop()
        if getattr(self, "runtime", None) is not None:
            try:
                self.runtime.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                LOGGER.debug("runtime close failed", exc_info=True)
        source_check = _compare_sources(getattr(self, "_source_before", {}), self._program_src)
        summary = {
            "beats": self._beats,
            "virtual_now": self.clock.now().isoformat(),
            "mock_calls": self.mock.calls_made if self.mock else 0,
            "llm_stats": self.llm.stats() if hasattr(self.llm, "stats") else {},
            "program_source_untouched": source_check,
        }
        self.logbook.event("harness_stop", summary, message=f"[stop] beats={self._beats} mock_calls={summary['mock_calls']}")
        self.logbook.close()
        return summary


def _safe_health(runtime: Any) -> dict[str, Any]:
    """Return the provider's health snapshot, or an error marker."""
    provider = getattr(runtime, "semantic_provider", None)
    if provider is None or not hasattr(provider, "health"):
        return {}
    try:
        return dict(provider.health())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _free_port() -> int:
    """Ask the OS for a free loopback port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _snapshot_sources(src: Path) -> dict[str, tuple[int, float]]:
    """Record size and mtime of every ``.py`` under ``src``.

    Comparing a before/after snapshot is the only honest way to claim "this run
    did not write the program": a heuristic such as "modified in the last minute"
    would also flag a file the developer edited by hand just before starting.
    """
    if not src.exists():
        return {}
    snapshot: dict[str, tuple[int, float]] = {}
    for path in src.rglob("*.py"):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path.relative_to(src))] = (stat.st_size, stat.st_mtime)
    return snapshot


def _compare_sources(before: Mapping[str, tuple[int, float]], src: Path) -> dict[str, Any]:
    """Report every program source file this run created, modified or deleted."""
    after = _snapshot_sources(src)
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    return {
        "checked": bool(before),
        "files": len(before),
        "created": created,
        "changed": changed,
        "deleted": deleted,
        "untouched": not (created or changed or deleted),
    }
