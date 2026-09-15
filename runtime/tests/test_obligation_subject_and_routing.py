"""A settled obligation keeps its subject, and an intention finds its own chat.

Both defects here were found by ``scripts/blackbox_user_simulation.py``, which drives
the real plugin against a real Runtime over HTTP. They are written from the failure
side, because neither produced an exception - both produced plausible-looking state
that only showed up days later in the user's chat:

1. **A reported result re-opened the obligation it had just closed.** Ingest resolves
   matters *before* it detects new ones precisely so a completion statement cannot
   re-open what it finished, but that protection travelled through
   ``resolved_topics``, and the ``result_reported`` rule declares no topics at all.
   With an empty subject list, the only remaining brake was ``existing`` - and
   ``existing`` was the set of *open* matters, from which the matter had just been
   removed a few lines earlier. So "面试过了！" closed the interview matter and, in
   the same breath, opened a fresh "等待面试结果" from the same sentence, and the
   user was asked about the interview for two more days.
2. **An obligation formed in one chat was delivered into another chat.** A follow-up
   candidate's source is ``unfinished:<id>``, which is not an event id, so the
   conversation lookup found no events for it and fell back to "the conversation of
   the most recent user message anywhere" - i.e. whichever chat spoke last.

The property under test in the first part is therefore not "the same message does not
create two matters" but "the *subject* of a just-settled obligation is spoken for".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator

import pytest

from companion_runtime import candidate as candidate_module
from companion_runtime import projections as projections_module
from companion_runtime import unfinished as unfinished_module
from companion_runtime.eventlog import EventQuery
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    Actor,
    CandidateIntent,
    EventType,
    Memory,
    RawEvent,
    UnfinishedMatter,
    new_id,
)

from conftest import BASE_TIME, build_config

#: Two distinct sessions, shaped like AstrBot's ``platform:type:id`` origins.
SESSION_A = "webchat:FriendMessage:10001"
SESSION_B = "webchat:GroupMessage:20002"

#: The user's promise, and the sentence that reports its outcome. The reporting
#: sentence is the one that used to open a matter about the very interview it ended.
PROMISE = "我明天下午三点面试，结束了告诉你。"
REPORTED = "面试过了！"
TOPIC_BAN = "以后别再提面试这件事了。"


# --------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------


class SimulatedClock:
    """A settable clock, mirroring the black-box harness's process-wide clock.

    The projection layer stamps a matter's ``updated_at`` from its *own* clock, while
    the ingest path decides "how old is this settlement" from the ingest ``stamp``.
    Those are the same instant in a real deployment (and in the simulation, which
    rebinds every project module's ``utcnow``), so the tests must keep them together
    too: with the wall clock in play, ``BASE_TIME`` lands in the past and every
    settlement looks either ancient or brand new depending on the day the suite runs.
    """

    def __init__(self, moment: datetime) -> None:
        """Start the clock at ``moment``."""
        self._moment = moment

    def now(self) -> datetime:
        """Return the current simulated moment."""
        return self._moment

    def set(self, moment: datetime) -> datetime:
        """Jump to ``moment`` and return it."""
        self._moment = moment
        return self._moment


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimulatedClock]:
    """Bind the projection layer's clock to the simulated timeline.

    Only ``companion_runtime.projections`` is rebound: that is the module that writes
    a matter's ``updated_at`` on both creation and resolution, which is the field the
    subject guard reads back.
    """
    simulated = SimulatedClock(BASE_TIME)
    monkeypatch.setattr(projections_module, "utcnow", simulated.now)
    yield simulated


def build_runtime() -> Runtime:
    """Build an in-memory Runtime on the simulated timeline."""
    config = build_config()
    config.conversation_id = "default"
    return Runtime(config=config, seed=1234, created_at=BASE_TIME)


def event(
    content: str,
    *,
    timestamp: datetime = BASE_TIME,
    conversation_id: str = SESSION_A,
    event_id: str = "evt_test",
) -> RawEvent:
    """Build an in-memory raw event for the detector-level assertions."""
    return RawEvent(
        event_id=event_id,
        event_type=EventType.USER_MESSAGE.value,
        timestamp=timestamp,
        actor=Actor.USER.value,
        conversation_id=conversation_id,
        content=content,
    )


def say(
    runtime: Runtime,
    clock: SimulatedClock,
    text: str,
    *,
    at: datetime,
    session: str = SESSION_A,
):
    """Ingest one user message at ``at``, moving the simulated clock there first."""
    clock.set(at)
    return runtime.process_user_message(
        content=text, conversation_id=session, timestamp=at
    )


def guards(runtime: Runtime, *, now: datetime) -> list[UnfinishedMatter]:
    """Return the subject guards the ingest path would use at ``now``."""
    return unfinished_module.subject_guards(
        runtime.projections.unfinished.list_all(limit=200), now=now
    )


def commit(
    runtime: Runtime, candidate: CandidateIntent, *, now: datetime
) -> tuple[str, str]:
    """Commit ``candidate`` the way the motivational round does, and return its ids."""
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        return runtime._commit_attempt(conn, chosen=candidate, state=state, now=now)


def committed_conversation(runtime: Runtime) -> str:
    """Return the conversation of the newest ``proactive_committed`` record."""
    records = runtime.events.read(
        EventQuery(
            event_types=[EventType.PROACTIVE_COMMITTED.value], limit=5, newest_first=True
        )
    )
    assert records, "the commit should have left a proactive_committed record"
    return str(records[0].conversation_id)


# --------------------------------------------------------------------------------------
# 1-4: the subject of a settled obligation stays spoken for
# --------------------------------------------------------------------------------------


class TestAReportedResultDoesNotReopenItsOwnSubject:
    """The sentence that reports a result must not promise the result again."""

    def test_the_result_report_leaves_one_settled_matter_and_creates_none(
        self, clock: SimulatedClock
    ) -> None:
        """One promise, one report, one matter - and that matter is resolved."""
        runtime = build_runtime()
        try:
            promised = say(runtime, clock, PROMISE, at=BASE_TIME)
            assert promised.unfinished_created, "the promise should open one matter"

            reported_at = BASE_TIME + timedelta(hours=20)
            reported = say(runtime, clock, REPORTED, at=reported_at)

            assert reported.unfinished_resolved == promised.unfinished_created
            assert reported.unfinished_created == [], (
                "the message that reported the result also opened a matter about it: "
                f"{[matter.title for matter in runtime.projections.unfinished.list_open()]}"
            )
            matters = runtime.projections.unfinished.list_all()
            assert len(matters) == 1, [matter.title for matter in matters]
            assert matters[0].status == "resolved"

            # The detector itself, on the same message, with the same guards the
            # ingest path now passes: the rule's own subject list is empty, which is
            # exactly why the guard has to carry the subject.
            report_event = event(REPORTED, timestamp=reported_at)
            assert (
                unfinished_module.resolved_topics(report_event, [("u_any", "result_reported")])
                == []
            ), "the result rule declares no subjects; the guard is the only brake left"
            assert (
                unfinished_module.detect(
                    report_event,
                    config=runtime.config,
                    existing=guards(runtime, now=reported_at),
                )
                == []
            ), "a settled obligation must keep its subject, so this is not a new promise"
        finally:
            runtime.close()

    def test_a_bare_mention_inside_the_window_creates_no_matter(
        self, clock: SimulatedClock
    ) -> None:
        """Forbidding the topic is not a promise about it either.

        This is the case that produced the phantom obligation in the black-box run:
        "以后别再提面试这件事了" names the subject, and a subject that is only
        guarded while it is *open* is no guard at all once it has been settled.
        """
        runtime = build_runtime()
        try:
            say(runtime, clock, PROMISE, at=BASE_TIME)
            reported_at = BASE_TIME + timedelta(hours=20)
            say(runtime, clock, REPORTED, at=reported_at)
            before = runtime.projections.unfinished.list_all()

            banned_at = reported_at + timedelta(hours=30)
            banned = say(runtime, clock, TOPIC_BAN, at=banned_at)

            assert banned.unfinished_created == [], (
                "a mention of a settled subject re-opened it: "
                f"{[matter.title for matter in runtime.projections.unfinished.list_open()]}"
            )
            after = runtime.projections.unfinished.list_all()
            assert len(after) == len(before) == 1
            assert runtime.projections.unfinished.list_open() == []
        finally:
            runtime.close()

    def test_a_genuine_new_promise_after_the_window_does_open_a_matter(
        self, clock: SimulatedClock
    ) -> None:
        """The guard is a reservation, not a permanent block on the subject."""
        runtime = build_runtime()
        try:
            say(runtime, clock, PROMISE, at=BASE_TIME)
            reported_at = BASE_TIME + timedelta(hours=20)
            say(runtime, clock, REPORTED, at=reported_at)

            later = reported_at + timedelta(
                seconds=unfinished_module.SETTLED_SUBJECT_RESERVATION_SECONDS + 3600
            )
            next_round = say(
                runtime, clock, "下周三还有个面试，结束告诉你", at=later
            )

            assert len(next_round.unfinished_created) == 1, (
                "a later interview is a new promise and must still be remembered"
            )
            titles = [matter.title for matter in runtime.projections.unfinished.list_open()]
            assert titles == ["等待面试结果"], titles
        finally:
            runtime.close()

    def test_a_different_subject_is_unaffected(self, clock: SimulatedClock) -> None:
        """Reserving the interview's subject must not reserve every subject."""
        runtime = build_runtime()
        try:
            say(runtime, clock, PROMISE, at=BASE_TIME)
            reported_at = BASE_TIME + timedelta(hours=20)
            say(runtime, clock, REPORTED, at=reported_at)

            exam = say(
                runtime,
                clock,
                "我下周要考试",
                at=reported_at + timedelta(hours=2),
            )

            assert len(exam.unfinished_created) == 1, (
                "an exam is a different subject from an interview: "
                f"{[matter.title for matter in runtime.projections.unfinished.list_open()]}"
            )
            titles = [matter.title for matter in runtime.projections.unfinished.list_open()]
            assert titles == ["等待考试结果"], titles
        finally:
            runtime.close()


class TestSubjectGuards:
    """The guard set is "live, plus recently released" - and nothing else."""

    def _matter(
        self, status: str, *, updated_at: datetime | None, title: str = "等待面试结果"
    ) -> UnfinishedMatter:
        """Build one matter with an explicit lifecycle timestamp."""
        return UnfinishedMatter(
            unfinished_id=new_id("unfinished"),
            title=title,
            status=status,
            updated_at=updated_at,
        )

    def test_a_live_matter_always_guards_its_subject(self) -> None:
        """Open, waiting, due and muted are all still live obligations."""
        live = [
            self._matter(status, updated_at=None)
            for status in sorted(unfinished_module.LIVE_MATTER_STATUSES)
        ]
        assert unfinished_module.LIVE_MATTER_STATUSES == frozenset(
            {"open", "waiting", "due", "muted"}
        )
        guarded = unfinished_module.subject_guards(live, now=BASE_TIME)
        assert {matter.status for matter in guarded} == {
            "open",
            "waiting",
            "due",
            "muted",
        }

    def test_a_matter_settled_one_second_ago_still_guards_its_subject(self) -> None:
        settled = self._matter(
            "resolved", updated_at=BASE_TIME - timedelta(seconds=1)
        )
        assert unfinished_module.subject_guards(
            [settled], now=BASE_TIME
        ) == [settled]

    def test_a_matter_settled_before_the_window_releases_its_subject(self) -> None:
        settled = self._matter(
            "resolved",
            updated_at=BASE_TIME
            - timedelta(
                seconds=unfinished_module.SETTLED_SUBJECT_RESERVATION_SECONDS + 1
            ),
        )
        assert unfinished_module.subject_guards([settled], now=BASE_TIME) == []

    def test_a_cancelled_matter_is_treated_like_a_settled_one(self) -> None:
        cancelled = self._matter("cancelled", updated_at=BASE_TIME)
        assert unfinished_module.subject_guards([cancelled], now=BASE_TIME) == [
            cancelled
        ]

    def test_a_matter_without_a_timestamp_stays_guarded(self) -> None:
        """A missing or unparsable stamp is no evidence that the subject was released."""
        unknown = self._matter("resolved", updated_at=None)
        assert unfinished_module.subject_guards([unknown], now=BASE_TIME) == [unknown]

    def test_a_future_timestamp_counts_as_recent(self) -> None:
        """A stamp ahead of ``now`` happens under a simulated clock, not in anger.

        Replaying the day moves the clock past the events it replays, so a settlement
        can look like it happened in the future; reading that as "long released" would
        re-open every subject the replay touched.
        """
        future = self._matter("resolved", updated_at=BASE_TIME + timedelta(days=2))
        assert unfinished_module.subject_guards([future], now=BASE_TIME) == [future]

    def test_the_window_is_configurable(self) -> None:
        settled = self._matter("resolved", updated_at=BASE_TIME - timedelta(hours=2))
        assert unfinished_module.subject_guards(
            [settled], now=BASE_TIME, reservation_seconds=3600.0
        ) == []
        assert unfinished_module.subject_guards(
            [settled], now=BASE_TIME, reservation_seconds=3 * 3600.0
        ) == [settled]

    def test_an_expired_matter_does_not_guard_its_subject(self) -> None:
        """Expiry is the obligation lapsing, which is not the same as settling it."""
        expired = self._matter("expired", updated_at=BASE_TIME)
        assert unfinished_module.subject_guards([expired], now=BASE_TIME) == []


# --------------------------------------------------------------------------------------
# 5-8: an intention is delivered to the chat it was formed in
# --------------------------------------------------------------------------------------


class TestAnObligationFindsItsOwnChat:
    """A candidate's sources are not all event ids, and the lookup must know that."""

    def _two_chats(self, runtime: Runtime, clock: SimulatedClock) -> UnfinishedMatter:
        """Open a matter in session B, then let session A speak more recently."""
        say(
            runtime,
            clock,
            "我明天下午有个体检，出结果告诉你。",
            at=BASE_TIME,
            session=SESSION_B,
        )
        say(
            runtime,
            clock,
            "我先去健身房了，回头聊。",
            at=BASE_TIME + timedelta(hours=1),
            session=SESSION_A,
        )
        open_matters = runtime.projections.unfinished.list_open()
        assert len(open_matters) == 1, [matter.title for matter in open_matters]
        return open_matters[0]

    def _follow_up(self, runtime: Runtime, matter: UnfinishedMatter, *, now: datetime):
        """Return the rule-generated follow-up candidate for ``matter``."""
        state = runtime.projections.runtime.ensure()
        produced = candidate_module.generate(
            state=state,
            config=runtime.config,
            unfinished=[matter],
            now=now,
        )
        follow_ups = [item for item in produced if item.type == "follow_up"]
        assert len(follow_ups) == 1, [item.type for item in produced]
        assert follow_ups[0].sources == [
            f"{candidate_module.UNFINISHED_SOURCE_PREFIX}{matter.unfinished_id}"
        ]
        return follow_ups[0]

    def test_a_follow_up_is_delivered_to_the_chat_that_promised_it(
        self, clock: SimulatedClock
    ) -> None:
        """The exact defect: session B's promise must not be sent into session A.

        Session A has spoken *later* than session B, so the pre-fix fallback - "the
        conversation of the newest user message anywhere" - resolves to A, and the
        group chat never hears about the appointment it asked to be reminded of.
        """
        runtime = build_runtime()
        try:
            matter = self._two_chats(runtime, clock)
            follow_up = self._follow_up(
                runtime, matter, now=BASE_TIME + timedelta(hours=2)
            )
            assert (
                runtime._event_ids_behind(follow_up.sources[0])
                == matter.source_event_ids
                != []
            ), "an unfinished source must resolve through the matter's own events"

            committed_at = BASE_TIME + timedelta(hours=2)
            attempt_id, outbox_id = commit(runtime, follow_up, now=committed_at)

            item = runtime.projections.outbox.get(outbox_id)
            assert item is not None
            assert item.conversation_id == SESSION_B, (
                "the follow-up was routed to whichever chat spoke last "
                f"({item.conversation_id}) instead of the one that made the promise"
            )
            assert committed_conversation(runtime) == SESSION_B

            attempt = runtime.projections.attempts.get(attempt_id)
            assert attempt is not None and attempt.outbox_id == outbox_id
        finally:
            runtime.close()

    def test_a_memory_sourced_candidate_resolves_through_the_memory(
        self, clock: SimulatedClock
    ) -> None:
        """``memory:<id>`` is an internal id too, and knows the events behind it."""
        runtime = build_runtime()
        try:
            matter = self._two_chats(runtime, clock)
            recorded = runtime.events.read(
                EventQuery(
                    event_types=[EventType.USER_MESSAGE.value],
                    conversation_id=SESSION_B,
                    limit=1,
                    newest_first=True,
                )
            )
            assert recorded, "session B's message should be in the log"
            memory = Memory(
                memory_id=new_id("memory"),
                kind="fact",
                summary="用户明天下午体检，会告知结果。",
                source_event_ids=[recorded[0].event_id],
            )
            with runtime.db.transaction() as conn:
                runtime.projections.memory.upsert_memory(conn, memory)

            candidate = CandidateIntent(
                candidate_id=new_id("candidate"),
                type="curious_question",
                intent="问问体检结果",
                goal="延续共同经历",
                target="体检",
                sources=[f"{candidate_module.MEMORY_SOURCE_PREFIX}{memory.memory_id}"],
            )
            assert runtime._event_ids_behind(candidate.sources[0]) == [
                recorded[0].event_id
            ]
            assert matter.source_event_ids == [recorded[0].event_id]

            _attempt_id, outbox_id = commit(
                runtime, candidate, now=BASE_TIME + timedelta(hours=2)
            )

            item = runtime.projections.outbox.get(outbox_id)
            assert item is not None and item.conversation_id == SESSION_B
            assert committed_conversation(runtime) == SESSION_B
        finally:
            runtime.close()

    def test_a_source_that_is_not_an_event_id_falls_back_instead_of_raising(
        self, clock: SimulatedClock
    ) -> None:
        """The permanent contact drive has no event behind it; it still gets sent.

        ``internal_approach_drive`` contains no ``:``, so it is *shaped* like an event
        id - and the honest answer is "there is no such event", which must degrade to
        the current conversation rather than to a failed commit.
        """
        runtime = build_runtime()
        try:
            self._two_chats(runtime, clock)
            candidate = CandidateIntent(
                candidate_id=new_id("candidate"),
                type="contact",
                intent="只是想和用户建立联系",
                goal="维持关系的连续性",
                target="relationship",
                sources=[candidate_module.CONTACT_SOURCE],
            )
            assert runtime._event_ids_behind(candidate_module.CONTACT_SOURCE) == [
                candidate_module.CONTACT_SOURCE
            ]
            assert (
                runtime._conversation_for(candidate, now=BASE_TIME + timedelta(hours=2))
                == SESSION_A
            ), "with no event behind it, the newest conversation is the right guess"

            _attempt_id, outbox_id = commit(
                runtime, candidate, now=BASE_TIME + timedelta(hours=2)
            )
            item = runtime.projections.outbox.get(outbox_id)
            assert item is not None and item.conversation_id == SESSION_A
        finally:
            runtime.close()

    def test_an_unknown_internal_namespace_is_never_an_event_id(
        self, clock: SimulatedClock
    ) -> None:
        """``emotion:<id>`` names another internal namespace, so it resolves to nothing.

        Sending it to the event log would be a type error the log happens to swallow:
        the lookup returns nothing either way, so the bug hides until a namespace
        collides with an event id and the intention is routed to a stranger's chat.
        """
        runtime = build_runtime()
        try:
            self._two_chats(runtime, clock)
            for source in (
                f"{candidate_module.EMOTION_SOURCE_PREFIX}emo_1",
                f"{candidate_module.SITUATION_SOURCE_PREFIX}sit_1",
                "emotion:",
            ):
                assert runtime._event_ids_behind(source) == [], source

            candidate = CandidateIntent(
                candidate_id=new_id("candidate"),
                type="share",
                intent="说说最近的心情",
                goal="分享当下",
                target="心情",
                sources=[f"{candidate_module.EMOTION_SOURCE_PREFIX}emo_1"],
            )
            assert (
                runtime._conversation_for(candidate, now=BASE_TIME + timedelta(hours=2))
                == SESSION_A
            )
        finally:
            runtime.close()

    def test_a_dangling_internal_id_does_not_break_the_commit(
        self, clock: SimulatedClock
    ) -> None:
        """A matter that no longer exists must not fail the send."""
        runtime = build_runtime()
        try:
            self._two_chats(runtime, clock)
            candidate = CandidateIntent(
                candidate_id=new_id("candidate"),
                type="follow_up",
                intent="询问等待检查结果",
                goal="了解后续进展",
                target="等待检查结果",
                sources=[f"{candidate_module.UNFINISHED_SOURCE_PREFIX}unf_missing"],
            )
            assert runtime._event_ids_behind(candidate.sources[0]) == []
            _attempt_id, outbox_id = commit(
                runtime, candidate, now=BASE_TIME + timedelta(hours=2)
            )
            item = runtime.projections.outbox.get(outbox_id)
            assert item is not None and item.conversation_id == SESSION_A
        finally:
            runtime.close()
