"""The beta's read-back tables: every verdict, the state curve, and every render.

A closed beta is judged from what the Runtime would otherwise discard. Three things
are pinned here, each because its absence makes the week unreadable afterwards:

* the motivational verdict of a round that **did not act** -- without those there is
  no way to answer "why did she never speak?" except by re-running the week;
* a state sample per round, turning the single current-state row into a curve;
* a record of what the host was handed on each context render -- the injected block
  is temporary by design, so "what did she know when she answered that?" otherwise
  has no answer at all.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from companion_runtime.api import create_app
from companion_runtime.db import Database
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME, build_config

#: A message that leaves a dated obligation behind, so a round has something to weigh.
MATTER_TEXT = "明天下午面试，结束告诉你结果。"


def _runtime(**flags: object) -> Runtime:
    """Return an in-memory Runtime, applying ``observability`` flags if given.

    ``build_config`` overrides top-level attributes with ``setattr``, so a nested
    section has to be adjusted on the built config rather than passed as a mapping.
    """
    config = build_config()
    for key, value in flags.items():
        setattr(config.observability, key, value)
    return Runtime(config, seed=11, database=Database(":memory:"), created_at=BASE_TIME)


def _due_at(runtime: Runtime) -> object:
    """Return a moment at which the open matter is overdue."""
    matter = runtime.projections.unfinished.list_open()[0]
    return matter.waiting_until + timedelta(hours=1)


def test_a_silent_round_is_recorded_too() -> None:
    """A round that decides "not yet" is exactly the data tuning needs."""
    runtime = _runtime()
    try:
        runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5))

        rows = runtime.projections.observability.list_decisions(limit=20)
        assert len(rows) == 1
        assert rows[0]["acted"] == 0
        assert rows[0]["reason"], "the reason must survive: it is the whole point"
        # The losers are kept: every candidate's breakdown, not just the winner's.
        assert isinstance(rows[0]["payload_json"]["outcome"]["utilities"], list)
    finally:
        runtime.close()


def test_an_acting_round_records_the_candidate_and_the_hazard() -> None:
    """The acted round keeps which candidate won and the draw that let it."""
    runtime = _runtime()
    try:
        runtime.process_user_message(content=MATTER_TEXT, timestamp=BASE_TIME)
        runtime.endogenous_round(now=_due_at(runtime), force=True)

        rows = runtime.projections.observability.list_decisions(limit=20)
        acted = [row for row in rows if row["acted"]]
        assert acted, [row["reason"] for row in rows]
        assert acted[-1]["chosen_candidate_id"]
        assert acted[-1]["hazard"] > 0
        assert acted[-1]["action_probability"] > 0
    finally:
        runtime.close()


def test_the_state_curve_has_one_sample_per_round() -> None:
    """Two verdicts and two samples: the curve is written where the decision is."""
    runtime = _runtime()
    try:
        for step in range(3):
            runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=step + 1))

        samples = runtime.projections.observability.list_state_samples(limit=50)
        rows = runtime.projections.observability.list_decisions(limit=50)
        assert len(samples) == len(rows) == 3
        assert samples[0]["sampled_at"] < samples[-1]["sampled_at"], "oldest first"
        state = runtime.state()
        assert samples[-1]["restraint"] == state.restraint
    finally:
        runtime.close()


def test_a_paused_round_is_recorded_as_a_verdict() -> None:
    """The commonest verdict of an active conversation: "she is talking, hold back".

    This path returns before the motivational game, so without its own write the
    busiest hours of the week would leave no decision rows at all.
    """
    runtime = _runtime()
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        runtime.endogenous_round(now=BASE_TIME + timedelta(seconds=5))

        rows = runtime.projections.observability.list_decisions(limit=10)
        assert rows, "a paused round is still a verdict"
        assert rows[-1]["reason"] == "foreground_pause"
        assert rows[-1]["acted"] == 0
        assert runtime.projections.observability.list_state_samples(limit=10), "curve too"
    finally:
        runtime.close()


def test_the_context_render_is_recorded() -> None:
    """What the host was told, per render: version, size, section sizes."""
    runtime = _runtime()
    try:
        with TestClient(create_app(runtime, runtime.config)) as client:
            response = client.post(
                "/v1/context",
                json={"session": "webchat:FriendMessage:u1", "trigger": "llm_request"},
            )
        assert response.status_code == 200

        row = runtime.projections.db.query_one(
            "SELECT conversation_id, metadata_json FROM raw_events "
            "WHERE content = 'context_rendered' ORDER BY rowid DESC LIMIT 1",
        )
        assert row is not None, "the render must leave a record"
        assert row["conversation_id"] == "webchat:FriendMessage:u1"
        assert '"trigger":"llm_request"' in row["metadata_json"].replace(" ", "")
        assert '"chars"' in row["metadata_json"].replace(" ", "")
        # Text is opt-in: it is reconstructible, and a week of it is bulk.
        assert '"text"' not in row["metadata_json"].replace(" ", "")
    finally:
        runtime.close()


def test_the_render_text_is_kept_when_asked_for() -> None:
    """With ``record_context_text`` the block itself is kept, for replay."""
    runtime = _runtime(record_context_text=True)
    try:
        with TestClient(create_app(runtime, runtime.config)) as client:
            client.post("/v1/context", json={"session": "s1", "trigger": "llm_request"})
        row = runtime.projections.db.query_one(
            "SELECT metadata_json FROM raw_events WHERE content = 'context_rendered' "
            "ORDER BY rowid DESC LIMIT 1",
        )
        assert row is not None
        assert '"text"' in row["metadata_json"].replace(" ", "")
    finally:
        runtime.close()


def test_observability_can_be_switched_off() -> None:
    """An operator who does not want the rows gets none of them."""
    runtime = _runtime(enabled=False)
    try:
        runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5))
        with TestClient(create_app(runtime, runtime.config)) as client:
            client.post("/v1/context", json={"session": "s1", "trigger": "llm_request"})

        assert runtime.projections.observability.list_decisions(limit=10) == []
        assert runtime.projections.observability.list_state_samples(limit=10) == []
        row = runtime.projections.db.query_one(
            "SELECT COUNT(*) AS n FROM raw_events WHERE content = 'context_rendered'",
        )
        assert row["n"] == 0
    finally:
        runtime.close()
