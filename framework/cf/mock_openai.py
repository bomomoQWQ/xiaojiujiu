"""A standard OpenAI-compatible endpoint that feeds the Runtime's strong semantics.

The program's only generative port is ``SemanticProvider``; its one non-disabled
implementation, ``RemoteAPIProvider``, talks to *any* OpenAI-compatible
``/v1/chat/completions`` endpoint. The program therefore already knows how to
consume this server -- pointing it here needs nothing but three environment
variables, and the framework never edits the program.

What the endpoint has to answer
-------------------------------
The program sends two different prompts down the same wire, distinguished only
by their system message:

* **deep refresh** -- asks for a JSON object with six suggestion collections.
  Five are arrays of operations that must cite ``sources`` (real ``event_id``
  values from the request) or the Runtime's grounding check discards them; one
  (``psychological_interpretation``) is a single object and needs no sources.
* **explain state** -- asks for a flat JSON object of six short first-person
  strings: ``experience, focus, conflict, impulse, inhibition, expression``,
  each at most 120 characters, at least one non-empty.

The default reply is *grounded*: it reads the variables out of the request the
program just sent (unresolved event ids, mood, pressure, candidates) and answers
using them, so the suggestions actually survive grounding and reach the reducer.
A mock that always returns an empty object would make the whole strong-semantics
path untestable -- it would look wired up while doing nothing.

Scripting and fault injection are deliberate: a test framework whose fake
endpoint can only succeed cannot test the failure paths that matter (timeout,
malformed JSON, HTTP 500), and the provider contract says every one of those
must degrade to "no strong semantics" rather than break the Runtime.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from .logbook import Logbook

LOGGER = logging.getLogger("cf.mock_openai")

#: Markers that identify which prompt the program sent. Both prompts are Chinese
#: and stable; matching on a distinctive noun is more robust than matching the
#: whole string, which is allowed to be reworded.
DEEP_REFRESH_MARKER = "深层认知整理器"
EXPLAIN_MARKER = "情绪解释器"

KIND_DEEP_REFRESH = "deep_refresh"
KIND_EXPLAIN = "explain_state"
KIND_UNKNOWN = "unknown"

#: The six fields of the deep-refresh contract, in the order the program lists them.
DEEP_REFRESH_FIELDS = (
    "reinterpretations",
    "psychological_interpretation",
    "candidate_intent_operations",
    "memory_suggestions",
    "unfinished_matter_suggestions",
    "user_model_evidence_suggestions",
)

#: The six fields of the explanation contract.
EXPLANATION_FIELDS = ("experience", "focus", "conflict", "impulse", "inhibition", "expression")


def classify_prompt(system_prompt: str) -> str:
    """Return which contract a system prompt belongs to.

    Args:
        system_prompt: The ``system`` message content.

    Returns:
        One of :data:`KIND_DEEP_REFRESH`, :data:`KIND_EXPLAIN`, :data:`KIND_UNKNOWN`.
    """
    if DEEP_REFRESH_MARKER in system_prompt:
        return KIND_DEEP_REFRESH
    if EXPLAIN_MARKER in system_prompt:
        return KIND_EXPLAIN
    return KIND_UNKNOWN


def _mood_line(mood: Mapping[str, Any]) -> str:
    """Turn the request's mood numbers into one first-person sentence.

    The point is not literary quality: it is that a human reading the log can see
    at a glance that the reply was derived from the variables the program sent,
    rather than being a constant.
    """
    valence = float(mood.get("valence") or 0.0)
    pressure = float(mood.get("pressure") or 0.0)
    restraint = float(mood.get("restraint") or 0.0)
    if pressure >= 0.6 and restraint < 0.4:
        return "我有话憋着，压不太住了，想现在就开口"
    if pressure >= 0.6:
        return "想说点什么，但还是先按住了自己"
    if valence <= -0.3:
        return "心里有点沉，还没缓过来"
    if valence >= 0.3:
        return "心情是松的，愿意多聊两句"
    return "没什么起伏，安静地待着"


def build_deep_refresh_payload(request_body: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a grounded suggestion set from the request the program just sent.

    Every emitted operation cites a real ``event_id`` taken from
    ``unresolved_events``; an operation that cited anything else would be thrown
    away by the Runtime's grounding pass and would prove nothing.

    Args:
        request_body: The decoded ``DeepRefreshRequest`` the program sent.

    Returns:
        A payload with all six contract fields present.
    """
    unresolved = [item for item in (request_body.get("unresolved_events") or []) if isinstance(item, Mapping)]
    event_ids = [str(item.get("event_id")) for item in unresolved if item.get("event_id")]
    mood = request_body.get("mood") if isinstance(request_body.get("mood"), Mapping) else {}
    candidates = request_body.get("candidates") or []
    unfinished = request_body.get("unfinished") or []

    payload: dict[str, Any] = {name: [] for name in DEEP_REFRESH_FIELDS}
    payload["psychological_interpretation"] = {
        "experience": _mood_line(mood),
        "focus": f"手上有 {len(unfinished)} 件没结清的事" if unfinished else "眼下没有压着的事",
        "conflict": "想靠近，又怕打扰" if float(mood.get("restraint") or 0) >= 0.5 else "没有明显拉扯",
        "impulse": "想把没弄明白的事想明白",
        "inhibition": "证据不够就先不下结论",
        "expression": "等一个自然的时机再说",
    }

    # One reinterpretation per unresolved event, capped so a big backlog does not
    # produce a hundred-line reply. The text names the event id, which makes the
    # log traceable end to end: request -> suggestion -> applied version.
    for event_id in event_ids[:2]:
        payload["reinterpretations"].append(
            {
                "sources": [event_id],
                "payload": {
                    "content": f"这件事（{event_id}）当时没看懂，现在看更像是对方在等一个回应。",
                    "confidence": 0.55,
                },
            }
        )

    # A candidate intent only when the pool is empty, so repeated refreshes do not
    # pile up duplicates -- the mock should be idempotent-ish for a calm scene.
    #
    # The shape is exact and easy to get wrong, which is why it is spelled out:
    # the operation key is ``op`` (not ``operation``), and ``sources`` must appear
    # BOTH on the operation (the Runtime's grounding pass reads it there) and
    # inside ``candidate`` (``validate_candidate`` rejects a candidate whose own
    # ``sources`` is empty as "a thought that came from nowhere"). The allowed
    # ``op`` values are add / update / retire / reinterpret, and the allowed
    # ``type`` values are contact, check_in, follow_up, curious_question, share,
    # repair, reply.
    if not candidates and event_ids:
        payload["candidate_intent_operations"].append(
            {
                "sources": [event_ids[0]],
                "payload": {
                    "op": "add",
                    "candidate": {
                        "type": "follow_up",
                        "intent": "把上次没说完的那件事接着问清楚",
                        "goal": "不让话题就这么断掉",
                        "sources": [event_ids[0]],
                        "confidence": 0.5,
                        "internal_need": 0.5,
                        "unfinished_relevance": 0.6,
                    },
                },
            }
        )
    return payload


