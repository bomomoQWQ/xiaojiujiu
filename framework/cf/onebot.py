"""A minimal OneBot v11 reverse-WebSocket client, standard library only.

The framework's own host (`cf.host.AstrBotHost`) drives the adapter *in process*. That
is the right tool for deterministic simulations, but it cannot answer questions like
"does the real AstrBot, with its real plugin loader and its real event pipeline, still
behave the way we think?" - for that something has to speak the wire protocol to a real
AstrBot instance. This is that something, and it is deliberately small.

The contract, verified against the aiocqhttp build AstrBot ships (``_handle_wsr`` in
``aiocqhttp/__init__.py``):

* the client connects to ``ws://<host>:<port>/ws`` and must send three headers:
  ``Authorization: Bearer <token>`` (absent -> 401, wrong -> 403),
  ``X-Client-Role: event|api|universal`` (read with ``[]``, so a miss is a 400) and
  ``X-Self-ID: <bot account>``;
* the client then pushes *events* (``post_type=message`` and friends) as text frames;
* AstrBot pushes *API calls* (``{"action": …, "params": …, "echo": …}``) and expects one
  reply per call, carrying the same ``echo`` - an unanswered call blocks the host side,
  so every inbound call is answered, including ones we do not implement (``retcode``
  1404), and every one is recorded for debugging.

Only the pieces needed for that conversation are implemented: no permessage-deflate, no
extensions, no server role. Fragmentation from the peer is handled; masking is applied
to everything we send, because RFC 6455 requires a client to mask.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

LOGGER = logging.getLogger("cf.onebot")

#: RFC 6455 handshake magic value.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Frame opcodes we care about.
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class OneBotError(RuntimeError):
    """Raised when the reverse-WebSocket link cannot be established or is lost."""


@dataclass
class FrameRecord:
    """One frame in either direction, kept for the debug surface."""

    at: float
    direction: str  # "in" (AstrBot -> us) or "out" (us -> AstrBot)
    kind: str  # "event" | "api" | "response" | "meta" | "control"
    payload: Any

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {"at": self.at, "direction": self.direction, "kind": self.kind, "payload": self.payload}


#: Actions that carry a message and therefore need a target.
_SEND_ACTIONS = frozenset({"send_msg", "send_private_msg", "send_group_msg"})


def _has_send_target(params: Mapping[str, Any], action: str) -> bool:
    """Return whether a send action names a destination (OneBot 11 spelling)."""
    if action == "send_group_msg":
        return params.get("group_id") not in (None, "")
    if action == "send_private_msg":
        return params.get("user_id") not in (None, "")
    return params.get("group_id") not in (None, "") or params.get("user_id") not in (None, "")


class _WebSocket:
    """The smallest WebSocket client that can hold a OneBot conversation.

    Blocking, single-socket, no event loop: the frontend runs this in one thread and
    talks to it through a lock, which is easier to reason about than an async stack for
    a debugging tool, and keeps the dependency list empty.
    """

    def __init__(self, url: str, headers: Mapping[str, str], *, timeout: float = 10.0) -> None:
        self.url = url
        self.headers = dict(headers)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._buffer = b""
        self._fragments: list[bytes] = []
        self._fragment_opcode: int | None = None

    # ------------------------------------------------------------------ connect

    def connect(self) -> None:
        """Open the TCP connection and complete the WebSocket upgrade."""
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "http"):
            raise OneBotError(f"unsupported scheme for {self.url!r}; expected ws://")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/ws"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        key = base64.b64encode(os.urandom(16)).decode()
        request = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        request.extend(f"{name}: {value}" for name, value in self.headers.items())
        raw = ("\r\n".join(request) + "\r\n\r\n").encode()

        sock = socket.create_connection((host, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        sock.sendall(raw)
        response = self._read_handshake(sock)
        status = response.split("\r\n", 1)[0]
        if "101" not in status:
            sock.close()
            raise OneBotError(f"handshake refused: {status}")
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if expected.lower() not in response.lower():
            sock.close()
            raise OneBotError("handshake accepted without a matching Sec-WebSocket-Accept")
        self._sock = sock
        self._buffer = b""

    @staticmethod
    def _read_handshake(sock: socket.socket) -> str:
        """Read the HTTP response headers, leaving any trailing bytes unread."""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8", errors="replace")

    def close(self) -> None:
        """Send a close frame (best effort) and drop the socket."""
        sock = self._sock
        self._sock = None
        if sock is None:
            return
        try:
            with self._send_lock:
                sock.sendall(self._frame(_OP_CLOSE, b""))
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def connected(self) -> bool:
        """Whether a socket is currently held."""
        return self._sock is not None

    # -------------------------------------------------------------------- frames

    @staticmethod
    def _frame(opcode: int, payload: bytes) -> bytes:
        """Build a masked client frame."""
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        return bytes(header) + mask + masked

    def send_text(self, text: str) -> None:
        """Send one text frame."""
        sock = self._sock
        if sock is None:
            raise OneBotError("not connected")
        with self._send_lock:
            sock.sendall(self._frame(_OP_TEXT, text.encode("utf-8")))

    def send_json(self, payload: Mapping[str, Any]) -> None:
        """Send one JSON text frame."""
        self.send_text(json.dumps(payload, ensure_ascii=False))

    def receive(self, timeout: float | None = None) -> tuple[int, bytes] | None:
        """Return the next data frame as ``(opcode, payload)``, or ``None`` on idle.

        Control frames are handled here: a ping is answered immediately and a close is
        acknowledged and surfaced, so callers only ever see text/binary payloads, an
        idle ``None``, or an exception when the peer went away.

        An idle read is *not* an error - a reverse-WebSocket link is silent whenever
        AstrBot has nothing to say - so a timeout returns ``None`` instead of raising.
        Partial reads are the reason the fragment accumulator lives on the instance:
        a frame that straddles two timeouts must survive the gap, or the link would
        corrupt the next message.
        """
        sock = self._sock
        if sock is None:
            raise OneBotError("not connected")
        sock.settimeout(self.timeout if timeout is None else timeout)
        while True:
            try:
                opcode, payload, final = self._read_frame(sock)
            except socket.timeout:
                return None
            if opcode == _OP_PING:
                with self._send_lock:
                    sock.sendall(self._frame(_OP_PONG, payload))
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                with self._send_lock:
                    sock.sendall(self._frame(_OP_CLOSE, payload[:2]))
                raise OneBotError("peer closed the connection")
            if opcode == _OP_CONTINUATION:
                self._fragments.append(payload)
            else:
                self._fragments = [payload]
                self._fragment_opcode = opcode
            if final:
                complete = b"".join(self._fragments)
                self._fragments = []
                return self._fragment_opcode or _OP_TEXT, complete

    def _read_frame(self, sock: socket.socket) -> tuple[int, bytes, bool]:
        """Read exactly one frame from the socket."""
        header = self._read_exactly(sock, 2)
        first, second = header[0], header[1]
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exactly(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exactly(sock, 8))[0]
        mask = self._read_exactly(sock, 4) if masked else b""
        payload = self._read_exactly(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload, final

    def _read_exactly(self, sock: socket.socket, count: int) -> bytes:
        """Read exactly ``count`` bytes, buffering whatever arrives in between."""
        while len(self._buffer) < count:
            chunk = sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise OneBotError("connection closed while reading a frame")
            self._buffer += chunk
        head, self._buffer = self._buffer[:count], self._buffer[count:]
        return head


@dataclass
class OneBotCall:
    """One API call AstrBot made, with the answer we gave."""

    at: float
    action: str
    params: dict[str, Any]
    echo: Any
    status: str
    retcode: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "at": self.at,
            "action": self.action,
            "params": self.params,
            "echo": self.echo,
            "status": self.status,
            "retcode": self.retcode,
            "data": self.data,
        }


class OneBotFrontend:
    """A OneBot v11 client that can be driven and observed.

    Responsibilities, in the order they matter for debugging:

    1. keep the reverse-WebSocket link up (reconnecting with backoff);
    2. push user messages in, as ``post_type=message`` events;
    3. answer **every** API call AstrBot makes, recording it (an unanswered call stalls
       the host, which is exactly the failure this tool exists to make visible);
    4. keep a transcript of both directions so a test can assert on it.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        token: str = "",
        self_id: str = "10001",
        user_id: str = "20001",
        group_id: str = "30001",
        role: str = "Universal",
        connect_timeout: float = 10.0,
        reconnect_interval: float = 3.0,
        on_frame: Callable[[FrameRecord], None] | None = None,
    ) -> None:
        """Store the link settings; nothing is opened until :meth:`start`.

        ``reconnect_interval`` follows the spec's ``ws_reverse.reconnect_interval``
        (3 s). This client doubles it on every consecutive failure up to 15 s, which is
        the spec's interval as a floor rather than a fixed rhythm: a debug frontend that
        hammers a stopped AstrBot every three seconds just fills the log.
        """
        self.ws_url = ws_url
        self.token = token
        self.self_id = str(self_id)
        self.user_id = str(user_id)
        self.group_id = str(group_id)
        self.role = role
        self.connect_timeout = connect_timeout
        self.reconnect_interval = float(reconnect_interval)
        self.on_frame = on_frame

        self.transcript: list[dict[str, Any]] = []
        self.frames: list[FrameRecord] = []
        self.calls: list[OneBotCall] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._lock = threading.RLock()
        self._socket: _WebSocket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._message_seq = 0
        self.connected = False
        self.connected_at: float | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the background reader thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="onebot-frontend", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Ask the reader loop to finish and close the socket."""
        self._stop.set()
        socket_holder = self._socket
        if socket_holder is not None:
            socket_holder.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self.connected = False

    def wait_connected(self, *, timeout: float = 10.0) -> bool:
        """Block until the link is up, or the timeout expires.

        Callers that send immediately after :meth:`start` would otherwise race the
        connect (the first version of the CLI did exactly that and sent into a closed
        socket). Returns whether the link came up.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.connected:
                return True
            time.sleep(0.05)
        return self.connected

    def _run(self) -> None:
        """Connect, read frames, reconnect with backoff until stopped."""
        backoff = self.reconnect_interval
        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = self.reconnect_interval
                self._read_loop()
            except Exception as exc:  # noqa: BLE001 - a debug tool must not die on a blip
                if self._stop.is_set():
                    break
                self.connected = False
                self._note_error(f"{type(exc).__name__}: {exc}")
                LOGGER.warning("onebot link down (%s); retrying in %.1fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)

    def _connect_once(self) -> None:
        """Open one connection and announce it in the transcript."""
        headers = {
            "X-Client-Role": self.role,
            "X-Self-ID": self.self_id,
            "User-Agent": "cf-onebot/1.0 (xiaojiujiu test frontend)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        socket_holder = _WebSocket(self.ws_url, headers, timeout=self.connect_timeout)
        socket_holder.connect()
        with self._lock:
            self._socket = socket_holder
            self.connected = True
            self.connected_at = time.time()
        self._record("control", "connected", {"url": self.ws_url, "self_id": self.self_id})
        LOGGER.info("onebot connected to %s as %s", self.ws_url, self.self_id)
        # Announce the lifecycle the moment the socket is up, exactly as a real client
        # does. Measured against AstrBot 4.28.1's reverse-WS server: a client that stays
        # silent after the upgrade completes the handshake and then gets dropped again,
        # and the host never logs "适配器已连接" - so the platform is never registered and
        # nothing is ever delivered. `send_meta_event` existed but was only reachable
        # from the CLI and the service HTTP endpoint, i.e. never on the reconnect path.
        self.send_meta_event("connect")

    def _read_loop(self) -> None:
        """Read frames until the peer goes away."""
        while not self._stop.is_set():
            socket_holder = self._socket
            if socket_holder is None:
                return
            frame = socket_holder.receive(timeout=1.0)
            if frame is None:
                continue
            opcode, payload = frame
            if opcode not in (_OP_TEXT, _OP_BINARY):
                continue
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._note_error(f"non-JSON frame: {payload[:120]!r}")
                continue
            if isinstance(decoded, dict) and "action" in decoded:
                self._handle_api_call(decoded)
            else:
                self._record("meta", "inbound", decoded)

    # ------------------------------------------------------------------- events

    def send_user_message(self, text: str, *, group: bool = False, user_id: str | None = None) -> dict[str, Any]:
        """Push one user message into AstrBot and return the event that was sent."""
        with self._lock:
            self._message_seq += 1
            message_id = 1000 + self._message_seq
            target_user = str(user_id or self.user_id)
            event: dict[str, Any] = {
                "post_type": "message",
                "message_type": "group" if group else "private",
                "sub_type": "normal",
                "message_id": message_id,
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
                "font": 0,
                "self_id": int(self.self_id) if self.self_id.isdigit() else self.self_id,
                "user_id": int(target_user) if target_user.isdigit() else target_user,
                "time": int(time.time()),
                "sender": {
                    "user_id": int(target_user) if target_user.isdigit() else target_user,
                    "nickname": "测试用户",
                    "card": "",
                    "role": "member",
                },
            }
            if group:
                group_value = int(self.group_id) if self.group_id.isdigit() else self.group_id
                event["group_id"] = group_value
                event["sender"]["role"] = "member"
                event["anonymous"] = None
            self._send(event, kind="event")
            self.transcript.append(
                {
                    "at": time.time(),
                    "kind": "user",
                    "session": f"group:{self.group_id}" if group else f"private:{target_user}",
                    "text": text,
                    "message_id": message_id,
                }
            )
            return event

    def send_meta_event(self, sub_type: str = "connect") -> dict[str, Any]:
        """Push a lifecycle meta event (what a real client sends on connect)."""
        event = {
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": sub_type,
            "time": int(time.time()),
            "self_id": int(self.self_id) if self.self_id.isdigit() else self.self_id,
        }
        self._send(event, kind="event")
        return event

    def _send(self, payload: Mapping[str, Any], *, kind: str) -> None:
        """Send one JSON frame, recording it and surfacing a dead link."""
        socket_holder = self._socket
        if socket_holder is None or not socket_holder.connected:
            raise OneBotError("not connected to AstrBot")
        socket_holder.send_json(payload)
        self._record(kind, "out", payload)

    # --------------------------------------------------------------- api replies

    #: Actions answered with real data. Everything else still gets a reply (1404), so a
    #: missing implementation shows up as a logged "unsupported" rather than a hang.
    def _api_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        """Return the supported OneBot actions."""
        return {
            "get_login_info": lambda _params: {
                "user_id": int(self.self_id) if self.self_id.isdigit() else self.self_id,
                "nickname": "测试机器人",
            },
            "get_status": lambda _params: {"online": True, "good": True},
            "get_version_info": lambda _params: {
                "app_name": "cf-onebot",
                "app_version": "1.0",
                "protocol_version": "v11",
            },
            "can_send_image": lambda _params: {"yes": True},
            "can_send_record": lambda _params: {"yes": True},
            "get_friend_list": lambda _params: [
                {"user_id": int(self.user_id) if self.user_id.isdigit() else self.user_id,
                 "nickname": "测试用户"}
            ],
            "get_group_list": lambda _params: [
                {"group_id": int(self.group_id) if self.group_id.isdigit() else self.group_id,
                 "group_name": "测试群"}
            ],
            "get_group_member_info": lambda params: {
                "user_id": params.get("user_id"),
                "nickname": "测试用户",
                "role": "member",
            },
            "get_stranger_info": lambda params: {"user_id": params.get("user_id"), "nickname": "测试用户"},
        }

    def _handle_api_call(self, payload: Mapping[str, Any]) -> None:
        """Answer one API call, always, and record it.

        The response envelope is the OneBot 11 one - ``status``/``retcode``/``data``/
        ``echo`` - and the codes are the CQHTTP convention the ecosystem agreed on:
        ``0`` ok, ``1400`` bad request (a parameter the action requires is missing),
        ``1404`` no such action, ``1500`` internal error. An unanswered call blocks the
        host, so every branch below ends in exactly one reply.
        """
        action = str(payload.get("action") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        echo = payload.get("echo")
        handler = self._api_handlers().get(action)
        is_send = action in _SEND_ACTIONS
        status, retcode, data = "ok", 0, {}

        if is_send and not _has_send_target(params, action):
            # A send without a destination is the caller's error, not our bug.
            status, retcode = "failed", 1400
            data = {"error": f"{action} requires user_id or group_id"}
        elif is_send:
            # Sending is the one thing this frontend exists for, so it is handled here
            # rather than through the handler table: the text becomes the transcript.
            text = _extract_text(params.get("message"))
            message_id = 9000 + len(self.sent_messages)
            data = {"message_id": message_id}
            record = {
                "at": time.time(),
                "kind": "bot",
                "action": action,
                "session": _describe_session(params, self.self_id),
                "text": text,
                "message_id": message_id,
            }
            with self._lock:
                self.sent_messages.append(record)
                self.transcript.append(record)
        elif handler is None:
            status, retcode = "failed", 1404
            LOGGER.info("onebot action %s is not implemented; answered 1404", action)
        else:
            try:
                data = handler(dict(params))
            except Exception as exc:  # noqa: BLE001 - never leave a call unanswered
                status, retcode = "failed", 1500
                data = {"error": f"{type(exc).__name__}: {exc}"}
                self._note_error(f"{action} handler failed: {exc}")

        call = OneBotCall(
            at=time.time(), action=action, params=dict(params), echo=echo,
            status=status, retcode=retcode, data=data,
        )
        with self._lock:
            self.calls.append(call)
        self._record("api", "in", payload)
        self._send({"status": status, "retcode": retcode, "data": data, "echo": echo}, kind="response")

    # ------------------------------------------------------------------ recording

    def _record(self, kind: str, direction: str, payload: Any) -> None:
        """Append one frame to the debug log."""
        frame = FrameRecord(at=time.time(), direction=direction, kind=kind, payload=payload)
        with self._lock:
            self.frames.append(frame)
            if len(self.frames) > 5000:
                del self.frames[:1000]
        if self.on_frame is not None:
            try:
                self.on_frame(frame)
            except Exception:  # noqa: BLE001 - a bad observer must not break the link
                LOGGER.warning("frame observer failed", exc_info=True)

    def _note_error(self, message: str) -> None:
        """Record an error both in the transcript and on stderr."""
        with self._lock:
            self.errors.append(message)
        LOGGER.warning("onebot: %s", message)

    # -------------------------------------------------------------------- reading

    def snapshot(self, *, since: int = 0) -> dict[str, Any]:
        """Return the observable state, optionally only transcript entries after ``since``."""
        with self._lock:
            return {
                "connected": self.connected,
                "ws_url": self.ws_url,
                "self_id": self.self_id,
                "counts": {
                    "transcript": len(self.transcript),
                    "calls": len(self.calls),
                    "sent": len(self.sent_messages),
                    "errors": len(self.errors),
                },
                "transcript": self.transcript[since:],
                "calls": [call.to_dict() for call in self.calls[-50:]],
                "errors": self.errors[-20:],
            }

    def frame_log(self, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Return raw frames (both directions) for protocol-level debugging."""
        with self._lock:
            return [frame.to_dict() for frame in self.frames[since:][:limit]]


def _extract_text(message: Any) -> str:
    """Flatten a OneBot message (array or CQ string) into plain text."""
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping):
        message = [message]
    if not isinstance(message, Sequence):
        return ""
    parts: list[str] = []
    for segment in message:
        if isinstance(segment, Mapping):
            data = segment.get("data")
            if isinstance(data, Mapping) and "text" in data:
                parts.append(str(data["text"]))
            elif segment.get("type") == "image":
                parts.append("[图片]")
        else:
            parts.append(str(segment))
    return "".join(parts)


def _describe_session(params: Mapping[str, Any], self_id: str) -> str:
    """Return a human-readable session for one outbound send."""
    group = params.get("group_id")
    if group:
        return f"group:{group}"
    user = params.get("user_id")
    return f"private:{user if user is not None else self_id}"


def iter_transcript_text(entries: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return just the readable lines of a transcript (for humans and assertions)."""
    lines: list[str] = []
    for entry in entries:
        kind = entry.get("kind")
        if kind == "user":
            lines.append(f"我: {entry.get('text')}")
        elif kind == "bot":
            lines.append(f"TA: {entry.get('text')}")
        else:
            lines.append(f"[{kind}] {entry}")
    return lines
