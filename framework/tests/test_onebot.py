"""Conformance tests for the OneBot 11 frontend, against a fake AstrBot.

The tests exist because the contract is not ours to invent: AstrBot's adapter validates
the handshake from the OneBot 11 reverse-WebSocket spec (`X-Self-ID`, `X-Client-Role`,
`Authorization: Bearer` - a missing role header is a 400, per `aiocqhttp._handle_wsr`),
and it will stall if an API call is left unanswered. So the peer side is implemented
here too, in the standard library, and the assertions are made on the bytes that cross
the wire rather than on our own bookkeeping.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from typing import Any

import pytest

from cf.onebot import OneBotError, OneBotFrontend
from cf.onebot_service import OneBotService

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeAstrBot:
    """The server half of a OneBot 11 reverse-WebSocket link."""

    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        self.headers: dict[str, str] = {}
        self.events: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.connections = 0
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, name="fake-astrbot", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ server side

    def _serve(self) -> None:
        """Accept connections until stopped; one at a time is enough for tests."""
        self._server.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._handshake(conn)
            except Exception:  # noqa: BLE001 - the test asserts on what it did get
                conn.close()
                continue
            with self._lock:
                self.connections += 1
                self._conn = conn
            self._read_frames(conn)

    def _handshake(self, conn: socket.socket) -> None:
        """Read the upgrade request, record the headers and accept it."""
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError("client went away during the handshake")
            request += chunk
        head, _, _rest = request.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", errors="replace").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        with self._lock:
            self.headers = headers

    def _read_frames(self, conn: socket.socket) -> None:
        """Read client frames (masked) until the peer closes."""
        buffer = b""
        conn.settimeout(0.5)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while True:
                frame, rest = _parse_frame(buffer)
                if frame is None:
                    break
                buffer = rest
                opcode, payload = frame
                if opcode == 0x8:
                    conn.close()
                    return
                if opcode in (0x1, 0x2):
                    try:
                        decoded = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        continue
                    with self._lock:
                        if isinstance(decoded, dict) and "echo" in decoded:
                            self.responses.append(decoded)
                        else:
                            self.events.append(decoded)

    # ----------------------------------------------------------------- client side

    def send_api_call(self, action: str, params: dict[str, Any], echo: str = "echo-1") -> None:
        """Push one API call to the connected client."""
        self._send_json({"action": action, "params": params, "echo": echo})

    def _send_json(self, payload: dict[str, Any]) -> None:
        """Send one unmasked text frame (servers must not mask)."""
        for _ in range(40):
            with self._lock:
                conn = self._conn
            if conn is not None:
                break
            time.sleep(0.05)
        if conn is None:
            raise AssertionError("no client connected")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = bytearray([0x81])
        length = len(body)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        conn.sendall(bytes(header) + body)

    def drop(self) -> None:
        """Close the current connection, as a restarting AstrBot would."""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        """Stop accepting and close everything."""
        self._stop.set()
        self.drop()
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def wait_for_event(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Wait until at least one event arrived and return the first one."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.events:
                    return self.events[0]
            time.sleep(0.02)
        raise AssertionError("no event reached the fake AstrBot")

    def wait_for_response(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Wait until at least one API response arrived and return the first one."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.responses:
                    return self.responses[0]
            time.sleep(0.02)
        raise AssertionError("the API call was never answered")

    def wait_for_message_event(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Return the first *message* event, skipping the connect handshake.

        A real OneBot client announces itself with a lifecycle meta event the moment
        the socket is up (see ``OneBotFrontend._connect_once``), so the first frame the
        host receives is not the user's message.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for event in self.events:
                    if event.get("post_type") == "message":
                        return event
            time.sleep(0.02)
        raise AssertionError("no message event reached the fake AstrBot")

    def wait_for_meta_event(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Return the first lifecycle meta event, i.e. the connect handshake."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for event in self.events:
                    if event.get("post_type") == "meta_event":
                        return event
            time.sleep(0.02)
        raise AssertionError("the frontend never announced its lifecycle")

    def meta_event_count(self) -> int:
        """Return how many lifecycle frames have arrived so far."""
        with self._lock:
            return sum(1 for event in self.events if event.get("post_type") == "meta_event")


def _parse_frame(buffer: bytes) -> tuple[tuple[int, bytes] | None, bytes]:
    """Parse one frame from ``buffer``; returns ``(frame, rest)`` or ``(None, buffer)``."""
    if len(buffer) < 2:
        return None, buffer
    first, second = buffer[0], buffer[1]
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < 4:
            return None, buffer
        length = struct.unpack("!H", buffer[2:4])[0]
        offset = 4
    elif length == 127:
        if len(buffer) < 10:
            return None, buffer
        length = struct.unpack("!Q", buffer[2:10])[0]
        offset = 10
    mask = b""
    if masked:
        if len(buffer) < offset + 4:
            return None, buffer
        mask = buffer[offset : offset + 4]
        offset += 4
    if len(buffer) < offset + length:
        return None, buffer
    payload = buffer[offset : offset + length]
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return (opcode, payload), buffer[offset + length :]


@pytest.fixture()
def fake() -> Any:
    """A fake AstrBot with a connected frontend."""
    server = FakeAstrBot()
    frontend = OneBotFrontend(
        ws_url=f"ws://127.0.0.1:{server.port}/ws",
        token="test-token",
        self_id="10001",
        user_id="20001",
        group_id="30001",
        reconnect_interval=0.2,
    )
    frontend.start()
    deadline = time.time() + 5
    while time.time() < deadline and not frontend.connected:
        time.sleep(0.02)
    assert frontend.connected, "the frontend never connected"
    yield server, frontend
    frontend.stop()
    server.close()


class TestHandshake:
    """The headers AstrBot's adapter reads before it will talk to us."""

    def test_the_three_spec_headers_are_sent(self, fake: Any) -> None:
        """OneBot 11 reverse WS: X-Self-ID, X-Client-Role and the bearer token."""
        server, _frontend = fake
        assert server.headers.get("x-self-id") == "10001"
        assert server.headers.get("x-client-role") == "Universal"
        assert server.headers.get("authorization") == "Bearer test-token"

    def test_the_client_announces_its_lifecycle_on_connect(self, fake: Any) -> None:
        """A reverse-WS client that stays silent after the upgrade is dropped again.

        Measured against AstrBot 4.28.1's aiocqhttp server: the handshake completes
        (101) and the host then closes the connection without ever logging
        "适配器已连接", so the platform is never registered and nothing is delivered.
        Sending the lifecycle frame is what makes the host accept it. The frontend could
        already build this frame, but only the CLI and the service HTTP endpoint called
        it - the connect path never did.
        """
        server, _frontend = fake
        event = server.wait_for_meta_event()
        assert event["meta_event_type"] == "lifecycle"
        assert event["sub_type"] == "connect"
        assert event["self_id"] == 10001
        assert isinstance(event["time"], int)

    def test_an_empty_token_sends_no_authorization_header(self) -> None:
        """No token configured means no Authorization header - not an empty one."""
        server = FakeAstrBot()
        frontend = OneBotFrontend(
            ws_url=f"ws://127.0.0.1:{server.port}/ws", token="", reconnect_interval=0.2
        )
        frontend.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not frontend.connected:
                time.sleep(0.02)
            assert frontend.connected
            assert "authorization" not in server.headers
        finally:
            frontend.stop()
            server.close()


