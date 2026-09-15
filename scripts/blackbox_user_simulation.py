#!/usr/bin/env python3
"""Black-box "a real person uses the deployed bot" acceptance simulation.

This script plays a *user*, not a test harness with privileged access. Everything it
asserts is something that person could observe from the chat window:

1. the **platform transcript** - what the bot actually sent into a chat session
   (per session, in order, with simulated timestamps), and what the user said;
2. the **host acting layer** - the reply AstrBot's main LLM produced, and (for a
   proactive message) the prompt the Runtime composed for it;
3. the **public HTTP surface** an operator would use: ``/health``, ``/v1/*``,
   ``/tick``, ``/context``.

Anything read beyond those three (``/state``, ``/unfinished``, ``/candidates``,
``/boundaries``, ``/attempts``, ``/schedule``, ``/outbox``) is labelled
``运维可观测面`` (operator-observable surface) in the output and is used only as a
diagnostic. It is never the primary evidence for a user-facing claim.

What is real
------------
* A real ``uvicorn`` server in front of a real
  :class:`~companion_runtime.runtime.Runtime`, on an OS-assigned loopback port,
  over a real **file** SQLite database in WAL mode with a real JSONL mirror.
* The real autonomous :class:`~companion_runtime.scheduler.Scheduler`, wired the
  way ``companion_runtime.cli.cmd_serve`` wires it, so proactive messages are
  decided by the Runtime's own loop - the script never calls ``/endogenous``.
* The **shipped AstrBot plugin** (``astrbot_plugin_companion_runtime/main.py``)
  loaded against the AstrBot public-API stubs that ship in
  ``astrbot_plugin_companion_runtime/tests/stubs``, using its real decorators
  (``filter.custom_filter`` / ``on_llm_request`` / ``after_message_sent``) and the
  plugin's own ``AiohttpRuntimeTransport`` for a real HTTP hop to the live
  Runtime. The user's words therefore travel through the plugin's actual hook,
  and a proactive message travels back through the plugin's lease/render/
  authorize/send path.

What is faked (and only this)
-----------------------------
* **The platform**: a fake chat app with a per-session address book. Delivery to
  an unregistered session fails exactly like an unmatched AstrBot platform.
* **AstrBot's host main LLM**: deterministic. A proactive render returns a
  sentence built from the Runtime's own ``- 想做的事：<intent>`` line (so the
  Runtime's words are what the user comes to see); a user turn is answered with a
  neutral acknowledgement that quotes the user's own message. The host never
  re-states the Runtime's injected background block, which that block itself
  instructs the model not to quote.
* **The clock**: the whole process shares one simulated clock (the harness
  rebinds ``companion_runtime.utility.utcnow`` and the adapter's
  ``utc_now_iso``). A real deployment has one system clock for host, adapter and
  Runtime; the simulation has one *simulated* clock, advanced by the script, so a
  ten-day story finishes in well under two minutes of wall clock. Every duration
  is therefore expressed in simulated time and stays a real window (the boundary
  window, the cooldown and the daily contact cap are all still enforced by the
  shipped code over that clock).

Because of that clock, the script, not the OS, drives time: it advances the
simulated clock and lets the Scheduler's next wake-up integrate the elapsed
interval, falling back to the public ``POST /tick`` when the Scheduler's gate is
closed (a closed gate means "no decision is due", not "time did not pass").

What is therefore NOT proven
----------------------------
* Real AstrBot behaviour: provider resolution, history persistence, concurrency
  and priority handling of the host pipeline are stubbed. Only the plugin's own
  hook bodies and wire traffic are real.
* The host LLM's own judgement. It is a deterministic stub, so this run cannot
  show that a *real* model would phrase a proactive message well; it shows which
  topics the Runtime handed it.
* Wall-clock timing: delivery latency, timeouts and retry pacing are exercised
  only in their simulated-clock aspect (the adapter's own retry queue and lease
  heartbeats still use the real monotonic clock).
* Multi-process deployment: Runtime and adapter share one process here.
* Anything about a real platform's message ordering guarantees beyond the
  per-session order the fake platform records.

Reuse note
----------
The plumbing (server boot, adapter import trick, config build, HTTP helpers,
teardown, artifact writing) follows ``scripts/e2e_resilience_simulation.py``,
which is already in this repository; this script intentionally re-implements the
small parts it needs rather than importing that 3.8k-line phase suite, so a
black-box run never depends on the white-box phases' assumptions.

The user's day, phase by phase
-----------------------------
1. ``setup`` - dependencies, environment scrub, artifact root, no secrets, and
   the wiring facts the later phases rely on (loopback, file SQLite/WAL, the real
   plugin hooks, one simulated clock).
2. ``greeting`` - the user says hello and chats.
3. ``timed_matter`` - the user leaves a dated promise and goes quiet; the bot has
   to speak first, from the prompt the Runtime itself composed.
4. ``closure`` - the user reports the result; the topic is then closed.
5. ``boundary`` - the user forbids the topic and asks not to be contacted; three
   simulated days must pass without an unprompted message.
6. ``resume`` - the user lifts the mute and gives a fresh reason to talk.
7. ``silence`` - an unanswered message must not become pressure or guilt.
8. ``isolation`` - a second chat exists and must not hear the first chat's life.
9. ``restart`` - host and Runtime are stopped and restarted on the same database;
   a message that was pending must arrive exactly once.
10. ``replay`` - the same user event and the same action result are re-delivered.
11. ``timeline`` - the whole user-perspective transcript, plus the global
    contract (daily cap, no duplicates, no leakage, no crosstalk, every message
    attributable to a scripted window).
12. ``teardown`` - every thread joined, artifacts confined to ``--base-dir``, no
    bytecode next to the sources.

Usage::

    python scripts/blackbox_user_simulation.py --base-dir F:\\bb-user
    python scripts/blackbox_user_simulation.py --base-dir ... --only setup,greeting
    python scripts/blackbox_user_simulation.py --base-dir ... --quiet
    python scripts/blackbox_user_simulation.py --list-phases

Exit code is ``0`` only when every check passed, ``1`` when a check failed, and
``2`` when the script cannot start (missing dependencies).

``--fault`` injects a controlled defect into the *harness* (never into repository
sources) so that a check can be shown to bite. It is off by default and exists
only to prove that the corresponding check fails when the fact it protects is
broken: ``leak`` (a hidden-context/credential marker in a delivered message),
``duplicate`` (every proactive message sent twice), ``topic`` (a proactive
message that ignores the topic ban), ``guilt`` (an accusatory message),
``cross_session`` (the private chat's messages delivered into the group chat) and
``default_session`` (everything delivered to the process-default conversation).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import os
import random
import re
import shutil
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# Never drop bytecode next to the project sources: the only artifacts a run may
# leave behind live under --base-dir.
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = REPO_ROOT / "runtime" / "src"
PLUGIN_ROOT = REPO_ROOT / "astrbot_plugin_companion_runtime"
PLUGIN_STUBS = PLUGIN_ROOT / "tests" / "stubs"
PLUGIN_PACKAGE = "astrbot_plugin_companion_runtime"

HOST = "127.0.0.1"

#: Two distinct AstrBot-style chat sessions (a private chat and a group).
SESSION_A = "webchat:FriendMessage:10001"
SESSION_B = "webchat:GroupMessage:20002"
#: The Runtime's own default conversation. No user-visible traffic may go there.
SESSION_DEFAULT = "default"

#: Seed for the Runtime's own decisions. A second, separate seed drives the
#: Scheduler's interval jitter so the character's decisions stay reproducible.
RUNTIME_SEED = 20260415
SCHEDULER_SEED = 771205

#: How far the simulated clock moves per step. The step is chosen so that a step
#: is shorter than every configured window it must respect (the Runtime's own
#: pressure-integration cap is 6 simulated hours, so nothing is truncated).
SIM_STEP = timedelta(hours=2)
#: Multiples of the step used by the scripted story.
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# ------------------------------------------------------------------ the story
# The user's words. Only the texts matter for the assertions; every one of them
# is a plausible thing for a person to type.
TEXT_GREETING = "你好呀，今天过得怎么样？"
TEXT_APPOINTMENT = "我明天下午三点面试，结束了告诉你。"
TEXT_RESULT = "面试过了！谢谢你那天惦记我。"
TEXT_TOPIC_BAN = "以后别再提面试这件事了。"
TEXT_CONTACT_BAN = "以后别主动找我了。"
TEXT_REVOKE = "我撤回刚才那句话，你可以主动找我了。"
TEXT_NEW_TOPIC = "对了，我明天上午有个考试，考完告诉你。"
TEXT_SESSION_B = "你好呀，我明天下午有个体检，出结果告诉你。"
TEXT_SMALL_TALK = "我先去健身房了，回头聊。"

#: Topics the user forbade after ``TEXT_TOPIC_BAN``.
FORBIDDEN_TOPICS = ("面试",)
#: Words that would make a message read as guilt-tripping, in either language.
GUILT_PHRASES = (
    "你怎么不理我",
    "为什么不回",
    "你是不是不想理我",
    "我很失望",
    "你都不理我",
    "你为什么不理",
    "你把我忘了吧",
    "又是我一个人",
    "你总是这样",
    "你根本不在乎",
    "我是不是很烦",
    "算了，不打扰你了",
)
#: Patterns that must never reach a user: hidden-context markers, credential
#: shapes and internal identifiers.
LEAKAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("context_tag", re.compile(r"<companion_runtime_context")),
    ("injection_banner", re.compile(r"以下是\s*Runtime\s*注入")),
    ("product_name", re.compile(r"companion_runtime")),
    ("api_key_word", re.compile(r"api[_-]?key", re.IGNORECASE)),
    ("bearer", re.compile(r"Bearer\s")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{6,}")),
    (
        "internal_id",
        re.compile(r"\b(?:evt|obx|att|cnd|unf|emo|obs)_[0-9a-f]{6,}\b", re.IGNORECASE),
    ),
)

OK_MARK = "[PASS]"
BAD_MARK = "[FAIL]"
NOTE_MARK = "  ."
OPS_LABEL = "运维可观测面"

#: Environment variables that could carry a provider credential. Scrubbed at
#: startup, then asserted absent, so a run can never reach a paid endpoint.
PROVIDER_ENV_PATTERN = re.compile(
    r"(API[_-]?KEY|_TOKEN$|^OPENAI|^ANTHROPIC|^DEEPSEEK|^GEMINI|^GOOGLE_API|"
    r"^AZURE_OPENAI|^DASHSCOPE|^MOONSHOT|^ZHIPU|^MISTRAL|^COHERE|^GROQ|^XAI)",
    re.IGNORECASE,
)

# ------------------------------------------------------------------ project imports

IMPORT_ERROR = ""
# The checkout's own source tree comes first, so the run always exercises the
# working tree rather than an installed copy.
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

try:
    import aiohttp as _aiohttp  # noqa: F401  (the adapter's HTTP transport needs it)

    import uvicorn as _uvicorn  # noqa: F401

    from companion_runtime import utility as runtime_utility
    from companion_runtime.api import create_app
    from companion_runtime.config import RuntimeConfig
    from companion_runtime.runtime import Runtime
    from companion_runtime.scheduler import Scheduler
except Exception as exc:  # noqa: BLE001 - reported as a setup failure, not a traceback
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ reporting


@dataclass
class Check:
    """One assertion, with the phase it belongs to."""

    phase: str
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Section:
    """A named group of checks, one per phase."""

    title: str
    identifier: str = ""
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


class Verifier:
    """Collects PASS/FAIL results, prints them and writes the run report."""

    def __init__(self, *, quiet: bool = False) -> None:
        """Create the verifier.

        Args:
            quiet: Suppress informational notes and diagnostics (checks and the
                transcript are still printed).
        """
        self.sections: list[Section] = []
        self.quiet = quiet
        self.current: Section | None = None
        self.extra_log: list[str] = []

    def load_phases(self, phases: Sequence[tuple[str, str]]) -> None:
        """Declare every phase up front so a skipped one is still visible.

        Args:
            phases: ``(identifier, title)`` pairs in run order.
        """
        for identifier, title in phases:
            self.sections.append(Section(title=title, identifier=identifier))

    def skip(self, identifier: str) -> Section:
        """Return the declared section for a phase, running or not.

        Args:
            identifier: Phase identifier.

        Returns:
            The declared section.
        """
        for section in self.sections:
            if section.identifier == identifier:
                return section
        raise KeyError(identifier)

    def phase(self, identifier: str, title: str) -> Section:
        """Start a phase: reuse its declared section and print its banner.

        Args:
            identifier: Phase identifier used by ``--only``.
            title: Human-readable title.

        Returns:
            The section checks should be recorded in.
        """
        section = self.skip(identifier)
        section.title = f"{title} [{identifier}]"
        self.current = section
        self._line("")
        self._line("=" * 78)
        self._line(section.title)
        self._line("=" * 78)
        return section

    def note(self, message: str) -> None:
        """Record and print an informational line."""
        if self.current is not None:
            self.current.notes.append(message)
        if not self.quiet:
            self._line(f"{NOTE_MARK} {message}")

    def ops(self, title: str, payload: Any) -> None:
        """Record and print an operator-surface diagnostic, clearly labelled.

        Args:
            title: What was read.
            payload: JSON-serialisable value.
        """
        text = f"[{OPS_LABEL}] {title}: {_short_json(payload)}"
        if self.current is not None:
            self.current.diagnostics.append(text)
        if not self.quiet:
            self._line(f"    {text}")

    def check(self, label: str, condition: Any, detail: str = "") -> bool:
        """Record one PASS/FAIL assertion.

        Args:
            label: The user-visible fact this check protects.
            condition: Truthy when the fact holds.
            detail: Expected vs actual, plus the offending text when it does not.

        Returns:
            Whether the check passed.
        """
        ok = bool(condition)
        check = Check(phase=self.current.title if self.current else "(none)", label=label, ok=ok, detail=detail)
        if self.current is not None:
            self.current.checks.append(check)
        suffix = f"  [{detail}]" if detail else ""
        self._line(f"  {OK_MARK if ok else BAD_MARK} {label}{suffix}")
        return ok

    def line(self, text: str = "") -> None:
        """Print one line of transcript/echo."""
        self._line(text)

    def _line(self, text: str) -> None:
        """Print and remember one line."""
        print(text, flush=True)

    def totals(self) -> tuple[int, int]:
        """Return ``(passed, failed)`` over every recorded check."""
        checks = [check for section in self.sections for check in section.checks]
        passed = sum(1 for check in checks if check.ok)
        return passed, len(checks) - passed

    def failures(self) -> list[Check]:
        """Return every failed check, in order."""
        return [check for section in self.sections for check in section.checks if not check.ok]

    def summary(self) -> int:
        """Print the summary and return the process exit code."""
        passed, failed = self.totals()
        self._line("")
        self._line("=" * 78)
        self._line("SUMMARY")
        self._line("=" * 78)
        for section in self.sections:
            section_passed = sum(1 for check in section.checks if check.ok)
            section_failed = len(section.checks) - section_passed
            mark = OK_MARK if section_failed == 0 else BAD_MARK
            empty = " (skipped)" if not section.checks else ""
            self._line(
                f"  {mark} {section.title}: {section_passed}/{len(section.checks)} checks passed{empty}"
            )
        self._line("")
        self._line(f"  checks passed: {passed}")
        self._line(f"  checks failed: {failed}")
        if failed:
            self._line("")
            self._line("FAILURES (with diagnostics)")
            self._line("-" * 78)
            for index, check in enumerate(self.failures(), start=1):
                self._line(f"  {index}. [{check.phase}] {check.label}")
                if check.detail:
                    self._line(f"       {check.detail}")
        return 1 if failed else 0

    def as_report(self) -> dict[str, Any]:
        """Return the JSON-serialisable run report."""
        return {
            "generated_at": _real_now_iso(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "repo_root": str(REPO_ROOT),
            "checks": [
                {"phase": check.phase, "label": check.label, "ok": check.ok, "detail": check.detail}
                for section in self.sections
                for check in section.checks
            ],
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
    """Keeps the Runtime's and the adapter's log lines for diagnostics.log."""

    def __init__(self, *, echo_level: int = logging.WARNING) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []
        self.echo_level = echo_level

    def emit(self, record: logging.LogRecord) -> None:
        """Record one log line, echoing anything at or above the echo level."""
        with contextlib.suppress(Exception):
            line = self.format(record)
            self.records.append(line)
            if record.levelno >= self.echo_level and not V.quiet:
                print(f"    [log] {line}", flush=True)


LOG_HANDLER = _MemoryLogHandler()


def configure_logging() -> None:
    """Route Runtime/uvicorn/adapter logs into the diagnostics buffer."""
    LOG_HANDLER.setFormatter(logging.Formatter("%(levelname)s %(name)s :: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    with contextlib.suppress(Exception):
        root.handlers = [LOG_HANDLER]
    for name in ("companion_runtime", "uvicorn", "uvicorn.error", "uvicorn.access", "astrbot"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


# ------------------------------------------------------------------ small helpers


def _real_now_iso() -> str:
    """Return the real wall-clock time (used only for report metadata)."""
    return datetime.now(timezone.utc).isoformat()


def _short(value: Any, limit: int = 200) -> str:
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


def _short_json(payload: Any, limit: int = 700) -> str:
    """Render a diagnostic payload readably."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_message(text: str) -> str:
    """Return a punctuation- and whitespace-insensitive form of a message.

    Args:
        text: Raw message text.

    Returns:
        The comparison form used by the duplicate check.
    """
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def free_port() -> int:
    """Return a currently unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def scrub_environment() -> list[str]:
    """Remove provider-shaped environment variables from this process.

    Returns:
        The names that were removed, for the setup check to report.
    """
    removed: list[str] = []
    for name in list(os.environ):
        if PROVIDER_ENV_PATTERN.search(name):
            os.environ.pop(name, None)
            removed.append(name)
    return removed


def scan_leakage(text: str) -> list[str]:
    """Return the names of every leakage pattern found in ``text``."""
    return [name for name, pattern in LEAKAGE_PATTERNS if pattern.search(text or "")]


def scan_guilt(text: str) -> list[str]:
    """Return every accusatory phrase found in ``text``."""
    return [phrase for phrase in GUILT_PHRASES if phrase in (text or "")]


# ------------------------------------------------------------------ simulated clock


class SimClock:
    """The simulation's single clock: host, adapter and Runtime all read it."""

    def __init__(self, start: datetime) -> None:
        """Create the clock at ``start`` (an aware UTC datetime)."""
        self._now = start
        self._origin = start

    def now(self) -> datetime:
        """Return the current simulated UTC moment."""
        return self._now

    def iso(self) -> str:
        """Return the simulated moment the way the adapter's wire format wants it."""
        return self._now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def advance(self, delta: timedelta) -> datetime:
        """Move the simulated clock forward and return the new moment."""
        self._now = self._now + delta
        return self._now

    def reset(self, moment: datetime) -> None:
        """Jump the clock to ``moment`` (only used to align a restart)."""
        self._now = moment

    @property
    def origin(self) -> datetime:
        """Return the moment the run started at."""
        return self._origin


