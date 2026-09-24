"""The JSONL mirror must be a copy of the event log, and it now is.

Found by `scripts/relationship_progression_simulation.py`, which compared the mirror with
the database at the end of a two-and-a-half-month run: `raw_events.jsonl` had 320 lines
while `/health.raw_events` reported 393 events, and **every** `user_message`,
`proactive_sent` and `assistant_message` was absent, with nothing in the log to say so.
`eventlog.py`'s own docstring and `runtime/README.md` both promise a faithful prefix.

The cause was in `EventLog._release_frame`. A released savepoint hands its queued lines to
its *enclosing* level, and the enclosing level was looked up as "the frame registered one
depth up" - which only exists if that level has already logged an event of its own. A user
message reaches the log two levels deep and was the only event of its savepoint, so the
lookup answered `None` and the lines were dropped. The enclosing level now gets a frame of
its own when it has none, so the lines wait for the commit that makes them durable.

These tests were written before the fix and the first one carried
``xfail(strict=True)`` - deliberately, so that fixing the defect would turn XPASS into a
failure and force the marker's removal rather than letting an expected-failure marker
outlive its bug. That is what happened here; the marker is gone.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from companion_runtime.runtime import Runtime
from companion_runtime.typing import CandidateIntent, EventType, new_id

from conftest import BASE_TIME, build_config


def _mirrored_events(path: Path) -> dict[str, str]:
    """Return ``{event_id: event_type}`` for everything the mirror holds."""
    import json

    mirrored: dict[str, str] = {}
    if not path.exists():
        return mirrored
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        mirrored[payload["event_id"]] = payload["event_type"]
    return mirrored


def test_every_event_reaches_the_mirror(tmp_path: Path) -> None:
    """A mixed sequence, compared by identifier and type - not by count alone."""
    config = build_config()
    config.storage.database_path = str(tmp_path / "mirror.sqlite3")
    config.storage.raw_log_path = str(tmp_path / "raw_events.jsonl")
    # The test fixture disables the mirror (most tests do not want a file); the shipped
    # configuration enables it, and this test is about the shipped behaviour.
    config.storage.mirror_raw_events = True
    runtime = Runtime(config=config, seed=3, created_at=BASE_TIME)
    try:
        runtime.process_user_message(content="我明天下午三点面试。", timestamp=BASE_TIME)
        candidate = CandidateIntent(
            candidate_id=new_id("candidate"),
            type="follow_up",
            intent="问面试结果",
            goal="关心",
            sources=["unfinished:unf_mirror"],
        )
        with runtime.write_session():
            with runtime.db.transaction() as conn:
                runtime.projections.candidates.upsert(conn, candidate)
                state = runtime.projections.runtime.ensure()
                runtime._commit_attempt(conn, chosen=candidate, state=state, now=BASE_TIME)

        database = {event.event_id: event.event_type for event in runtime.events.recent(500)}
        mirrored = _mirrored_events(tmp_path / "raw_events.jsonl")
    finally:
        runtime.close()

    missing = {event_id: kind for event_id, kind in database.items() if event_id not in mirrored}
    assert not missing, f"events absent from the mirror: {missing}"
    assert mirrored == database, "the mirror must hold the same events, not merely as many"
    # The three types the simulation found missing, named so a regression is legible: a
    # user message reaches the log two levels deep, which is the shape that used to lose
    # its lines outright.
    assert EventType.USER_MESSAGE.value in mirrored.values()
    assert EventType.PROACTIVE_COMMITTED.value in mirrored.values()


def test_a_run_without_the_mirror_still_works(tmp_path: Path) -> None:
    """The control: with the mirror off, nothing is written and nothing breaks."""
    config = build_config()
    config.storage.database_path = str(tmp_path / "nomirror.sqlite3")
    config.storage.raw_log_path = str(tmp_path / "absent.jsonl")
    runtime = Runtime(config=config, seed=4, created_at=BASE_TIME)
    try:
        runtime.process_user_message(content="你好。", timestamp=BASE_TIME)
        runtime.lazy_tick(BASE_TIME + timedelta(minutes=1))
    finally:
        runtime.close()
    assert not (tmp_path / "absent.jsonl").exists()
