#!/usr/bin/env python3
"""Completely isolated platform canary for Runtime -> OneBot delivery.

This runner starts only two in-process loopback fakes on fixed ports:

* ``127.0.0.1:16300`` -- OneBot v11 forward HTTP
* ``127.0.0.1:16301`` -- OneBot v11 forward WebSocket

It never starts the production Runtime/AstrBot/NapCat services.  The Runtime is used as
an in-process component over a disposable SQLite file and FastAPI ``TestClient``.  Each
case enters as an external candidate, is committed, leased for render, reports explicit
render metadata, is leased for send, passes the authorization gate, and reaches one of
``sent`` / ``fail`` / ``unknown`` at the fake platform.  The unknown case deliberately
loses the ACK, restarts the Runtime on the same SQLite file, and proves that the expired
lease can be reclaimed without pretending that delivery succeeded or failed.

Safety is deny-by-default: hosts, ports, scope and synthetic identifiers are constants;
startup refuses proxy environment variables, occupied ports, non-loopback resolution,
real-looking numeric QQ identifiers, or a scope outside ``canary:``.  The only durable
output is a redacted evidence JSON below ``--base-dir``.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.dont_write_bytecode = True
REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = REPO_ROOT / "runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

HOST = "127.0.0.1"
HTTP_PORT = 16300
WS_PORT = 16301
SCOPE = "canary:FriendMessage:synthetic-user"
SYNTHETIC_USER = "synthetic-user"
SYNTHETIC_BOT = "synthetic-bot"
ADAPTER_ID = "platform-canary-adapter"
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class CanaryFailure(RuntimeError):
    """A safety invariant or canary assertion failed."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer closed")
        data += chunk
    return data


def _read_ws_frame(conn: socket.socket) -> tuple[int, bytes]:
    head = _recv_exact(conn, 2)
    opcode, length = head[0] & 0x0F, head[1] & 0x7F
    masked = bool(head[1] & 0x80)
    if length == 126:
        length = struct.unpack("!H", _recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(conn, 8))[0]
    mask = _recv_exact(conn, 4) if masked else b""
    payload = _recv_exact(conn, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _send_ws_frame(conn: socket.socket, payload: bytes) -> None:
    head = bytearray([0x81])
    if len(payload) < 126:
        head.append(len(payload))
    elif len(payload) < 65536:
        head.append(126)
        head.extend(struct.pack("!H", len(payload)))
    else:
        head.append(127)
        head.extend(struct.pack("!Q", len(payload)))
    conn.sendall(bytes(head) + payload)


def _ws_client_action(port: int, payload: Mapping[str, Any], *, timeout: float = 3.0) -> Mapping[str, Any]:
    """Send one masked text frame to the local fake forward-WS endpoint."""
    conn = socket.create_connection((HOST, port), timeout=timeout)
    conn.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET /onebot HTTP/1.1\r\nHost: {HOST}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    conn.sendall(request)
    response = b""
    while b"\r\n\r\n" not in response:
        response += conn.recv(4096)
    if not response.startswith(b"HTTP/1.1 101"):
        conn.close()
        raise CanaryFailure(f"fake WS handshake refused: {response[:80]!r}")
    body = _json_bytes(payload)
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))
    head = bytearray([0x81])
    if len(body) < 126:
        head.append(0x80 | len(body))
    elif len(body) < 65536:
        head.append(0x80 | 126)
        head.extend(struct.pack("!H", len(body)))
    else:
        raise CanaryFailure("canary WS request unexpectedly large")
    conn.sendall(bytes(head) + mask + masked)
    try:
        opcode, raw = _read_ws_frame(conn)
    except (ConnectionError, OSError, socket.timeout) as exc:
        raise TimeoutError("OneBot WS ACK unknown") from exc
    finally:
        conn.close()
    if opcode != 1:
        raise CanaryFailure(f"unexpected WS opcode {opcode}")
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise CanaryFailure("fake WS returned a non-object")
    return decoded