def install_process_clock(clock: SimClock) -> int:
    """Bind every project module's clock to the simulated one.

    A real deployment has one system clock shared by the host framework, the
    adapter and the Runtime. The simulation keeps that invariant with a simulated
    clock instead of the OS clock, which is the only way a ten-day story can run
    in seconds without changing any shipped behaviour.

    Args:
        clock: The simulated clock.

    Returns:
        How many module attributes were rebound (reported by the setup check).
    """
    rebound = 0
    modules: list[Any] = []
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if name == "companion_runtime" or name.startswith("companion_runtime."):
            modules.append(module)
        elif name.startswith(PLUGIN_PACKAGE):
            modules.append(module)
    for module in modules:
        if hasattr(module, "utcnow"):
            module.utcnow = clock.now
            rebound += 1
        if hasattr(module, "utc_now_iso"):
            module.utc_now_iso = clock.iso
            rebound += 1
    runtime_utility.utcnow = clock.now
    rebound += 1
    return rebound


# ------------------------------------------------------------------ HTTP client


@dataclass
class Reply:
    """One HTTP reply, captured without raising."""

    status: int
    json: Any
    text: str
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
        return f"status={self.status} body={_short(self.json, 240)}"


def http_call(
    base_url: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> Reply:
    """Perform one JSON request against the loopback server, never raising.

    Args:
        base_url: ``http://127.0.0.1:<port>``.
        method: HTTP method.
        path: Absolute path including the query string.
        body: JSON body, or ``None``.
        timeout: Per-request timeout in seconds.

    Returns:
        A :class:`Reply`; transport failures arrive as ``status=0`` with
        :attr:`Reply.error` set.
    """
    data = None if body is None else json.dumps(body, default=str).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "companion-runtime-blackbox-user/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback
            raw = response.read().decode("utf-8", "replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:  # a 4xx/5xx is a result, not an error
        raw = exc.read().decode("utf-8", "replace")
        status = int(exc.code)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return Reply(status=0, json=None, text="", error=f"{type(exc).__name__}: {exc}")
    parsed: Any = None
    if raw.strip():
        with contextlib.suppress(ValueError):
            parsed = json.loads(raw)
    return Reply(status=status, json=parsed, text=raw)


# ------------------------------------------------------------------ the fake platform


@dataclass
class Delivery:
    """One message the platform accepted (or refused) for a session."""

    at: datetime
    session: str
    text: str
    kind: str  # "reply" (host answer to a user turn) or "proactive"
    delivered: bool = True


class Platform:
    """Stand-in for AstrBot's platform adapters: an address book per session."""

    def __init__(self, recorder: "Recorder") -> None:
        """Create the platform.

        Args:
            recorder: Transcript recorder every delivery is appended to.
        """
        self._registered: dict[str, bool] = {}
        self._recorder = recorder
        self.deliveries: list[Delivery] = []

    def register(self, session: str) -> None:
        """Make a session resolvable, like binding a platform account."""
        self._registered[session] = True

    def resolve(self, session: str) -> bool:
        """Whether a session can currently be addressed."""
        return bool(self._registered.get(session, False))

    def deliver(self, session: str, text: str, *, kind: str, at: datetime) -> bool:
        """Deliver a message into a session, recording it either way.

        Args:
            session: Target session (``unified_msg_origin``).
            text: Message body.
            kind: ``"reply"`` or ``"proactive"``.
            at: Simulated moment of the delivery.

        Returns:
            ``True`` when the platform had somewhere to put the message, which is
            what AstrBot's own ``send_message`` reports.
        """
        delivered = self.resolve(session)
        record = Delivery(at=at, session=session, text=text, kind=kind, delivered=delivered)
        self.deliveries.append(record)
        self._recorder.record(record)
        return delivered


# ------------------------------------------------------------------ the fake host


def proactive_text_for(prompt: str, *, variant: int = 0) -> str:
    """Return the message a main LLM would write for a Runtime render prompt.

    The prompt is the Runtime's, unmodified: this reads the ``- 想做的事：`` line
    the Runtime composed, which is what makes an assertion on the delivered text
    meaningful - anything the Runtime asks for becomes visible to the user. The
    phrasing rotates between renders the way a real model's would, so two
    *identical* delivered messages can only come from a duplicate delivery of the
    same action (whose text is fixed when it is rendered), never from this stub.

    Args:
        prompt: The render prompt the Runtime handed the adapter.
        variant: Which phrasing to use.

    Returns:
        The sentence the fake host LLM produces.
    """
    intent = "你"
    for line in (prompt or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- 想做的事："):
            intent = stripped.split("：", 1)[1].strip() or intent
            break
    templates = (
        "刚才忽然想起{intent}，现在怎么样了？",
        "这两天一直惦记着{intent}，有消息了吗？",
        "想起{intent}，还好吗？",
        "关于{intent}，我有点好奇结果怎么样了。",
        "又想到{intent}了，方便说说进展吗？",
        "不知道{intent}顺不顺利，有点挂念。",
        "关于{intent}，要是有消息了记得跟我说一声。",
        "刚忙完，突然想知道{intent}怎么样了。",
        "关于{intent}，我一直留意着呢。",
        "有件事一直放在心上：{intent}，怎么样了？",
        "想到{intent}，希望一切顺利。",
        "关于{intent}，要是不方便说也没关系，就是问问。",
        "刚看到时间，又想起{intent}了。",
        "关于{intent}，有进展了就告诉我一声吧。",
        "想起{intent}，不知道现在是什么情况了。",
        "关于{intent}，我这边一直记着，还好吗？",
        "突然有点想知道{intent}的后续。",
        "关于{intent}，等你方便的时候说一声就好。",
        "想到{intent}，不着急，就是想问问。",
        "关于{intent}，最近有什么新消息吗？",
    )
    return templates[variant % len(templates)].format(intent=intent)


def reply_text_for(user_text: str) -> str:
    """Return the host LLM's answer to a user turn.

    Deliberately a quote of the user's own words and nothing else: the hidden
    Runtime background block is explanatory, and the block itself instructs the
    model not to quote it. A reply that mentions a topic therefore only does so
    because the user raised it in that turn.

    Args:
        user_text: What the user said.

    Returns:
        The reply body.
    """
    body = " ".join((user_text or "").split())
    return f"我在听，你说的「{body}」我记下了。"


@dataclass
class LLMCall:
    """One recorded host LLM call."""

    at: datetime
    session: str
    prompt: str
    text: str


class HostLLM:
    """The host's main LLM: deterministic, and derived from the prompt only."""

    def __init__(self, clock: SimClock) -> None:
        """Create the model.

        Args:
            clock: Simulated clock used to timestamp calls.
        """
        self._clock = clock
        self.calls: list[LLMCall] = []
        self.proactive_calls: list[LLMCall] = []

    async def generate(self, *, provider_id: str, prompt: str, session: str = "") -> str:
        """Answer one prompt deterministically.

        Args:
            provider_id: Provider id resolved by the adapter (recorded only).
            prompt: The prompt the host pipeline actually passed.
            session: Session the call belongs to, when the caller knows it.

        Returns:
            The generated text.
        """
        del provider_id
        is_render = "- 想做的事：" in (prompt or "")
        text = ""
        if is_render:
            text = proactive_text_for(prompt, variant=len(self.proactive_calls))
        call = LLMCall(at=self._clock.now(), session=session, prompt=prompt, text=text)
        self.calls.append(call)
        if is_render:
            self.proactive_calls.append(call)
        return text


class HostContext:
    """The three public AstrBot APIs the shipped executor actually calls."""

    def __init__(self, *, platform: Platform, llm: HostLLM, clock: SimClock, faults: "Faults") -> None:
        """Wire the context.

        Args:
            platform: Fake platform messages are sent to.
            llm: Fake main LLM used by both pipeline paths.
            clock: Simulated clock.
            faults: Harness fault injector (off unless asked for).
        """
        self._platform = platform
        self._llm = llm
        self._clock = clock
        self._faults = faults

    async def get_current_chat_provider_id(self, umo: str | None = None) -> str:
        """Resolve the session's current chat provider."""
        if not self._platform.resolve(str(umo or "")):
            raise RuntimeError(f"no chat provider for session {umo!r}")
        return f"webchat-provider::{umo}"

    async def llm_generate(self, *, chat_provider_id: str, prompt: str, **kwargs: Any) -> Any:
        """Generate one completion through the session's provider."""
        del kwargs
        session = str(chat_provider_id).split("::", 1)[-1]
        text = await self._llm.generate(provider_id=chat_provider_id, prompt=prompt, session=session)
        return types.SimpleNamespace(completion_text=text)

    async def send_message(self, session: Any, chain: Any) -> bool:
        """Deliver a proactive message chain; ``False`` mirrors an unmatched session."""
        text = chain if isinstance(chain, str) else "".join(str(part.text) for part in chain.chain)
        text = self._faults.mutate_proactive(text)
        target = self._faults.redirect_session(str(session))
        delivered = self._platform.deliver(target, text, kind="proactive", at=self._clock.now())
        if self._faults.duplicate_proactive:
            self._platform.deliver(target, text, kind="proactive", at=self._clock.now())
        return delivered


class HostLoop:
    """A private asyncio loop for the fake host, like AstrBot's own runtime."""

    def __init__(self, name: str) -> None:
        """Start the loop on its own thread."""
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

    @property
    def alive(self) -> bool:
        """Whether the loop thread is still running."""
        return self._thread.is_alive()

    def close(self) -> None:
        """Cancel what is left, stop the loop and join its thread."""

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


class Faults:
    """Harness-side fault injection, used only to prove that checks bite.

    Every switch is off unless ``--fault`` names it. None of them touch
    repository sources: they corrupt what this script itself feeds the world
    (a delivered message, a session, a replay) so the corresponding user-visible
    check has to fail.
    """

    def __init__(self, names: Iterable[str] = ()) -> None:
        """Create the injector from a set of fault names."""
        self.names = set(names)
        self.marker = "[LEAK-MARKER]"

    @property
    def active(self) -> bool:
        """Whether any fault is enabled."""
        return bool(self.names)

    def mutate_proactive(self, text: str) -> str:
        """Corrupt a proactive message according to the enabled faults."""
        body = text
        if "leak" in self.names:
            body = f"{body} {self.marker} companion_runtime_context api_key=sk-abcdef123456 evt_deadbeef01"
        if "topic" in self.names:
            body = "面试还顺利吗？"
        if "guilt" in self.names:
            body = "你怎么不理我了，我很失望。"
        return body

    @property
    def duplicate_proactive(self) -> bool:
        """Whether every proactive message should be sent twice."""
        return "duplicate" in self.names

    def redirect_session(self, session: str) -> str:
        """Return the session a message is actually addressed to."""
        if "cross_session" in self.names and session == SESSION_A:
            return SESSION_B
        if "default_session" in self.names:
            return SESSION_DEFAULT
        return session


# ------------------------------------------------------------------ transcript


@dataclass
class Turn:
    """One line of the user-perspective transcript."""

    at: datetime
    session: str
    who: str  # "user" or "bot"
    kind: str  # "user", "reply" or "proactive"
    text: str
    delivered: bool = True
    detail: str = ""

    def render(self) -> str:
        """Return the human-readable line written to ``transcript.md``."""
        stamp = self.at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        speaker = "我" if self.who == "user" else "TA"
        note = "" if self.delivered else "  [未送达/undeliverable]"
        detail = f"   <{self.detail}>" if self.detail else ""
        return f"[{stamp}] {self.session}  {speaker}: {self.text}{detail}{note}"


class Recorder:
    """The transcript: what the user saw, per session, in order."""

    def __init__(self) -> None:
        """Create an empty transcript."""
        self.turns: list[Turn] = []
        self._turn_index: dict[str, int] = {}
        self._last_user: dict[str, Turn] = {}

    def record(self, delivery: Delivery) -> None:
        """Record one bot delivery (reply or proactive)."""
        self.turns.append(
            Turn(
                at=delivery.at,
                session=delivery.session,
                who="bot",
                kind=delivery.kind,
                text=delivery.text,
                delivered=delivery.delivered,
            )
        )

    def user(self, *, at: datetime, session: str, text: str) -> Turn:
        """Record one user message and return it."""
        turn = Turn(at=at, session=session, who="user", kind="user", text=text)
        self.turns.append(turn)
        self._last_user[session] = turn
        self._turn_index[session] = self._turn_index.get(session, 0) + 1
        return turn

    def last_user(self, session: str) -> Turn | None:
        """Return the most recent user message in one session."""
        return self._last_user.get(session)

    def turns_for(self, session: str) -> list[Turn]:
        """Return the transcript of one session, in order."""
        return [turn for turn in self.turns if turn.session == session]

    def bot_turns(self, session: str = "", *, kind: str = "") -> list[Turn]:
        """Return bot messages, optionally filtered by session and kind."""
        return [
            turn
            for turn in self.turns
            if turn.who == "bot"
            and (not session or turn.session == session)
            and (not kind or turn.kind == kind)
        ]

    def user_turns(self, session: str = "") -> list[Turn]:
        """Return user messages, optionally filtered by session."""
        return [turn for turn in self.turns if turn.who == "user" and (not session or turn.session == session)]

    def between(self, start: datetime, end: datetime, session: str = "") -> list[Turn]:
        """Return every transcript line inside a simulated time window."""
        return [
            turn
            for turn in self.turns
            if start <= turn.at <= end and (not session or turn.session == session)
        ]

    def markdown(self) -> str:
        """Render the whole transcript as markdown."""
        lines = ["# 用户视角时间线 / user-perspective timeline", ""]
        sessions = sorted({turn.session for turn in self.turns})
        for session in sessions:
            lines.append(f"## {session}")
            lines.append("")
            for turn in self.turns_for(session):
                lines.append(f"- {turn.render()}")
            lines.append("")
        return "\n".join(lines)


# ------------------------------------------------------------------ the Runtime under test


class RuntimeServer:
    """A live Runtime sidecar: real uvicorn, real file SQLite/WAL, own port."""

    def __init__(self, *, config: Any, clock: SimClock, name: str) -> None:
        """Store the configuration; :meth:`start` boots the server."""
        self.config = config
        self.clock = clock
        self.name = name
        self.port = 0
        self.base_url = ""
        self.runtime: Any = None
        self.holder: dict[str, Any] = {}
        self.thread: threading.Thread | None = None

    @property
    def scheduler(self) -> Any:
        """Return the live Scheduler, or ``None`` before it is created."""
        return self.holder.get("scheduler")

    def start(self, *, startup_timeout: float = 30.0) -> None:
        """Boot the server, the Scheduler and the uvicorn loop.

        Args:
            startup_timeout: Seconds to wait for ``/health``.

        Raises:
            RuntimeError: When the server never became reachable.
        """
        import uvicorn

        config = self.config
        created_at = self.clock.now()
        self.runtime = Runtime(config, seed=RUNTIME_SEED, created_at=created_at)
        app = create_app(self.runtime, config)
        self.port = free_port()
        self.base_url = f"http://{HOST}:{self.port}"
        ready = threading.Event()
        holder = self.holder
        clock = self.clock
        runtime = self.runtime

        def _thread_main() -> None:
            """Own the loop: serve HTTP, run the Scheduler, then shut down."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder["loop"] = loop

            async def _main() -> None:
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host=HOST,
                        port=self.port,
                        log_level="warning",
                        access_log=False,
                        log_config=None,
                    )
                )
                holder["server"] = server
                # Wired exactly like `companion-runtime serve`: the same real
                # Scheduler, the same round callback. The only difference is that
                # the round is handed the *simulated* moment, which is how the
                # script plays the world clock; the Scheduler still decides when
                # to wake and whether the gate is open.
                scheduler = Scheduler(
                    config=config,
                    round_callback=lambda: runtime.endogenous_round(now=clock.now()),
                    rng=random.Random(SCHEDULER_SEED),
                    runtime=runtime,
                )
                holder["scheduler"] = scheduler
                await scheduler.start()
                ready.set()
                try:
                    await server.serve()
                finally:
                    await scheduler.stop()

            try:
                loop.run_until_complete(_main())
            finally:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self.thread = threading.Thread(target=_thread_main, name=f"bb-runtime-{self.name}", daemon=True)
        self.thread.start()
        if not ready.wait(timeout=startup_timeout):
            raise RuntimeError(f"runtime {self.name}: the server loop never became ready")
        if not _wait_until(lambda: self.get("/health", timeout=2.0).ok, timeout=startup_timeout):
            raise RuntimeError(f"runtime {self.name}: /health never answered on {self.base_url}")

    def stop(self) -> None:
        """Stop the server, the Scheduler and the Runtime, and join the thread."""
        server = self.holder.get("server")
        if server is not None:
            server.should_exit = True
        thread = self.thread
        if thread is not None:
            thread.join(timeout=25)
            self.thread = None
        if self.runtime is not None:
            with contextlib.suppress(Exception):
                self.runtime.close()

    # -- HTTP convenience ---------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> Reply:
        """GET against the live server."""
        return http_call(self.base_url, "GET", path, **kwargs)

    def post(self, path: str, body: Mapping[str, Any] | None = None, **kwargs: Any) -> Reply:
        """POST against the live server."""
        return http_call(self.base_url, "POST", path, body, **kwargs)

    def tick(self, moment: datetime | None = None) -> Reply:
        """Advance the Runtime's own clock through the public tick endpoint."""
        return self.post("/tick", {"now": (moment or self.clock.now()).isoformat()})

    def health(self) -> dict[str, Any]:
        """Return the health payload (``{}`` when unavailable)."""
        payload = self.get("/health").json
        return payload if isinstance(payload, dict) else {}


def _wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if predicate():
                return True
        time.sleep(interval)
    return False


def build_runtime_config(directory: Path) -> Any:
    """Build the Runtime configuration for the story.

    Every interval is expressed in *simulated* time and is a real window: the
    cooldown is a cooldown, the daily cap a cap, the matter expiry an expiry. They
    are chosen to sit below the story's own granularity (a two-simulated-hour
    step) so a ten-day story is exercised in full.

    Args:
        directory: Scenario directory (database and JSONL mirror live here).

    Returns:
        A configured :class:`~companion_runtime.config.RuntimeConfig`.
    """
    config = RuntimeConfig()
    config.storage.database_path = str(directory / "runtime.sqlite3")
    config.storage.raw_log_path = str(directory / "raw_events.jsonl")
    config.storage.mirror_raw_events = True
    config.storage.wal = True
    config.conversation_id = SESSION_DEFAULT
    # No semantic provider, no network, no key: the standard deployment.
    config.semantic.provider = "disabled"
    config.semantic.settle_on_ingest = True
    config.semantic.deep_refresh_enabled = False
    # Simulated-time windows.
    config.drive.cooldown_seconds = 4 * 3600.0
    config.drive.max_contacts_per_day = 3
    config.boundary.default_temporal_hours = 24.0
    config.unfinished.default_expiry_hours = 72.0
    config.outbox.lease_seconds = 900.0
    config.action.send_expiry_seconds = 3600.0
    config.utility.repeat_window_seconds = 24 * 3600.0
    # The Scheduler's own cadence is wall-clock work, so it is compressed hard:
    # the loop wakes many times per simulated step, and every wake-up that matters
    # is the one after the script moved the world clock.
    config.scheduler.min_interval_seconds = 0.02
    config.scheduler.max_interval_seconds = 0.05
    config.scheduler.busy_poll_seconds = 0.02
    config.scheduler.foreground_pause_seconds = 60.0
    config.utility.min_sleep_seconds = 0.02
    config.utility.max_sleep_seconds = 0.05
    config.task.merge_window_seconds = 5.0
    return config


# ------------------------------------------------------------------ the shipped plugin


class RecordingTransport:
    """The shipped aiohttp transport, plus a record of what went over the wire.

    The plugin's own integration tests replace ``AiohttpRuntimeTransport`` the
    same way; here the replacement *is* the shipped client, so the HTTP hop to the
    live Runtime is real and the replay checks can re-send an identical body. Each
    instance keeps its own log, and :data:`WIRE_HISTORY` keeps the whole run's, so
    a replay check still has the bodies after a host restart.
    """

    instances: list["RecordingTransport"] = []
    base_class: Any = None

    def __init__(self, *, settings: Any = None, log: Any = None) -> None:
        """Create the real transport and remember every body this instance sent."""
        self._inner = type(self).base_class(settings=settings, log=log)
        self.sent: list[tuple[str, dict[str, Any]]] = []
        type(self).instances.append(self)

    async def post_events(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Record and send one event envelope."""
        record = ("events", json.loads(json.dumps(body)))
        self.sent.append(record)
        WIRE_HISTORY.append(record)
        await self._inner.post_events(body, timeout_s=timeout_s)

    async def report_action(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Record and send one action result."""
        record = ("result", json.loads(json.dumps(body)))
        self.sent.append(record)
        WIRE_HISTORY.append(record)
        await self._inner.report_action(body, timeout_s=timeout_s)

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else (health, context, leases) to the real client."""
        return getattr(self._inner, name)


#: Every body the adapter put on the wire during this run, in order.
WIRE_HISTORY: list[tuple[str, dict[str, Any]]] = []


class StubMessageEvent:
    """Minimal stand-in for ``AstrMessageEvent`` with the fields the plugin reads."""

    def __init__(
        self,
        *,
        text: str,
        session: str,
        message_id: str,
        result_text: str = "",
        wake: bool = True,
    ) -> None:
        """Build one platform event."""
        self.unified_msg_origin = session
        self.message_str = text
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.is_at_or_wake_command = wake
        self._result_text = result_text
        self._session = session

    def get_platform_name(self) -> str:
        """Return the platform name (the part before the first ``:``)."""
        return self._session.split(":", 1)[0]

    def get_message_type(self) -> Any:
        """Return AstrBot's message class for this session."""
        scope = self._session.split(":")[1] if ":" in self._session else "FriendMessage"
        return types.SimpleNamespace(value=scope)

    def get_sender_id(self) -> str:
        """Return the sender id."""
        return self._session.rsplit(":", 1)[-1]

    def get_sender_name(self) -> str:
        """Return the sender display name."""
        return "User"

    def get_self_id(self) -> str:
        """Return the bot's own id."""
        return "companion-bot"

    def get_group_id(self) -> str:
        """Return the group id for group sessions."""
        return self._session.rsplit(":", 1)[-1] if "GroupMessage" in self._session else ""

    def get_result(self) -> Any:
        """Return the message AstrBot just sent, as the plugin's hook reads it."""
        if not self._result_text:
            return None
        from astrbot.api.event import MessageEventResult
        from astrbot.api.message_components import Plain

        return MessageEventResult([Plain(self._result_text)])


class PluginHost:
    """The shipped AstrBot plugin, driven through its real hooks."""

    def __init__(
        self,
        *,
        base_url: str,
        platform: Platform,
        llm: HostLLM,
        clock: SimClock,
        faults: Faults,
        adapter_id: str,
        startup_timeout: float = 30.0,
    ) -> None:
        """Load the plugin package with the AstrBot stubs and start its workers."""
        self.base_url = base_url
        self.adapter_id = adapter_id
        self.platform = platform
        self.llm = llm
        self.clock = clock
        self.faults = faults
        self.startup_timeout = startup_timeout
        self.loop: HostLoop | None = None
        self.plugin: Any = None
        self.module: Any = None
        self.filters: Any = None
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.recording: RecordingTransport | None = None

    def start(self) -> None:
        """Import the plugin, install the stubs and run ``initialize``.

        Raises:
            RuntimeError: When the plugin package cannot be loaded.
        """
        if not (PLUGIN_ROOT / "main.py").is_file():
            raise RuntimeError(f"shipped plugin not found at {PLUGIN_ROOT / 'main.py'}")
        for path in (str(PLUGIN_STUBS), str(PLUGIN_ROOT)):
            if path not in sys.path:
                sys.path.insert(0, path)
        if PLUGIN_PACKAGE not in sys.modules:
            package = types.ModuleType(PLUGIN_PACKAGE)
            package.__path__ = [str(PLUGIN_ROOT)]
            sys.modules[PLUGIN_PACKAGE] = package
        self.module = importlib.import_module(f"{PLUGIN_PACKAGE}.main")
        self.filters = importlib.import_module("astrbot.api.event.filter")
        # The adapter's modules only exist now, so the simulated clock has to be
        # bound to them here as well: otherwise the adapter would stamp events
        # with the real clock while the Runtime lives on the simulated one.
        install_process_clock(self.clock)
        # The real transport, remembered: the same substitution the plugin's own
        # integration test performs, and the only way a black-box script can see
        # what the adapter put on the wire. The *unpatched* class is looked up in
        # the module it is defined in, so restarting the host cannot chain the
        # recorder onto itself.
        http_client = importlib.import_module(f"{PLUGIN_PACKAGE}.companion_runtime.http_client")
        RecordingTransport.base_class = http_client.AiohttpRuntimeTransport
        self.module.AiohttpRuntimeTransport = RecordingTransport
        RecordingTransport.instances.clear()

        self.loop = HostLoop("bb-host")
        context = HostContext(
            platform=self.platform, llm=self.llm, clock=self.clock, faults=self.faults
        )
        self.plugin = self.module.CompanionRuntimePlugin(
            context=context,
            config={
                "enabled": True,
                "runtime_base_url": self.base_url,
                "adapter_id": self.adapter_id,
                "observe_mode": "all",
                "report_assistant_messages": True,
                "inject_enabled": True,
                "context_timeout_ms": 500,
                # The world clock moves much faster than this cache's TTL, so the
                # cache is disabled rather than serving a stale background block.
                "context_cache_ttl_ms": 0,
                "context_prefetch": True,
                "request_timeout_ms": 2000,
                "outbox_enabled": True,
                "outbox_poll_interval_ms": 250,
                "outbox_max_actions_per_poll": 2,
                "outbox_lease_ttl_ms": 900000,
                "outbox_max_concurrency": 1,
                "render_timeout_ms": 10000,
                "send_timeout_ms": 10000,
                "queue_base_backoff_ms": 100,
                "queue_max_backoff_ms": 500,
            },
        )
        self.loop.call(self.plugin.initialize())
        for name in ("on_message_observed", "on_llm_request", "on_after_message_sent"):
            self.handlers[name] = self.filters.handler_by_name(name)
        self.recording = RecordingTransport.instances[-1] if RecordingTransport.instances else None

    def stop(self) -> dict[str, Any]:
        """Terminate the plugin and join its loop, leaving nothing running.

        Returns:
            A small report (``loop_alive``, ``tasks_pending``) the restart and
            teardown checks use to state what actually stopped.
        """
        loop = self.loop
        plugin = self.plugin
        if loop is None:
            return {"loop_alive": False, "tasks_pending": 0}
        with contextlib.suppress(Exception):
            loop.call(plugin.terminate(), timeout=30)
        tasks_pending = len(getattr(plugin, "_tasks", []))
        loop.close()
        self.loop = None
        return {"loop_alive": loop.alive, "tasks_pending": tasks_pending}

    @property
    def running(self) -> bool:
        """Whether the plugin's event loop thread is alive."""
        return self.loop is not None and self.loop.alive

    # -- the AstrBot message pipeline ------------------------------------------------

    def user_turn(self, *, text: str, session: str, message_id: str) -> str:
        """Drive one user turn through the real AstrBot hook order.

        The order mirrors AstrBot: observe the message, inject the Runtime's
        context into the LLM request, generate the reply, deliver it, then report
        the delivered message back.

        Args:
            text: What the user typed.
            session: Session the message arrived in.
            message_id: Platform message id.

        Returns:
            The reply the host pipeline produced.
        """
        from astrbot.api.provider import ProviderRequest

        assert self.loop is not None and self.plugin is not None
        event = StubMessageEvent(text=text, session=session, message_id=message_id)
        self.loop.call(self.handlers["on_message_observed"](self.plugin, event))
        request = ProviderRequest(prompt=text)
        self.loop.call(self.handlers["on_llm_request"](self.plugin, event, request))
        injected = "".join(
            getattr(part, "text", "") for part in getattr(request, "extra_user_content_parts", [])
        )
        reply = reply_text_for(text)
        for part in getattr(request, "extra_user_content_parts", []):
            # mark_as_temp() must be set by the plugin: hidden context may never be
            # persisted into conversation history.
            assert getattr(part, "_no_save", False), "injected context part is not temporary"
        event._result_text = reply
        self.platform.deliver(session, reply, kind="reply", at=self.clock.now())
        self.loop.call(self.handlers["on_after_message_sent"](self.plugin, event))
        del injected
        return reply


# ------------------------------------------------------------------ the story driver


@dataclass
class Window:
    """A scripted stretch of the user's life, and what it permits."""

    name: str
    session: str
    start: datetime
    end: datetime
    proactive_allowed: bool
    note: str = ""


class Story:
    """The user's life, driven against one live Runtime + host pair."""

    def __init__(
        self,
        *,
        base_dir: Path,
        clock: SimClock,
        faults: Faults,
    ) -> None:
        """Create the story (nothing is started until :meth:`start`)."""
        self.base_dir = base_dir
        self.clock = clock
        self.faults = faults
        self.recorder = Recorder()
        self.platform = Platform(self.recorder)
        self.llm = HostLLM(clock)
        self.server: RuntimeServer | None = None
        self.host: PluginHost | None = None
        self.windows: list[Window] = []
        self.topic_ban_at: datetime | None = None
        self.observations: list[str] = []
        self.message_seq = 0
        self.last_host_stop: dict[str, Any] = {}
        self.step_timings: list[tuple[float, float, str]] = []

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Boot the Runtime, then the host, and open the scripted sessions."""
        directory = self.base_dir / "scenario"
        directory.mkdir(parents=True, exist_ok=True)
        self.server = RuntimeServer(
            config=build_runtime_config(directory),
            clock=self.clock,
            name="user-sim",
        )
        self.server.start()
        for session in (SESSION_A, SESSION_B):
            self.platform.register(session)
        self.start_host()

    def start_host(self) -> None:
        """Start (or restart) the shipped plugin adapter against the live Runtime."""
        assert self.server is not None
        self.host = PluginHost(
            base_url=self.server.base_url,
            platform=self.platform,
            llm=self.llm,
            clock=self.clock,
            faults=self.faults,
            adapter_id="blackbox-user-adapter",
        )
        self.host.start()

    def stop_host(self) -> dict[str, Any]:
        """Stop the adapter only, keeping the Runtime and database running."""
        if self.host is not None:
            self.last_host_stop = self.host.stop()
            self.host = None
        return self.last_host_stop

    def stop(self) -> dict[str, Any]:
        """Stop the host and the Runtime, joining every thread they own.

        Returns:
            The adapter stop report (``loop_alive``, ``tasks_pending``).
        """
        report = self.stop_host()
        if self.server is not None:
            self.server.stop()
            self.server = None
        return report

    # -- the user ------------------------------------------------------------------

    def say(self, *, session: str, text: str) -> str:
        """Have the user say something and receive the host's reply.

        Args:
            session: Session the user is typing in.
            text: What the user types.

        Returns:
            The reply text delivered to that session.
        """
        assert self.host is not None and self.server is not None
        self.message_seq += 1
        self.recorder.user(at=self.clock.now(), session=session, text=text)
        reply = self.host.user_turn(
            text=text, session=session, message_id=f"msg-{self.message_seq:04d}"
        )
        self._drain_outbox()
        return reply

    def _drain_outbox(self, timeout: float = 3.0) -> dict[str, Any]:
        """Let the adapter finish the delivery work it is holding.

        Called after every user turn and every clock step so a suspension happens
        with a settled queue, exactly as a person would experience it. An attempt
        that has already been *sent* is not work in progress - it is waiting for
        the user - so only undelivered queue rows count here.

        Args:
            timeout: Upper bound on the wait.

        Returns:
            The last observed queue state, for the diagnostics of a slow step.
        """
        assert self.server is not None
        deadline = time.monotonic() + timeout
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            health = self.server.health()
            outbox = health.get("outbox") or {}
            busy = int(outbox.get("pending") or 0) + int(outbox.get("leased") or 0)
            state = {
                "in_flight_attempts": int(health.get("in_flight_attempts") or 0),
                "outbox": outbox,
            }
            if not busy:
                return state | {"settled": True}
            time.sleep(0.05)
        state["settled"] = False
        adapter = getattr(getattr(self.host, "plugin", None), "_outbox", None)
        if adapter is not None:
            state["adapter"] = {key: value for key, value in vars(adapter.stats).items() if value}
        return state

    # -- the world clock -----------------------------------------------------------

    def step(self, *, label: str = "", size: timedelta = SIM_STEP) -> str:
        """Move the simulated clock by one step and let the Runtime live it.

        The Scheduler's own wake-up is what integrates the elapsed interval, so a
        proactive decision is always the Runtime's, never the script's. When the
        Scheduler's gate is closed the Runtime still has to see time pass, so the
        public tick endpoint is used instead - and no decision is taken, which is
        exactly what a closed gate means.

        Args:
            label: Free-text note for the diagnostics.
            size: How far to move the clock.

        Returns:
            ``"round"``, ``"gate_closed"`` or ``"timeout"``.
        """
        assert self.server is not None
        scheduler = self.server.scheduler
        before = int(scheduler.status().get("rounds") or 0) if scheduler is not None else 0
        self.clock.advance(size)
        started = time.monotonic()
        outcome = "timeout"
        deadline = started + 3.0
        while time.monotonic() < deadline:
            status = scheduler.status() if scheduler is not None else {}
            if int(status.get("rounds") or 0) > before:
                outcome = "round"
                break
            reason = status.get("dispatch_reason")
            if status.get("dispatch_allowed") is False and reason in {
                "boundary_blocks_proactive",
                "quiet_hours",
                "no_runtime",
            }:
                outcome = "gate_closed"
                break
            time.sleep(0.02)
        if outcome != "round":
            self.server.tick()
        wait_started = time.monotonic()
        drain = self._drain_outbox()
        self.step_timings.append(
            (round(time.monotonic() - started, 3), round(time.monotonic() - wait_started, 3), outcome)
        )
        if not V.quiet and label:
            V.note(
                f"[{OPS_LABEL}] step {label}: {outcome} in "
                f"{time.monotonic() - started:.2f}s (drain {time.monotonic() - wait_started:.2f}s) "
                f"@ {self.clock.now().isoformat()}"
            )
        if not drain.get("settled") and self.host is not None:
            # While the host is deliberately down, an undelivered row is the point
            # of the phase rather than a symptom, so it is not reported as one.
            V.ops(f"queue never settled during step {label}", drain)
        return outcome

    def advance(self, delta: timedelta, *, label: str = "") -> list[str]:
        """Advance the simulated clock by ``delta`` in fixed steps."""
        outcomes: list[str] = []
        remaining = delta
        while remaining > timedelta(0):
            chunk = min(SIM_STEP, remaining)
            outcomes.append(self.step(label=label, size=chunk))
            remaining -= chunk
        return outcomes

    # -- scripted windows ----------------------------------------------------------

    def open_window(self, window: Window) -> Window:
        """Register a scripted stretch of the user's life."""
        self.windows.append(window)
        return window

    def bot_messages(self, start: datetime, end: datetime, session: str = "") -> list[Turn]:
        """Return bot messages inside a simulated window."""
        return [
            turn
            for turn in self.recorder.between(start, end, session)
            if turn.who == "bot"
        ]

    def proactive_messages(self, start: datetime, end: datetime, session: str = "") -> list[Turn]:
        """Return proactive messages inside a simulated window."""
        return [turn for turn in self.bot_messages(start, end, session) if turn.kind == "proactive"]

    # -- operator-observable probes (diagnostics only) ------------------------------

    def ops_snapshot(self, label: str) -> None:
        """Print the operator-visible state, clearly labelled as such."""
        assert self.server is not None
        payload = {
            "unfinished": [
                {"title": item.get("title"), "status": item.get("status")}
                for item in (self.server.get("/unfinished").field("matters", default=[]) or [])
            ],
            "boundaries": [
                {
                    "type": item.get("type"),
                    "scope": item.get("scope"),
                    "allow_proactive": item.get("allow_proactive"),
                    "expires_at": item.get("expires_at"),
                    "revoked_at": item.get("revoked_at"),
                }
                for item in (self.server.get("/boundaries").field("boundaries", default=[]) or [])
            ],
            "candidates": [
                {"type": item.get("type"), "intent": item.get("intent"), "status": item.get("status")}
                for item in (self.server.get("/candidates").field("candidates", default=[]) or [])
            ],
            "outbox": self.server.get("/outbox").field("stats", default={}),
        }
        V.ops(label, payload)

    def ops_note(self, title: str, text: str) -> None:
        """Record a product observation proven through the operator surface."""
        self.observations.append(f"[{OPS_LABEL}] {title}: {text}")
        V.ops(title, text)

    def ops_matters(self, label: str) -> list[dict[str, Any]]:
        """Return ``[{title, status}]`` for the open obligations, as the operator sees them."""
        assert self.server is not None
        matters = [
            {"title": item.get("title"), "status": item.get("status")}
            for item in (self.server.get("/unfinished").field("matters", default=[]) or [])
        ]
        V.ops(label, matters)
        return matters

    def ops_matter_routing(self) -> None:
        """Show which chat each open obligation came from, and where the queue is addressed.

        Read entirely through the operator surface and labelled as such: it is the
        evidence that turns "the second chat never heard anything" into an
        actionable finding.
        """
        assert self.server is not None
        matters = self.server.get("/unfinished").field("matters", default=[]) or []
        rows = self.server.get("/outbox").field("items", default=[]) or []
        origins: list[dict[str, Any]] = []
        for matter in matters[:4]:
            sources = matter.get("source_event_ids") or []
            conversation = ""
            if sources:
                event = self.server.get(f"/events/{sources[0]}").field("event", default={}) or {}
                conversation = str(event.get("conversation_id") or "")
            origins.append(
                {
                    "matter": matter.get("title"),
                    "status": matter.get("status"),
                    "said_in": conversation,
                }
            )
        V.ops("which chat each open obligation was formed in", origins)
        V.ops(
            "where the delivery queue addressed its recent rows",
            [
                {
                    "kind": row.get("kind"),
                    "status": row.get("status"),
                    "addressed_to": row.get("conversation_id"),
                }
                for row in rows[:6]
            ],
        )


# ------------------------------------------------------------------ phases

PHASES: list[tuple[str, str]] = [
    ("setup", "PHASE 1 setup"),
    ("greeting", "PHASE 2 greeting and small talk / 打招呼与闲聊"),
    ("timed_matter", "PHASE 3 a dated promise / 说了一件有时限的事"),
    ("closure", "PHASE 4 closing the loop / 回复闭环"),
    ("boundary", "PHASE 5 drawing a boundary / 划定边界"),
    ("resume", "PHASE 6 normal talk resumes / 恢复自然交流"),
    ("silence", "PHASE 7 silence is not rejection / 沉默不等于负面"),
    ("isolation", "PHASE 8 two chats, no crosstalk / 多会话隔离"),
    ("restart", "PHASE 9 a restart that does not disturb / 重启不打扰"),
    ("replay", "PHASE 10 duplicate delivery and replay / 重复投递"),
    ("timeline", "PHASE 11 the whole day, replayed / 一整天时间线回放"),
    ("teardown", "PHASE 12 teardown"),
]
PHASE_IDS = [identifier for identifier, _title in PHASES]


def phase_setup(story: Story, ctx: "Context") -> None:
    """Verify the run can start: dependencies, environment, artifact root, no secrets."""
    V.phase("setup", "PHASE 1 setup")
    assert story.server is not None and story.host is not None
    V.check(
        "the required dependencies are importable (uvicorn, aiohttp, fastapi)",
        not IMPORT_ERROR,
        IMPORT_ERROR or "imports ok",
    )
    V.check(
        "every provider-shaped environment variable was scrubbed before startup",
        all(not PROVIDER_ENV_PATTERN.search(name) for name in os.environ),
        f"removed={_short(ctx.scrubbed_env) or 'none'}",
    )
    V.check(
        "no API credential is present in the environment at all",
        not any(PROVIDER_ENV_PATTERN.search(name) for name in os.environ),
        _short([name for name in os.environ if PROVIDER_ENV_PATTERN.search(name)]),
    )
    V.check(
        "the Runtime runs with no semantic provider and the adapter with no token",
        story.server.config.semantic.provider == "disabled"
        and not story.host.plugin._settings.token,
        _short(
            {
                "semantic_provider": story.server.config.semantic.provider,
                "adapter_token_configured": bool(story.host.plugin._settings.token),
            }
        ),
    )
    V.check(
        "the sidecar answers on a loopback address only",
        story.server.base_url.startswith(f"http://{HOST}:"),
        story.server.base_url,
    )
    V.check(
        "the database is a real file in WAL mode with a JSONL mirror",
        (story.base_dir / "scenario" / "runtime.sqlite3").exists()
        and bool(story.server.config.storage.wal)
        and bool(story.server.config.storage.mirror_raw_events),
        f"db={story.base_dir / 'scenario' / 'runtime.sqlite3'}",
    )
    V.check(
        "the Runtime keeps its database inside the artifact root this run was given",
        (story.base_dir / "scenario" / "runtime.sqlite3").exists()
        and str(story.base_dir) in str(story.server.config.storage.database_path),
        _short(
            {
                "base_dir": str(story.base_dir),
                "database_path": story.server.config.storage.database_path,
            }
        ),
    )
    V.check(
        "the shipped plugin registered its three real AstrBot hooks",
        set(story.host.handlers)
        == {"on_message_observed", "on_llm_request", "on_after_message_sent"},
        _short(sorted(story.host.handlers)),
    )
    V.check(
        "the adapter talks to the live Runtime over its own HTTP transport",
        story.host.recording is not None,
        _short(type(story.host.recording).__name__),
    )
    V.check(
        "host, adapter and Runtime share one clock (the simulated one)",
        ctx.clock_bindings > 0,
        f"rebound module clock bindings={ctx.clock_bindings}",
    )
    V.note(
        f"simulated clock starts at {story.clock.now().isoformat()}; wall clock is "
        f"{_real_now_iso()}"
    )


def phase_greeting(story: Story, ctx: "Context") -> None:
    """打招呼与闲聊: the user says hello and chats; the bot answers in that session."""
    V.phase("greeting", "PHASE 2 greeting and small talk / 打招呼与闲聊")
    session = SESSION_A
    before = len(story.recorder.turns)
    reply = story.say(session=session, text=TEXT_GREETING)
    story.open_window(
        Window(
            name="the private chat is open and the user is not asking for silence",
            session=session,
            start=story.clock.now(),
            end=story.clock.now() + 30 * DAY,
            proactive_allowed=True,
            note="an open private chat; the boundary phase carves the silence window out of it",
        )
    )
    turns = story.recorder.turns[before:]
    bot = [turn for turn in turns if turn.who == "bot"]
    V.check(
        "the user gets an answer at all",
        len(bot) == 1,
        f"expected=1 actual={len(bot)} transcript={[_short(turn.text, 60) for turn in turns]}",
    )
    V.check(
        "the answer is non-empty",
        bool(reply.strip()),
        f"reply={_short(reply, 80)}",
    )
    V.check(
        "the answer arrives in the session the user wrote in",
        bool(bot) and bot[0].session == session,
        f"expected={session} actual={bot[0].session if bot else '(none)'}",
    )
    V.check(
        "the answer is the host main LLM's own reply to the user's words",
        bool(bot) and normalize_message(bot[0].text) == normalize_message(reply),
        _short({"delivered": bot[0].text if bot else "", "generated": reply}),
    )
    V.check(
        "the user's own words never appear as a bot message",
        all(normalize_message(turn.text) != normalize_message(TEXT_GREETING) for turn in bot),
        _short([turn.text for turn in bot]),
    )


def phase_timed_matter(story: Story, ctx: "Context") -> None:
    """说了一件有时限的事: the user leaves a dated promise, goes quiet, and the bot speaks first."""
    V.phase("timed_matter", "PHASE 3 a dated promise / 说了一件有时限的事")
    session = SESSION_A
    story.advance(timedelta(minutes=30), label="chat gap")
    story.say(session=session, text=TEXT_APPOINTMENT)
    promised_at = story.clock.now()
    # The promise is "tomorrow afternoon"; whichever time of day the run starts
    # at, the Runtime derives a due moment at most 42 simulated hours away.
    deadline = promised_at + timedelta(hours=42)
    window = story.open_window(
        Window(
            name="after the promised appointment",
            session=session,
            start=promised_at + HOUR,
            end=deadline + 6 * HOUR,
            proactive_allowed=True,
            note="the user promised to report back and then went quiet",
        )
    )
    before_users = len(story.recorder.user_turns(session))
    story.advance(window.end - story.clock.now(), label="quiet after the promise")

    proactives = story.proactive_messages(window.start, window.end, session)
    V.check(
        "the bot speaks first, with no user message to trigger it",
        len(proactives) >= 1,
        f"expected>=1 actual={len(proactives)} window={window.start.isoformat()}..{window.end.isoformat()} "
        f"user_messages_in_window="
        f"{[turn.text for turn in story.recorder.user_turns(session)[before_users:] if turn.at >= window.start]}",
    )
    if proactives:
        first = proactives[0]
        V.check(
            "the unprompted message lands in the same session the promise was made in",
            first.session == session,
            f"expected={session} actual={first.session}",
        )
        calls = [
            call
            for call in story.llm.proactive_calls
            if call.session == session and normalize_message(call.text) == normalize_message(first.text)
        ]
        V.check(
            "it is the host main LLM's rendering of the prompt the Runtime composed",
            bool(calls),
            _short({"delivered": first.text, "prompts": len(story.llm.proactive_calls)}),
        )
        V.check(
            "the Runtime's own prompt is what the user ends up seeing (no host invention)",
            bool(calls) and "- 想做的事：" in calls[0].prompt,
            _short({"delivered": first.text, "intent_line": [line for line in (calls[0].prompt.splitlines() if calls else []) if "想做的事" in line]}),
        )
        V.check(
            "the unprompted message is about the thing the user promised to report",
            FORBIDDEN_TOPICS[0] in first.text,
            f"delivered={_short(first.text, 90)} (expected the interview topic, which is not yet forbidden)",
        )
        story.ops_note(
            "proactive attempt", f"delivered at {first.at.isoformat()} after {len(proactives)} attempt(s)"
        )
    story.ops_snapshot("state after the promise came due")


def phase_closure(story: Story, ctx: "Context") -> None:
    """回复闭环: the user answers the unprompted message; the topic is then closed."""
    V.phase("closure", "PHASE 4 closing the loop / 回复闭环")
    session = SESSION_A
    before_report = story.ops_matters("obligations before the user reports the result")
    reply = story.say(session=session, text=TEXT_RESULT)
    after_report = story.ops_matters("obligations after the user reports the result")
    answered_at = story.clock.now()
    opened = [item for item in after_report if item not in before_report]
    if opened:
        story.ops_note(
            "the result report re-opened an obligation",
            "the message that reported the result also created a new open matter "
            f"{_short(opened)}; nothing in the user's words asks the character to wait for "
            "that result again, so any later question about it comes from this phantom matter",
        )
    V.check(
        "the user's answer gets a reply in the same session",
        bool(reply.strip()),
        _short({"reply": reply, "session": session}),
    )
    window = story.open_window(
        Window(
            name="two days after the user reported the result",
            session=session,
            start=answered_at,
            end=answered_at + 2 * DAY,
            proactive_allowed=True,
            note="the matter is settled; the bot may talk, but not about the interview again",
        )
    )
    story.advance(window.end - story.clock.now(), label="two days after the result")

    bot = story.bot_messages(window.start, window.end, session)
    offenders = [
        turn
        for turn in bot
        if turn.kind == "proactive" and any(topic in turn.text for topic in FORBIDDEN_TOPICS)
    ]
    V.check(
        "the bot never asks about that topic again once the user has reported the result",
        not offenders,
        "offending opportunistic message(s): "
        + _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in offenders])
        if offenders
        else f"0 of {len(bot)} bot message(s) in the window mention it",
    )
    V.check(
        "the answer to the result does not ask for the result again",
        "结果" not in reply,
        _short({"reply": reply}),
    )
    story.ops_note(
        "matters after the result was reported",
        _short(
            [
                {"title": item.get("title"), "status": item.get("status")}
                for item in (story.server.get("/unfinished").field("matters", default=[]) or [])
            ]
        ),
    )


def phase_boundary(story: Story, ctx: "Context") -> None:
    """划定边界: the user forbids the topic and asks not to be contacted."""
    V.phase("boundary", "PHASE 5 drawing a boundary / 划定边界")
    session = SESSION_A
    story.say(session=session, text=TEXT_TOPIC_BAN)
    story.ops_note(
        "topic-only instruction",
        "no boundary was recorded for '%s' on its own (the operator surface shows an empty "
        "boundary list), so the topic restriction is only enforced through what the bot is "
        "willing to say" % TEXT_TOPIC_BAN,
    )
    story.say(session=session, text=TEXT_CONTACT_BAN)
    banned_at = story.clock.now()
    story.topic_ban_at = banned_at
    window = story.open_window(
        Window(
            name="boundary window (three days)",
            session=session,
            start=banned_at,
            end=banned_at + 3 * DAY,
            proactive_allowed=False,
            note="the user asked not to be contacted proactively",
        )
    )
    story.advance(window.end - story.clock.now(), label="three days under the boundary")

    all_bot = story.bot_messages(window.start, window.end, session)
    proactives = [turn for turn in all_bot if turn.kind == "proactive"]
    answers = [
        turn for turn in all_bot if turn.kind == "reply" and _has_user_turn_before(story, turn)
    ]
    unsolicited = [turn for turn in all_bot if turn not in answers]
    V.check(
        "for the whole boundary window the user receives zero unprompted messages",
        not proactives,
        f"expected=0 actual={len(proactives)} "
        + _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in proactives]),
    )
    V.check(
        "the user hears nothing they did not ask for while they are asking for quiet",
        not unsolicited,
        f"expected=0 actual={len(unsolicited)} "
        + _short([{"at": turn.at.isoformat(), "kind": turn.kind, "text": turn.text} for turn in unsolicited]),
    )
    offenders = [
        turn
        for turn in all_bot
        if any(topic in turn.text for topic in FORBIDDEN_TOPICS) and not _is_user_echo(turn, story)
    ]
    V.check(
        "nothing the bot says in the boundary window introduces the forbidden topic",
        not offenders,
        _short([{"at": turn.at.isoformat(), "kind": turn.kind, "text": turn.text} for turn in offenders]),
    )
    V.check(
        "the boundary is a real window, not a permanent mute of the process",
        story.server.health().get("allow_proactive") is False,
        _short({"allow_proactive": story.server.health().get("allow_proactive")}),
    )


