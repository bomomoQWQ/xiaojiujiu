"""Read the Runtime's observable variables, for logging and for the CLI.

Everything here goes through the program's **public read-only HTTP surface**.
That is a deliberate constraint, not an implementation detail: an external
framework that reached into the Runtime object graph would be testing its own
private copy of the state, and would silently keep "passing" after the program
changed a projection. Going over HTTP means the framework can only see what an
operator could see, so a variable that shows up in the log is a variable that
really is observable in production.

Endpoint roles follow the program's own split:

* ``/state``, ``/schedule``, ``/health`` -- the public surface.
* ``/unfinished``, ``/candidates``, ``/boundaries``, ``/outbox``,
  ``/cognition/backlog``, ``/attempts`` -- the operator-observable surface. They
  are used here as diagnostics, never as the primary evidence for a claim.

A failing endpoint is recorded as an ``errors`` entry and never raises. A probe
that dies because one optional projection endpoint returned 500 would take the
whole test run with it, which is exactly backwards: partial visibility is still
useful, and the gap itself is a finding.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

LOGGER = logging.getLogger("cf.variables")

#: Endpoints read on every heartbeat, in the order they are logged.
PROBED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("state", "/state"),
    ("schedule", "/schedule"),
    ("backlog", "/cognition/backlog"),
    ("unfinished", "/unfinished"),
    ("candidates", "/candidates"),
    ("boundaries", "/boundaries"),
    ("outbox", "/outbox"),
    ("attempts", "/attempts"),
    ("health", "/health"),
)

#: Scalar variables lifted to the top level of the trace, so the rolling log line
#: and any later grep can address them by name instead of by path.
LOG_VARIABLES: tuple[str, ...] = (
    "mood_valence",
    "mood_arousal",
    "mood_stability",
    "impulse",
    "restraint",
    "pressure",
    "allow_proactive",
    "contact_count_today",
    "cooldown_until",
    "unresolved",
    "unfinished_open",
    "candidates_active",
    "boundaries_effective",
    "outbox_pending",
    "outbox_delivered",
    "attempts_open",
    "next_wake_at",
    "next_wake_in_s",
    "next_wake_reasons",
    "quiet_hours",
)


def _get(url: str, timeout_s: float) -> Any:
    """Perform one GET and decode the JSON body.

    Raises:
        Exception: Any transport or decoding failure, for the caller to record.
    """
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def _as_list(value: Any) -> list[Any]:
    """Return ``value`` as a list, unwrapping the common envelope keys."""
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        for key in ("items", "results", "data", "unfinished", "candidates", "boundaries", "outbox"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
    return []


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce to float, tolerating ``None`` and junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _seconds_until(moment: str | None, now: datetime) -> float | None:
    """Return how many seconds separate ``moment`` from ``now``, or ``None``."""
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - now).total_seconds()


@dataclass(slots=True)
class Snapshot:
    """One reading of every observable variable.

    Attributes:
        variables: Flat scalars, ready for a log line.
        raw: The full decoded response per endpoint, for the JSONL trace.
        errors: Endpoint name -> error text, for anything that failed.
    """

    variables: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {"variables": dict(self.variables), "detail": dict(self.raw), "errors": dict(self.errors)}


class VariableProbe:
    """Collects variable snapshots from a running Runtime.

    Args:
        base_url: Root URL of the Runtime, e.g. ``http://127.0.0.1:8000``.
        timeout_s: Per-endpoint deadline.
        clock: Optional clock, used to compute "how long until the next wake-up"
            against virtual rather than wall time.
    """

    def __init__(self, base_url: str, *, timeout_s: float = 5.0, clock: Any = None) -> None:
        """Store the target and the deadline."""
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.clock = clock

    def snapshot(self) -> Snapshot:
        """Read every endpoint and flatten the result.

        Returns:
            A :class:`Snapshot`. Endpoint failures land in ``errors``.
        """
        raw: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, path in PROBED_ENDPOINTS:
            try:
                raw[name] = _get(f"{self.base_url}{path}", self.timeout_s)
            except urllib.error.HTTPError as exc:
                errors[name] = f"HTTP {exc.code}"
            except urllib.error.URLError as exc:
                errors[name] = f"unreachable: {exc.reason}"
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors[name] = f"undecodable: {type(exc).__name__}"
            except Exception as exc:  # noqa: BLE001 - a probe must never kill the run
                errors[name] = f"{type(exc).__name__}: {exc}"
        return Snapshot(variables=self._flatten(raw), raw=raw, errors=errors)

    def _flatten(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Reduce the endpoint responses to the logged scalar variables."""
        now = self.clock.now() if self.clock is not None else datetime.now(timezone.utc)
        state = raw.get("state") if isinstance(raw.get("state"), Mapping) else {}
        mood = state.get("mood") if isinstance(state.get("mood"), Mapping) else {}
        drive = state.get("drive") if isinstance(state.get("drive"), Mapping) else {}
        schedule = raw.get("schedule") if isinstance(raw.get("schedule"), Mapping) else {}
        plan = schedule.get("plan") if isinstance(schedule.get("plan"), Mapping) else {}

        variables: dict[str, Any] = {
            "mood_valence": _num(mood.get("valence")),
            "mood_arousal": _num(mood.get("arousal")),
            "mood_stability": _num(mood.get("stability")),
            "impulse": _num(drive.get("approach_impulse")),
            "restraint": _num(drive.get("restraint")),
            "pressure": _num(drive.get("pressure")),
            "allow_proactive": bool(state.get("allow_proactive", False)),
            "contact_count_today": int(_num(state.get("contact_count_today"))),
            "cooldown_until": state.get("cooldown_until"),
            "unresolved": self._count_unresolved(raw.get("backlog")),
            "unfinished_open": self._count_status(raw.get("unfinished"), ("open", "due", "waiting")),
            "candidates_active": self._count_status(raw.get("candidates"), ("new", "active")),
            "boundaries_effective": self._count_effective_boundaries(raw.get("boundaries"), now),
            "outbox_pending": self._count_status(raw.get("outbox"), ("pending", "queued", "claimed")),
            "outbox_delivered": self._count_status(raw.get("outbox"), ("delivered", "sent", "acked")),
            "attempts_open": self._count_status(
                raw.get("attempts"), ("proposed", "committed", "rendering", "ready_to_send", "sent")
            ),
            "next_wake_at": plan.get("next_wake_at"),
            "next_wake_in_s": (
                round(value, 1) if (value := _seconds_until(plan.get("next_wake_at"), now)) is not None else None
            ),
            "next_wake_reasons": list(plan.get("reasons") or []),
            "quiet_hours": bool(plan.get("quiet_hours", False)),
        }
        return variables

    @staticmethod
    def _count_unresolved(backlog: Any) -> int:
        """Count unresolved events from ``/cognition/backlog``."""
        if isinstance(backlog, Mapping):
            for key in ("unresolved", "unresolved_count", "count", "pending"):
                value = backlog.get(key)
                if isinstance(value, int):
                    return value
                if isinstance(value, list):
                    return len(value)
        return len(_as_list(backlog))

    @staticmethod
    def _count_status(payload: Any, wanted: tuple[str, ...]) -> int:
        """Count items whose ``status``/``state`` is one of ``wanted``."""
        items = _as_list(payload)
        if not items:
            return 0
        total = 0
        for item in items:
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or item.get("state") or "").lower()
            if not wanted or status in wanted:
                total += 1
        return total if wanted else len(items)

    @staticmethod
    def _count_effective_boundaries(payload: Any, now: datetime) -> int:
        """Count boundaries that are currently in force."""
        total = 0
        for item in _as_list(payload):
            if not isinstance(item, Mapping):
                continue
            if item.get("revoked_at"):
                continue
            expires = item.get("expires_at")
            if expires and (_seconds_until(str(expires), now) or 0) <= 0:
                continue
            total += 1
        return total