@dataclass
class FakeOneBot:
    """Fixed-port OneBot HTTP/WS fake with deterministic ACK outcomes."""

    outcomes: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    http: ThreadingHTTPServer | None = None
    http_thread: threading.Thread | None = None
    ws_socket: socket.socket | None = None
    ws_thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                response = fake._answer(body, transport="http")
                if response is None:
                    self.close_connection = True
                    return
                raw = _json_bytes(response)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.http = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, name="canary-onebot-http", daemon=True)
        self.http_thread.start()
        self.ws_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ws_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ws_socket.bind((HOST, WS_PORT))
        self.ws_socket.listen(8)
        self.ws_socket.settimeout(0.2)
        self.ws_thread = threading.Thread(target=self._serve_ws, name="canary-onebot-ws", daemon=True)
        self.ws_thread.start()

    def _serve_ws(self) -> None:
        assert self.ws_socket is not None
        while not self.stop_event.is_set():
            try:
                conn, _address = self.ws_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_ws, args=(conn,), daemon=True).start()

    def _handle_ws(self, conn: socket.socket) -> None:
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            headers: dict[str, str] = {}
            for line in request.decode("ascii", errors="replace").split("\r\n")[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower().strip()] = value.strip()
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + _GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
            _opcode, raw = _read_ws_frame(conn)
            body = json.loads(raw.decode("utf-8"))
            response = self._answer(body, transport="ws")
            if response is not None:
                _send_ws_frame(conn, _json_bytes(response))
        except (OSError, ConnectionError, ValueError, KeyError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _answer(self, body: Mapping[str, Any], *, transport: str) -> dict[str, Any] | None:
        params = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        marker = str(params.get("canary_case") or "sent")
        outcome = self.outcomes.get(marker, marker)
        self.calls.append({"transport": transport, "action": body.get("action"), "params": dict(params), "outcome": outcome})
        if outcome == "unknown":
            return None
        if outcome == "fail":
            return {"status": "failed", "retcode": 1500, "data": {"error": "synthetic_failure"}, "echo": body.get("echo")}
        return {"status": "ok", "retcode": 0, "data": {"message_id": f"canary-{len(self.calls)}"}, "echo": body.get("echo")}

    def stop(self) -> None:
        self.stop_event.set()
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
        if self.ws_socket is not None:
            self.ws_socket.close()
        if self.http_thread is not None:
            self.http_thread.join(timeout=3)
        if self.ws_thread is not None:
            self.ws_thread.join(timeout=3)


def safety_preflight(base_dir: Path) -> dict[str, Any]:
    """Refuse any configuration that could possibly name a real platform/user."""
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise CanaryFailure("non-loopback host refused")
    resolved = {item[4][0] for item in socket.getaddrinfo(HOST, None)}
    if not resolved or any(value not in {"127.0.0.1", "::1"} for value in resolved):
        raise CanaryFailure(f"host did not resolve exclusively to loopback: {sorted(resolved)}")
    if not SCOPE.startswith("canary:") or "synthetic" not in SCOPE:
        raise CanaryFailure("scope is not the fixed virtual canary scope")
    for identity in (SYNTHETIC_USER, SYNTHETIC_BOT):
        if identity.isdigit() or "synthetic" not in identity:
            raise CanaryFailure("real-looking QQ identity refused")
    inherited = sorted(key for key in PROXY_KEYS if os.environ.get(key))
    if inherited:
        raise CanaryFailure(f"proxy environment refused: {', '.join(inherited)}")
    for port in (HTTP_PORT, WS_PORT):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((HOST, port))
        except OSError as exc:
            raise CanaryFailure(f"fixed canary port {port} is occupied; refusing fallback") from exc
        finally:
            probe.close()
    base_dir = base_dir.resolve()
    if base_dir == REPO_ROOT or REPO_ROOT in base_dir.parents:
        raise CanaryFailure("--base-dir must be outside the source checkout")
    return {"loopback_only": True, "fixed_ports": [HTTP_PORT, WS_PORT], "scope": SCOPE,
            "synthetic_ids": [SYNTHETIC_USER, SYNTHETIC_BOT], "proxy_env": "empty"}


def _runtime(db_path: Path) -> Any:
    from companion_runtime.config import RuntimeConfig
    from companion_runtime.db import Database
    from companion_runtime.runtime import Runtime

    config = RuntimeConfig()
    config.storage.database_path = str(db_path)
    config.storage.mirror_raw_events = False
    config.conversation_id = SCOPE
    config.outbox.lease_seconds = 0.25
    config.outbox.max_attempts = 3
    return Runtime(config, seed=16300, database=Database(str(db_path)), created_at=datetime.now(timezone.utc))


def _client(runtime: Any) -> Any:
    from companion_runtime.api import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app(runtime, runtime.config))


def _post(client: Any, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=dict(payload))
    if response.status_code != 200:
        raise CanaryFailure(f"{path} -> HTTP {response.status_code}: {response.text[:200]}")
    value = response.json()
    if not isinstance(value, dict):
        raise CanaryFailure(f"{path} returned non-object")
    return value