def phase_resume(story: Story, ctx: "Context") -> None:
    """恢复自然交流: the user lifts the mute and talks about something else."""
    V.phase("resume", "PHASE 6 normal talk resumes / 恢复自然交流")
    session = SESSION_A
    story.say(session=session, text=TEXT_REVOKE)
    V.check(
        "a user message is still answered while the mute is in force",
        bool(story.recorder.bot_turns(session, kind="reply")),
        _short([turn.text for turn in story.recorder.bot_turns(session, kind="reply")[-1:]]),
    )
    reply = story.say(session=session, text=TEXT_NEW_TOPIC)
    V.check(
        "the new topic gets a normal reply",
        bool(reply.strip()) and normalize_message(reply) == normalize_message(reply_text_for(TEXT_NEW_TOPIC)),
        _short({"reply": reply}),
    )
    new_topic_at = story.clock.now()
    window = story.open_window(
        Window(
            name="after the mute was lifted and a new promise was made",
            session=session,
            start=new_topic_at,
            end=new_topic_at + 2 * DAY,
            proactive_allowed=True,
            note="the user lifted the boundary and gave a fresh, dated reason to talk",
        )
    )
    story.advance(window.end - story.clock.now(), label="two days after the mute was lifted")

    proactives = story.proactive_messages(window.start, window.end, session)
    V.check(
        "an unprompted message is allowed again once the user lifts the mute",
        len(proactives) >= 1,
        f"expected>=1 actual={len(proactives)} window={window.start.isoformat()}..{window.end.isoformat()}",
    )
    offenders = [
        turn for turn in proactives if any(topic in turn.text for topic in FORBIDDEN_TOPICS)
    ]
    V.check(
        "the new unprompted message is not about the forbidden topic",
        not offenders,
        _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in offenders])
        or f"topics: {_short([turn.text for turn in proactives], 160)}",
    )
    story.ops_snapshot("state after normal talk resumed")


