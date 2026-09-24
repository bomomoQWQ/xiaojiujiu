"""The hazard interval is measured between decisions, not between clock advances (§50).

``P(act) = 1 - exp(-λ(t)·Δt)`` is only frequency-independent while ``Δt`` is the time
between two *opportunities to act*; ``test_two_short_ticks_match_one_long_tick`` pins
that at the level of :func:`companion_runtime.motivation.decide`. This module pins where
the interval comes from in a *running* Runtime.

It used to be read off ``last_tick_at``, which every entry writes - so an entry that
only reads could consume the character's waiting window. The failure this was found
through is worth stating numerically because it is the regression test's justification:
in the resilience simulation one ``GET /schedule`` collapsed the interval from the
three-day offline window to 2 ms, P(act) from ~1 to ~0, and the autonomous round that
was supposed to reach out stayed silent (which is exactly the pair of checks - a queued
render row and the dispatch gate it closes - that the scheduler phase asserts).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from companion_runtime.api import create_app
from companion_runtime.db import Database
from companion_runtime.runtime import LAST_DECISION_META_KEY, Runtime
from companion_runtime.utility import parse_datetime

from conftest import BASE_TIME, build_config

#: A message that leaves a dated obligation behind, so the pool holds a concrete reason.
MATTER_TEXT = "明天下午面试，结束告诉你结果。"


def _runtime_with_a_due_matter(seed: int = 7) -> Runtime:
    """Return a Runtime holding one open matter whose moment has come."""
    config = build_config()
    runtime = Runtime(
        config, seed=seed, database=Database(":memory:"), created_at=BASE_TIME
    )
    runtime.process_user_message(content=MATTER_TEXT, timestamp=BASE_TIME)
    return runtime


def _due_at(runtime: Runtime) -> object:
    """Return the moment the first open matter is overdue."""
    matter = runtime.projections.unfinished.list_open()[0]
    return matter.waiting_until + timedelta(hours=1)


def _client(runtime: Runtime) -> TestClient:
    """A TestClient bound to the Runtime under test."""
    return TestClient(create_app(runtime, runtime.config))


def test_a_read_entry_does_not_advance_the_clock_or_the_anchor() -> None:
    """``/schedule`` and ``/user-model/predict`` inspect; they do not mutate.

    A query that moves the world is its own defect: the caller learns nothing extra
    from it and the character's behaviour changes because someone looked.
    """
    runtime = _runtime_with_a_due_matter()
    try:
        runtime.endogenous_round(now=_due_at(runtime), force=True)
        before = runtime.state()
        anchor = before.meta[LAST_DECISION_META_KEY]

        with _client(runtime) as client:
            assert client.get("/schedule").status_code == 200
            assert client.post("/user-model/predict", json={}).status_code == 200

        after = runtime.state()
        assert after.last_tick_at == before.last_tick_at, "a read must not tick"
        assert after.meta[LAST_DECISION_META_KEY] == anchor, "a read must not decide"
        assert runtime._last_decision() == parse_datetime(anchor)
    finally:
        runtime.close()


def test_the_hazard_interval_runs_from_the_previous_decision(monkeypatch) -> None:
    """A tick between two rounds must not shorten the second round's interval."""
    runtime = _runtime_with_a_due_matter()
    try:
        first_at = _due_at(runtime)
        # Freeze the hazard draw so neither round acts. ``delta_t`` is only reported on
        # a round that has an eligible candidate, and committing the first one would
        # change the pool (and the cooldown) the second round is judged against.
        monkeypatch.setattr(runtime.rng, "random", lambda: 1.0)

        first = runtime.endogenous_round(now=first_at, force=True)
        first_outcome = first.decision["outcome"]
        assert first_outcome["reason"] == "hazard_not_triggered"
        # The first interval is the whole wait since the Runtime came into existence.
        assert first_outcome["delta_t"] == pytest.approx(
            (first_at - BASE_TIME).total_seconds()
        )

        # A decision entry that only reads (the ``/context`` shape) advances the clock.
        read_at = first_at + timedelta(hours=2)
        runtime.tick_for_entry(read_at)
        assert runtime.state().last_tick_at == read_at

        # ... and the next round still integrates from the previous *decision*, so the
        # read cost the character nothing.
        second_at = read_at + timedelta(minutes=1)
        second_outcome = runtime.endogenous_round(now=second_at, force=True).decision["outcome"]
        assert second_outcome["reason"] == "hazard_not_triggered"
        assert second_outcome["delta_t"] == pytest.approx(2 * 3600 + 60)
    finally:
        runtime.close()


def _decision_shape(decision: dict) -> dict:
    """Return a decision with per-Runtime identifiers removed.

    Candidate identifiers are minted from process entropy, not from the Runtime's RNG,
    so two Runtimes driven through the same history never share them. They are the only
    thing that may differ when the behaviour is identical, so they are replaced by the
    position that carries them.
    """
    return {
        **{key: value for key, value in decision.items() if key != "utilities"},
        "chosen_candidate_id": decision.get("chosen_candidate_id") is not None,
        "utilities": [
            {key: value for key, value in item.items() if key != "candidate_id"}
            for item in decision.get("utilities") or []
        ],
    }


def test_a_read_entry_does_not_change_what_the_next_round_decides(monkeypatch) -> None:
    """The same two rounds, with and without a poll in between, decide identically.

    This is the invariant the two checks in the resilience simulation assert in
    practice. The comparison is exact rather than approximate on purpose: a read entry
    that advances the clock does not merely shift one number, it re-splits the whole
    time integration *and* (before the anchor was decoupled) consumed the interval - so
    any tick hiding in these endpoints fails here immediately.
    """
    plain = _runtime_with_a_due_matter(seed=7)
    polled = _runtime_with_a_due_matter(seed=7)
    try:
        for runtime in (plain, polled):
            monkeypatch.setattr(runtime.rng, "random", lambda: 1.0)
        first_at = _due_at(plain)
        second_at = first_at + timedelta(hours=2, minutes=1)

        for runtime in (plain, polled):
            runtime.endogenous_round(now=first_at, force=True)

        with _client(polled) as client:
            assert client.get("/schedule").status_code == 200
            assert client.post(
                "/user-model/predict", json={"action": {"type": "follow_up", "proactive": True}}
            ).status_code == 200

        plain_outcome = plain.endogenous_round(now=second_at, force=True).decision["outcome"]
        polled_outcome = polled.endogenous_round(now=second_at, force=True).decision["outcome"]
        assert _decision_shape(polled_outcome) == _decision_shape(plain_outcome)
    finally:
        plain.close()
        polled.close()