class TestEvents:
    """What we push: a user message AstrBot can route."""

    def test_a_user_message_is_a_valid_onebot_event(self, fake: Any) -> None:
        """The event carries the fields the adapter and the plugin read."""
        server, frontend = fake
        frontend.send_user_message("你好呀")
        event = server.wait_for_message_event()
        assert event["post_type"] == "message"
        assert event["message_type"] == "private"
        assert event["raw_message"] == "你好呀"
        assert event["message"] == [{"type": "text", "data": {"text": "你好呀"}}]
        assert event["self_id"] == 10001
        assert event["user_id"] == 20001
        assert isinstance(event["message_id"], int)
        assert isinstance(event["time"], int)
        assert event["sender"]["user_id"] == 20001

    def test_a_group_message_carries_a_group_id(self, fake: Any) -> None:
        """Group traffic needs group_id; private traffic must not pretend to have one."""
        server, frontend = fake
        frontend.send_user_message("群里好", group=True)
        event = server.wait_for_message_event()
        assert event["message_type"] == "group"
        assert event["group_id"] == 30001
        assert event["sender"]["role"] == "member"


class TestApiCalls:
    """What we answer: every call, with the spec's envelope and codes."""

    def test_send_msg_is_answered_and_recorded_as_the_bot_reply(self, fake: Any) -> None:
        """A reply is the echo-matched response plus a transcript entry."""
        server, frontend = fake
        server.send_api_call(
            "send_msg",
            {"message_type": "private", "user_id": 20001, "message": [{"type": "text", "data": {"text": "我在"}}]},
            echo="e-1",
        )
        response = server.wait_for_response()
        assert response == {"status": "ok", "retcode": 0, "data": {"message_id": 9000}, "echo": "e-1"}

        deadline = time.time() + 3
        while time.time() < deadline and not frontend.sent_messages:
            time.sleep(0.02)
        assert frontend.sent_messages[0]["text"] == "我在"
        assert frontend.sent_messages[0]["session"] == "private:20001"

    def test_a_cq_string_message_is_flattened(self, fake: Any) -> None:
        """The spec allows a message to be a string; the transcript must still read."""
        server, frontend = fake
        server.send_api_call("send_private_msg", {"user_id": 20001, "message": "纯文本回复"}, echo="e-2")
        server.wait_for_response()
        deadline = time.time() + 3
        while time.time() < deadline and not frontend.sent_messages:
            time.sleep(0.02)
        assert frontend.sent_messages[0]["text"] == "纯文本回复"

    def test_an_unknown_action_is_answered_1404(self, fake: Any) -> None:
        """Never leave a call unanswered: an unimplemented action is a logged 1404."""
        server, frontend = fake
        server.send_api_call("set_group_ban", {"group_id": 30001, "user_id": 20001, "duration": 60}, echo="e-3")
        response = server.wait_for_response()
        assert response["retcode"] == 1404
        assert response["status"] == "failed"
        assert response["echo"] == "e-3"
        assert frontend.calls[-1].action == "set_group_ban"

    def test_a_send_without_a_target_is_answered_1400(self, fake: Any) -> None:
        """A malformed request is the caller's error, and is reported as one."""
        server, _frontend = fake
        server.send_api_call("send_msg", {"message": "无目标"}, echo="e-4")
        response = server.wait_for_response()
        assert response["retcode"] == 1400

    def test_a_failing_handler_is_answered_1500(self, fake: Any) -> None:
        """An internal error is 1500 and still carries the echo."""
        server, frontend = fake

        def explode(_params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("boom")

        frontend._api_handlers = lambda: {"get_status": explode}  # type: ignore[method-assign]
        server.send_api_call("get_status", {}, echo="e-5")
        response = server.wait_for_response()
        assert response["retcode"] == 1500
        assert "boom" in response["data"]["error"]


class TestReconnect:
    """The spec requires reconnecting; a dead link must not need a restart."""

    def test_the_client_reconnects_after_the_peer_drops(self, fake: Any) -> None:
        """Dropping the connection leads to a new one, and events flow again."""
        server, frontend = fake
        first = server.connections
        announced = server.meta_event_count()
        server.drop()
        deadline = time.time() + 8
        while time.time() < deadline and server.connections <= first:
            time.sleep(0.05)
        assert server.connections > first, "the frontend never reconnected"
        deadline = time.time() + 5
        while time.time() < deadline and not frontend.connected:
            time.sleep(0.02)
        # The handshake has to be repeated on every connection, not just the first:
        # the host drops a client that reconnects silently.
        deadline = time.time() + 5
        while time.time() < deadline and server.meta_event_count() <= announced:
            time.sleep(0.02)
        assert server.meta_event_count() > announced, "the reconnect did not re-announce"
        frontend.send_user_message("重连之后还在")
        assert server.wait_for_message_event()["raw_message"] in {"你好呀", "重连之后还在"}

    def test_sending_while_disconnected_is_an_error_not_a_crash(self) -> None:
        """A send with no link raises the typed error the callers catch."""
        frontend = OneBotFrontend(ws_url="ws://127.0.0.1:1/ws", reconnect_interval=0.2)
        try:
            with pytest.raises(OneBotError):
                frontend.send_user_message("没有连接")
        finally:
            frontend.stop()


class TestControlSurface:
    """The routes a human and an agent drive the frontend with."""

    def _service(self, fake: Any) -> OneBotService:
        server, frontend = fake
        service = OneBotService(frontend, host="127.0.0.1", port=0)
        return service

    def test_send_route_pushes_a_message_and_records_it(self, fake: Any) -> None:
        """POST /send is the agent's keyboard."""
        service = self._service(fake)
        status, _ctype, payload = service.handle(
            "POST", "/send", json.dumps({"text": "从 HTTP 发来的"}).encode()
        )
        assert status == 200
        assert json.loads(payload)["ok"] is True
        server, _frontend = fake
        assert server.wait_for_message_event()["raw_message"] == "从 HTTP 发来的"

    def test_state_route_reports_connection_and_counts(self, fake: Any) -> None:
        """GET /state is how a test waits for something to have happened."""
        service = self._service(fake)
        status, _ctype, payload = service.handle("GET", "/state", b"")
        state = json.loads(payload)
        assert status == 200
        assert state["connected"] is True
        assert state["counts"]["transcript"] == 0
        assert state["self_id"] == "10001"

    def test_transcript_route_reads_like_a_conversation(self, fake: Any) -> None:
        """GET /transcript renders the human view of the same data."""
        service = self._service(fake)
        server, _frontend = fake
        service.handle("POST", "/send", json.dumps({"text": "在吗"}).encode())
        server.send_api_call(
            "send_msg",
            {"message_type": "private", "user_id": 20001, "message": "在的"},
            echo="e-6",
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            _status, _ctype, payload = service.handle("GET", "/transcript", b"")
            text = payload.decode()
            if "在的" in text:
                break
            time.sleep(0.05)
        assert "我: 在吗" in text
        assert "TA: 在的" in text

    def test_an_unknown_route_is_a_404_with_json(self, fake: Any) -> None:
        """Debug surfaces must fail loudly, in a parseable way."""
        service = self._service(fake)
        status, ctype, payload = service.handle("GET", "/nope", b"")
        assert status == 404
        assert ctype.startswith("application/json")
        assert json.loads(payload)["ok"] is False

    def test_a_send_without_text_is_a_400(self, fake: Any) -> None:
        """Empty input is rejected instead of being pushed as an empty event."""
        service = self._service(fake)
        status, _ctype, payload = service.handle("POST", "/send", json.dumps({"text": "  "}).encode())
        assert status == 400
        assert json.loads(payload)["error"] == "text is required"
