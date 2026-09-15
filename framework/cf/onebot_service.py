"""The debug surface around :class:`cf.onebot.OneBotFrontend`.

Two audiences, one process:

* a human wants a chat box and a live log - so there is a small HTML page and a
  ``/transcript`` view that reads like a conversation;
* an agent or a test wants to drive the thing and read its mind - so every route is
  JSON, every frame is addressable by index (``/frames?since=``), and the transcript can
  be fetched incrementally.

Standard library only (``http.server``), matching the rest of the framework: the whole
frontend container then needs no package installation at all.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .onebot import FrameRecord, OneBotError, OneBotFrontend, iter_transcript_text

LOGGER = logging.getLogger("cf.onebot.service")

INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>cf onebot · 测试前端</title>
<style>
 body{font:14px/1.6 system-ui,"Segoe UI",sans-serif;margin:0;display:flex;height:100vh}
 #left{flex:1;display:flex;flex-direction:column;border-right:1px solid #ddd}
 #log{flex:1;overflow:auto;padding:12px;background:#fafafa}
 #right{width:42%;overflow:auto;padding:12px;background:#111;color:#ddd;font:12px/1.5 ui-monospace,monospace}
 .me{color:#06c}.ta{color:#333;margin-bottom:6px}.meta{color:#999;font-size:12px}
 form{display:flex;gap:8px;padding:10px;border-top:1px solid #ddd}
 input[type=text]{flex:1;padding:8px}
 button{padding:8px 14px}
 select{padding:8px}
</style></head><body>
<div id="left">
  <div id="log"></div>
  <form onsubmit="send(event)">
    <select id="where"><option value="private">私聊</option><option value="group">群聊</option></select>
    <input type="text" id="text" placeholder="输入一句话，回车发送" autocomplete="off" autofocus>
    <button>发送</button>
  </form>
</div>
<div id="right"><div id="frames"></div></div>
<script>
let cursor = 0, frameCursor = 0;
async function send(e){
  e.preventDefault();
  const box = document.getElementById('text');
  const text = box.value.trim(); if(!text) return; box.value = '';
  await fetch('/send',{method:'POST',headers:{'content-type':'application/json'},
    body: JSON.stringify({text, group: document.getElementById('where').value === 'group'})});
  refresh();
}
function line(entry){
  const d = document.createElement('div');
  if(entry.kind === 'user'){ d.className='me'; d.textContent = '我: ' + entry.text; }
  else if(entry.kind === 'bot'){ d.className='ta'; d.textContent = 'TA: ' + entry.text; }
  else { d.className='meta'; d.textContent = '[' + entry.kind + '] ' + JSON.stringify(entry).slice(0,300); }
  return d;
}
async function refresh(){
  const state = await (await fetch('/state?since=' + cursor)).json();
  const log = document.getElementById('log');
  (state.transcript||[]).forEach(entry => log.appendChild(line(entry)));
  cursor += (state.transcript||[]).length;
  log.scrollTop = log.scrollHeight;
  const frames = await (await fetch('/frames?since=' + frameCursor + '&limit=100')).json();
  const box = document.getElementById('frames');
  (frames.frames||[]).forEach(f => {
    const d = document.createElement('div');
    const arrow = f.direction === 'in' ? '←' : '→';
    d.textContent = arrow + ' ' + f.kind + ' ' + JSON.stringify(f.payload).slice(0,400);
    box.appendChild(d);
  });
  frameCursor += (frames.frames||[]).length;
  box.scrollTop = box.scrollHeight;
}
setInterval(refresh, 700); refresh();
</script></body></html>
"""


