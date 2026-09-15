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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from . import action as action_module
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
    asks the dispatch gate whether a round may run at all, calls the runtime's
    endogenous round, and replans. It is designed so that the same logic is
    testable synchronously - the planner has no side effects.

    When it is handed a ``runtime`` it also *plans from state*: the wake-up
    anchors of the live Runtime (unfinished matter due times, boundary expiries,
    the foreground pause, the last round's hazard) decide when to wake, so a calm
    character sleeps and a character with something due wakes promptly. This is
    what makes a standard ``companion-runtime serve`` deployment actually think on
    its own: without it the sidecar only ever reacts to requests.
    """

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        round_callback: Callable[[], Any],
        rng: random.Random | None = None,
        runtime: Any = None,
    ) -> None:
        """Store the configuration and the callback that performs one round.

        Args:
            config: Resolved configuration.
            round_callback: Called once per wake-up; its return value may carry a
                ``next_wake_at`` attribute (an
                :class:`~companion_runtime.runtime.EndogenousOutcome` does).
            rng: Random source for the interval jitter.
            runtime: Optional Runtime used to collect wake-up anchors and to read
                the in-flight attempts the dispatch gate needs. Without it the
                loop still runs, but it can only use the delay implied by the
                previous round.
        """
        self._config = config
        self._round = round_callback
        self._rng = rng or random.Random()
        self._runtime = runtime
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._hazard_wake_at: datetime | None = None
        self._error_backoff: float | None = None
        #: The plan the loop is currently waiting on.
        self.last_plan: WakePlan | None = None
        #: ``(allowed, reason)`` from the most recent dispatch-gate evaluation.
        self.last_gate: tuple[bool, str] | None = None
        #: Set while a round is executing in its worker thread. The thread clears
        #: it, not the coroutine, so it stays true even if this task is cancelled
        #: mid-round - which is exactly what ``stop`` has to wait for.
        self._round_in_flight = False
        self.rounds = 0

    async def start(self) -> None:
        """Start the background loop (no-op when already running)."""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="companion-runtime-scheduler")

    async def stop(self) -> None:
        """Stop the background loop and wait for it to finish.

        A round that is already running is given a bounded moment to complete:
        it owns the write lock and the database connection, so a caller that
        closes the Runtime immediately after this returns must not race it.
        """
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
            self._task = None
        grace = min(5.0, max(1.0, float(self._config.scheduler.busy_poll_seconds)))
        deadline = time.monotonic() + grace
        while self._round_in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    @property
    def running(self) -> bool:
        """Return whether the loop task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def round_in_flight(self) -> bool:
        """Return whether a round is currently executing in a worker thread."""
        return self._round_in_flight

    def status(self) -> dict[str, Any]:
        """Return an inspection-friendly snapshot of the loop."""
        return {
            "running": self.running,
            "rounds": self.rounds,
            "round_in_flight": self._round_in_flight,
            "plan": self.last_plan.to_dict() if self.last_plan is not None else None,
            "dispatch_allowed": None if self.last_gate is None else self.last_gate[0],
            "dispatch_reason": None if self.last_gate is None else self.last_gate[1],
            "hazard_wake_at": isoformat(self._hazard_wake_at),
        }

    # ------------------------------------------------------------------ planning

    def replan(self, *, now: datetime | None = None) -> WakePlan:
        """Recompute and store the wake plan (and the gate verdict).

        Public because an operator or a test should be able to ask what the loop
        is about to do without waiting for it to wake up.

        Args:
            now: Reference time; defaults to the wall clock.

        Returns:
            The plan the loop will wait on.
        """
        stamp = ensure_aware(now) or utcnow()
        if self._runtime is None:
            # No Runtime to read: fall back to the delay the previous round
            # implied, which is the only information available.
            delay = clamp(
                self.last_plan.delay_seconds if self.last_plan else self._config.scheduler.min_interval_seconds,
                self._config.scheduler.min_interval_seconds,
                self._config.scheduler.max_interval_seconds,
            )
            self.last_plan = WakePlan(
                next_wake_at=stamp + timedelta(seconds=delay),
                delay_seconds=delay,
                reasons=["previous_round"],
            )
            self.last_gate = (True, "no_runtime")
            return self.last_plan

        signals = collect_signals(
            runtime=self._runtime, now=stamp, hazard_wake_at=self._hazard_wake_at
        )
        planned = plan(signals, config=self._config, rng=self._rng)
        allowed, reason = should_dispatch(
            signals=signals,
            # Only work *in preparation* blocks a new round. A message that has
            # already been delivered does not: it is waiting for the user, and it
            # may never be answered at all - gating on it would silence the
            # character permanently after one unanswered message. Its pacing is
            # the motivational layer's business (cooldown, pressure, daily
            # budget), not the scheduler's.
            attempt_states=[
                attempt.state
                for attempt in self._runtime.projections.attempts.list_by_state(
                    list(action_module.PRE_SEND_STATES), limit=10
                )
            ],
            config=self._config,
        )
        if not allowed and reason == "attempt_in_flight":
            # The blocker is not an anchor: it disappears when the delivery worker
            # finishes (or fails) its row, and nothing wakes the scheduler when it
            # does. Poll at the busy rate so a retry cannot stretch one round into
            # a full base interval.
            planned = WakePlan(
                next_wake_at=stamp + timedelta(seconds=self._config.scheduler.busy_poll_seconds),
                delay_seconds=clamp(
                    self._config.scheduler.busy_poll_seconds,
                    self._config.scheduler.min_interval_seconds,
                    self._config.scheduler.max_interval_seconds,
                ),
                reasons=planned.reasons + ["attempt_in_flight"],
                quiet_hours=planned.quiet_hours,
            )
        elif signals.state.last_tick_at is None and planned.reasons == ["base_interval"]:
            # A Runtime that has never ticked has no anchor to sleep on, and the
            # base interval is deliberately the *maximum* one - so a freshly
            # started sidecar would think nothing at all for up to 90 minutes,
            # including after a restart. The first wake is therefore prompt: one
            # configured minimum interval, which lets the round integrate the time
            # since creation and lets the hazard decide what to do about it.
            first = clamp(
                self._config.scheduler.min_interval_seconds,
                self._config.scheduler.min_interval_seconds,
                self._config.scheduler.max_interval_seconds,
            )
            planned = WakePlan(
                next_wake_at=stamp + timedelta(seconds=first),
                delay_seconds=first,
                reasons=["bootstrap"],
                quiet_hours=planned.quiet_hours,
            )
        self.last_plan = planned
        self.last_gate = (allowed, reason)
        return planned

    async def _wait(self, delay: float) -> bool:
        """Sleep for ``delay`` seconds; ``True`` when the loop was asked to stop."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, delay))
            return True
        except asyncio.TimeoutError:
            return False

    def _call_round(self) -> Any:
        """Invoke the round callback, clearing the in-flight flag when it returns."""
        try:
            return self._round()
        finally:
            self._round_in_flight = False

    async def _run_round(self) -> None:
        """Run one endogenous round off the event loop, recording its wake-up.

        The round is blocking work - SQLite writes, and possibly a semantic
        provider call during a deep refresh - so it runs in a worker thread, the
        same way the sidecar's maintenance pass does. A round that stalls must not
        stop the HTTP surface from answering.
        """
        self._round_in_flight = True
        try:
            result = await asyncio.to_thread(self._call_round)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken round must not kill the loop
            LOGGER.exception("Endogenous round failed; continuing")
            self._hazard_wake_at = None
            self._error_backoff = max(0.05, self._config.scheduler.busy_poll_seconds)
            return
        self.rounds += 1
        next_wake = ensure_aware(getattr(result, "next_wake_at", None))
        self._hazard_wake_at = next_wake
        if next_wake is not None:
            delay = clamp(
                (next_wake - utcnow()).total_seconds(),
                self._config.scheduler.min_interval_seconds,
                self._config.scheduler.max_interval_seconds,
            )
            self.last_plan = WakePlan(
                next_wake_at=utcnow() + timedelta(seconds=delay),
                delay_seconds=delay,
                reasons=["round_result"],
            )

    async def _run(self) -> None:
        """Loop until stopped: plan, wait, run one endogenous round, replan."""
        while not self._stop.is_set():
            backoff = self._error_backoff
            self._error_backoff = None
            if backoff is not None:
                self.last_plan = WakePlan(
                    next_wake_at=utcnow() + timedelta(seconds=backoff),
                    delay_seconds=backoff,
                    reasons=["error_backoff"],
                )
            else:
                self.replan()
            planned = self.last_plan
            if planned is None:  # pragma: no cover - replan always sets one
                return
            if await self._wait(planned.delay_seconds):
                return
            allowed, _reason = self.last_gate or (True, "unknown")
            if not allowed:
                # The gate is closed; replanning right away would spin, so the
                # planned delay above *is* the poll. Nothing to run this cycle.
                continue
            await self._run_round()

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