def build_explanation_payload(request_body: Mapping[str, Any]) -> dict[str, str]:
    """Derive the six explanation strings from the structured state.

    Args:
        request_body: The decoded psychological-state payload the program sent.

    Returns:
        A mapping with all six explanation fields, each a short string.
    """
    mood = request_body.get("mood") if isinstance(request_body.get("mood"), Mapping) else {}
    line = _mood_line(mood)
    return {
        "experience": line,
        "focus": "注意力停在还没结清的那件事上",
        "conflict": "想开口又觉得时机不对",
        "impulse": "想主动一点",
        "inhibition": "怕打扰对方",
        "expression": "先安静等着",
    }


@dataclass
class MockReply:
    """One scripted response.

    Args:
        status: HTTP status to return.
        payload: JSON object to serialise into the assistant message. ``None``
            means "derive a grounded reply from the request".
        content: Raw assistant text, overriding ``payload``. Use this to send
            malformed JSON on purpose.
        delay_s: Sleep before answering, to exercise the provider's timeout path.
        close: Close the connection without a response.
    """

    status: int = 200
    payload: dict[str, Any] | None = None
    content: str | None = None
    delay_s: float = 0.0
    close: bool = False

    @classmethod
    def http_error(cls, status: int) -> MockReply:
        """Return a reply that fails with an HTTP status."""
        return cls(status=status, content="")

    @classmethod
    def malformed(cls, text: str = "这不是 JSON，只是一句话。") -> MockReply:
        """Return a reply whose content is not JSON."""
        return cls(content=text)

    @classmethod
    def timeout(cls, delay_s: float = 60.0) -> MockReply:
        """Return a reply that stalls past any sane deadline."""
        return cls(delay_s=delay_s)