class OneBotService:
    """HTTP control surface plus the link itself."""

    def __init__(
        self,
        frontend: OneBotFrontend,
        *,
        host: str = "127.0.0.1",
        port: int = 6300,
        log_path: str | Path | None = None,
    ) -> None:
        """Store the settings; nothing is bound until :meth:`start`."""
        self.frontend = frontend
        self.host = host
        self.port = int(port)
        self.log_path = Path(log_path) if log_path else None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._log_lock = threading.Lock()
        self._log_file = None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = self.log_path.open("a", encoding="utf-8")

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the WebSocket client and the HTTP surface."""
        self.frontend.on_frame = self._on_frame
        self.frontend.start()
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="cf-onebot-http", daemon=True)
        self._thread.start()
        LOGGER.info("control surface on http://%s:%s/", self.host, self.port)

    def stop(self) -> None:
        """Stop both loops and close the log."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.frontend.stop()
        with self._log_lock:
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None

    def _on_frame(self, frame: FrameRecord) -> None:
        """Persist every frame to the JSONL log, if one was requested."""
        self._append_log({"type": "frame", **frame.to_dict()})

    def _append_log(self, entry: Mapping[str, Any]) -> None:
        """Append one JSON line to the log file."""
        with self._log_lock:
            if self._log_file is None:
                return
            self._log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._log_file.flush()

    # -------------------------------------------------------------------- routing

    def handle(self, method: str, target: str, body: bytes) -> tuple[int, str, bytes]:
        """Answer one request; returns ``(status, content_type, payload)``.

        Kept free of HTTP objects so the whole surface can be tested by calling it
        directly, which is also how the tests drive it.
        """
        parsed = urlparse(target)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        try:
            if method == "GET" and path == "/":
                return 200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8")
            if method == "GET" and path == "/state":
                since = int((query.get("since") or ["0"])[0])
                return self._json(self.frontend.snapshot(since=since))
            if method == "GET" and path == "/frames":
                since = int((query.get("since") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
                return self._json({"frames": self.frontend.frame_log(since=since, limit=limit)})
            if method == "GET" and path == "/transcript":
                since = int((query.get("since") or ["0"])[0])
                entries = self.frontend.snapshot(since=since)["transcript"]
                text = "\n".join(iter_transcript_text(entries))
                return 200, "text/plain; charset=utf-8", text.encode("utf-8")
            if method == "POST" and path == "/send":
                data = json.loads(body or b"{}")
                text = str(data.get("text") or "").strip()
                if not text:
                    return self._json({"ok": False, "error": "text is required"}, status=400)
                event = self.frontend.send_user_message(
                    text, group=bool(data.get("group")), user_id=data.get("user_id")
                )
                entry = {"type": "user", "text": text, "event": event}
                self._append_log(entry)
                return self._json({"ok": True, "event": event})
            if method == "POST" and path == "/event":
                event = json.loads(body or b"{}")
                if not isinstance(event, dict) or "post_type" not in event:
                    return self._json({"ok": False, "error": "an OneBot event object is required"}, 400)
                self.frontend._send(event, kind="event")
                return self._json({"ok": True})
            if method == "POST" and path == "/meta":
                data = json.loads(body or b"{}")
                event = self.frontend.send_meta_event(str(data.get("sub_type") or "connect"))
                return self._json({"ok": True, "event": event})
            return self._json({"ok": False, "error": f"no route for {method} {path}"}, status=404)
        except OneBotError as exc:
            return self._json({"ok": False, "error": str(exc)}, status=503)
        except Exception as exc:  # noqa: BLE001 - a debug surface reports, never crashes
            LOGGER.exception("control route failed")
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    @staticmethod
    def _json(payload: Any, *, status: int = 200) -> tuple[int, str, bytes]:
        """Serialise a JSON response."""
        return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _make_handler(service: OneBotService) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to one service instance."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, content_type, payload = service.handle(method, self.path, body)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            LOGGER.debug("http %s", format % args)

    return Handler


def tail_transcript(service: OneBotService, *, seconds: float, interval: float = 0.5) -> list[str]:
    """Poll the transcript for ``seconds`` and return the readable lines it produced.

    A convenience for scripted debugging: "send this, then collect what the bot says
    for ten seconds" is the loop every manual test ends up writing.
    """
    deadline = time.time() + seconds
    seen = 0
    lines: list[str] = []
    while time.time() < deadline:
        entries = service.frontend.snapshot(since=seen)["transcript"]
        for line in iter_transcript_text(entries):
            lines.append(line)
        seen += len(entries)
        time.sleep(interval)
    return lines
