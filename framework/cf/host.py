"""AstrBot, simulated: the host the Runtime is a sidecar to.

This is the piece that turns the framework from "a harness that pokes the
Runtime" into "a chat you can actually have". It plays the three roles AstrBot
plays in a real deployment, in the same order AstrBot plays them:

1. **the platform** -- a chat app with a per-session address book. Delivering to
   an unknown session fails, exactly like an unmatched AstrBot platform.
2. **the host pipeline** -- observe the message, let the plugin inject the
   Runtime's background block into the LLM request, call the main LLM, deliver
   the reply, report the delivered message back.
3. **the plugin host** -- the *shipped* ``astrbot_plugin_companion_runtime``
   loaded against its own AstrBot stubs, with its real decorators. Nothing about
   the adapter is reimplemented here, so the lease / render / authorize / send
   path being exercised is the path that ships.

Why the real plugin and not a shortcut
--------------------------------------
It would be a dozen lines to call the Runtime's HTTP API directly from the chat
loop. That shortcut would skip context injection, the temporary-part contract,
lease handling and the delivery report -- which is to say, it would test the part
that already works and skip every part that has ever broken. The plugin's own
integration test takes the same route for the same reason.

The only thing faked is the platform and the terminal. The Runtime is real, the
adapter is real, and the main LLM is real (an OpenAI-compatible endpoint) or,
when none is configured, a deterministic stand-in that labels itself as such.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import sys
import threading
import types
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .clock import ControllableClock, install_process_clock
from .logbook import Logbook
from .main_llm import MainLLM

LOGGER = logging.getLogger("cf.host")

#: The AstrBot plugin package, cloned inside the project (see HANDOFF §1).
DEFAULT_PLUGIN_ROOT = "../astrbot_plugin_companion_runtime"

#: Adapter module names, used when re-binding the process clock.
PLUGIN_PACKAGE = "astrbot_plugin_companion_runtime"

#: The default session, in AstrBot's ``platform:MessageType:id`` form.
SESSION_DEFAULT = "webchat:FriendMessage:default"

#: The adapter configuration this host runs the plugin with.
#:
#: This started as a copy of the shipped black-box simulation's configuration
#: (``scripts/blackbox_user_simulation.py``), but the two have since **drifted**: five
#: values differ, and nothing anywhere records why. Two differences are *not* drift and
#: must not be "fixed":
#:
#: * ``runtime_base_url`` is absent here on purpose - the black-box puts it in this same
#:   mapping, while this host injects it at construction time
#:   (:meth:`HostHandle.start`);
#: * ``adapter_id`` differs because it has to.
#:
#: The five real divergences, against the black-box values:
#:
#:     context_timeout_ms     2000     (black-box 500)
#:     context_prefetch       False    (black-box True)
#:     request_timeout_ms     5000     (black-box 2000)
#:     render_timeout_ms      60000    (black-box 10000)
#:     queue_max_backoff_ms   1000     (black-box 500)
#:
#: An earlier comment here claimed the two "drive the plugin identically", which was not
#: true and could not be checked from the code - this host has a hand-driven clock and a
#: mock LLM endpoint, so longer timeouts and no prefetch are plausible *choices*, but a
#: plausible reason is not a recorded one. Treat it as an open question (see
#: ``docs/SIMULATION_INTEGRATION.md`` at the repository root), not as a licence to change
#: one side quietly.
#:
#: ``context_cache_ttl_ms`` is 0 in both, for the reason that was always correct: the world
#: clock moves faster than any TTL, so the cache would serve a stale background block.
PLUGIN_CONFIG: Mapping[str, Any] = {
    "enabled": True,
    "adapter_id": "framework-host",
    "observe_mode": "all",
    "report_assistant_messages": True,
    "inject_enabled": True,
    "context_timeout_ms": 2000,
    "context_cache_ttl_ms": 0,
    "context_prefetch": False,
    "request_timeout_ms": 5000,
    "outbox_enabled": True,
    "outbox_poll_interval_ms": 250,
    "outbox_max_actions_per_poll": 2,
    "outbox_lease_ttl_ms": 900000,
    "outbox_max_concurrency": 1,
    "render_timeout_ms": 60000,
    "send_timeout_ms": 10000,
    "queue_base_backoff_ms": 100,
    "queue_max_backoff_ms": 1000,
}


@dataclass
class Delivered:
    """One message that reached the chat window, whoever produced it."""

    at: datetime
    session: str
    text: str
    kind: str  # "user" | "reply" | "proactive"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {"at": self.at.isoformat(), "session": self.session, "text": self.text, "kind": self.kind}


class Platform:
    """A fake chat app: sessions exist only once they are opened.

    Args:
        on_deliver: Called with every :class:`Delivered` message. The TUI uses it
            to print proactive messages the instant they arrive.
    """

    def __init__(self, on_deliver: Callable[[Delivered], None] | None = None) -> None:
        """Start with an empty address book."""
        self._sessions: set[str] = set()
        self.messages: list[Delivered] = []
        self.on_deliver = on_deliver
        self._lock = threading.RLock()

    def open(self, session: str) -> None:
        """Register a session -- the user opening a chat."""
        with self._lock:
            self._sessions.add(session)

    def resolve(self, session: str) -> bool:
        """Return whether this session exists."""
        with self._lock:
            return session in self._sessions

    def sessions(self) -> list[str]:
        """Return every known session."""
        with self._lock:
            return sorted(self._sessions)

    def deliver(self, session: str, text: str, *, kind: str, at: datetime) -> bool:
        """Deliver a message, returning ``False`` for an unregistered session.

        The failure is deliberate: an unmatched session is the single most common
        real-world reason a proactive message vanishes, and a platform that
        silently accepted everything would hide it.
        """
        with self._lock:
            if session not in self._sessions:
                LOGGER.warning("no such session %r; message dropped", session)
                return False
            message = Delivered(at=at, session=session, text=text, kind=kind)
            self.messages.append(message)
        if self.on_deliver is not None:
            self.on_deliver(message)
        return True


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
        """Return the group id for group sessions, else an empty string.

        The plugin reads this on every observed message, so a stand-in without it
        fails the whole observation path -- silently, because the adapter is
        required to swallow observation failures. The symptom is a chat that
        works perfectly while the Runtime never hears about anything.
        """
        return self._session.rsplit(":", 1)[-1] if "GroupMessage" in self._session else ""

    def get_result(self) -> Any:
        """Return the message AstrBot just sent, as the plugin's hook reads it."""
        if not self._result_text:
            return None
        from astrbot.api.event import MessageEventResult

        return MessageEventResult().message(self._result_text)