@dataclass
class MockScript:
    """Decides what the Nth call to the endpoint returns.

    With no replies configured the endpoint answers every call with a grounded
    reply. With replies configured they are consumed in order; once exhausted,
    the last one repeats unless ``repeat_last`` is false, in which case the
    endpoint falls back to grounded replies.

    Args:
        replies: Scripted responses, consumed in order.
        repeat_last: Repeat the final scripted reply forever once reached.
    """

    replies: list[MockReply] = field(default_factory=list)
    repeat_last: bool = True

    def reply_for(self, index: int) -> MockReply | None:
        """Return the scripted reply for call ``index`` (0-based), if any."""
        if not self.replies:
            return None
        if index < len(self.replies):
            return self.replies[index]
        if self.repeat_last:
            return self.replies[-1]
        return None

    def append(self, reply: MockReply) -> None:
        """Add one scripted reply to the end of the script."""
        self.replies.append(reply)

    def clear(self) -> None:
        """Drop every scripted reply, returning to grounded mode."""
        self.replies.clear()


class MockOpenAIServer:
    """A threaded OpenAI-compatible endpoint, for the Runtime to consume.

    Args:
        logbook: Where to record every request and reply. Optional: the server is
            usable without one, it just stops being observable.
        host: Bind address.
        port: Bind port; 0 asks the OS for a free one.
        script: Initial script. Mutate ``server.script`` while running to change
            behaviour mid-test.
        model_name: Model name advertised by ``/v1/models`` and echoed in replies.
        require_auth: When true, requests without a bearer token get 401. The
            default is false, because the point is to be easy to point the
            program at; the program still insists on *having* a key configured
            before it will call at all.
    """

    def __init__(
        self,
        logbook: Logbook | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        script: MockScript | None = None,
        model_name: str = "framework-mock",
        require_auth: bool = False,
    ) -> None:
        """Store the configuration; call :meth:`start` to bind."""
        self.logbook = logbook
        self.host = host
        self.port = port
        self.script = script or MockScript()
        self.model_name = model_name
        self.require_auth = require_auth
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._calls = 0
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------- life

    def start(self) -> str:
        """Bind and serve in a background thread.

        Returns:
            The base URL to hand to the program (``http://host:port/v1``).
        """
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="cf-mock-openai", daemon=True)
        self._thread.start()
        base_url = self.base_url
        if self.logbook is not None:
            self.logbook.event("mock_openai_start", {"base_url": base_url, "model": self.model_name})
        return base_url

    def stop(self, timeout: float = 5.0) -> None:
        """Stop serving and release the port."""
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=timeout)
        if self.logbook is not None:
            self.logbook.event("mock_openai_stop", {"calls": self._calls})

    @property
    def base_url(self) -> str:
        """The OpenAI-style base URL, including the ``/v1`` suffix."""
        return f"http://{self.host}:{self.port}/v1"

    @property
    def calls_made(self) -> int:
        """How many completion requests have been answered."""
        with self._lock:
            return self._calls

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of what the endpoint has served."""
        with self._lock:
            kinds: dict[str, int] = {}
            for call in self.calls:
                kinds[call.get("prompt_kind", KIND_UNKNOWN)] = kinds.get(call.get("prompt_kind", KIND_UNKNOWN), 0) + 1
            return {"calls": self._calls, "by_kind": kinds, "base_url": self.base_url}

    # ---------------------------------------------------------------- handling

    def handle_completion(self, body: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Answer one ``/v1/chat/completions`` request.

        Args:
            body: Decoded request body.
            headers: Request headers. The bearer token is never recorded, only
                whether one was present.

        Returns:
            ``(status, response_body, trace_fields)``.
        """
        with self._lock:
            index = self._calls
            self._calls += 1

        messages = body.get("messages") or []
        system_prompt = ""
        user_content = ""
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            if message.get("role") == "system":
                system_prompt = str(message.get("content") or "")
            elif message.get("role") == "user":
                user_content = str(message.get("content") or "")
        kind = classify_prompt(system_prompt)

        request_payload: Any = None
        try:
            request_payload = json.loads(user_content) if user_content else None
        except json.JSONDecodeError:
            request_payload = None

        scripted = self.script.reply_for(index)
        if scripted is not None and scripted.delay_s:
            time.sleep(scripted.delay_s)

        status = scripted.status if scripted is not None else 200
        if scripted is not None and scripted.content is not None:
            content = scripted.content
        else:
            payload = scripted.payload if scripted is not None and scripted.payload is not None else None
            if payload is None:
                payload = self._grounded_payload(kind, request_payload)
            content = json.dumps(payload, ensure_ascii=False)

        response = {
            "id": f"chatcmpl-mock-{index}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or self.model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(user_content) // 4,
                "completion_tokens": len(content) // 4,
                "total_tokens": (len(user_content) + len(content)) // 4,
            },
        }

        trace = {
            "mock_call": index,
            # Named ``prompt_kind`` rather than ``kind`` on purpose: the trace
            # record's own ``kind`` is its routing field, and letting the payload
            # share the name would shadow it.
            "prompt_kind": kind,
            "model": body.get("model") or "",
            "auth_present": bool(headers.get("Authorization")),
            "scripted": scripted is not None,
            "status": status,
            "request": request_payload,
            "reply": content if scripted is not None and scripted.content is not None else json.loads(content),
        }
        return status, response, trace

    def _grounded_payload(self, kind: str, request_payload: Any) -> dict[str, Any]:
        """Build the default reply for a request, according to its contract."""
        body = request_payload if isinstance(request_payload, Mapping) else {}
        if kind == KIND_DEEP_REFRESH:
            return build_deep_refresh_payload(body)
        if kind == KIND_EXPLAIN:
            return build_explanation_payload(body)
        return {}

    def record(self, trace: dict[str, Any], error: str = "") -> None:
        """Store and log one served request."""
        with self._lock:
            self.calls.append(trace)
        if error:
            trace = {**trace, "error": error}
        if self.logbook is not None:
            self.logbook.event("mock_openai_call", trace)