def phase_silence(story: Story, ctx: "Context") -> None:
    """沉默不等于负面: an unanswered message must not turn into pressure or guilt."""
    V.phase("silence", "PHASE 7 silence is not rejection / 沉默不等于负面")
    session = SESSION_A
    window = story.open_window(
        Window(
            name="the user does not answer",
            session=session,
            start=story.clock.now(),
            end=story.clock.now() + 30 * HOUR,
            proactive_allowed=True,
            note="the bot may speak again, but only as fast as its own cap allows",
        )
    )
    story.advance(window.end - story.clock.now(), label="the user stays silent")

    proactives = story.proactive_messages(window.start, window.end, session)
    cap = story.server.config.drive.max_contacts_per_day
    worst = 0
    worst_at: datetime | None = None
    for turn in proactives:
        inside = [
            other
            for other in proactives
            if timedelta(0) <= (other.at - turn.at) <= DAY
        ]
        if len(inside) > worst:
            worst, worst_at = len(inside), turn.at
    V.check(
        "no 24-hour stretch contains more unprompted messages than the daily cap allows",
        worst <= cap,
        f"cap={cap} worst_24h={worst}"
        + (f" starting {worst_at.isoformat()}" if worst_at else "")
        + " "
        + _short([turn.at.isoformat() for turn in proactives]),
    )
    cooldown = timedelta(seconds=story.server.config.drive.cooldown_seconds)
    gaps = [
        (later.at - earlier.at)
        for earlier, later in zip(proactives, proactives[1:])
    ]
    too_close = [gap for gap in gaps if gap < cooldown]
    V.check(
        "two unprompted messages are never closer together than the configured cooldown",
        not too_close,
        f"cooldown={cooldown} gaps={[str(gap) for gap in gaps]}",
    )
    guilty = [
        turn
        for turn in story.bot_messages(window.start, window.end, session)
        if scan_guilt(turn.text)
    ]
    V.check(
        "the bot never complains that the user did not answer",
        not guilty,
        _short([{"text": turn.text, "matched": scan_guilt(turn.text)} for turn in guilty])
        or "no accusatory phrasing in this configuration; the table stays as a guard",
    )
    story.ops_snapshot("state after being left on read")


