"""Which entries advance the clock, and the two traps that wiring it has (§86.4).

The invariant is "a decision must not be judged against a state that has not integrated
the elapsed time". Where it is enforced was measured, not assumed:

* **decision entries** tick - ``/authorize``, ``/context``, operator commands. They read
  a drive value, a hazard rate or a confidence, and the audit's complaint is exactly that
  those reads were stale.
* **read-only entries** do not - ``GET /schedule`` and ``POST /user-model/predict``
  inspect the state instead of judging it, and a query that mutates the world changes
  what the next round decides. The hazard anchor is the previous *decision* rather than
  the previous tick (``Runtime._record_decision``), so removing those two ticks cannot
  cost the character its waiting window; the invariant is pinned in
  ``test_hazard_anchor.py``.
* **write entries** do not. Wiring the reducer's write entries was implemented and then
  withdrawn: turning it on made two resilience invariants red - the autonomous round
  stopped queuing render work, and a committed-but-undelivered attempt stopped closing
  the dispatch gate - because the attempt was aged out from under the very step that was
  reporting it. A claim, a render report and a delivery receipt are steps *of* the
  outbox lifecycle; integrating time inside one races it.

The two traps are pinned here because both were silent until something hung:
recursion (the tick writes through the lifecycle, which used to tick back into it) and a
lock-order inversion (an entry inside a transaction asking for the runtime's write lock).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from companion_runtime.runtime import Runtime
from companion_runtime.typing import AttemptState, CandidateIntent, OutboxKind, new_id

from conftest import BASE_TIME, build_config


def test_an_entry_advances_the_clock_but_a_write_entry_leaves_it_alone(runtime: Runtime) -> None:
    """The boundary, in one place: decisions tick, lifecycle steps do not."""
    ticks: list[object] = []
    original = runtime.lazy_tick

    def counting(now=None, **kwargs):
        ticks.append(now)
        return original(now, **kwargs)

    runtime.lazy_tick = counting  # type: ignore[method-assign]
    try:
        # A decision entry.
        runtime.tick_for_entry(BASE_TIME + timedelta(minutes=1))
        assert ticks, "a decision entry must advance the clock"

        # A write entry: the reducer ages nothing, by design and with the resilience
        # evidence recorded in ``tick_for_entry``.
        ticks.clear()
        runtime.reducer.claim_outbox(owner="w", now=BASE_TIME + timedelta(minutes=2), limit=1)
        assert not ticks, "a write entry must not age the lifecycle it is reporting"

        # ... and neither does one called from inside someone else's write session.
        ticks.clear()
        with runtime.write_session():
            runtime.reducer.claim_outbox(owner="w", now=BASE_TIME + timedelta(minutes=3), limit=1)
        assert not ticks
    finally:
        runtime.lazy_tick = original  # type: ignore[method-assign]


def test_the_write_session_and_transaction_guards_stop_a_nested_tick(runtime: Runtime) -> None:
    """Both guards exist because a tick opens its own transaction on the connection."""
    ticks: list[object] = []
    original = runtime.lazy_tick

    def counting(now=None, **kwargs):
        ticks.append(now)
        return original(now, **kwargs)

    runtime.lazy_tick = counting  # type: ignore[method-assign]
    try:
        with runtime.write_session():
            assert runtime.tick_for_entry(BASE_TIME + timedelta(minutes=4)).dt_seconds == 0.0
        assert not ticks, "inside a write session the clock belongs to that entry"

        with runtime.db.transaction():
            runtime.tick_for_entry(BASE_TIME + timedelta(minutes=5))
        assert not ticks, "inside a transaction the entry must not ask for the write lock"
    finally:
        runtime.lazy_tick = original  # type: ignore[method-assign]


def test_a_tick_triggered_from_inside_a_tick_returns_instead_of_recursing(
    runtime: Runtime,
) -> None:
    """The heartbeat's tick writes through the lifecycle; a nested tick must stop.

    This is the shape that produced ``RecursionError`` when the reducer entries were
    decorated: ``lazy_tick`` -> close stalled attempts -> a write entry -> ``lazy_tick``.
    The write entries no longer tick, but the guard stays because the heartbeat's tick
    still reaches reducer calls, and it is the only thing standing between the two.
    """
    reports: list[object] = []

    class Probe:
        """Stand in for a reducer call made from inside the tick."""

        def __call__(self) -> None:
            reports.append(runtime.lazy_tick())

    runtime._close_stalled_attempts = lambda conn, *, now: reports.append(  # type: ignore[method-assign]
        runtime.lazy_tick(now)
    ) or []

    report = runtime.lazy_tick(BASE_TIME + timedelta(minutes=6))

    assert reports, "the tick must have reached the nested call"
    assert report.dt_seconds > 0.0, "the outer tick still integrates the interval"


def test_a_write_inside_a_transaction_does_not_deadlock_against_the_write_lock(
    tmp_path,
) -> None:
    """Two threads, the two lock orders, one file: it must finish.

    Thread A is inside a database transaction (holding the connection) and calls a write
    entry; thread B holds the runtime's write lock and reads the database. If the entry
    asked for the write lock while holding the connection, the two would wait for each
    other - a hang, which no value assertion can catch.
    """
    config = build_config()
    config.storage.database_path = str(tmp_path / "locks.sqlite3")
    config.storage.raw_log_path = str(tmp_path / "locks.jsonl")
    runtime = Runtime(config=config, seed=11, created_at=BASE_TIME)
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type="follow_up",
        intent="询问面试结果",
        goal="表达关心",
        sources=["unfinished:unf_lock"],
    )
    try:
        with runtime.db.transaction() as conn:
            runtime.projections.candidates.upsert(conn, candidate)
            state = runtime.projections.runtime.ensure()
            attempt_id, _ = runtime._commit_attempt(
                conn, chosen=candidate, state=state, now=BASE_TIME
            )
        render_row = [
            item
            for item in runtime.projections.outbox.list_items(status=None, limit=50)
            if item.kind == OutboxKind.RENDER.value
        ][0]
        runtime.reducer.complete_render(outbox_id=render_row.outbox_id, text="在吗", now=BASE_TIME)
        send_row = [
            item
            for item in runtime.projections.outbox.list_items(status=None, limit=50)
            if item.kind == OutboxKind.SEND.value
        ][0]

        def inside_a_transaction() -> str:
            """Hold the connection and write, the way /rendered does."""
            with runtime.db.transaction():
                runtime.reducer.mark_delivered(
                    outbox_id=send_row.outbox_id, now=BASE_TIME + timedelta(seconds=1)
                )
            return "done"

        def holding_the_write_lock() -> str:
            """Hold the runtime's lock and tick, the way the heartbeat does."""
            with runtime.write_session():
                runtime.projections.outbox.get(send_row.outbox_id)
                runtime.lazy_tick(BASE_TIME + timedelta(seconds=2))
            return "done"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(inside_a_transaction)
            second = pool.submit(holding_the_write_lock)
            assert first.result(timeout=15) == "done"
            assert second.result(timeout=15) == "done"

        assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    finally:
        runtime.close()
