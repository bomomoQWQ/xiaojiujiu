"""aiohttp based Runtime transport (the adapter's HTTP client).

Failures always surface as :class:`RuntimeTransportError`; callers decide how to
degrade. Two rules keep this client safe to run inside AstrBot:

* response bodies are only echoed into debug logs, truncated, because Runtime
  responses can quote user content;
* the shared token is placed in a header and is never logged, echoed, or stored
  anywhere but the config file / environment.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from .protocol import (
    ActionReport,
    AuthorizeDecision,
    AuthorizeRequest,
    ContextRequest,
    ContextSnapshot,
    LeaseHeartbeat,
    LeaseRequest,
    LeasedAction,
    truncate_error,
)
from .retry_queue import NULL_LOG
from .settings import Settings

try:  # pragma: no cover - exercised through the (disabled) fallback path
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

#: Runtime HTTP API paths. See README.md for the full protocol description.
EVENTS_PATH = "/v1/events"
CONTEXT_PATH = "/v1/context"
OUTBOX_LEASE_PATH = "/v1/outbox/lease"
OUTBOX_HEARTBEAT_PATH = "/v1/outbox/{action_id}/heartbeat"
OUTBOX_RESULT_PATH = "/v1/outbox/{action_id}/result"
ACTION_AUTHORIZE_PATH = "/v1/actions/{action_id}/authorize"
#: Advisory liveness and level probe. Read-only, never on the message path.
HEALTH_PATH = "/health"

#: Response bodies echoed into debug logs are truncated to this length.
ERROR_BODY_LOG_LIMIT = 200


class RuntimeTransportError(RuntimeError):
    """Raised when the Runtime is unreachable or returns an unusable response."""


class AiohttpRuntimeTransport:
    """Asynchronous HTTP client implementing :class:`RuntimeTransport`."""

    def __init__(self, *, settings: Settings, log: Any = NULL_LOG) -> None:
        """Create the transport; no connection is opened until the first call."""
        self._settings = settings
        self._log = log
        self._session: Any = None
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    async def _client(self) -> Any:
        """Return a live client session bound to the running event loop."""
        if aiohttp is None:
            raise RuntimeTransportError(
                "aiohttp is unavailable; Runtime reporting is disabled",
            )
        loop = asyncio.get_running_loop()
        session = self._session
        if session is not None and not session.closed and self._session_loop is loop:
            return session
        async with self._lock:
            session = self._session
            if session is not None and not session.closed and self._session_loop is loop:
                return session
            if session is not None and not session.closed:
                await self._close()
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._settings.request_timeout_s),
                headers=self._headers(),
            )
            self._session_loop = loop
            return self._session

    def _headers(self) -> dict[str, str]:
        """Build request headers, including the token when one is configured."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-companion-runtime/1",
        }
        if self._settings.token:
            # Never logged anywhere in this plugin.
            headers["Authorization"] = f"Bearer {self._settings.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None,
        timeout_s: float,
    ) -> Any:
        """Perform one request and decode its JSON response.

        Args:
            method: HTTP method.
            path: Absolute path on the Runtime base URL.
            body: JSON request body, or ``None``.
            timeout_s: Per-request timeout.

        Returns:
            The decoded JSON body, or ``None`` for an empty response.

        Raises:
            RuntimeTransportError: On transport failure, non-2xx status, or
                unparseable JSON.
        """
        session = await self._client()
        url = f"{self._settings.base_url}{path}"
        timeout = aiohttp.ClientTimeout(
            total=max(0.05, float(timeout_s)),
            connect=min(max(0.05, float(timeout_s)), 5.0),
        )
        try:
            async with session.request(
                method,
                url,
                json=body,
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                status = response.status
                text = await response.text()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeTransportError(truncate_error(exc)) from exc

        if status < 200 or status >= 300:
            raise RuntimeTransportError(
                f"HTTP {status}: {' '.join(text.split())[:ERROR_BODY_LOG_LIMIT]}",
            )
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise RuntimeTransportError("Runtime response is not valid JSON") from exc

    async def post_events(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Append an event envelope to the Runtime event log."""
        await self._request("POST", EVENTS_PATH, body=body, timeout_s=timeout_s)

    async def fetch_health(self, *, timeout_s: float) -> dict[str, Any] | None:
        """Fetch the Runtime health payload, or ``None`` when unavailable.

        Used by the status command to surface which appraisal level is live and
        how many events the Runtime has deliberately left uninterpreted. A
        Runtime that predates patch v0.2 simply has no such fields, and a Runtime
        that is down raises - both are normal, so the caller must treat the
        result as advisory only and never let it affect message handling.

        Args:
            timeout_s: Per-request timeout.

        Returns:
            The decoded health mapping, or ``None`` when it cannot be read.
        """
        try:
            data = await self._request("GET", HEALTH_PATH, body=None, timeout_s=timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    async def fetch_context(
        self,
        request: ContextRequest,
        *,
        timeout_s: float,
    ) -> ContextSnapshot | None:
        """Fetch the Runtime's current context for one session."""
        data = await self._request(
            "POST",
            CONTEXT_PATH,
            body=request.to_wire(),
            timeout_s=timeout_s,
        )
        if not isinstance(data, dict):
            return None
        nested = data.get("context")
        return ContextSnapshot.from_wire(nested if isinstance(nested, dict) else data)

    async def lease_actions(
        self,
        request: LeaseRequest,
        *,
        timeout_s: float,
    ) -> list[LeasedAction]:
        """Lease up to ``request.max_actions`` outbox actions."""
        data = await self._request(
            "POST",
            OUTBOX_LEASE_PATH,
            body=request.to_wire(),
            timeout_s=timeout_s,
        )
        raw = data.get("actions") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        actions: list[LeasedAction] = []
        for item in raw:
            action = LeasedAction.from_wire(item)
            if action is None:
                self._log.debug("ignoring malformed outbox action from Runtime")
                continue
            actions.append(action)
        return actions

    async def heartbeat_lease(self, request: LeaseHeartbeat, *, timeout_s: float) -> bool:
        """Extend a lease; a 2xx response counts as confirmation."""
        data = await self._request(
            "POST",
            OUTBOX_HEARTBEAT_PATH.format(action_id=quote(request.action_id, safe="")),
            body=request.to_wire(),
            timeout_s=timeout_s,
        )
        if isinstance(data, dict) and "extended" in data:
            return bool(data.get("extended"))
        return True

    async def authorize_action(
        self,
        request: AuthorizeRequest,
        *,
        timeout_s: float,
    ) -> AuthorizeDecision:
        """Ask the Runtime whether an irreversible send may proceed right now."""
        data = await self._request(
            "POST",
            ACTION_AUTHORIZE_PATH.format(action_id=quote(request.action_id, safe="")),
            body=request.to_wire(),
            timeout_s=timeout_s,
        )
        if isinstance(data, dict):
            nested = data.get("authorization")
            return AuthorizeDecision.from_wire(nested if isinstance(nested, dict) else data)
        return AuthorizeDecision.denied("malformed_authorize_response")

    async def report_action(self, body: dict[str, Any], *, timeout_s: float) -> None:
        """Report the outcome of a leased action."""
        action_id = str(body.get("action_id") or "")
        # ``action_id`` lives in the path, so keep it alongside the wire body.
        payload = dict(body)
        path = OUTBOX_RESULT_PATH.format(action_id=quote(action_id, safe=""))
        await self._request("POST", path, body=payload, timeout_s=timeout_s)

    async def aclose(self) -> None:
        """Close the underlying client session. Safe to call repeatedly."""
        await self._close()

    async def _close(self) -> None:
        """Close and forget the current session."""
        session, self._session = self._session, None
        self._session_loop = None
        if session is None or session.closed:
            return
        try:
            await session.close()
        except Exception as exc:  # pragma: no cover - best effort cleanup
            self._log.debug("closing Runtime HTTP session failed: %s", truncate_error(exc))


def action_report_body(report: ActionReport) -> dict[str, Any]:
    """Build the result request body, including the path parameter."""
    body = report.to_wire()
    body["action_id"] = report.action_id
    return body