def phase_isolation(story: Story, ctx: "Context") -> None:
    """多会话隔离: two chats must never leak into each other."""
    V.phase("isolation", "PHASE 8 two chats, no crosstalk / 多会话隔离")
    session_b = SESSION_B
    story.say(session=session_b, text=TEXT_SESSION_B)
    b_turn_at = story.clock.now()
    a_quiet_start = story.clock.now()
    window_b = story.open_window(
        Window(
            name="the second chat's own dated promise",
            session=session_b,
            start=b_turn_at,
            end=b_turn_at + 2 * DAY,
            proactive_allowed=True,
            note="a promise made in the group chat",
        )
    )
    # The same promise, still unanswered. The design lets an unfinished matter live
    # for days (``unfinished.default_expiry_hours`` is 168 h) and the character may
    # gently follow up while the daily cap and the cooldown still hold, so the
    # reminders that arrive after the first two days are not messages "out of
    # nowhere" - they belong to this window, which exists so the timeline audit can
    # tell them apart from speaking during silence or long after the matter lapsed.
    story.open_window(
        Window(
            name="the second chat's promise is still unanswered",
            session=session_b,
            start=b_turn_at + 2 * DAY,
            end=b_turn_at + 3 * DAY,
            proactive_allowed=True,
            note="the same open promise, one day later",
        )
    )
    # The first chat says something unrelated while the second chat's promise is
    # pending: whatever happens next must respect the session it belongs to.
    story.advance(6 * HOUR, label="both chats quiet")
    story.say(session=SESSION_A, text=TEXT_SMALL_TALK)
    a_turn_at = story.clock.now()
    story.advance(window_b.end - story.clock.now(), label="second chat's promise comes due")

    b_proactives = story.proactive_messages(window_b.start, window_b.end, session_b)
    if not b_proactives:
        story.ops_matter_routing()
        story.ops_snapshot("the second chat never heard from the bot")
        story.ops_note(
            "how to see the same obligation gone astray on its own",
            "run `python scripts/blackbox_user_simulation.py --base-dir <dir> --only setup,isolation`: "
            "with only this phase in play the due obligation formed in the group chat is delivered "
            "into the private chat, because the follow-up candidate carries 'unfinished:<id>' as its "
            "only source and that is not an event id, so the conversation falls back to whichever "
            "chat wrote last",
        )
    V.check(
        "the second chat gets its own unprompted message",
        len(b_proactives) >= 1,
        f"expected>=1 actual={len(b_proactives)} window={window_b.start.isoformat()}.."
        f"{window_b.end.isoformat()}",
    )
    a_messages = story.recorder.between(a_quiet_start, story.clock.now(), SESSION_A)
    a_proactives = [turn for turn in a_messages if turn.kind == "proactive"]
    V.check(
        "what was said in the second chat never produces a message in the first",
        not any("体检" in turn.text for turn in a_proactives),
        _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in a_proactives]),
    )
    texts_a = {normalize_message(turn.text) for turn in story.recorder.bot_turns(SESSION_A)}
    texts_b = {normalize_message(turn.text) for turn in story.recorder.bot_turns(SESSION_B)}
    shared = texts_a & texts_b
    V.check(
        "the same proactive wording is never delivered into both chats",
        not shared,
        _short(sorted(shared)) or f"a={len(texts_a)} b={len(texts_b)} distinct",
    )
    leaked_topic = [
        {"at": turn.at.isoformat(), "text": turn.text}
        for turn in a_proactives
        if any(word in turn.text for word in ("体检", "检查"))
    ]
    V.check(
        "a topic the user only ever mentioned in the second chat never appears in the first",
        not leaked_topic,
        _short(leaked_topic)
        or f"{len(a_proactives)} unprompted message(s) in the first chat, none about the second chat",
    )
    V.check(
        "the user's message in the first chat is answered in the first chat only",
        any(
            turn.kind == "reply" and turn.at >= a_turn_at
            for turn in story.recorder.bot_turns(SESSION_A)
        )
        and all(
            _has_user_turn_before(story, turn)
            for turn in story.recorder.bot_turns(SESSION_B, kind="reply")
        ),
        _short(
            [
                {"session": turn.session, "kind": turn.kind, "text": turn.text}
                for turn in story.recorder.bot_turns()
                if a_turn_at <= turn.at <= a_turn_at + timedelta(minutes=30)
            ]
        ),
    )
    default_traffic = [
        turn for turn in story.recorder.turns if turn.session == SESSION_DEFAULT
    ]
    V.check(
        "no user-visible message is ever addressed to the process-default conversation",
        not default_traffic,
        _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in default_traffic]),
    )
    story.ops_snapshot("state with two live chats")