class HostLoop:
    """A private asyncio loop for the host, like AstrBot's own runtime."""

    def __init__(self, name: str = "cf-host") -> None:
        """Start the loop on its own thread."""
        self.name = name
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Run the loop until it is asked to stop."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Any, *, timeout: float = 90.0) -> Any:
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


class HostContext:
    """The AstrBot public API the shipped plugin actually calls.

    Exactly three methods. Everything the plugin does to the outside world goes
    through them, which is why substituting this object is enough to move the
    plugin from a real AstrBot onto a simulated one.
    """

    def __init__(
        self,
        *,
        platform: Platform,
        llm: MainLLM,
        clock: ControllableClock,
        logbook: Logbook | None = None,
    ) -> None:
        """Wire the context."""
        self.platform = platform
        self.llm = llm
        self.clock = clock
        self.logbook = logbook

    async def get_current_chat_provider_id(self, umo: str | None = None) -> str:
        """Resolve the session's current chat provider.

        Raises:
            RuntimeError: For a session the platform does not know -- the same
                thing AstrBot does, so the plugin's failure path is real.
        """
        session = str(umo or "")
        if not self.platform.resolve(session):
            raise RuntimeError(f"no chat provider for session {session!r}")
        return f"framework-provider::{session}"

    async def llm_generate(self, *, chat_provider_id: str, prompt: str, **kwargs: Any) -> Any:
        """Generate one completion through the session's provider."""
        del kwargs
        session = str(chat_provider_id).split("::", 1)[-1]
        text = await self.llm.generate(provider_id=chat_provider_id, prompt=prompt, session=session)
        return types.SimpleNamespace(completion_text=text)

    async def send_message(self, session: Any, chain: Any) -> bool:
        """Deliver a proactive message chain; ``False`` mirrors an unmatched session."""
        text = chain if isinstance(chain, str) else "".join(str(part.text) for part in chain.chain)
        return self.platform.deliver(str(session), text, kind="proactive", at=self.clock.now())