def _external_candidate(runtime: Any, client: Any, case: str) -> tuple[Any, dict[str, Any]]:
    candidate_id = f"canary-candidate-{case}-{uuid.uuid4().hex[:8]}"
    operation = {
        "op": "add",
        "candidate": {
            "candidate_id": candidate_id,
            "type": "follow_up",
            "intent": f"platform canary {case}",
            "goal": "verify isolated delivery semantics",
            "target": SYNTHETIC_USER,
            "sources": [f"canary-source:{case}"],
            "constraints": ["synthetic only", "never contact QQ"],
            "confidence": 1.0,
            "internal_need": 1.0,
            "proposed_by": "external_canary",
        },
        "sources": [f"canary-source:{case}"],
    }
    response = _post(client, "/candidates/operations", {"operations": [operation]})
    candidate = runtime.projections.candidates.get(candidate_id)
    if candidate is None:
        raise CanaryFailure("external candidate was not persisted")
    with runtime.db.transaction() as conn:
        state = runtime.projections.runtime.ensure()
        attempt_id, render_id = runtime._commit_attempt(conn, chosen=candidate, state=state, now=datetime.now(timezone.utc))
    return candidate, {"operation_response": response, "candidate_id": candidate_id,
                       "attempt_id": attempt_id, "render_outbox_id": render_id}


def _lease(client: Any, capability: str) -> dict[str, Any]:
    body = _post(client, "/v1/outbox/lease", {
        "protocol_version": "1", "adapter_id": ADAPTER_ID, "capabilities": [capability],
        "max_actions": 1, "lease_ttl_ms": 1000, "sessions": [SCOPE],
    })
    actions = body.get("actions") or []
    if len(actions) != 1:
        raise CanaryFailure(f"expected one {capability} claim, got {len(actions)}")
    action = actions[0]
    if action.get("session") != SCOPE or action.get("action_type") != capability:
        raise CanaryFailure(f"claim escaped virtual scope/type: {action!r}")
    return action


def _report(client: Any, action: Mapping[str, Any], *, status: str, result: Mapping[str, Any], error: str = "") -> dict[str, Any]:
    payload = {
        "protocol_version": "1", "adapter_id": ADAPTER_ID, "action_id": action["action_id"],
        "lease_id": action["lease_id"], "action_type": action["action_type"], "status": status,
        "attempt_id": action.get("attempt_id", ""), "session": SCOPE,
        "reported_at": datetime.now(timezone.utc).isoformat(), "result": dict(result),
    }
    if error:
        payload["error"] = error
    return _post(client, f"/v1/outbox/{action['action_id']}/result", payload)