def phase_restart(story: Story, ctx: "Context") -> None:
    """重启不打扰: stop host and Runtime, keep the database, restart both."""
    V.phase("restart", "PHASE 9 a restart that does not disturb / 重启不打扰")
    assert story.server is not None and story.host is not None
    already_delivered = [
        normalize_message(turn.text) for turn in story.recorder.bot_turns() if turn.delivered
    ]
    db_path = story.base_dir / "scenario" / "runtime.sqlite3"
    # The user goes quiet with a fresh dated reason, then the host side is taken
    # down before it can deliver anything: whatever the Runtime decides now is
    # pending across the restart.
    story.say(session=SESSION_A, text="我明天上午还有个复诊，结束了跟你说。")
    stop_report = story.stop_host()
    V.check(
        "the host side is really down before the restart test begins",
        story.host is None
        and stop_report.get("loop_alive") is False
        and int(stop_report.get("tasks_pending") or 0) == 0,
        _short(stop_report),
    )
    decided_at = story.clock.now()
    pending_before = 0
    for _ in range(16):
        story.step(label="deciding while the host is down")
        outbox = story.server.health().get("outbox") or {}
        pending_before = int(outbox.get("pending") or 0) + int(outbox.get("leased") or 0)
        if pending_before:
            break
    V.check(
        "the Runtime decided to speak while the host was down, leaving a message pending",
        pending_before >= 1,
        f"pending_rows={pending_before} at {story.clock.now().isoformat()}",
    )
    story.server.stop()
    story.server = None
    V.check(
        "the Runtime process is stopped and only the database survives",
        ctx.runtime_stopped(story) and db_path.exists(),
        _short({"db": str(db_path), "exists": db_path.exists()}),
    )
    story.start()
    V.check(
        "the Runtime and the host both come back on the same database",
        not ctx.runtime_stopped(story)
        and ctx.host_running(story)
        and getattr(story.host.plugin, "_queue", None) is not None
        and getattr(story.host.plugin, "_outbox", None) is not None,
        _short(
            {
                "base_url": story.server.base_url,
                "db": str(db_path),
                "adapter_queue": getattr(story.host.plugin, "_queue", None) is not None,
                "adapter_outbox": getattr(story.host.plugin, "_outbox", None) is not None,
            }
        ),
    )
    window = story.open_window(
        Window(
            name="after the restart",
            session=SESSION_A,
            start=decided_at,
            end=story.clock.now() + 2 * DAY,
            proactive_allowed=True,
            note="a message decided before the restart must still arrive, once",
        )
    )
    story.advance(12 * HOUR, label="after the restart")

    delivered_after = [
        turn for turn in story.recorder.bot_turns(SESSION_A) if turn.at >= decided_at and turn.delivered
    ]
    duplicates = [
        turn
        for turn in delivered_after
        if already_delivered.count(normalize_message(turn.text)) >= 1
    ]
    V.check(
        "the restart delivers nothing the user had already seen",
        not duplicates,
        _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in duplicates])
        or f"{len(delivered_after)} new message(s) after the restart",
    )
    texts = [normalize_message(turn.text) for turn in delivered_after]
    repeated = {text: texts.count(text) for text in set(texts) if texts.count(text) > 1}
    V.check(
        "a message that was pending across the restart is delivered exactly once",
        bool(delivered_after) and not repeated,
        _short({"after_restart": [turn.text for turn in delivered_after], "repeated": repeated}),
    )
    del window
    story.ops_snapshot("state after the restart")


