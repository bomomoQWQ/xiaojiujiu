"""Run one Runtime process per person inside a single container.

Why a supervisor instead of one container per person: ten containers means ten
things to watch, restart and upgrade by hand. Isolation does not need a container
boundary here -- each Runtime already keeps its whole world in its own SQLite file
under its own ``--base-dir``, and the processes share nothing -- so what is
actually needed is one place to start them, watch them, log them separately, and
answer "which address serves this session".

What this process provides:

* one child ``companion-runtime serve`` per person, on its own port and base dir;
* per-person log files, restart-on-crash with backoff, and a restart counter;
* ``GET  /fleet/routes``   -- ``{session: url}``, the routing registry the plugin
  polls so a new person needs no AstrBot restart;
* ``GET  /fleet/status``   -- per-person port, pid, restarts, health, event count;
* ``POST /fleet/restart/<person>`` and ``POST /fleet/deprovision/<person>``;
* ``POST /fleet/provision`` ``{"session": ...}`` -- start a new Runtime and make it
  reachable immediately.

The people file is the source of truth and is rewritten on every change, so a
container restart comes back with the same fleet.

Usage (inside the container)::

    python runtime_fleet.py --people-file /fleet/people.json \\
        --data-root /data --base-port 8787 --control-port 8800 \\
        --advertise-host runtime-fleet
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG_FORMAT = "%Y-%m-%dT%H:%M:%S"


def stamp() -> str:
    """Return a short timestamp for supervisor log lines."""
    return datetime.now(timezone.utc).strftime(LOG_FORMAT)


def say(message: str) -> None:
    """Log one supervisor line (line-buffered so ``docker logs`` is readable)."""
    print(f"{stamp()} fleet: {message}", flush=True)


def slugify(session: str) -> str:
    """Turn a session into a filesystem- and DNS-safe name."""
    cleaned = "".join(char if char.isalnum() else "-" for char in session)
    return cleaned.strip("-").lower()[-40:] or "person"


class RuntimeProcess:
    """One person's Runtime: its child process, its port, its log, its restarts."""

    def __init__(self, session: str, port: int, data_root: Path, log_dir: Path, env: dict[str, str]):
        """Store the wiring; nothing is started until :meth:`start`."""
        self.session = session
        self.slug = slugify(session)
        self.port = port
        self.base_dir = data_root / self.slug
        self.log_path = log_dir / f"{self.slug}.log"
        self.env = dict(env)
        self.env["CR_CONVERSATION_ID"] = session
        self.process: subprocess.Popen[bytes] | None = None
        self.restarts = 0
        self.started_at: float | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """The address other containers use to reach this Runtime."""
        return f"http://{ADVERTISE_HOST}:{self.port}"

    def start(self) -> None:
        """Start the child and its supervisor thread (idempotent enough)."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log_path.open("ab", buffering=0)
        handle.write(f"--- {stamp()} starting on port {self.port} ---\n".encode())
        self.process = subprocess.Popen(
            [
                "companion-runtime",
                "--base-dir",
                str(self.base_dir),
                "serve",
                "--host",
                "0.0.0.0",
                "--port",
                str(self.port),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=self.env,
        )
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._supervise, name=f"fleet-{self.slug}", daemon=True)
        self._thread.start()
        say(f"{self.slug} started pid={self.process.pid} port={self.port}")

    def _supervise(self) -> None:
        """Restart the child if it dies while the fleet is running."""
        while not self._stopping:
            process = self.process
            if process is None:
                return
            code = process.wait()
            if self._stopping:
                return
            self.restarts += 1
            delay = min(30.0, 1.0 * (2 ** min(self.restarts, 5)))
            say(f"{self.slug} exited with {code}; restarting in {delay:.0f}s (restart #{self.restarts})")
            time.sleep(delay)
            if self._stopping:
                return
            self._spawn_only()

    def _spawn_only(self) -> None:
        """Restart the child without spawning a second supervisor thread."""
        handle = self.log_path.open("ab", buffering=0)
        handle.write(f"--- {stamp()} restarting on port {self.port} ---\n".encode())
        self.process = subprocess.Popen(
            [
                "companion-runtime",
                "--base-dir",
                str(self.base_dir),
                "serve",
                "--host",
                "0.0.0.0",
                "--port",
                str(self.port),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=self.env,
        )
        self.started_at = time.monotonic()

    def health(self) -> dict[str, object]:
        """Return this Runtime's health payload, or a failure description."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=4) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            return {"status": "unreachable", "error": str(error)[:120]}

    def status(self) -> dict[str, object]:
        """Return one line of fleet status for this person."""
        process = self.process
        payload = self.health()
        return {
            "person": self.slug,
            "session": self.session,
            "port": self.port,
            "pid": process.pid if process else None,
            "running": bool(process and process.poll() is None),
            "restarts": self.restarts,
            "uptime_s": round(time.monotonic() - self.started_at, 1) if self.started_at else None,
            "raw_events": payload.get("raw_events"),
            "allow_proactive": payload.get("allow_proactive"),
            "health": payload.get("status"),
        }

    def restart(self) -> None:
        """Terminate the child; the supervisor thread brings it back."""
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def stop(self) -> None:
        """Stop supervising and terminate the child."""
        self._stopping = True
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


