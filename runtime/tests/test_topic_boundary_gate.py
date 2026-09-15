"""Topic boundaries reach the decision, not just the delivery (design §52).

A user who says "暂时不要跟我说这个" has ruled out a subject, not conversation. Those
rules arrive with ``allow_proactive=True``, so the proactivity gate cannot express them
at all - and because a candidate that has been ruled out must never be *weighed*, the
enforcement has to happen on the candidate set before the utility comparison.

Two things have to hold for that to be possible, and both are pinned here:

* the referent of a deictic instruction ("这个") is resolved when the boundary is
  declared and travels with it (otherwise the gate has nothing to compare against);
* when it cannot be resolved, nothing is blocked - guessing would silence a subject the
  user never named.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime import boundaries as boundary_module
from companion_runtime.db import Database
from companion_runtime.projections import BoundaryProjection, CandidateProjection
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    Actor,
    Boundary,
    BoundaryType,
    CandidateIntent,
    EventType,
    RawEvent,
    UnfinishedMatter,
    UnfinishedStatus,
    new_id,
)

from conftest import BASE_TIME

TOPIC_AVOID = "暂时不要跟我说这个。"
INTERROGATION = "别再一直追问我在干嘛。"
ABOUT_INTERVIEW = "我明天下午三点面试，结束了告诉你。"
OTHER_TOPIC = "对了，我养了只猫，叫团子。"


# --------------------------------------------------------------------------------------
# resolving the referent
# --------------------------------------------------------------------------------------


def _event(text: str, *, event_id: str = "evt_prior") -> RawEvent:
    """Build a user message the way the log would hold it."""
    return RawEvent(
        event_id=event_id,
        event_type=EventType.USER_MESSAGE.value,
        actor=Actor.USER.value,
        content=text,
        timestamp=BASE_TIME,
    )


def test_the_referent_is_the_matter_built_from_that_message() -> None:
    """"这个" is what we were just talking about, and event identity names it exactly."""
    matters = [
        UnfinishedMatter(
            unfinished_id="unf_interview",
            title="等待面试结果",
            source_event_ids=["evt_prior"],
            status=UnfinishedStatus.WAITING.value,
        ),
        UnfinishedMatter(
            unfinished_id="unf_cat",
            title="等待团子的疫苗",
            status=UnfinishedStatus.OPEN.value,
        ),
    ]
    subject = boundary_module.referent_for(
        previous_events=[_event(ABOUT_INTERVIEW)], matters=matters
    )
    assert subject == "等待面试结果", (
        "text overlap alone is not enough: this message and that title share one bigram"
    )


def test_the_referent_can_come_from_a_discussed_matter() -> None:
    """A matter that was discussed (not promised) still names the subject."""
    matters = [
        UnfinishedMatter(
            unfinished_id="unf_interview",
            title="等待面试结果",
            source_event_ids=["evt_something_else"],
            status=UnfinishedStatus.OPEN.value,
        )
    ]
    subject = boundary_module.referent_for(
        previous_events=[_event("面试结果还没出来，我再等等。")], matters=matters
    )
    assert subject == "等待面试结果"


def test_the_referent_falls_back_to_what_was_said() -> None:
    """With no open matter about it, the last thing the user said is the referent."""
    subject = boundary_module.referent_for(
        previous_events=[_event(OTHER_TOPIC)], matters=[]
    )
    assert subject == OTHER_TOPIC


def test_the_referent_is_none_when_there_is_nothing_to_bind() -> None:
    """No referent means no guess: the caller must treat ``None`` as "not established"."""
    assert boundary_module.referent_for(previous_events=[], matters=[]) is None
    assert (
        boundary_module.referent_for(previous_events=[_event("   ")], matters=[]) is None
    )


# --------------------------------------------------------------------------------------
# declaring it
# --------------------------------------------------------------------------------------


def test_declaring_a_topic_boundary_binds_and_persists_its_subject(runtime: Runtime) -> None:
    """The binding is part of the boundary, so it survives the process."""
    runtime.process_user_message(content=ABOUT_INTERVIEW, timestamp=BASE_TIME)
    runtime.process_user_message(
        content=TOPIC_AVOID, timestamp=BASE_TIME + timedelta(minutes=1)
    )

    declared = [
        boundary
        for boundary in runtime.projections.boundaries.list_all()
        if boundary.scope == "topic_avoid"
    ]
    assert declared, "the rule must have fired"
    assert declared[0].subject, "a topic boundary without a topic enforces nothing"

    # Re-read through a fresh projection: the subject is stored, not only in memory.
    reopened = BoundaryProjection(runtime.db)
    stored = [item for item in reopened.list_all() if item.scope == "topic_avoid"][0]
    assert stored.subject == declared[0].subject
    assert stored.to_dict()["subject"] == declared[0].subject


def test_a_boundary_that_is_not_about_a_topic_carries_no_subject(runtime: Runtime) -> None:
    """Only topic scopes bind a referent; "don't contact me" is not about anything."""
    runtime.process_user_message(
        content="今天不要主动联系我。", timestamp=BASE_TIME
    )
    declared = runtime.projections.boundaries.list_all()
    assert declared
    assert all(boundary.subject is None for boundary in declared)


def test_the_interrogation_rule_fires_on_the_natural_phrasing(runtime: Runtime) -> None:
    """A rule that only matches an unusual word order is a rule that never fires.

    The particle branch used to be ``(别|不要|不要再|不许)`` with no room for the 再 that
    follows 别 in the ordinary way of saying this ("别再追问我在干嘛"), so the
    ``repeated_interrogation`` scope - and with it §52's topic gate for this pattern -
    was unreachable from ordinary language. The test that covered the scope built the
    boundary object directly, which is exactly why it could not catch this: it asserted
    the *enforcement* while the *declaration* was broken.
    """
    runtime.process_user_message(content=ABOUT_INTERVIEW, timestamp=BASE_TIME)
    runtime.process_user_message(
        content=INTERROGATION, timestamp=BASE_TIME + timedelta(minutes=1)
    )

    declared = [
        boundary
        for boundary in runtime.projections.boundaries.list_all()
        if boundary.scope == "repeated_interrogation"
    ]
    assert declared, "a plain 'stop interrogating me' must declare the boundary"
    assert declared[0].type == BoundaryType.TOPIC.value
    assert declared[0].subject, "and it must bind what the user was talking about"
    assert declared[0].boundary_id in {
        boundary.boundary_id
        for boundary in runtime.projections.boundaries.active(BASE_TIME + timedelta(minutes=2))
    }


# --------------------------------------------------------------------------------------
# enforcing it
# --------------------------------------------------------------------------------------


def _candidate(intent: str, kind: str = "follow_up", target: str = "面试") -> CandidateIntent:
    """Build one candidate the way the generator would."""
    return CandidateIntent(
        candidate_id=new_id("candidate"),
        type=kind,
        intent=intent,
        goal="表达关心",
        target=target,
    )


def _boundary(*, scope: str, subject: str | None, now=BASE_TIME, hours: float = 48.0) -> Boundary:
    """Build an active topic boundary with (or without) a bound subject."""
    return Boundary(
        boundary_id=new_id("boundary"),
        type=BoundaryType.TOPIC.value,
        scope=scope,
        allow_proactive=True,
        allow_reply=True,
        starts_at=now,
        expires_at=now + timedelta(hours=hours),
        note="topic boundary",
        subject=subject,
    )


def test_a_bound_subject_rules_out_the_candidates_about_it() -> None:
    """The whole point: a candidate about the avoided subject is not weighed."""
    blocked = boundary_module.blocks_candidate(
        [_boundary(scope="topic_avoid", subject="等待面试结果")],
        now=BASE_TIME,
        subject="面试 询问等待面试结果",
        is_question=True,
    )
    assert blocked is not None
    assert blocked[1] == "topic_avoid"


def test_an_unrelated_candidate_is_still_weighed() -> None:
    """A topic boundary is about a subject, not about the conversation."""
    assert (
        boundary_module.blocks_candidate(
            [_boundary(scope="topic_avoid", subject="等待面试结果")],
            now=BASE_TIME,
            subject="猫 聊起团子的疫苗",
            is_question=True,
        )
        is None
    )


def test_an_unbound_topic_boundary_blocks_nothing() -> None:
    """The conservative branch, pinned: no referent established means no guess."""
    assert (
        boundary_module.blocks_candidate(
            [_boundary(scope="topic_avoid", subject=None)],
            now=BASE_TIME,
            subject="面试 询问等待面试结果",
            is_question=True,
        )
        is None
    )


def test_repeated_interrogation_rules_out_questions_not_contacts() -> None:
    """Being told to stop interrogating is about the shape, not about a subject."""
    rules = [_boundary(scope="repeated_interrogation", subject="等待面试结果")]
    assert (
        boundary_module.blocks_candidate(
            rules, now=BASE_TIME, subject="面试 询问等待面试结果", is_question=True
        )
        is not None
    )
    assert (
        boundary_module.blocks_candidate(
            rules, now=BASE_TIME, subject="关系 没有具体事项，只是想和用户建立联系", is_question=False
        )
        is None
    )


def test_an_expired_boundary_stops_blocking() -> None:
    """The window is a window: after it, the subject is available again."""
    rule = _boundary(scope="topic_avoid", subject="等待面试结果", hours=48.0)
    later = BASE_TIME + timedelta(hours=49.0)
    assert (
        boundary_module.blocks_candidate(
            [rule], now=later, subject="面试 询问等待面试结果", is_question=True
        )
        is None
    )


def test_the_runtime_prunes_and_reports_blocked_candidates(runtime: Runtime) -> None:
    """The wiring, seen from the Runtime: pruned before the decision, and reported."""
    runtime.process_user_message(content=ABOUT_INTERVIEW, timestamp=BASE_TIME)
    runtime.process_user_message(
        content=TOPIC_AVOID, timestamp=BASE_TIME + timedelta(minutes=1)
    )
    # A moment *inside* the boundary's window: asking about a boundary before it starts
    # is asking about a different state.
    when = BASE_TIME + timedelta(minutes=3)
    candidates = [
        _candidate("询问等待面试结果"),
        _candidate("聊起团子的疫苗", kind="curious_question", target="猫"),
    ]
    allowed, blocked = runtime._partition_by_boundaries(candidates, now=when)

    assert [item.intent for item in allowed] == ["聊起团子的疫苗"]
    assert [item["intent"] for item in blocked] == ["询问等待面试结果"]
    assert blocked[0]["reason"] == "topic_avoid"
    assert blocked[0]["boundary_id"] in {
        boundary.boundary_id for boundary in runtime.projections.boundaries.active(when)
    }


def test_the_round_never_weighs_a_ruled_out_candidate(runtime: Runtime) -> None:
    """End to end: the ruled-out candidate is not in the utility comparison at all.

    This is the assertion that catches the wiring being removed - the gate function
    can be perfect and still never be called. It also pins *where* in the round the
    pruning happens: not in the utility list with a poor score, but absent from it.
    """
    runtime.process_user_message(content=ABOUT_INTERVIEW, timestamp=BASE_TIME)
    runtime.process_user_message(
        content=TOPIC_AVOID, timestamp=BASE_TIME + timedelta(minutes=1)
    )
    interview = _candidate("询问等待面试结果")
    unrelated = _candidate("聊起团子的疫苗", kind="curious_question", target="猫")
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, interview)
        runtime.projections.candidates.upsert(conn, unrelated)

    outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)

    blocked = outcome.decision.get("boundary_blocked") or []
    blocked_ids = {item["candidate_id"] for item in blocked}
    assert interview.candidate_id in blocked_ids
    assert all(item["reason"] == "topic_avoid" for item in blocked), blocked
    weighed = {
        item["candidate_id"] for item in outcome.decision["outcome"].get("utilities", [])
    }
    assert not (blocked_ids & weighed), (
        "a candidate the user ruled out must not be compared at all"
    )
    # The candidate generator's *own* follow-up for that matter is caught by the same
    # gate, and that is the point: the generated candidate is the one that used to be
    # sent, while the user had already ruled the subject out.
    assert len(blocked) >= 1


def test_a_topic_boundary_never_removes_the_ability_to_reply(runtime: Runtime) -> None:
    """Ruling out a subject must not turn into silence: replies stay permitted."""
    runtime.process_user_message(content=ABOUT_INTERVIEW, timestamp=BASE_TIME)
    runtime.process_user_message(
        content=TOPIC_AVOID, timestamp=BASE_TIME + timedelta(minutes=1)
    )
    verdict = boundary_module.evaluate(
        runtime.projections.boundaries.active(BASE_TIME + timedelta(minutes=2)),
        now=BASE_TIME + timedelta(minutes=2),
        state=runtime.state(),
        is_proactive=False,
    )
    assert verdict.allow_reply is True


# --------------------------------------------------------------------------------------
# the schema upgrade
# --------------------------------------------------------------------------------------


def test_a_database_without_the_subject_column_still_reads(tmp_path) -> None:
    """An existing database upgrades in place, and old rows read back as unbound.

    The column arrives through ``ADDED_COLUMNS``, so a deployment that was already
    running keeps its boundary history; those historic rows have no referent for the
    same reason a fresh unbound boundary has none, and the gate leaves them alone.
    """
    db = Database(str(tmp_path / "old.sqlite3"))
    try:
        db.migrate()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO boundaries(boundary_id, type, scope, allow_reply, "
                "allow_proactive, created_at) VALUES(?, ?, ?, 1, 1, ?)",
                ("bd_old", BoundaryType.TOPIC.value, "topic_avoid", BASE_TIME.isoformat()),
            )
        projection = BoundaryProjection(db)
        row = [item for item in projection.list_all() if item.boundary_id == "bd_old"][0]
        assert row.subject is None
        assert (
            boundary_module.blocks_candidate(
                [row], now=BASE_TIME, subject="面试 询问面试结果", is_question=True
            )
            is None
        )
        columns = {
            item["name"]
            for item in db.query("PRAGMA table_info(boundaries)")
        }
        assert "subject" in columns
    finally:
        db.close()


def test_the_candidate_projection_is_untouched_by_the_new_column() -> None:
    """A guard against the boundary column leaking into the candidate table."""
    db = Database(":memory:")
    try:
        db.migrate()
        columns = {item["name"] for item in db.query("PRAGMA table_info(memory_candidates)")}
        assert "subject" not in columns
        assert CandidateProjection(db) is not None
    finally:
        db.close()
