"""Adaptive endogenous scheduler.

The Runtime deliberately does not run a fixed one-minute heartbeat. Instead it is
event-driven plus lazy: time only passes when something happens, and the
scheduler's job is to choose the *next* moment the Runtime should wake itself up.

``t_next = min(t_hazard, t_unfinished, t_boundary, t_cooldown, t_candidate)``

so a calm character may sleep for a long time while a character with a due
unfinished matter or a nearly-expired boundary wakes up promptly. Interval
jitter keeps two instances from synchronising.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from . import boundaries as boundary_module
from . import unfinished as unfinished_module
from .config import RuntimeConfig
from .typing import AttemptState, CandidateStatus, RuntimeState
from .utility import clamp, ensure_aware, isoformat, local_now, utcnow

LOGGER = logging.getLogger("companion_runtime.scheduler")


@dataclass(slots=True)
class WakePlan:
    """Why and when the Runtime should next wake itself."""

    next_wake_at: datetime
    delay_seconds: float
    reasons: list[str]
    quiet_hours: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "next_wake_at": isoformat(self.next_wake_at),
            "delay_seconds": round(self.delay_seconds, 3),
            "reasons": list(self.reasons),
            "quiet_hours": self.quiet_hours,
        }


@dataclass(slots=True)
class SchedulerSignals:
    """The candidate wake-up anchors collected from every subsystem."""

    now: datetime
    state: RuntimeState
    hazard_wake_at: datetime | None = None
    unfinished_wake_at: datetime | None = None
    boundary_wake_at: datetime | None = None
    cooldown_until: datetime | None = None
    candidate_wake_at: datetime | None = None
    foreground_pause_until: datetime | None = None
    maintenance_due_at: datetime | None = None
    boundary_proactive_allowed: bool = True


def collect_signals(
    *,
    runtime: Any,
    now: datetime | None = None,
    hazard_wake_at: datetime | None = None,
) -> SchedulerSignals:
    """Collect every endogenous wake-up anchor from the Runtime.

    Args:
        runtime: A :class:`~companion_runtime.runtime.Runtime`.
        now: Reference time.
        hazard_wake_at: Wake time implied by the last motivational round.

    Returns:
        A :class:`SchedulerSignals`.
    """
    stamp = ensure_aware(now) or utcnow()
    state = runtime.projections.runtime.ensure()
    matters = runtime.projections.unfinished.list_open()
    boundaries = runtime.projections.boundaries.active(stamp)
    candidates = runtime.projections.candidates.list_active(limit=100)

    candidate_wake = min(
        (
            candidate.expires_at
            for candidate in candidates
            if candidate.expires_at is not None and candidate.expires_at > stamp
        ),
        default=None,
    )
    verdict = boundary_module.evaluate(boundaries, now=stamp, state=state, is_proactive=True)

    return SchedulerSignals(
        now=stamp,
        state=state,
        hazard_wake_at=hazard_wake_at,
        unfinished_wake_at=unfinished_module.next_due_at(matters, stamp),
        boundary_wake_at=boundary_module.nearest_expiry(boundaries, stamp),
        cooldown_until=state.cooldown_until,
        candidate_wake_at=candidate_wake,
        foreground_pause_until=state.foreground_pause_until,
        maintenance_due_at=_maintenance_due(runtime, stamp),
        boundary_proactive_allowed=verdict.allow_proactive,
    )


def _maintenance_due(runtime: Any, now: datetime) -> datetime | None:
    """Return when the next P3 maintenance pass is due, if anything is pending."""
    pending = runtime.projections.memory.pending_candidates(limit=5)
    if not pending:
        return None
    newest = max((c.created_at for c in pending if c.created_at is not None), default=None)
    if newest is None:
        return now
    return newest + timedelta(seconds=runtime.config.memory.consolidation_interval_seconds)


def plan(
    signals: SchedulerSignals,
    *,
    config: RuntimeConfig,
    rng: random.Random | None = None,
) -> WakePlan:
    """Compute the next wake-up time from the collected anchors.

    Args:
        signals: Collected anchors.
        config: Runtime configuration.
        rng: Random source for interval jitter.

    Returns:
        A :class:`WakePlan`.
    """
    source = rng or random.Random()
    now = signals.now
    anchors: list[tuple[datetime, str]] = []

    def consider(value: datetime | None, label: str) -> None:
        """Register a candidate anchor when it lies in the future."""
        if value is None:
            return
        anchor = ensure_aware(value)
        if anchor > now:
            anchors.append((anchor, label))

    consider(signals.hazard_wake_at, "hazard")
    consider(signals.unfinished_wake_at, "unfinished")
    consider(signals.boundary_wake_at, "boundary")
    consider(signals.cooldown_until, "cooldown")
    consider(signals.candidate_wake_at, "candidate")
    consider(signals.foreground_pause_until, "foreground_pause")
    consider(signals.maintenance_due_at, "maintenance")

    # A blocked proactive permission is worth re-checking at the boundary expiry,
    # which is already covered; otherwise fall back to the base interval.
    if anchors:
        target, _ = min(anchors, key=lambda item: item[0])
        reasons = sorted({label for anchor, label in anchors if anchor == target})
    else:
        target = now + timedelta(seconds=config.scheduler.max_interval_seconds)
        reasons = ["base_interval"]

    delay = clamp(
        (target - now).total_seconds(),
        config.scheduler.min_interval_seconds,
        config.scheduler.max_interval_seconds,
    )
    # Jitter spreads wake-ups so parallel instances do not beat in sync. It is
    # applied *after* clamping so the configured bounds are always respected.
    delay = clamp(
        delay * (1.0 + source.uniform(-0.05, 0.05)),
        config.scheduler.min_interval_seconds,
        config.scheduler.max_interval_seconds,
    )

    quiet = _in_quiet_hours(config, target)
    if quiet:
        target = _next_quiet_end(config, target)
        delay = clamp(
            (target - now).total_seconds(),
            config.scheduler.min_interval_seconds,
            max(config.scheduler.max_interval_seconds, 8 * 3600.0),
        )
        reasons.append("quiet_hours")
    return WakePlan(next_wake_at=now + timedelta(seconds=delay), delay_seconds=delay, reasons=reasons, quiet_hours=quiet)


def _in_quiet_hours(config: RuntimeConfig, moment: datetime) -> bool:
    """Return whether ``moment`` falls inside the configured quiet hours."""
    start = config.scheduler.quiet_hours_start
    end = config.scheduler.quiet_hours_end
    if start is None or end is None or start == end:
        return False
    hour = local_now(moment).hour
    if start < end:
        return start <= hour < end
    # Window crosses midnight.
    return hour >= start or hour < end


def _next_quiet_end(config: RuntimeConfig, moment: datetime) -> datetime:
    """Return the end of the current quiet window."""
    end = config.scheduler.quiet_hours_end or 0
    local = local_now(moment)
    candidate = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def should_dispatch(
    *,
    signals: SchedulerSignals,
    attempt_states: Sequence[str] = (),
    config: RuntimeConfig,
) -> tuple[bool, str]:
    """Return whether an endogenous dispatch is currently allowed.

    Dispatch is suppressed while the user is active (the entry barrier), while a
    proactive contact is still in flight, or while a hard boundary is in force.

    Args:
        signals: Collected anchors.
        attempt_states: States of the in-flight attempts.
        config: Runtime configuration.

    Returns:
        ``(allowed, reason)``.
    """
    now = signals.now
    if signals.foreground_pause_until is not None and now < signals.foreground_pause_until:
        return False, "foreground_pause"
    if any(state not in {AttemptState.RESOLVED.value, AttemptState.ABORTED.value,
                         AttemptState.EXPIRED.value, AttemptState.FAILED.value} for state in attempt_states):
        return False, "attempt_in_flight"
    if not signals.boundary_proactive_allowed:
        return False, "boundary_blocks_proactive"
    if _in_quiet_hours(config, now):
        return False, "quiet_hours"
    return True, "allowed"


class Scheduler:
    """Runs the endogenous loop while the sidecar is alive.

    The loop is a thin shell around :func:`plan`: it waits for the planned delay,
    calls the runtime's endogenous round, and replans. It is designed so that the
    same logic is testable synchronously - the planner has no side effects.
    """

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        round_callback: Callable[[], Any],
        rng: random.Random | None = None,
    ) -> None:
        """Store the configuration and the callback that performs one round."""
        self._config = config
        self._round = round_callback
        self._rng = rng or random.Random()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_plan: WakePlan | None = None
        self.rounds = 0

    async def start(self) -> None:
        """Start the background loop (no-op when already running)."""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="companion-runtime-scheduler")

    async def stop(self) -> None:
        """Stop the background loop and wait for it to finish."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
            self._task = None

    async def _run(self) -> None:
        """Loop until stopped: plan, sleep, run one endogenous round, replan."""
        while not self._stop.is_set():
            delay = self.last_plan.delay_seconds if self.last_plan else self._config.scheduler.min_interval_seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, delay))
                return
            except asyncio.TimeoutError:
                pass
            try:
                result = self._round()
                self.rounds += 1
                next_wake = getattr(result, "next_wake_at", None)
                if next_wake is not None:
                    self.last_plan = WakePlan(
                        next_wake_at=ensure_aware(next_wake) or utcnow(),
                        delay_seconds=clamp(
                            (ensure_aware(next_wake) - utcnow()).total_seconds(),
                            self._config.scheduler.min_interval_seconds,
                            self._config.scheduler.max_interval_seconds,
                        ),
                        reasons=["round_result"],
                    )
                else:
                    self.last_plan = None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken round must not kill the loop
                LOGGER.exception("Endogenous round failed; continuing")
                self.last_plan = WakePlan(
                    next_wake_at=utcnow() + timedelta(seconds=self._config.scheduler.busy_poll_seconds),
                    delay_seconds=self._config.scheduler.busy_poll_seconds,
                    reasons=["error_backoff"],
                )

    @property
    def running(self) -> bool:
        """Return whether the loop task is alive."""
        return self._task is not None and not self._task.done()


def next_wake_summary(signals: SchedulerSignals, plan_result: WakePlan) -> dict[str, Any]:
    """Return an inspection-friendly summary of the scheduling decision."""
    return {
        "now": isoformat(signals.now),
        "plan": plan_result.to_dict(),
        "anchors": {
            "hazard": isoformat(signals.hazard_wake_at),
            "unfinished": isoformat(signals.unfinished_wake_at),
            "boundary": isoformat(signals.boundary_wake_at),
            "cooldown": isoformat(signals.cooldown_until),
            "candidate": isoformat(signals.candidate_wake_at),
            "foreground_pause": isoformat(signals.foreground_pause_until),
            "maintenance": isoformat(signals.maintenance_due_at),
        },
        "boundary_proactive_allowed": signals.boundary_proactive_allowed,
    }
