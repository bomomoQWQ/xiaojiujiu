"""A thin client for the program's public HTTP surface.

A test framework that can only *watch* a program cannot test it: something has
to put words in the user's mouth. This client covers the handful of calls a
scene needs -- say something, let time pass, force a decision round, ask for a
deep refresh -- and nothing else.

Every call goes over HTTP against the running program, so the framework keeps
the same "only what an operator could do" discipline as :mod:`cf.variables`.
Failures raise :class:`ProgramError` with the status and the program's own error
body, because a 422 from the program is nearly always a wrong request shape and
hiding it behind a generic exception makes that much harder to see.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Mapping

LOGGER = logging.getLogger("cf.program")

EVENT_TYPE_USER_MESSAGE = "user_message"


class ProgramError(Exception):
    """Raised when the program refuses or cannot answer a call."""


class ProgramClient:
    """Calls the program's HTTP API.

    Args:
        base_url: Root URL of the running program.
        timeout_s: Per-request deadline.
    """

    def __init__(self, base_url: str, *, timeout_s: float = 15.0) -> None:
        """Store the target and the deadline."""
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    # ---------------------------------------------------------------- transport

    def get(self, path: str, **params: Any) -> Any:
        """GET one path and decode the JSON body."""
        url = f"{self.base_url}{path}"
        if params:
            query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
            if query:
                url = f"{url}?{query}"
        return self._request(url, None)

    def post(self, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        """POST one path and decode the JSON body."""
        return self._request(f"{self.base_url}{path}", dict(payload or {}))

    def _request(self, url: str, payload: dict[str, Any] | None) -> Any:
        """Perform one request, converting HTTP errors into :class:`ProgramError`."""
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310 - loopback only
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise ProgramError(f"{request.method} {url} -> HTTP {exc.code}: {detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise ProgramError(f"{request.method} {url} -> unreachable: {exc.reason}") from exc
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise ProgramError(f"{request.method} {url} -> undecodable body: {body[:200]!r}") from exc

    # ------------------------------------------------------------------- calls

    def health(self) -> dict[str, Any]:
        """Return ``/health``."""
        return dict(self.get("/health"))

    def state(self) -> dict[str, Any]:
        """Return the current runtime projection."""
        return dict(self.get("/state"))

    def say(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one user message, running the program's full foreground path.

        Args:
            text: What the user said.
            conversation_id: Which chat it happened in.
            event_id: Supply one to make the call idempotent; a repeat returns
                ``duplicate: true`` instead of appending twice.
            timestamp: The moment it happened, on the caller's clock.
            metadata: Extra fields recorded with the event.

        Returns:
            The program's response, including the settlement outcome.
        """
        payload: dict[str, Any] = {"event_type": EVENT_TYPE_USER_MESSAGE, "actor": "user", "content": text}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if event_id:
            payload["event_id"] = event_id
        if timestamp:
            payload["timestamp"] = timestamp
        if metadata:
            payload["metadata"] = dict(metadata)
        return dict(self.post("/events", payload))

    def append_event(
        self,
        event_type: str,
        *,
        actor: str = "user",
        content: str | None = None,
        conversation_id: str | None = None,
        timestamp: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append any other raw event verbatim."""
        payload: dict[str, Any] = {"event_type": event_type, "actor": actor}
        if content is not None:
            payload["content"] = content
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if timestamp:
            payload["timestamp"] = timestamp
        if metadata:
            payload["metadata"] = dict(metadata)
        return dict(self.post("/events", payload))

    def tick(self, now: str | None = None) -> dict[str, Any]:
        """Run ``lazy_tick`` explicitly, optionally at a supplied moment."""
        return dict(self.post("/tick", {"now": now} if now else {}))

    def endogenous(self, *, now: str | None = None, force: bool = False) -> dict[str, Any]:
        """Run one endogenous decision round."""
        payload: dict[str, Any] = {"force": force}
        if now:
            payload["now"] = now
        return dict(self.post("/endogenous", payload))

    def refresh(self, *, now: str | None = None, force: bool = False, **signals: Any) -> dict[str, Any]:
        """Ask for a deep cognitive refresh -- the call that uses the mock endpoint.

        The trigger signals go at the **top level** of the body, not nested under
        a ``trigger_context`` key. ``trigger_context`` is the name of the
        *Python* keyword argument :meth:`Runtime.deep_refresh` takes; the HTTP
        endpoint reads ``payload["major_event"]`` and friends directly. Nesting
        them is accepted without complaint and silently ignored, which turns a
        deliberate "a major event happened" into "not_needed" -- so this client
        flattens them and names the parameter ``signals`` to match the wire.

        Args:
            now: Reference moment.
            force: Skip the trigger check entirely. A refresh still declines when
                no provider is configured.
            **signals: Any of ``major_event``, ``matter_due``,
                ``candidate_pool_size``, ``wants_proactive``,
                ``proactive_grounded``, ``history_suspect``,
                ``user_evidence_overturns``, ``hours_since_last_refresh``.

        Returns:
            The refresh outcome. ``ran: false`` is normal operation, not an error.
        """
        payload: dict[str, Any] = {}
        if now:
            payload["now"] = now
        if force:
            payload["force"] = True
        payload.update(signals)
        return dict(self.post("/cognition/refresh", payload))

    def backlog(self) -> dict[str, Any]:
        """Return the unresolved-event backlog."""
        return dict(self.get("/cognition/backlog"))

    def context(self, *, conversation_id: str | None = None, now: str | None = None) -> dict[str, Any]:
        """Return the psychological context block the host would inject."""
        payload: dict[str, Any] = {}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if now:
            payload["now"] = now
        return dict(self.post("/context", payload))