def phase_replay(story: Story, ctx: "Context") -> None:
    """重复投递/重放: the same user message and the same result arrive twice."""
    V.phase("replay", "PHASE 10 duplicate delivery and replay / 重复投递")
    assert story.server is not None and story.host is not None
    story.open_window(
        Window(
            name="the restart window",
            session=SESSION_A,
            start=story.clock.now(),
            end=story.clock.now() + 2 * DAY,
            proactive_allowed=True,
            note="the user is quiet and the bot may follow up on the check-up",
        )
    )
    recording = story.host.recording
    captured = sum(len(item.sent) for item in RecordingTransport.instances)
    V.check(
        "the adapter's own wire traffic was captured for the replay",
        bool(WIRE_HISTORY),
        f"captured={len(recording.sent) if recording else 0} bodies on the live adapter, "
        f"{len(WIRE_HISTORY)} over the whole run "
        f"(instances={len(RecordingTransport.instances)}, total={captured})",
    )
    if not WIRE_HISTORY:
        return

    bot_before = len(story.recorder.bot_turns())
    user_events = [
        body
        for kind, body in WIRE_HISTORY
        if kind == "events"
        and any(record.get("kind") == "user_message" for record in body.get("events") or [])
    ]
    result_bodies = [body for kind, body in WIRE_HISTORY if kind == "result"]
    V.check(
        "the adapter reported at least one action result over the wire",
        bool(result_bodies),
        f"results={len(result_bodies)} user_event_envelopes={len(user_events)}",
    )

    first = http_call(
        story.server.base_url,
        "POST",
        "/v1/events",
        user_events[-1] if user_events else {"events": []},
    )
    V.check(
        "re-delivering the same user message is recognised as a duplicate by the public v1 API",
        first.ok and first.field("duplicates") == 1,
        first.describe() + f" adapter={story.host.adapter_id}",
    )
    counts_after_event = len(story.recorder.bot_turns())
    V.check(
        "the duplicate user message produces no extra reply",
        counts_after_event == bot_before,
        f"before={bot_before} after={counts_after_event}",
    )

    if result_bodies:
        action_id = str(result_bodies[-1].get("action_id") or "")
        replay = http_call(
            story.server.base_url,
            "POST",
            f"/v1/outbox/{urllib.parse.quote(action_id, safe='')}/result",
            result_bodies[-1],
        )
        V.check(
            "re-reporting the same action result is accepted without changing anything",
            replay.status in (200, 202, 404, 409),
            replay.describe(),
        )

    story.advance(12 * HOUR, label="after the replay")
    texts = [normalize_message(turn.text) for turn in story.recorder.bot_turns() if turn.delivered]
    repeated = {text: texts.count(text) for text in set(texts) if texts.count(text) > 1}
    V.check(
        "after the replay the user still sees exactly one copy of every message",
        not repeated,
        _short(repeated) or f"{len(texts)} delivered message(s), all distinct",
    )
    proactives = story.recorder.bot_turns(SESSION_A, kind="proactive")
    V.check(
        "the replayed input does not trigger a second unprompted message",
        len(proactives) == len({normalize_message(turn.text) for turn in proactives}),
        _short([turn.text for turn in proactives]),
    )


def phase_timeline(story: Story, ctx: "Context") -> None:
    """一整天时间线回放: print the whole user-perspective transcript and audit it globally."""
    V.phase("timeline", "PHASE 11 the whole day, replayed / 一整天时间线回放")
    markdown = story.recorder.markdown()
    if not ctx.quiet:
        V.line("")
        V.line("-" * 78)
        V.line("USER-PERSPECTIVE TRANSCRIPT (all sessions, chronological)")
        V.line("-" * 78)
        for turn in story.recorder.turns:
            V.line(f"  {turn.render()}")
        V.line("-" * 78)
    (ctx.base_dir / "transcript.md").write_text(markdown, encoding="utf-8")
    V.note(f"transcript written to {ctx.base_dir / 'transcript.md'} ({len(story.recorder.turns)} lines)")

    bot_turns = [turn for turn in story.recorder.bot_turns() if turn.delivered]
    proactives = [turn for turn in bot_turns if turn.kind == "proactive"]

    # -- the daily contact cap, over the whole run --------------------------------
    cap = story.server.config.drive.max_contacts_per_day
    worst = 0
    worst_start: datetime | None = None
    for turn in proactives:
        inside = [other for other in proactives if timedelta(0) <= (other.at - turn.at) <= DAY]
        if len(inside) > worst:
            worst, worst_start = len(inside), turn.at
    V.check(
        "over the whole run no 24-hour window exceeds the configured daily contact cap",
        worst <= cap,
        f"cap={cap} worst_24h={worst} (starting {worst_start.isoformat() if worst_start else '-'}) "
        f"total_proactive={len(proactives)}",
    )

    # -- duplicates ---------------------------------------------------------------
    texts = [normalize_message(turn.text) for turn in bot_turns]
    repeated = {text: texts.count(text) for text in set(texts) if texts.count(text) > 1}
    V.check(
        "no two user-visible messages are duplicates",
        not repeated,
        _short({"repeated": repeated, "texts": len(texts)}),
    )

    # -- the forbidden topic ------------------------------------------------------
    if story.topic_ban_at is not None:
        banned = [
            turn
            for turn in bot_turns
            if turn.at >= story.topic_ban_at
            and any(topic in turn.text for topic in FORBIDDEN_TOPICS)
            and not _is_user_echo(turn, story)
        ]
        V.check(
            "after the user forbade the topic, the bot never brings it up on its own",
            not banned,
            _short([{"at": turn.at.isoformat(), "kind": turn.kind, "text": turn.text} for turn in banned])
            or f"{len([t for t in bot_turns if t.at >= story.topic_ban_at])} message(s) checked after the ban",
        )
    else:
        V.check("the forbidden-topic window was reached at all", False, "phase boundary did not run")

    # -- leakage ------------------------------------------------------------------
    leaked: list[dict[str, str]] = []
    for turn in bot_turns:
        found = scan_leakage(turn.text)
        if found:
            leaked.append({"at": turn.at.isoformat(), "patterns": ",".join(found), "text": turn.text})
    V.check(
        "no user-visible message leaks hidden context, credentials or internal ids",
        not leaked,
        _short(leaked),
    )

    # -- session isolation ---------------------------------------------------------
    cross: list[dict[str, str]] = []
    a_texts = {normalize_message(turn.text) for turn in story.recorder.bot_turns(SESSION_A)}
    b_texts = {normalize_message(turn.text) for turn in story.recorder.bot_turns(SESSION_B)}
    for shared in a_texts & b_texts:
        cross.append({"shared": shared[:60]})
    default_traffic = [turn for turn in story.recorder.turns if turn.session == SESSION_DEFAULT]
    V.check(
        "no message crosses between the two chats, and none goes to the default conversation",
        not cross and not default_traffic,
        _short({"shared": cross, "default": [turn.text for turn in default_traffic]}),
    )
    wrong_session = [
        turn
        for turn in story.recorder.turns
        if turn.session not in {SESSION_A, SESSION_B, SESSION_DEFAULT}
    ]
    V.check(
        "every message went to a chat the user actually uses",
        not wrong_session,
        _short([{"session": turn.session, "text": turn.text} for turn in wrong_session]),
    )

    # -- every unprompted message is attributable to a scripted window -------------
    orphans: list[dict[str, str]] = []
    for turn in proactives:
        matches = [
            window
            for window in story.windows
            if window.session == turn.session and window.start <= turn.at <= window.end
        ]
        if not matches or not any(window.proactive_allowed for window in matches):
            orphans.append(
                {
                    "at": turn.at.isoformat(),
                    "session": turn.session,
                    "text": turn.text,
                    "windows": ",".join(window.name for window in matches) or "none",
                }
            )
    V.check(
        "every unprompted message falls inside a scripted window where speaking made sense",
        not orphans,
        _short(orphans) or f"{len(proactives)} unprompted message(s) matched a scripted window",
    )
    forbidden_windows = [window for window in story.windows if not window.proactive_allowed]
    intrusions = [
        {"at": turn.at.isoformat(), "text": turn.text}
        for turn in proactives
        for window in forbidden_windows
        if window.session == turn.session and window.start <= turn.at <= window.end
    ]
    V.check(
        "nothing appears out of nowhere in a window where the user asked for silence",
        not intrusions,
        _short(intrusions)
        or f"{len(forbidden_windows)} silence window(s), 0 intrusion(s)",
    )
    never_opened = [
        {"at": turn.at.isoformat(), "session": turn.session, "text": turn.text}
        for turn in proactives
        if not any(
            other.who == "user" and other.session == turn.session and other.at <= turn.at
            for other in story.recorder.turns
        )
    ]
    V.check(
        "the bot never speaks first in a chat the user has not opened yet",
        not never_opened,
        _short(never_opened) or "every chat was opened by the user first",
    )

    # -- the promise of a reply ----------------------------------------------------
    unanswered = [
        turn
        for turn in story.recorder.user_turns()
        if turn.delivered
        and not any(
            other.who == "bot"
            and other.session == turn.session
            and other.kind == "reply"
            and turn.at <= other.at <= turn.at + timedelta(hours=2)
            for other in story.recorder.turns
        )
    ]
    V.check(
        "every message the user sent got an answer",
        not unanswered,
        _short([{"at": turn.at.isoformat(), "text": turn.text} for turn in unanswered])
        or f"{len(story.recorder.user_turns())} user message(s) answered",
    )
    for observation in story.observations:
        V.note(observation)


