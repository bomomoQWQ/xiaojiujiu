"""A process clock the framework can move from outside.

The Runtime has no clock abstraction: 130-odd call sites read
``companion_runtime.utility.utcnow``, which is a plain module-level function
wrapping ``datetime.now(timezone.utc)``. An *external* framework therefore has
exactly two ways to move the program's time without editing it:

1. drive ``POST /tick`` with an explicit timestamp -- the Runtime already honours
   a caller-supplied moment on that path -- which advances the persistent state
   but leaves everything that reads the wall clock (the Scheduler's own wake-ups,
   ``/schedule``, attempt bookkeeping) on real time; or
2. import the program into this process and rebind the ``utcnow`` name *inside
   every already-imported program module*, which moves the whole process at once.

This module implements (2), because partial time control is worse than none: a
test that moves ``lazy_tick`` but not the Scheduler cannot reproduce "the user
went away for 8 hours" faithfully. The rebinding trick is the same one the
shipped end-to-end simulations use; it is the only technique that reaches every
call site, including the modules that did ``from .utility import utcnow`` and so
hold their own reference to the function object.

Nothing here writes to the program's files: rebinding a module attribute at
runtime is process-local state, not a source change.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

LOGGER = logging.getLogger("cf.clock")

#: Module-name prefixes whose ``utcnow`` / ``utc_now_iso`` bindings get replaced.
#: ``companion_runtime`` is the program; the plugin package is included so the
#: adapter sees the same clock when the framework loads it.
DEFAULT_PREFIXES: tuple[str, ...] = ("companion_runtime", "astrbot_plugin_companion_runtime")

#: Attribute names rebound inside every matching module.
CLOCK_ATTRIBUTES: tuple[str, ...] = ("utcnow", "utc_now_iso")


def _utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_when(text: str) -> datetime:
    """Parse an absolute instant supplied on a command line.

    Accepts ISO-8601 in any offset, plus the bare forms ``2026-09-15`` and
    ``2026-09-15T12:30`` (interpreted as UTC), and the literal ``now``.

    Args:
        text: The user-supplied instant.

    Returns:
        A timezone-aware UTC datetime.

    Raises:
        ValueError: When the text is not a recognised instant.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("empty time")
    if cleaned.lower() == "now":
        return datetime.now(timezone.utc)
    candidate = cleaned.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return _utc(datetime.fromisoformat(candidate))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return _utc(datetime.strptime(cleaned, fmt))
        except ValueError:
            continue
    raise ValueError(f"unrecognised time {text!r}; use ISO-8601, e.g. 2026-09-15T08:00:00Z")


_UNIT_SECONDS = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
}


def parse_duration(text: str) -> timedelta:
    """Parse a duration such as ``90s``, ``15m``, ``8h``, ``3d`` or ``1h30m``.

    A bare number is read as seconds.

    Args:
        text: The user-supplied duration.

    Returns:
        The parsed duration.

    Raises:
        ValueError: When the text is not a recognised duration.
    """
    cleaned = (text or "").strip().lower().replace(" ", "")
    if not cleaned:
        raise ValueError("empty duration")
    try:
        return timedelta(seconds=float(cleaned))
    except ValueError:
        pass

    total = 0.0
    number = ""
    unit = ""
    saw_unit = False
    for char in cleaned:
        if char.isdigit() or char == ".":
            if unit:
                total += _finish_segment(number, unit)
                number, unit = "", ""
            number += char
        elif char.isalpha():
            unit += char
            saw_unit = True
        else:
            raise ValueError(f"unrecognised duration {text!r}")
    if number or unit:
        total += _finish_segment(number, unit)
    if not saw_unit or total <= 0:
        raise ValueError(f"unrecognised duration {text!r}; use e.g. 90s, 15m, 8h, 3d")
    return timedelta(seconds=total)


def _finish_segment(number: str, unit: str) -> float:
    """Convert one ``<number><unit>`` segment to seconds."""
    if not number:
        raise ValueError(f"duration segment {unit!r} has no number")
    if unit not in _UNIT_SECONDS:
        raise ValueError(f"unknown duration unit {unit!r}")
    return float(number) * _UNIT_SECONDS[unit]


@dataclass(slots=True)
class ClockState:
    """A snapshot of the clock's configuration, for logging and the control API."""

    virtual_now: datetime
    scale: float
    frozen: bool
    wall_now: datetime
    offset_seconds: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "virtual_now": self.virtual_now.isoformat(),
            "wall_now": self.wall_now.isoformat(),
            "offset_seconds": round(self.offset_seconds, 3),
            "scale": self.scale,
            "frozen": self.frozen,
        }