def _make_handler(server: MockOpenAIServer) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to ``server``."""

    class Handler(BaseHTTPRequestHandler):
        """Minimal OpenAI-compatible surface."""

        protocol_version = "HTTP/1.1"
        server_version = "cf-mock-openai/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib signature
            """Silence the default stderr access log; the logbook is the record."""
            LOGGER.debug("mock http: " + fmt, *args)

        def _send_json(self, status: int, payload: Any) -> None:
            """Write one JSON response."""
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            """Serve ``/v1/models`` and a plain health probe."""
            path = self.path.split("?", 1)[0].rstrip("/")
            if path.endswith("/models"):
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": server.model_name,
                                "object": "model",
                                "created": 0,
                                "owned_by": "framework",
                            }
                        ],
                    },
                )
                return
            if path.endswith("/health"):
                self._send_json(200, {"ok": True, **server.stats()})
                return
            self._send_json(404, {"error": {"message": f"no such path {self.path}", "type": "invalid_request_error"}})

        def do_POST(self) -> None:  # noqa: N802 - stdlib signature
            """Serve ``/v1/chat/completions``."""
            path = self.path.split("?", 1)[0].rstrip("/")
            if not path.endswith("/chat/completions"):
                self._send_json(404, {"error": {"message": f"no such path {self.path}", "type": "invalid_request_error"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                server.record({"prompt_kind": KIND_UNKNOWN, "error": "unparsable_request_body"})
                self._send_json(400, {"error": {"message": "body is not JSON", "type": "invalid_request_error"}})
                return

            headers = {key: value for key, value in self.headers.items()}
            if server.require_auth and not headers.get("Authorization"):
                server.record({"prompt_kind": classify_prompt(_system_of(body)), "error": "missing_bearer"})
                self._send_json(401, {"error": {"message": "missing bearer token", "type": "invalid_request_error"}})
                return

            status, response, trace = server.handle_completion(body, headers)
            server.record(trace)
            if status != 200:
                self._send_json(status, {"error": {"message": f"scripted failure {status}", "type": "server_error"}})
                return
            self._send_json(200, response)

    return Handler


def _system_of(body: Mapping[str, Any]) -> str:
    """Return the system message of a request body, defensively."""
    for message in body.get("messages") or []:
        if isinstance(message, Mapping) and message.get("role") == "system":
            return str(message.get("content") or "")
    return ""