def phase_teardown(story: Story, ctx: "Context") -> None:
    """teardown: nothing is left running, and nothing was written outside --base-dir."""
    V.phase("teardown", "PHASE 12 teardown")
    host_thread_alive = ctx.host_running(story)
    server = story.server
    server_thread_alive = bool(server and server.thread and server.thread.is_alive())
    tasks_before = len(getattr(story.host.plugin, "_tasks", []) if story.host else [])
    report = story.stop()
    time.sleep(0.4)
    V.check(
        "the adapter's event loop and every task it owned were stopped",
        report.get("loop_alive") is False and int(report.get("tasks_pending") or 0) == 0,
        _short({"host_alive_before_stop": host_thread_alive, "tasks_before_stop": tasks_before, **report}),
    )
    lingering_servers = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("bb-runtime") and thread.is_alive()
    ]
    V.check(
        "the Runtime server thread is joined and nothing is left listening",
        not lingering_servers and story.server is None,
        _short({"server_alive_before_stop": server_thread_alive, "still_alive": lingering_servers}),
    )
    lingering = [
        thread.name
        for thread in threading.enumerate()
        if thread is not threading.current_thread()
        and (thread.name.startswith("bb-") or "companion" in thread.name.lower())
    ]
    V.check(
        "no scheduler, adapter or server thread is still running",
        not lingering,
        _short(lingering) or "no harness threads remain",
    )
    V.check(
        "no bytecode was written next to the sources this run imports",
        bool(sys.dont_write_bytecode) and not ctx.new_pyc_files,
        _short(ctx.new_pyc_files[:5]) or "sys.dont_write_bytecode=True and no new .pyc",
    )
    V.check(
        "no file appeared among those sources, and none of them changed",
        not ctx.repo_files_created and not ctx.repo_sources_changed,
        _short(
            {
                "created": ctx.repo_files_created[:5],
                "changed": ctx.repo_sources_changed[:5],
            }
        )
        or "the imported source trees are untouched",
    )
    if ctx.repo_other_changes:
        V.note(
            "files outside this run's source trees changed while it was in flight "
            "(another process in the same checkout, not written by this run): "
            + _short(ctx.repo_other_changes[:5])
        )
    produced = [
        str(path.relative_to(ctx.base_dir))
        for path in (
            ctx.base_dir / "scenario" / "runtime.sqlite3",
            ctx.base_dir / "scenario" / "raw_events.jsonl",
            ctx.base_dir / "transcript.md",
        )
        if path.exists()
    ]
    V.check(
        "the run's own artifacts are inside --base-dir (report.json and diagnostics.log follow)",
        len(produced) >= 2,
        _short({"present": produced, "base_dir": str(ctx.base_dir)}),
    )
    if story.step_timings:
        slowest = max(story.step_timings)
        total = sum(item[0] for item in story.step_timings)
        V.note(
            f"{len(story.step_timings)} simulated-clock step(s) in {total:.1f}s wall clock "
            f"(slowest {slowest[0]:.2f}s, drain {slowest[1]:.2f}s, outcome {slowest[2]}); "
            f"{len(story.llm.calls)} host main-LLM call(s)"
        )


def _has_user_turn_before(story: Story, bot_turn: Turn, *, within: timedelta = timedelta(hours=2)) -> bool:
    """Whether a bot reply has a user message to be a reply *to*."""
    return any(
        turn.who == "user"
        and turn.session == bot_turn.session
        and timedelta(0) <= (bot_turn.at - turn.at) <= within
        for turn in story.recorder.turns
    )


def _is_user_echo(turn: Turn, story: Story, *, within: timedelta = timedelta(minutes=2)) -> bool:
    """Whether a bot message only repeats a topic the user raised in that turn.

    A reply that quotes the user's own words is not the bot *introducing* a
    topic, so it cannot violate a topic ban the user stated in the very message
    being answered. A proactive message can never be excused.
    """
    if turn.kind != "reply":
        return False
    return any(
        candidate.who == "user"
        and candidate.session == turn.session
        and timedelta(0) <= (turn.at - candidate.at) <= within
        and any(topic in candidate.text for topic in FORBIDDEN_TOPICS)
        for candidate in story.recorder.turns
    )


# ------------------------------------------------------------------ context/harness glue


@dataclass
class Context:
    """Everything a phase needs beyond the story itself."""

    base_dir: Path
    quiet: bool
    scrubbed_env: list[str]
    clock_bindings: int
    repo_snapshot: dict[str, tuple[int, float]]

    def host_running(self, story: Story) -> bool:
        """Whether the adapter's own event loop is alive right now."""
        return bool(story.host is not None and story.host.running)

    def runtime_stopped(self, story: Story) -> bool:
        """Whether the Runtime server thread is gone."""
        server = story.server
        if server is None:
            return True
        return server.thread is None or not server.thread.is_alive()

    @property
    def new_pyc_files(self) -> list[str]:
        """Return bytecode files this run could have written next to its sources."""
        return sorted(
            name
            for name in self._owned_files()
            if name.endswith(".pyc") and name not in self.repo_snapshot
        )

    @property
    def repo_files_created(self) -> list[str]:
        """Return files this run could have created in its own source trees."""
        return sorted(name for name in self._owned_files() if name not in self.repo_snapshot)

    @property
    def repo_sources_changed(self) -> list[str]:
        """Return owned source/config files that changed during the run."""
        watched = (".py", ".pyc", ".json", ".jsonl", ".yaml", ".yml", ".toml")
        current = self._owned_files()
        return sorted(
            name
            for name, stamp in current.items()
            if self.repo_snapshot.get(name) != stamp and name.endswith(watched)
        )

    @property
    def repo_other_changes(self) -> list[str]:
        """Return files outside this run's own trees that changed while it ran.

        Reported as a note, never as a failure: another process working in the
        same checkout (a test run, an editor) is not this simulation's doing, and
        blaming it would make the check useless in a shared working tree.
        """
        current = _tree_files(REPO_ROOT, skip=self.base_dir)
        return sorted(
            name
            for name, stamp in current.items()
            if self.repo_snapshot.get(name) != stamp and not name.startswith(OWNED_TREES)
        )

    def _owned_files(self) -> dict[str, tuple[int, float]]:
        """Snapshot the source trees this run imports."""
        return _tree_files(REPO_ROOT, skip=self.base_dir, only=OWNED_TREES)


#: The files this run actually imports, and therefore the only ones it is allowed
#: to have any effect on. AstrBot/ is upstream and is never imported (the adapter
#: is loaded against the shipped stubs), and anything else in these two
#: repositories belongs to whoever is working in them - blaming this simulation
#: for another process's edits would make the check useless in a shared tree.
OWNED_TREES = (
    "runtime/src/companion_runtime",
    "astrbot_plugin_companion_runtime/companion_runtime",
    "astrbot_plugin_companion_runtime/main.py",
    "astrbot_plugin_companion_runtime/astrbot_executor.py",
    "scripts/blackbox_user_simulation.py",
)


def _tree_files(
    root: Path,
    *,
    skip: Path,
    only: Sequence[str] | None = None,
) -> dict[str, tuple[int, float]]:
    """Return ``path -> (size, mtime)`` for every file under ``root``.

    Args:
        root: Directory to walk.
        skip: A directory whose contents are ignored (the run's own --base-dir).
        only: Optional relative path prefixes to restrict the walk to.

    Returns:
        A snapshot mapping, keyed by path relative to ``root``.
    """
    snapshot: dict[str, tuple[int, float]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        with contextlib.suppress(ValueError):
            if path.is_relative_to(skip):
                continue
        relative = str(path.relative_to(root))
        if only is not None and not relative.startswith(tuple(only)):
            continue
        with contextlib.suppress(OSError):
            stat = path.stat()
            snapshot[relative] = (stat.st_size, round(stat.st_mtime, 3))
    return snapshot


# ------------------------------------------------------------------ entry point


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Black-box user simulation for the companion Runtime sidecar.",
    )
    parser.add_argument("--base-dir", required=False, default="", help="artifact directory")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated phase ids to run (default: all)",
    )
    parser.add_argument("--quiet", action="store_true", help="only print checks and the summary")
    parser.add_argument("--list-phases", action="store_true", help="print the phase ids and exit")
    parser.add_argument(
        "--fault",
        action="append",
        default=[],
        choices=["leak", "duplicate", "topic", "guilt", "cross_session", "default_session"],
        help="inject a harness-side defect to prove that a check bites (never a repo change)",
    )
    return parser.parse_args(argv)


def _write_artifacts(
    base_dir: Path,
    *,
    report: Mapping[str, Any],
    extra: Sequence[str],
    narrative: Sequence[str],
) -> None:
    """Write ``report.json`` and ``diagnostics.log`` under ``base_dir``.

    Args:
        base_dir: Artifact root.
        report: The JSON run report.
        extra: Header lines for the diagnostics log.
        narrative: The run's notes and operator diagnostics, in order.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = list(extra) + ["", "--- run notes and operator diagnostics ---"] + list(narrative)
    lines += ["", "--- runtime and adapter log ---"] + list(LOG_HANDLER.records)
    (base_dir / "diagnostics.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the simulation and return the process exit code."""
    args = parse_args(argv)
    if args.list_phases:
        for identifier, title in PHASES:
            print(f"{identifier:12s} {title}")
        return 0
    if IMPORT_ERROR:
        print(f"cannot start: required dependencies are missing ({IMPORT_ERROR})")
        return 2
    if not args.base_dir:
        print("cannot start: --base-dir is required")
        return 2

    global V
    V = Verifier(quiet=bool(args.quiet))
    V.load_phases(PHASES)
    configure_logging()

    base_dir = Path(args.base_dir).expanduser().resolve()
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    scrubbed = scrub_environment()
    clock = SimClock(datetime.now(timezone.utc).replace(microsecond=0))
    bindings = install_process_clock(clock)
    random.seed(RUNTIME_SEED)

    selected = [item.strip() for item in args.only.split(",") if item.strip()] or PHASE_IDS
    unknown = [item for item in selected if item not in PHASE_IDS]
    if unknown:
        print(f"cannot start: unknown phase id(s) {unknown}; use --list-phases")
        return 2

    ctx = Context(
        base_dir=base_dir,
        quiet=bool(args.quiet),
        scrubbed_env=scrubbed,
        clock_bindings=bindings,
        repo_snapshot=_tree_files(REPO_ROOT, skip=base_dir),
    )
    story = Story(base_dir=base_dir, clock=clock, faults=Faults(args.fault))

    started = False
    fatal = ""
    try:
        story.start()
        started = True
    except Exception as exc:  # noqa: BLE001 - reported as a startup failure
        fatal = f"{type(exc).__name__}: {exc}"
        V.current = V.sections[0]
        V.check("the simulation can start at all", False, fatal)

    if started:
        runners: dict[str, Callable[[Story, Context], None]] = {
            "setup": phase_setup,
            "greeting": phase_greeting,
            "timed_matter": phase_timed_matter,
            "closure": phase_closure,
            "boundary": phase_boundary,
            "resume": phase_resume,
            "silence": phase_silence,
            "isolation": phase_isolation,
            "restart": phase_restart,
            "replay": phase_replay,
            "timeline": phase_timeline,
        }
        try:
            for identifier, _title in PHASES:
                if identifier == "teardown" or identifier not in selected:
                    continue
                # A phase that blows up is recorded as a failed check and the
                # story continues: the user's day does not stop because one
                # assertion crashed, and the remaining phases still report.
                try:
                    runners[identifier](story, ctx)
                except Exception as exc:  # noqa: BLE001 - reported, never re-raised
                    import traceback

                    V.check(
                        f"the story runs to the end of {identifier} without the harness crashing",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                    LOG_HANDLER.records.append(traceback.format_exc())
        finally:
            try:
                phase_teardown(story, ctx)
            except Exception as exc:  # noqa: BLE001 - teardown must still stop everything
                with contextlib.suppress(Exception):
                    story.stop()
                V.check("teardown completes", False, f"{type(exc).__name__}: {exc}")
    else:
        story.stop()
        V.current = V.sections[-1]
        V.check(
            "the run could not start, so nothing was verified",
            False,
            fatal,
        )

    code = V.summary()
    report = V.as_report() | {
        "base_dir": str(base_dir),
        "phases_selected": selected,
        "faults": sorted(set(args.fault)),
        "clock_bindings": bindings,
        "scrubbed_env": scrubbed,
        "observations": list(story.observations),
        "fatal": fatal,
    }
    _write_artifacts(
        base_dir,
        report=report,
        extra=[f"simulated clock origin: {clock.origin.isoformat()}"],
        narrative=[f"{NOTE_MARK} {note}" for section in V.sections for note in section.notes]
        + [line for section in V.sections for line in section.diagnostics],
    )
    print(f"\nartifacts: {base_dir / 'report.json'}, {base_dir / 'diagnostics.log'}, "
          f"{base_dir / 'transcript.md'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