class Fleet:
    """The whole fleet: the people file, the children, and the port allocator."""

    def __init__(self, args: argparse.Namespace):
        """Load the people file and build (but do not start) every Runtime."""
        self.people_file = Path(args.people_file)
        self.data_root = Path(args.data_root)
        self.log_dir = Path(args.log_dir)
        self.base_port = int(args.base_port)
        self.advertise_host = args.advertise_host
        self.control_port = int(args.control_port)
        self.lock = threading.Lock()
        self.people: dict[str, RuntimeProcess] = {}
        for session in self._read_people():
            self._add(session, start=False)

    # ---------------------------------------------------------------- people file

    def _read_people(self) -> list[str]:
        """Return the sessions listed in the people file (possibly empty)."""
        if not self.people_file.exists():
            return []
        try:
            data = json.loads(self.people_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            say(f"people file unreadable ({error}); starting empty")
            return []
        if isinstance(data, dict):
            data = data.get("people", [])
        return [str(item) for item in data if str(item).strip()]

    def _write_people(self) -> None:
        """Persist the current fleet so a container restart comes back the same.

        A failure here must never take the supervisor down: the fleet is already
        running in memory, and losing the bookkeeping write is strictly better than
        losing ten Runtimes. Measured once: the mounted people-file directory was
        owned by the host user while the container runs as uid 10001, and the
        PermissionError killed the supervisor on its first start.
        """
        self.people_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [item.session for item in self._ordered()]
        try:
            self.people_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            say(
                f"WARNING could not persist the people file ({error}); the fleet keeps "
                "running but a container restart would lose the provisioned members",
            )

    def _ordered(self) -> list[RuntimeProcess]:
        """Return the people ordered by port, so port allocation is stable."""
        return sorted(self.people.values(), key=lambda item: item.port)

    def _next_port(self) -> int:
        """Return the lowest free port in the fleet's range."""
        used = {item.port for item in self.people.values()}
        port = self.base_port
        while port in used:
            port += 1
        return port

    # ---------------------------------------------------------------- membership

    def _add(self, session: str, *, start: bool = True, port: int | None = None) -> RuntimeProcess:
        """Add one person (starting it unless told otherwise)."""
        with self.lock:
            process = RuntimeProcess(
                session=session,
                port=port or self._next_port(),
                data_root=self.data_root,
                log_dir=self.log_dir,
                env=os.environ.copy(),
            )
            self.people[session] = process
            self._write_people()
        if start:
            process.start()
        return process

    def provision(self, session: str) -> RuntimeProcess:
        """Start (or return) the Runtime for ``session``."""
        existing = self.people.get(session)
        if existing is not None:
            return existing
        return self._add(session)

    def deprovision(self, session: str) -> bool:
        """Stop and forget one person's Runtime; data on disk is kept."""
        with self.lock:
            process = self.people.pop(session, None)
            if process is None:
                return False
            self._write_people()
        process.stop()
        say(f"{process.slug} deprovisioned (data kept at {process.base_dir})")
        return True

    def start_all(self) -> None:
        """Start every configured Runtime."""
        for process in self._ordered():
            process.start()

    def stop_all(self) -> None:
        """Stop every Runtime."""
        for process in self._ordered():
            process.stop()

    # ---------------------------------------------------------------- registry

    def routes(self) -> dict[str, str]:
        """Return ``{session: url}`` for the plugin's routing registry."""
        with self.lock:
            return {item.session: item.url for item in self._ordered()}


ADVERTISE_HOST = "runtime-fleet"


def make_handler(fleet: Fleet):
    """Build the request handler bound to ``fleet``."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "companion-fleet/1"

        def _send(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/fleet/routes":
                self._send({"routes": fleet.routes()})
                return
            if path == "/fleet/status":
                with fleet.lock:
                    people = [item.status() for item in fleet._ordered()]
                self._send({"count": len(people), "people": people})
                return
            self._send({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            path = self.path.split("?", 1)[0].rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send({"error": "invalid json"}, 400)
                return

            if path == "/fleet/provision":
                session = str(payload.get("session") or "").strip()
                if not session:
                    self._send({"error": "session is required"}, 400)
                    return
                process = fleet.provision(session)
                self._send({"session": session, "url": process.url, "port": process.port})
                return
            if path.startswith("/fleet/restart/"):
                slug = path.rsplit("/", 1)[-1]
                match = next((item for item in fleet.people.values() if item.slug == slug), None)
                if match is None:
                    self._send({"error": "unknown person"}, 404)
                    return
                match.restart()
                self._send({"restarted": slug})
                return
            if path.startswith("/fleet/deprovision/"):
                slug = path.rsplit("/", 1)[-1]
                session = next(
                    (item.session for item in fleet.people.values() if item.slug == slug),
                    None,
                )
                if session is None:
                    self._send({"error": "unknown person"}, 404)
                    return
                self._send({"deprovisioned": session} if fleet.deprovision(session) else {"error": "failed"})
                return
            self._send({"error": "not found"}, 404)

        def log_message(self, fmt: str, *args: object) -> None:
            say(f"control: {fmt % args}")

    return Handler


def main() -> int:
    """Entry point."""
    global ADVERTISE_HOST
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--people-file", default="/fleet/people.json")
    parser.add_argument("--data-root", default="/data")
    parser.add_argument("--log-dir", default="/data/logs")
    parser.add_argument("--base-port", type=int, default=8787)
    parser.add_argument("--control-port", type=int, default=8800)
    parser.add_argument("--advertise-host", default="runtime-fleet")
    args = parser.parse_args()
    ADVERTISE_HOST = args.advertise_host

    fleet = Fleet(args)
    fleet.start_all()

    server = ThreadingHTTPServer(("0.0.0.0", args.control_port), make_handler(fleet))
    # A burst of provisioning calls must not be reset the way the test frontend was.
    server.request_queue_size = 128
    say(f"control surface on :{args.control_port}; {len(fleet.people)} runtime(s)")

    def shutdown(*_: object) -> None:
        say("stopping")
        fleet.stop_all()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
