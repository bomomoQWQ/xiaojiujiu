"""The JSONL mirror must be a copy of the event log, and today it is not.

Found by `scripts/relationship_progression_simulation.py`, which compared the mirror with
the database at the end of a two-and-a-half-month run: `raw_events.jsonl` had 320 lines
while `/health.raw_events` reported 393 events, and **every** `user_message`,
`proactive_sent` and `assistant_message` was absent, with nothing in the log to say so.
`eventlog.py`'s own docstring and `runtime/README.md` both promise a faithful prefix.

The test below reproduces it in process, without the simulation, and is marked
``xfail(strict=True)``: it documents the defect without leaving a red suite, and the
moment the defect is fixed the test starts passing - which under ``strict`` turns into a
failure that forces whoever fixed it to delete the marker. That is deliberate: an
expected-failure marker must not outlive the bug it describes.

What the reproduction shows (measured, this file's first run):

    database events: 12   mirror lines: 4
      proactive_committed      db=3   mirror=1
      system                   db=6   mirror=3
      user_message             db=3   mirror=0

So the loss is not per event *type* by design - it is a batch: events appended inside a
transaction do not reach the mirror. The strongest lead is in `EventLog._mirror_after_commit`,
which keys its frames by transaction depth and registers the flush hooks only on the frame's
*first* event (``already_scheduled``), so a frame that outlives the transaction that
created it would swallow later events without a hook to write them. That is a hypothesis,
not a conclusion - the fix has to establish it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from companion_runtime.runtime import Runtime
from companion_runtime.typing import CandidateIntent, new_id

from conftest import BASE_TIME, build_config


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known defect: events appended inside a transaction never reach the JSONL mirror "
        "(user_message 3->0, system 6->3 in the minimal reproduction). Remove this marker "
        "when the mirror is complete."
    ),
)
def test_every_event_reaches_the_mirror(tmp_path: Path) -> None:
    """A mixed sequence, counted twice: once in the database, once in the mirror."""
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

        database = {event.event_id for event in runtime.events.recent(500)}
        mirrored = {
            line.strip()
            for line in (tmp_path / "raw_events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    finally:
        runtime.close()

    assert len(mirrored) == len(database), (
        f"the mirror must hold every event: {len(database)} in the database, "
        f"{len(mirrored)} in the mirror"
    )


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