def _platform_send(case: str, transport: str, text: str) -> tuple[str, dict[str, Any] | None]:
    request = {"action": "send_private_msg", "params": {"user_id": SYNTHETIC_USER, "message": text,
                "canary_case": case}, "echo": f"canary-{case}"}
    try:
        if transport == "http":
            raw = _json_bytes(request)
            req = urllib.request.Request(f"http://{HOST}:{HTTP_PORT}/", data=raw, method="POST",
                                         headers={"Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=3) as response:
                ack = json.loads(response.read().decode("utf-8"))
        else:
            ack = _ws_client_action(WS_PORT, request)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return "unknown", {"error_type": type(exc).__name__, "detail": "ACK not observed"}
    if ack.get("status") == "ok" and int(ack.get("retcode", -1)) == 0:
        return "sent", dict(ack)
    return "fail", dict(ack)


def run(base_dir: Path) -> dict[str, Any]:
    safety = safety_preflight(base_dir)
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)
    db_path = base_dir / "canary.sqlite3"
    fake = FakeOneBot(outcomes={"sent": "sent", "fail": "fail", "unknown": "unknown"})
    runtime = None
    test_client = None
    cases: list[dict[str, Any]] = []
    restart: dict[str, Any] = {}
    fake.start()
    try:
        runtime = _runtime(db_path)
        test_client = _client(runtime)
        test_client.__enter__()
        for case, transport in (("sent", "http"), ("fail", "ws"), ("unknown", "http")):
            candidate, evidence = _external_candidate(runtime, test_client, case)
            render = _lease(test_client, "render")
            render_metadata = {
                "render_version": "platform-canary-v1", "template_version": "fixed-canary-template-v1",
                "encoder_version": "none", "scope": SCOPE, "candidate_id": candidate.candidate_id,
                "synthetic": True, "claims_completion": False, "task_ref": None,
                "witness_requirement": None,
            }
            rendered_text = f"[CANARY:{case}] isolated synthetic message"
            render_result = _report(test_client, render, status="ok", result={
                "text": rendered_text, "chars": len(rendered_text), "render_metadata": render_metadata,
            })
            send = _lease(test_client, "send")
            authorize = _post(test_client, f"/v1/actions/{send['action_id']}/authorize", {
                "protocol_version": "1", "adapter_id": ADAPTER_ID, "lease_id": send["lease_id"],
                "session": SCOPE, "attempt_id": send.get("attempt_id", ""),
                "text_preview": rendered_text, "text_sha256": hashlib.sha256(rendered_text.encode()).hexdigest(),
            })
            if authorize.get("authorized") is not True:
                raise CanaryFailure(f"synthetic send was not authorized: {authorize}")
            outcome, ack = _platform_send(case, transport, rendered_text)
            item = {
                **evidence, "transport": transport, "render_claim": render,
                "render_metadata": render_metadata, "render_result": render_result,
                "send_claim": send, "authorize": authorize, "platform_ack": ack, "ack_state": outcome,
            }
            if case == "sent":
                if outcome != "sent":
                    raise CanaryFailure(f"sent case produced {outcome}")
                item["delivery_result"] = _report(test_client, send, status="ok", result={"sent": True})
            elif case == "fail":
                if outcome != "fail":
                    raise CanaryFailure(f"fail case produced {outcome}")
                item["delivery_result"] = _report(test_client, send, status="failed",
                                                   result={"sent": False, "reason": "synthetic_failure"},
                                                   error="synthetic_failure")
            else:
                if outcome != "unknown":
                    raise CanaryFailure(f"unknown case produced {outcome}")
                item["delivery_result"] = None
                item["pre_restart_state"] = runtime.projections.attempts.get(evidence["attempt_id"]).state
                unknown_send_id = send["action_id"]
            cases.append(item)

        test_client.__exit__(None, None, None)
        test_client = None
        runtime.close()
        runtime = None
        time.sleep(0.35)
        runtime = _runtime(db_path)
        # The v1 lease endpoint rate-limits lazy ticks across restarts. Explicitly run
        # the queue's own restart recovery step so this canary does not wait for that
        # unrelated cadence before proving lease reclamation.
        with runtime.db.transaction() as conn:
            runtime.projections.outbox.reclaim_expired(conn, datetime.now(timezone.utc))
        test_client = _client(runtime)
        test_client.__enter__()
        reclaimed = _lease(test_client, "send")
        if reclaimed["action_id"] != unknown_send_id or int(reclaimed.get("attempts", 0)) < 2:
            raise CanaryFailure(f"restart did not reclaim unknown send: {reclaimed}")
        attempt = runtime.projections.attempts.get(cases[-1]["attempt_id"])
        restart = {
            "performed": True, "same_database": True, "unknown_action_reclaimed": True,
            "action_id": reclaimed["action_id"], "claim_attempts": reclaimed.get("attempts"),
            "attempt_state": attempt.state, "no_false_terminal_ack": attempt.state == "ready_to_send",
        }
        if not restart["no_false_terminal_ack"]:
            raise CanaryFailure("unknown ACK became a false sent/failed terminal state")

        states = {case["ack_state"] for case in cases}
        if states != {"sent", "fail", "unknown"}:
            raise CanaryFailure(f"ACK matrix incomplete: {sorted(states)}")
        evidence = {
            "schema": "platform-canary-evidence/v1", "passed": True,
            "generated_at": datetime.now(timezone.utc).isoformat(), "safety": safety,
            "production_services_started": False, "runtime_mode": "in_process_component",
            "database": {"kind": "disposable_sqlite", "path": str(db_path)},
            "cases": cases, "restart": restart, "fake_onebot_calls": fake.calls,
            "assertions": {
                "external_candidate": all(case["candidate_id"] for case in cases),
                "render_metadata": all(case["render_metadata"]["synthetic"] for case in cases),
                "claimed_in_virtual_scope": all(case["send_claim"]["session"] == SCOPE for case in cases),
                "http_and_ws_exercised": {case["transport"] for case in cases} == {"http", "ws"},
                "ack_matrix": states == {"sent", "fail", "unknown"},
                "restart_reclaims_unknown": restart["unknown_action_reclaimed"],
                "real_qq_or_host_used": False,
            },
        }
        return evidence
    finally:
        if test_client is not None:
            test_client.__exit__(None, None, None)
        if runtime is not None:
            runtime.close()
        fake.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True,
                        help="Disposable output directory outside the source checkout")
    parser.add_argument("--evidence", default="evidence.json",
                        help="Evidence filename inside --base-dir (default: evidence.json)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.base_dir.resolve() / args.evidence
    try:
        evidence = run(args.base_dir)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": True, "evidence": str(output), "ports": [HTTP_PORT, WS_PORT]},
                         ensure_ascii=False))
        return 0
    except (CanaryFailure, OSError, ValueError) as exc:
        failure = {"schema": "platform-canary-evidence/v1", "passed": False,
                   "error": f"{type(exc).__name__}: {exc}",
                   "generated_at": datetime.now(timezone.utc).isoformat()}
        try:
            args.base_dir.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