class ControllableClock:
    """A thread-safe virtual clock anchored to real monotonic time.

    The clock is ``virtual = anchor_virtual + (monotonic - anchor_monotonic) *
    scale``, recomputed against a fresh anchor whenever the controller changes
    speed or jumps. ``frozen`` pins the current instant and ignores ``scale``.

    Time only moves forward on its own when ``scale > 0`` and ``frozen`` is
    false; ``advance`` and ``set`` move it instantly.

    Args:
        origin: Starting instant. Defaults to the real current UTC time.
        scale: Virtual seconds per real second.
        frozen: Start pinned to ``origin``.
    """

    def __init__(
        self,
        origin: datetime | None = None,
        *,
        scale: float = 1.0,
        frozen: bool = False,
    ) -> None:
        """Anchor the clock and record its initial speed."""
        self._lock = threading.RLock()
        self._anchor_virtual = _utc(origin) if origin is not None else datetime.now(timezone.utc)
        self._anchor_monotonic = time.monotonic()
        self._scale = float(scale)
        self._frozen = bool(frozen)
        #: Every mutation, in order, so a log reader can reconstruct the run.
        self.journal: list[dict[str, Any]] = []
        self._record("init", origin=self._anchor_virtual, scale=self._scale, frozen=self._frozen)

    # ------------------------------------------------------------------ reading

    def now(self) -> datetime:
        """Return the current virtual time as a timezone-aware UTC datetime."""
        with self._lock:
            return self._current()

    def iso(self) -> str:
        """Return the current virtual time as an ISO-8601 string.

        Bound over the program's ``utc_now_iso`` so the adapter sees one clock.
        """
        return self.now().isoformat()

    def monotonic(self) -> float:
        """Return a virtual monotonic second counter, for elapsed-time maths."""
        with self._lock:
            return self._current().timestamp()

    def _current(self) -> datetime:
        """Return the current virtual instant. Caller holds the lock."""
        if self._frozen:
            return self._anchor_virtual
        elapsed = time.monotonic() - self._anchor_monotonic
        return self._anchor_virtual + timedelta(seconds=elapsed * self._scale)

    def _reanchor(self) -> None:
        """Fold the elapsed real time into the anchor. Caller holds the lock."""
        self._anchor_virtual = self._current()
        self._anchor_monotonic = time.monotonic()

    # ----------------------------------------------------------------- control

    def set(self, when: datetime) -> datetime:
        """Jump to an absolute instant.

        Args:
            when: The instant to jump to; naive values are read as UTC.

        Returns:
            The new virtual now.
        """
        with self._lock:
            self._anchor_virtual = _utc(when)
            self._anchor_monotonic = time.monotonic()
            self._record("set", to=self._anchor_virtual)
            return self._anchor_virtual

    def advance(self, delta: timedelta) -> datetime:
        """Step forward (or backward, for negative deltas) by ``delta``.

        Args:
            delta: How far to move.

        Returns:
            The new virtual now.
        """
        with self._lock:
            self._anchor_virtual = self._current() + delta
            self._anchor_monotonic = time.monotonic()
            self._record("advance", by_seconds=delta.total_seconds(), to=self._anchor_virtual)
            return self._anchor_virtual

    def set_scale(self, scale: float) -> float:
        """Change how fast virtual time runs.

        Args:
            scale: Virtual seconds per real second. ``0`` effectively pins time
                without setting ``frozen``.

        Returns:
            The scale actually stored.

        Raises:
            ValueError: When ``scale`` is negative or not finite.
        """
        value = float(scale)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("scale must be finite")
        if value < 0:
            raise ValueError("scale must not be negative")
        with self._lock:
            self._reanchor()
            self._scale = value
            self._record("scale", to=value)
            return value

    def freeze(self) -> datetime:
        """Pin virtual time to its current instant."""
        with self._lock:
            if not self._frozen:
                self._anchor_virtual = self._current()
                self._anchor_monotonic = time.monotonic()
                self._frozen = True
                self._record("freeze", at=self._anchor_virtual)
            return self._anchor_virtual

    def unfreeze(self) -> datetime:
        """Resume the configured speed from the current instant."""
        with self._lock:
            if self._frozen:
                self._anchor_monotonic = time.monotonic()
                self._frozen = False
                self._record("unfreeze", at=self._anchor_virtual, scale=self._scale)
            return self._current()

    # ------------------------------------------------------------------ report

    @property
    def scale(self) -> float:
        """Virtual seconds per real second."""
        with self._lock:
            return self._scale

    @property
    def frozen(self) -> bool:
        """Whether time is pinned."""
        with self._lock:
            return self._frozen

    def state(self) -> ClockState:
        """Return a snapshot of the clock's configuration."""
        with self._lock:
            virtual = self._current()
            wall = datetime.now(timezone.utc)
            return ClockState(
                virtual_now=virtual,
                scale=self._scale,
                frozen=self._frozen,
                wall_now=wall,
                offset_seconds=(virtual - wall).total_seconds(),
            )

    def _record(self, action: str, **fields: Any) -> None:
        """Append one mutation to the journal. Caller holds the lock."""
        entry: dict[str, Any] = {
            "action": action,
            "at_wall": datetime.now(timezone.utc).isoformat(),
        }
        for key, value in fields.items():
            entry[key] = value.isoformat() if isinstance(value, datetime) else value
        self.journal.append(entry)


def install_process_clock(
    clock: ControllableClock,
    *,
    prefixes: Iterable[str] = DEFAULT_PREFIXES,
    attributes: Iterable[str] = CLOCK_ATTRIBUTES,
) -> int:
    """Rebind the program's clock functions to ``clock`` throughout the process.

    Every already-imported module whose name starts with one of ``prefixes`` and
    that *has* one of ``attributes`` gets that attribute replaced. Walking the
    module table (rather than patching ``utility`` alone) is what makes this work
    for modules that did ``from .utility import utcnow``: they hold their own
    reference to the original function object, so patching the source module
    would leave them on real time.

    Safe to call repeatedly, and worth calling again after any late import --
    a module imported after this call binds the real ``utcnow`` and would
    otherwise run on wall-clock time.

    Args:
        clock: The clock to install.
        prefixes: Module-name prefixes to consider.
        attributes: Attribute names to replace when present.

    Returns:
        How many module attributes were rebound.
    """
    wanted = tuple(prefixes)
    names = tuple(attributes)
    rebound = 0
    for module_name, module in list(sys.modules.items()):
        if module is None:
            continue
        if not any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in wanted):
            continue
        for attribute in names:
            if not hasattr(module, attribute):
                continue
            setattr(module, attribute, clock.iso if attribute == "utc_now_iso" else clock.now)
            rebound += 1
    if rebound:
        LOGGER.debug("process clock installed over %d module attribute(s)", rebound)
    return rebound
