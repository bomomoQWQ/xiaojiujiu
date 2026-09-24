"""The control plane: how the command line moves a *running* framework.

Time control has to work while the process is up -- a harness that must be
restarted to change the clock cannot reproduce "the user went away for eight
hours" in the middle of a scene. So the clock lives in the harness process and
this module exposes it over loopback HTTP; ``cf time advance 8h`` is a thin
client for ``POST /control/time/advance``.

The interface is deliberately tiny and loopback-only:

======================  ============================================
``GET  /control/status``    clock state, heartbeat counters, ports
``POST /control/time/set``      jump to an absolute instant
``POST /control/time/advance``  step forward/backward by a duration
``POST /control/time/scale``    change virtual seconds per real second
``POST /control/time/freeze``   pin time
``POST /control/time/unfreeze`` resume
``POST /control/tick``          run one heartbeat immediately
``POST /control/endogenous``    force one endogenous decision round
``POST /control/shutdown``      stop everything
======================  ============================================

There is no authentication: the server binds to ``127.0.0.1`` and that is the
whole boundary. Exposing this on a routable address would hand an attacker the
ability to skip a companion character forward by a decade and flood the user
with messages, so :meth:`ControlServer.__init__` refuses a non-loopback host
unless the caller insists with ``allow_remote=True``.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from .clock import ControllableClock, parse_duration, parse_when
from .logbook import Logbook

LOGGER = logging.getLogger("cf.control")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ControlError(Exception):
    """Raised for a control request that cannot be honoured."""


class ControlServer:
    """Loopback HTTP control plane for a running harness.

    Args:
        clock: The clock to expose.
        hooks: Callables the control plane may invoke: ``status`` (returns a
            dict), ``tick`` (forces one heartbeat), ``endogenous`` (forces one
            decision round), ``shutdown`` (stops the harness).
        logbook: Where to record control operations.
        host: Bind address; must be loopback unless ``allow_remote``.
        port: Bind port; 0 asks the OS.
        allow_remote: Permit binding a routable address.

    Raises:
        ValueError: When asked to bind a non-loopback address without
            ``allow_remote``.
    """

    def __init__(
        self,
        clock: ControllableClock,
        *,
        hooks: Mapping[str, Callable[..., Any]] | None = None,
        logbook: Logbook | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        allow_remote: bool = False,
    ) -> None:
        """Validate the bind address and store the dependencies."""
        if host not in LOOPBACK_HOSTS and not allow_remote:
            raise ValueError(
                f"refusing to bind the control plane to {host!r}: it has no authentication. "
                "Pass allow_remote=True if you really mean it."
            )
        self.clock = clock
        self.hooks: dict[str, Callable[..., Any]] = dict(hooks or {})
        self.logbook = logbook
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------- life

    def start(self) -> str:
        """Bind and serve in a background thread.

        Returns:
            The control base URL.
        """
        self._server = ThreadingHTTPServer((self.host, self.port), _make_handler(self))
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="cf-control", daemon=True)
        self._thread.start()
        if self.logbook is not None:
            self.logbook.event("control_start", {"base_url": self.base_url})
        return self.base_url

    def stop(self, timeout: float = 5.0) -> None:
        """Stop serving."""
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=timeout)

    @property
    def base_url(self) -> str:
        """The control base URL."""
        return f"http://{self.host}:{self.port}"

    # ---------------------------------------------------------------- actions

    def dispatch(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run one control action.

        Args:
            action: Action name, e.g. ``time.advance``.
            payload: Action arguments.

        Returns:
            A JSON-serialisable result.

        Raises:
            ControlError: When the action is unknown or its arguments are bad.
        """
        try:
            if action == "time.set":
                when = parse_when(str(payload.get("when", "")))
                now = self.clock.set(when)
                return {"ok": True, "virtual_now": now.isoformat()}
            if action == "time.advance":
                delta = parse_duration(str(payload.get("by", "")))
                now = self.clock.advance(delta)
                return {"ok": True, "virtual_now": now.isoformat(), "advanced_s": delta.total_seconds()}
            if action == "time.scale":
                scale = self.clock.set_scale(float(payload.get("scale", 1.0)))
                return {"ok": True, "scale": scale}
            if action == "time.freeze":
                return {"ok": True, "virtual_now": self.clock.freeze().isoformat(), "frozen": True}
            if action == "time.unfreeze":
                return {"ok": True, "virtual_now": self.clock.unfreeze().isoformat(), "frozen": False}
            if action == "tick":
                hook = self.hooks.get("tick")
                if hook is None:
                    raise ControlError("this harness exposes no tick hook")
                return {"ok": True, "result": hook(payload)}
            if action == "endogenous":
                hook = self.hooks.get("endogenous")
                if hook is None:
                    raise ControlError("this harness exposes no endogenous hook")
                return {"ok": True, "result": hook(payload)}
            if action == "shutdown":
                hook = self.hooks.get("shutdown")
                result = hook() if hook is not None else None
                return {"ok": True, "result": result}
            if action == "status":
                hook = self.hooks.get("status")
                return {"ok": True, "result": hook() if hook is not None else {}}
        except ControlError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised into the thread
            raise ControlError(f"{type(exc).__name__}: {exc}") from exc
        raise ControlError(f"unknown control action {action!r}")


def _make_handler(server: ControlServer) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to ``server``."""

    class Handler(BaseHTTPRequestHandler):
        """Minimal control surface."""

        protocol_version = "HTTP/1.1"
        server_version = "cf-control/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib signature
            """Silence the default access log."""
            LOGGER.debug("control http: " + fmt, *args)

        def _send(self, status: int, payload: Any) -> None:
            """Write one JSON response."""
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            """Serve ``/control/status``."""
            path = self.path.split("?", 1)[0].rstrip("/")
            if path.endswith("/control/status"):
                self._send(200, {"ok": True, "clock": server.clock.state().to_dict(), "journal": server.clock.journal[-20:]})
                return
            self._send(404, {"ok": False, "error": f"no such path {self.path}"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib signature
            """Dispatch one control action named by the path."""
            path = self.path.split("?", 1)[0].rstrip("/")
            if not path.endswith("/control/time/set") and "/control/" not in path:
                self._send(404, {"ok": False, "error": f"no such path {self.path}"})
                return
            # /control/time/advance -> time.advance ; /control/tick -> tick
            action = path.split("/control/", 1)[1].replace("/", ".")
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"ok": False, "error": "body is not JSON"})
                return
            if not isinstance(payload, Mapping):
                self._send(400, {"ok": False, "error": "body must be a JSON object"})
                return
            try:
                result = server.dispatch(action, payload)
            except ControlError as exc:
                if server.logbook is not None:
                    server.logbook.warn("control_error", {"action": action, "error": str(exc)})
                self._send(400, {"ok": False, "error": str(exc)})
                return
            if server.logbook is not None:
                clock_state = server.clock.state().to_dict()
                server.logbook.event(
                    "control",
                    {"action": action, "request": dict(payload), "clock": clock_state},
                    message=f"[control] {action} -> {clock_state['virtual_now']}",
                )
            self._send(200, result)

    return Handler