class AstrBotHost:
    """The shipped plugin, driven through its real hooks.

    Args:
        runtime_base_url: The Runtime the plugin should talk to.
        platform: The fake chat app.
        llm: The acting layer.
        clock: The process clock, re-bound over the adapter's modules too.
        logbook: Where to record turns.
        plugin_root: The plugin checkout.
        message_ids: Optional id generator; defaults to a counter.

    Raises:
        RuntimeError: When the plugin checkout is missing or cannot be imported.
    """

    def __init__(
        self,
        *,
        runtime_base_url: str,
        platform: Platform,
        llm: MainLLM,
        clock: ControllableClock,
        logbook: Logbook | None = None,
        plugin_root: str | Path = DEFAULT_PLUGIN_ROOT,
        message_ids: Iterable[str] | None = None,
    ) -> None:
        """Store the wiring; call :meth:`start` to load the plugin."""
        self.runtime_base_url = runtime_base_url
        self.platform = platform
        self.llm = llm
        self.clock = clock
        self.logbook = logbook
        self.plugin_root = Path(plugin_root).expanduser().resolve()
        self.loop: HostLoop | None = None
        self.plugin: Any = None
        self.module: Any = None
        self.filters: Any = None
        self.handlers: dict[str, Callable[..., Any]] = {}
        self._ids = iter(message_ids) if message_ids is not None else None
        self._seq = 0
        self.turns = 0
        self._turn_lock = threading.RLock()

    # ------------------------------------------------------------------- life

    def start(self, *, startup_timeout: float = 30.0) -> None:
        """Import the plugin against the stubs and run ``initialize``.

        Raises:
            RuntimeError: When the plugin package cannot be loaded.
        """
        if not (self.plugin_root / "main.py").is_file():
            raise RuntimeError(
                f"shipped plugin not found at {self.plugin_root / 'main.py'}; "
                "pass --plugin-root, and see HANDOFF §1 for where it must be cloned"
            )
        stubs = self.plugin_root / "tests" / "stubs"
        for path in (str(stubs), str(self.plugin_root)):
            if path not in sys.path:
                sys.path.insert(0, path)
        if PLUGIN_PACKAGE not in sys.modules:
            package = types.ModuleType(PLUGIN_PACKAGE)
            package.__path__ = [str(self.plugin_root)]
            sys.modules[PLUGIN_PACKAGE] = package
        try:
            self.module = importlib.import_module(f"{PLUGIN_PACKAGE}.main")
            self.filters = importlib.import_module("astrbot.api.event.filter")
        except Exception as exc:  # noqa: BLE001 - reported as a setup failure
            raise RuntimeError(f"cannot import the plugin from {self.plugin_root}: {type(exc).__name__}: {exc}") from exc

        # The adapter's modules only exist now, so the clock has to be re-bound
        # here as well; otherwise the adapter stamps events with wall time while
        # the Runtime lives on virtual time.
        install_process_clock(self.clock)

        config = dict(PLUGIN_CONFIG)
        config["runtime_base_url"] = self.runtime_base_url
        self.loop = HostLoop("cf-host")
        context = HostContext(platform=self.platform, llm=self.llm, clock=self.clock, logbook=self.logbook)
        self.plugin = self.module.CompanionRuntimePlugin(context=context, config=config)
        self.loop.call(self.plugin.initialize(), timeout=startup_timeout)
        for name in ("on_message_observed", "on_llm_request", "on_after_message_sent"):
            self.handlers[name] = self.filters.handler_by_name(name)
        if self.logbook is not None:
            self.logbook.event(
                "host_started",
                {
                    "plugin_root": str(self.plugin_root),
                    "runtime_base_url": self.runtime_base_url,
                    "sessions": self.platform.sessions(),
                    "handlers": sorted(self.handlers),
                },
                message=f"[host] plugin loaded from {self.plugin_root.name}",
            )

    def stop(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """Terminate the plugin and join its loop, leaving nothing running."""
        loop, plugin = self.loop, self.plugin
        if loop is None:
            return {"loop_alive": False, "tasks_pending": 0}
        with contextlib.suppress(Exception):
            loop.call(plugin.terminate(), timeout=timeout)
        tasks_pending = len(getattr(plugin, "_tasks", []))
        loop.close()
        self.loop = None
        self.plugin = None
        report = {"loop_alive": loop.alive, "tasks_pending": tasks_pending, "turns": self.turns}
        if self.logbook is not None:
            self.logbook.event("host_stopped", report, message=f"[host] stopped after {self.turns} turn(s)")
        return report

    @property
    def running(self) -> bool:
        """Whether the host's event loop thread is alive."""
        return self.loop is not None and self.loop.alive

    # ------------------------------------------------------- the message paths

    def user_turn(
        self,
        text: str,
        *,
        session: str = SESSION_DEFAULT,
        at: datetime | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """Drive one user turn through the real AstrBot hook order.

        The order mirrors AstrBot: observe the message, inject the Runtime's
        context into the LLM request, generate the reply, deliver it, then report
        the delivered message back.

        The generated reply is produced from the prompt **including** the injected
        background block. The shipped black-box simulation discards that block and
        answers from a canned script; keeping it is the entire difference between
        testing that the block is *built* and testing that it is *used*.

        Args:
            text: What the user typed.
            session: Session the message arrived in.
            at: When it happened, on the caller's clock.
            on_delta: Called with each fragment of the reply as it arrives, so a
                caller can show the answer while it is still being written. The
                proactive render path never streams: the character composing a
                message the user has not received yet has no audience.

        Returns:
            The reply the host pipeline produced.

        Raises:
            RuntimeError: When the host has not been started.
        """
        if self.loop is None or self.plugin is None:
            raise RuntimeError("the host is not running; call start() first")
        from astrbot.api.provider import ProviderRequest

        moment = at or self.clock.now()
        event = StubMessageEvent(text=text, session=session, message_id=self._next_id())
        with self._turn_lock:
            self.turns += 1
            self.platform.deliver(session, text, kind="user", at=moment)
            self.loop.call(self.handlers["on_message_observed"](self.plugin, event))

            request = ProviderRequest(prompt=text)
            self.loop.call(self.handlers["on_llm_request"](self.plugin, event, request))
            injected = self._injected_text(request)

            prompt = text if not injected else f"{text}\n\n{injected}"
            # The main LLM is called on the host loop, exactly where the plugin's
            # own render path calls it.
            reply = self.loop.call(
                self.loop_llm_generate(session=session, prompt=prompt, on_delta=on_delta),
                timeout=180.0,
            )

            event._result_text = reply
            self.platform.deliver(session, reply, kind="reply", at=self.clock.now())
            self.loop.call(self.handlers["on_after_message_sent"](self.plugin, event))

        if self.logbook is not None:
            self.logbook.event(
                "user_turn",
                {
                    "virtual_now": moment.isoformat(),
                    "session": session,
                    "text": text,
                    "reply": reply,
                    "injected_chars": len(injected),
                    "injected": injected,
                    "turn": self.turns,
                },
                message=f"[turn {self.turns}] {session} in={len(text)}c ctx={len(injected)}c out={len(reply)}c",
            )
        return reply

    async def loop_llm_generate(
        self, *, session: str, prompt: str, on_delta: Callable[[str], None] | None = None
    ) -> str:
        """Call the main LLM from the host loop."""
        return await self.llm.generate(
            provider_id=f"framework-provider::{session}",
            prompt=prompt,
            session=session,
            on_delta=on_delta,
        )

    @staticmethod
    def _injected_text(request: Any) -> str:
        """Return the context the plugin injected, asserting it is temporary.

        ``mark_as_temp()`` is a real contract, not a detail: hidden Runtime
        context must never be persisted into conversation history. The shipped
        black-box simulation asserts the same thing, and it is exactly the sort of
        invariant that would otherwise rot silently.
        """
        parts = list(getattr(request, "extra_user_content_parts", []) or [])
        for part in parts:
            if not getattr(part, "_no_save", False):
                raise AssertionError("injected context part is not temporary (mark_as_temp was not called)")
        return "".join(getattr(part, "text", "") for part in parts)

    def _next_id(self) -> str:
        """Return the next platform message id."""
        if self._ids is not None:
            return next(self._ids)
        self._seq += 1
        return f"msg-{self._seq:05d}"

    # ------------------------------------------------------------------ report

    def stats(self) -> dict[str, Any]:
        """Return a summary of the host's activity."""
        return {
            "running": self.running,
            "turns": self.turns,
            "sessions": self.platform.sessions(),
            "messages": len(self.platform.messages),
        }


def deliveries_by_kind(platform: Platform) -> dict[str, int]:
    """Count a platform's messages by kind, for assertions and the status line."""
    counts: dict[str, int] = {}
    for message in platform.messages:
        counts[message.kind] = counts.get(message.kind, 0) + 1
    return counts
