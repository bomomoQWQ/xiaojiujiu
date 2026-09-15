"""The candidate shapes that had no producer, and the state that invalidates them.

Two audited gaps are under test here.

**Gap 1 (audit item 5, design §37-§43).** ``validate_candidate`` declared seven
candidate types, but the rule generator only ever produced three of them: ``share``,
``repair`` and ``reply`` had no producer at all, and the ``emotion:`` / ``situation:``
source prefixes had no writer. Patch v0.2 §12's "re-evaluate -> repair candidate" path
was therefore unreachable with ``semantic.provider = "disabled"``. These tests build the
state each new shape needs through the public projections and typing constructors - the
real ingest path, the memory pipeline, real boundaries and real emotions - and then
assert that the shape appears, that it names the state it came from, and that it does
**not** appear when that state is absent.

Every candidate's ``sources`` are checked against the routing layer, because that is the
defect the last routing fix was about: a candidate whose source nothing can resolve is
delivered into whichever chat spoke last. The generators therefore put a resolvable
source (a raw event id, ``unfinished:`` or ``memory:``) on every candidate, and carry
``emotion:`` / ``situation:`` *in addition*. ``Runtime._event_ids_behind`` is called
directly in the assertions below - it is the one function that decides routing, and the
existing routing tests assert through it too.

**Gap 2 (audit item 7).** ``invalidate_when`` was matched through a four-entry keyword
table, so a candidate generated from real state could not be invalidated by the state it
came from. The generator now derives those conditions from the record behind the
candidate (a matter, a memory, a question, a boundary, an emotion), and two evaluators
read them: :func:`invalidated_by_situation` for text that says so, and
:func:`invalidated_by_source_state` for records that are resolved, archived, superseded,
answered or released.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from companion_runtime import candidate as candidate_module
from companion_runtime import memory as memory_module
from companion_runtime import unfinished as unfinished_module
from companion_runtime.config import RuntimeConfig
from companion_runtime.eventlog import EventQuery
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    ActionAttempt,
    Actor,
    Boundary,
    CandidateIntent,
    EventType,
    Memory,
    MemoryStatus,
    OutboxItem,
    UnfinishedMatter,
    UnfinishedStatus,
    new_id,
)
from companion_runtime.user_model import BehaviourReaction

from conftest import BASE_TIME, build_config

#: Two distinct sessions, shaped like AstrBot's ``platform:type:id`` origins.
SESSION_A = "webchat:FriendMessage:10001"
SESSION_B = "webchat:GroupMessage:20002"

#: A durable statement about the user -> a ``user_preference`` memory.
PREFERENCE = "我喜欢喝手冲咖啡，不加糖。"
#: A relational act -> a ``relationship`` memory, and a positive emotion event.
THANKS = "谢谢你那天陪我去医院。"
#: A question nothing answers -> the material for a ``reply`` candidate.
QUESTION = "你上次说的那家咖啡店叫什么名字？"
#: Explicit negative feedback about the character -> the material for ``repair``.
DISLIKE = "我讨厌你这样说话。"
#: A promise -> an unfinished matter, and therefore a follow-up candidate.
PROMISE = "我明天下午三点面试，结束了告诉你。"
#: The sentence that settles that promise.
REPORTED = "面试过了！"
#: A topic boundary is a complaint about the character; a temporal one is not.
BOUNDARY_INTERROGATION = "别一直追问我在干嘛。"
BOUNDARY_SILENCE = "今天不要找我了。"


# --------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------


def build_runtime() -> Runtime:
    """Build an in-memory Runtime on the fixed test timeline."""
    config = build_config()
    config.conversation_id = "default"
    return Runtime(config=config, seed=1234, created_at=BASE_TIME)


def say(
    runtime: Runtime,
    text: str,
    *,
    at: datetime,
    session: str = SESSION_B,
    reason: BehaviourReaction | None = None,
):
    """Ingest one user message at ``at`` through the real foreground path."""
    return runtime.process_user_message(
        content=text,
        conversation_id=session,
        timestamp=at,
        reason=reason,
    )


def generate(
    runtime: Runtime, *, now: datetime, **overrides: object
) -> list[CandidateIntent]:
    """Generate candidates from the Runtime's own state.

    This is deliberately the *call shape reported to the Runtime owner*: every input the
    new shapes need is read from a public projection, and ``overrides`` lets a test take
    one of them away to prove the shape then does not fire.
    """
    inputs: dict[str, object] = {
        "state": runtime.projections.runtime.ensure(),
        "config": runtime.config,
        "unfinished": runtime.projections.unfinished.list_open(),
        "activated": runtime.memory_store.activated_memories(limit=4),
        "existing": runtime.projections.candidates.list_active(limit=50),
        "spoken_for": [
            matter.title
            for matter in unfinished_module.subject_guards(
                runtime.projections.unfinished.list_all(limit=200), now=now
            )
        ],
        "now": now,
        "observations": runtime.projections.user_model.list_observations(limit=20),
        "emotions": runtime.projections.emotion.list_active(),
        "situations": runtime.projections.situation.list_active(limit=20),
        "boundaries": runtime.projections.boundaries.list_all(include_revoked=True),
        "recent_events": runtime.events.read(EventQuery(limit=40, newest_first=True)),
    }
    inputs.update(overrides)
    return candidate_module.generate(**inputs)  # type: ignore[arg-type]


def of_type(candidates: list[CandidateIntent], kind: str) -> list[CandidateIntent]:
    """Return the candidates of one type."""
    return [item for item in candidates if item.type == kind]


def consolidate(runtime: Runtime, *, now: datetime) -> list[Memory]:
    """Run the real consolidation pass and return every stored memory."""
    runtime.consolidate(now=now)
    return runtime.projections.memory.list_memories(status=None)


def activate(runtime: Runtime, memories: list[Memory], *, activation: float = 0.9) -> None:
    """Put memories into the working set.

    This is the write ``MemoryStore.activate`` performs, issued through the public
    projection so a test does not depend on a retrieval score.
    """
    with runtime.db.transaction() as conn:
        for memory in memories:
            runtime.projections.memory.upsert_activation(
                conn,
                candidate_module.ActivatedMemory(
                    memory_id=memory.memory_id, activation=activation
                ),
            )


def deliver_a_proactive_message(runtime: Runtime, *, session: str = SESSION_B) -> str:
    """Record a sent attempt whose message went out in ``session``.

    Reply attribution looks up the newest *sent* attempt **in the conversation the reply
    arrived in**, and it needs the outbox row to know that conversation. Both are built
    through the public projections, so the negative observation below comes from the real
    ingest path rather than from a hand-made record.
    """
    with runtime.db.transaction() as conn:
        outbox_id = runtime.projections.outbox.enqueue(
            conn,
            OutboxItem(
                outbox_id=new_id("outbox"),
                kind="send",
                payload={"text": "在忙什么呢？"},
                status="delivered",
                conversation_id=session,
            ),
        )
        attempt_id = runtime.projections.attempts.upsert(
            conn,
            ActionAttempt(
                attempt_id=new_id("attempt"),
                candidate_id=None,
                state="sent",
                intent="问候一下",
                outbox_id=outbox_id,
            ),
        )
    return attempt_id


def resolvable_conversations(runtime: Runtime, candidate: CandidateIntent) -> set[str]:
    """Return the conversations a candidate's sources route to.

    ``Runtime._event_ids_behind`` is the routing layer's resolver: it turns each source
    into the events behind it. Sources it cannot resolve contribute nothing, which is
    exactly the failure mode the new shapes must not rely on.
    """
    identifiers: list[str] = []
    for source in candidate.sources:
        identifiers.extend(runtime._event_ids_behind(source))
    events = runtime.events.get_many(identifiers) if identifiers else []
    return {str(event.conversation_id) for event in events if event.conversation_id}


def has_resolvable_source(runtime: Runtime, candidate: CandidateIntent) -> bool:
    """Return whether at least one of a candidate's sources names something real."""
    return any(runtime._event_ids_behind(source) for source in candidate.sources)


def append_assistant_message(
    runtime: Runtime, text: str, *, at: datetime, session: str
) -> str:
    """Record the character's own message, the way the host reports it."""
    with runtime.db.transaction() as conn:
        event = runtime.events.append(
            EventType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            content=text,
            conversation_id=session,
            timestamp=at,
            runtime_version=runtime.state().version,
            connection=conn,
        )
    return event.event_id


# --------------------------------------------------------------------------------------
# Gap 1 - share
# --------------------------------------------------------------------------------------


class TestShareIsProducedFromAStoredMemory:
    """``share`` is something of the character's own, taken from what it keeps."""

    def test_a_preference_memory_becomes_a_share_candidate(self) -> None:
        """The content is the stored summary; the source is the stored memory."""
        runtime = build_runtime()
        try:
            say(runtime, PREFERENCE, at=BASE_TIME)
            memories = consolidate(runtime, now=BASE_TIME + timedelta(minutes=5))
            assert [memory.kind for memory in memories] == ["user_preference"]
            activate(runtime, memories)

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=6))
            shares = of_type(produced, "share")
            assert len(shares) == 1, [item.type for item in produced]
            share = shares[0]
            assert share.sources == [f"memory:{memories[0].memory_id}"]
            # The intent quotes the memory, so the *content* comes from stored state.
            assert memories[0].summary[:12] in share.intent
            assert share.confidence >= runtime.config.candidate.confidence_floor
            # One memory yields one shape: offering it and asking about it are not both
            # proposed.
            assert of_type(produced, "curious_question") == []
        finally:
            runtime.close()

    def test_a_memory_of_another_kind_stays_a_curiosity_candidate(self) -> None:
        """Only what the character holds *about the user* is something to offer."""
        runtime = build_runtime()
        try:
            say(runtime, QUESTION, at=BASE_TIME)
            memories = consolidate(runtime, now=BASE_TIME + timedelta(minutes=5))
            assert [memory.kind for memory in memories] == ["episodic"]
            activate(runtime, memories)

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=6))
            assert of_type(produced, "share") == []
            assert len(of_type(produced, "curious_question")) == 1
        finally:
            runtime.close()

    def test_a_share_carries_the_active_emotion_as_an_extra_source(self) -> None:
        """``emotion:<id>`` is produced, and never as the only source."""
        runtime = build_runtime()
        try:
            say(runtime, THANKS, at=BASE_TIME)
            emotions = runtime.projections.emotion.list_active()
            assert [(event.direction, round(event.intensity, 2)) for event in emotions] == [
                ("+", 0.62)
            ]
            memories = consolidate(runtime, now=BASE_TIME + timedelta(seconds=30))
            assert [memory.kind for memory in memories] == ["relationship"]
            activate(runtime, memories)

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=1))
            shares = of_type(produced, "share")
            assert len(shares) == 1
            share = shares[0]
            assert share.sources == [
                f"memory:{memories[0].memory_id}",
                f"{candidate_module.EMOTION_SOURCE_PREFIX}{emotions[0].emotion_event_id}",
            ]
            # The emotion colours the offer; the memory is what makes it routable.
            assert has_resolvable_source(runtime, share)
            assert resolvable_conversations(runtime, share) == {SESSION_B}
        finally:
            runtime.close()

    def test_no_share_without_an_activated_memory(self) -> None:
        """The state the shape depends on, removed.

        Consolidation itself leaves a new memory in the working set (it is what makes a
        fresh fact part of what the character currently knows), so the state is removed
        explicitly here rather than by skipping the activation write.
        """
        runtime = build_runtime()
        try:
            say(runtime, PREFERENCE, at=BASE_TIME)
            consolidate(runtime, now=BASE_TIME + timedelta(minutes=5))
            assert runtime.memory_store.activated_memories(limit=4), (
                "consolidation should have put the new memory in the working set"
            )
            produced = generate(
                runtime, now=BASE_TIME + timedelta(minutes=6), activated=[]
            )
            assert of_type(produced, "share") == []
        finally:
            runtime.close()

    def test_a_memory_a_live_matter_owns_does_not_become_a_share(self) -> None:
        """The obligation path keeps its subject, exactly as the curiosity path does."""
        runtime = build_runtime()
        try:
            say(runtime, PREFERENCE, at=BASE_TIME)
            memories = consolidate(runtime, now=BASE_TIME + timedelta(minutes=5))
            activate(runtime, memories)
            with runtime.db.transaction() as conn:
                runtime.projections.unfinished.upsert(
                    conn,
                    UnfinishedMatter(
                        unfinished_id=new_id("unfinished"),
                        title="等待咖啡店的答复",
                        status=UnfinishedStatus.OPEN.value,
                        priority=0.6,
                    ),
                )

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=6))
            assert of_type(produced, "share") == []
            assert of_type(produced, "curious_question") == []
            assert len(of_type(produced, "follow_up")) == 1
        finally:
            runtime.close()


# --------------------------------------------------------------------------------------
# Gap 1 - repair
# --------------------------------------------------------------------------------------


class TestRepairIsProducedFromNegativeEvidence:
    """``repair`` needs the character to have done something the user reacted badly to."""

    def test_a_negative_reaction_to_the_characters_outreach_produces_a_repair(self) -> None:
        """The evidence is a stored interaction observation, not a guess."""
        runtime = build_runtime()
        try:
            attempt_id = deliver_a_proactive_message(runtime)
            outcome = say(
                runtime,
                DISLIKE,
                at=BASE_TIME + timedelta(minutes=30),
                reason=BehaviourReaction(replied=True, explicit_negative=True),
            )
            assert outcome.attributed_attempt_id == attempt_id
            observations = runtime.projections.user_model.list_observations(limit=5)
            assert len(observations) == 1
            assert observations[0]["outcome_json"]["explicit_negative"] is True
            assert observations[0]["action_json"]["proactive"] is True

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=31))
            repairs = of_type(produced, "repair")
            assert len(repairs) == 1, [item.type for item in produced]
            repair = repairs[0]
            # The event behind the observation is the routing source; the working
            # situation row that recorded it is carried as well.
            assert observations[0]["source_event_ids"][0] in repair.sources
            assert any(
                source.startswith(candidate_module.SITUATION_SOURCE_PREFIX)
                for source in repair.sources
            )
            assert has_resolvable_source(runtime, repair)
            assert resolvable_conversations(runtime, repair) == {SESSION_B}
        finally:
            runtime.close()

    def test_the_repair_keeps_the_emotion_that_carries_the_impulse(self) -> None:
        """The negative emotion is cited as ``emotion:<id>`` (patch v0.2 §12)."""
        runtime = build_runtime()
        try:
            deliver_a_proactive_message(runtime)
            say(
                runtime,
                DISLIKE,
                at=BASE_TIME + timedelta(minutes=30),
                reason=BehaviourReaction(replied=True, explicit_negative=True),
            )
            emotions = runtime.projections.emotion.list_active()
            assert [event.direction for event in emotions] == ["-"]

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=31))
            repairs = of_type(produced, "repair")
            assert len(repairs) == 1
            assert (
                f"{candidate_module.EMOTION_SOURCE_PREFIX}{emotions[0].emotion_event_id}"
                in repairs[0].sources
            )
            assert repairs[0].emotion_relevance == pytest.approx(emotions[0].intensity)
        finally:
            runtime.close()

    def test_a_topic_boundary_produces_a_repair(self) -> None:
        """A boundary reply is negative evidence about the character's behaviour."""
        runtime = build_runtime()
        try:
            declared = say(runtime, BOUNDARY_INTERROGATION, at=BASE_TIME)
            boundaries = runtime.projections.boundaries.list_all()
            assert [boundary.type for boundary in boundaries] == ["topic"]

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=1))
            repairs = of_type(produced, "repair")
            assert len(repairs) == 1
            assert declared.event.event_id in repairs[0].sources
            assert has_resolvable_source(runtime, repairs[0])
        finally:
            runtime.close()

    def test_a_temporal_boundary_produces_no_repair(self) -> None:
        """Being busy is not a complaint about what the character did."""
        runtime = build_runtime()
        try:
            say(runtime, BOUNDARY_SILENCE, at=BASE_TIME)
            assert [b.type for b in runtime.projections.boundaries.list_all()] == ["temporal"]
            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=1))
            assert of_type(produced, "repair") == []
        finally:
            runtime.close()

    def test_a_positive_reply_to_the_outreach_produces_no_repair(self) -> None:
        """Nothing to repair when the user reacted well."""
        runtime = build_runtime()
        try:
            deliver_a_proactive_message(runtime)
            say(
                runtime,
                "谢谢你惦记我，我挺好的。",
                at=BASE_TIME + timedelta(minutes=30),
                reason=BehaviourReaction(replied=True, explicit_positive=True),
            )
            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=31))
            assert of_type(produced, "repair") == []
        finally:
            runtime.close()

    def test_stale_negative_evidence_produces_no_repair(self) -> None:
        """Three days later an apology is dredging, not repairing."""
        runtime = build_runtime()
        try:
            deliver_a_proactive_message(runtime)
            say(
                runtime,
                DISLIKE,
                at=BASE_TIME,
                reason=BehaviourReaction(replied=True, explicit_negative=True),
            )
            produced = generate(
                runtime,
                now=BASE_TIME
                + timedelta(seconds=candidate_module.REPAIR_EVIDENCE_MAX_AGE_SECONDS + 60),
            )
            assert of_type(produced, "repair") == []
        finally:
            runtime.close()


# --------------------------------------------------------------------------------------
# Gap 1 - reply
# --------------------------------------------------------------------------------------


class TestReplyIsProducedFromAnUnansweredQuestion:
    """``reply`` is a queued answer, read from the event log."""

    def test_an_unanswered_question_produces_a_reply(self) -> None:
        runtime = build_runtime()
        try:
            asked = say(runtime, QUESTION, at=BASE_TIME)
            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=10))
            replies = of_type(produced, "reply")
            assert len(replies) == 1, [item.type for item in produced]
            reply = replies[0]
            assert reply.sources[0] == asked.event.event_id
            assert QUESTION[:12] in reply.intent
            assert has_resolvable_source(runtime, reply)
            assert resolvable_conversations(runtime, reply) == {SESSION_B}
        finally:
            runtime.close()

    def test_a_question_answered_in_its_own_chat_is_not_queued(self) -> None:
        runtime = build_runtime()
        try:
            asked = say(runtime, QUESTION, at=BASE_TIME)
            append_assistant_message(
                runtime,
                "叫「山丘」，你上次说想去看看。",
                at=BASE_TIME + timedelta(minutes=1),
                session=SESSION_B,
            )
            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=10))
            assert of_type(produced, "reply") == []
            # And the queued answer it would have been is invalidated by that state.
            queued = CandidateIntent(
                candidate_id=new_id("candidate"),
                type="reply",
                intent="回答用户之前问过的问题",
                target=QUESTION,
                sources=[asked.event.event_id],
                invalidate_when=[
                    f"{candidate_module.QUESTION_ANSWERED_PREFIX}{asked.event.event_id}"
                ],
            )
            events = runtime.events.read(EventQuery(limit=40, newest_first=True))
            assert candidate_module.invalidated_by_source_state(
                queued, recent_events=events, now=BASE_TIME + timedelta(minutes=10)
            ) == f"{candidate_module.QUESTION_ANSWERED_PREFIX}{asked.event.event_id}"
            # Nothing is answered when the events are not supplied.
            assert (
                candidate_module.invalidated_by_source_state(
                    queued, now=BASE_TIME + timedelta(minutes=10)
                )
                is None
            )
        finally:
            runtime.close()

    def test_an_answer_in_another_chat_leaves_the_question_queued(self) -> None:
        """The routing property, from the reply side: another chat's words are not an answer."""
        runtime = build_runtime()
        try:
            asked = say(runtime, QUESTION, at=BASE_TIME, session=SESSION_B)
            append_assistant_message(
                runtime,
                "好呀，回头聊。",
                at=BASE_TIME + timedelta(minutes=1),
                session=SESSION_A,
            )
            outstanding = candidate_module.unanswered_questions(
                runtime.events.read(EventQuery(limit=40, newest_first=True)),
                now=BASE_TIME + timedelta(minutes=10),
            )
            assert [event.event_id for event in outstanding] == [asked.event.event_id]
        finally:
            runtime.close()

    def test_a_question_a_live_obligation_owns_produces_no_reply(self) -> None:
        """The follow-up path asks; the reply path must not ask the same thing twice."""
        runtime = build_runtime()
        try:
            say(runtime, "我明天面试，结束了告诉你。", at=BASE_TIME)
            say(runtime, "面试怎么样了？", at=BASE_TIME + timedelta(minutes=30))
            assert [matter.title for matter in runtime.projections.unfinished.list_open()] == [
                "等待面试结果"
            ]
            produced = generate(runtime, now=BASE_TIME + timedelta(hours=20))
            assert of_type(produced, "reply") == []
            assert len(of_type(produced, "follow_up")) == 1
        finally:
            runtime.close()

    def test_a_stale_question_produces_no_reply(self) -> None:
        runtime = build_runtime()
        try:
            say(runtime, QUESTION, at=BASE_TIME)
            produced = generate(
                runtime,
                now=BASE_TIME
                + timedelta(seconds=candidate_module.REPLY_QUESTION_MAX_AGE_SECONDS + 60),
            )
            assert of_type(produced, "reply") == []
        finally:
            runtime.close()


# --------------------------------------------------------------------------------------
# Gap 1 - traceability, routing and pacing
# --------------------------------------------------------------------------------------


class TestEveryNewShapeIsTraceableAndBounded:
    """All three shapes at once: resolvable sources, the right chat, a bounded count."""

    def _rich_state(self, runtime: Runtime) -> datetime:
        """Build a state that can produce all three new shapes, in session B."""
        say(runtime, PREFERENCE, at=BASE_TIME, session=SESSION_B)
        say(runtime, QUESTION, at=BASE_TIME + timedelta(minutes=1), session=SESSION_B)
        memories = consolidate(runtime, now=BASE_TIME + timedelta(minutes=5))
        activate(runtime, memories)
        deliver_a_proactive_message(runtime, session=SESSION_B)
        say(
            runtime,
            DISLIKE,
            at=BASE_TIME + timedelta(hours=2),
            session=SESSION_B,
            reason=BehaviourReaction(replied=True, explicit_negative=True),
        )
        # Session A speaks last, so "the conversation of the newest user message
        # anywhere" is A: a candidate with an unresolvable source would land there.
        say(
            runtime,
            "我先去健身房了，回头聊。",
            at=BASE_TIME + timedelta(hours=3),
            session=SESSION_A,
        )
        return BASE_TIME + timedelta(hours=2, minutes=1)

    def test_every_new_shape_carries_a_source_the_routing_layer_can_resolve(self) -> None:
        runtime = build_runtime()
        try:
            now = self._rich_state(runtime)
            produced = generate(runtime, now=now)
            new_shapes = [item for item in produced if item.type in {"share", "repair", "reply"}]
            assert {item.type for item in new_shapes} == {"share", "repair", "reply"}
            for item in new_shapes:
                assert has_resolvable_source(runtime, item), item.sources
                assert resolvable_conversations(runtime, item) == {SESSION_B}, (
                    f"{item.type} was routed away from the chat it was formed in: "
                    f"{item.sources}"
                )
        finally:
            runtime.close()

    def test_the_new_shapes_are_bounded_per_round(self) -> None:
        """A rich state still yields one of each: the pool is not flooded."""
        runtime = build_runtime()
        try:
            # Four preference memories, in front of the pool by activation.
            for index, statement in enumerate(
                (
                    "我喜欢喝手冲咖啡。",
                    "我最喜欢下雨天。",
                    "我习惯晚上十点睡。",
                    "我讨厌吵闹的地方。",
                )
            ):
                say(runtime, statement, at=BASE_TIME + timedelta(seconds=index), session=SESSION_B)
            # Three unanswered questions.
            for index, question in enumerate(
                (
                    "你上次说的那家咖啡店叫什么名字？",
                    "你周末一般做什么？",
                    "你养的那只猫叫什么？",
                )
            ):
                say(
                    runtime,
                    question,
                    at=BASE_TIME + timedelta(minutes=1 + index),
                    session=SESSION_B,
                )
            memories = consolidate(runtime, now=BASE_TIME + timedelta(minutes=5))
            preferences = [memory for memory in memories if memory.kind == "user_preference"]
            assert len(preferences) == 4, [memory.kind for memory in memories]
            activate(runtime, preferences, activation=0.95)
            activate(
                runtime,
                [memory for memory in memories if memory.kind != "user_preference"],
                activation=0.4,
            )
            # Three negative observations about the character's own outreach.
            for index in range(3):
                deliver_a_proactive_message(runtime, session=SESSION_B)
                say(
                    runtime,
                    f"我讨厌你这样说话（{index}）。",
                    at=BASE_TIME + timedelta(minutes=10 + index),
                    session=SESSION_B,
                    reason=BehaviourReaction(replied=True, explicit_negative=True),
                )
            assert len(runtime.projections.user_model.list_observations(limit=10)) == 3

            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=20))
            assert len(of_type(produced, "share")) == candidate_module.MAX_SHARE_PER_ROUND
            assert len(of_type(produced, "repair")) == candidate_module.MAX_REPAIR_PER_ROUND
            assert len(of_type(produced, "reply")) == candidate_module.MAX_REPLY_PER_ROUND
        finally:
            runtime.close()

    def test_the_new_shapes_do_not_fire_when_their_state_is_absent(self) -> None:
        """No observations, emotions, situations, boundaries or events: still quiet."""
        runtime = build_runtime()
        try:
            produced = generate(
                runtime,
                now=BASE_TIME,
                observations=[],
                emotions=[],
                situations=[],
                boundaries=[],
                recent_events=[],
            )
            assert of_type(produced, "share") == []
            assert of_type(produced, "repair") == []
            assert of_type(produced, "reply") == []
            assert [item.type for item in produced] == ["contact"]
        finally:
            runtime.close()

    def test_all_three_shapes_pass_the_pool_managers_whitelist(self) -> None:
        """The whitelist entries were already there; now something produces them."""
        for kind in ("share", "repair", "reply"):
            candidate = CandidateIntent(
                candidate_id=new_id("candidate"),
                type=kind,
                intent="x",
                target=f"{kind}-target",
                sources=["internal_approach_drive"],
            )
            assert candidate_module.validate_candidate(candidate) is None


# --------------------------------------------------------------------------------------
# Gap 2 - invalidation derived from the source
# --------------------------------------------------------------------------------------


class TestMatterSourcedInvalidation:
    """A follow-up dies with the obligation it was built from."""

    def _follow_up(self, runtime: Runtime):
        """Open a matter through the ingest path and return ``(matter, candidate)``."""
        say(runtime, PROMISE, at=BASE_TIME, session=SESSION_B)
        matter = runtime.projections.unfinished.list_open()[0]
        produced = generate(runtime, now=BASE_TIME + timedelta(hours=19))
        follow_ups = of_type(produced, "follow_up")
        assert len(follow_ups) == 1
        return matter, follow_ups[0]

    def test_the_legacy_condition_is_still_carried_and_still_matches(self) -> None:
        """The old signal keeps working; nothing that used to retire it stops."""
        runtime = build_runtime()
        try:
            _matter, follow_up = self._follow_up(runtime)
            assert "已经得知后续结果" in follow_up.invalidate_when
            assert (
                candidate_module.invalidated_by_situation(
                    follow_up,
                    situation_text="未尽之事：等待面试结果",
                    user_message="面试过啦，结果是过了",
                )
                == "已经得知后续结果"
            )
            # Only part of the legacy condition present: not enough, as before. The
            # derived pair is stricter still and does not rescue it.
            assert (
                candidate_module.invalidated_by_situation(
                    follow_up, situation_text="", user_message="面试有点紧张"
                )
                is None
            )
        finally:
            runtime.close()

    def test_the_candidate_names_the_matter_that_invalidates_it(self) -> None:
        runtime = build_runtime()
        try:
            matter, follow_up = self._follow_up(runtime)
            assert candidate_module.matter_invalidation(matter) == [
                f"{candidate_module.MATTER_RESOLVED_PREFIX}{matter.title}",
                f"{candidate_module.MATTER_RELEASED_PREFIX}{matter.title}",
            ]
            for condition in candidate_module.matter_invalidation(matter):
                assert condition in follow_up.invalidate_when
        finally:
            runtime.close()

    def test_resolving_the_matter_invalidates_the_follow_up(self) -> None:
        """Through the state, and through the text the state writes."""
        runtime = build_runtime()
        try:
            matter, follow_up = self._follow_up(runtime)
            reported = say(
                runtime, REPORTED, at=BASE_TIME + timedelta(hours=20), session=SESSION_B
            )
            assert reported.unfinished_resolved == [matter.unfinished_id]

            matched = candidate_module.invalidated_by_source_state(
                follow_up,
                unfinished=runtime.projections.unfinished.list_all(limit=200),
                now=BASE_TIME + timedelta(hours=20),
            )
            assert matched == f"{candidate_module.MATTER_RESOLVED_PREFIX}{matter.title}"

            situation_text = " ".join(
                str(item.get("content") or "")
                for item in runtime.projections.situation.list_active(limit=20)
            )
            assert f"{candidate_module.MATTER_RESOLVED_PREFIX}{matter.title}" in situation_text, (
                "the ingest path writes the resolution into the working situation, which is "
                "what makes the derived condition matchable as text too"
            )
        finally:
            runtime.close()

    @pytest.mark.parametrize(
        "status",
        [
            UnfinishedStatus.CANCELLED.value,
            UnfinishedStatus.EXPIRED.value,
            UnfinishedStatus.INVALIDATED.value,
        ],
    )
    def test_a_matter_that_lapses_unsettled_invalidates_the_follow_up(self, status: str) -> None:
        runtime = build_runtime()
        try:
            matter, follow_up = self._follow_up(runtime)
            with runtime.db.transaction() as conn:
                runtime.projections.unfinished.set_status(conn, matter.unfinished_id, status)
            matched = candidate_module.invalidated_by_source_state(
                follow_up,
                unfinished=runtime.projections.unfinished.list_all(limit=200),
                now=BASE_TIME + timedelta(hours=20),
            )
            assert matched == f"{candidate_module.MATTER_RELEASED_PREFIX}{matter.title}"
        finally:
            runtime.close()

    def test_a_live_matter_does_not_invalidate_its_follow_up(self) -> None:
        runtime = build_runtime()
        try:
            _matter, follow_up = self._follow_up(runtime)
            assert (
                candidate_module.invalidated_by_source_state(
                    follow_up,
                    unfinished=runtime.projections.unfinished.list_all(limit=200),
                    now=BASE_TIME + timedelta(hours=19),
                )
                is None
            )
        finally:
            runtime.close()

    def test_an_unrelated_mention_of_the_subject_does_not_invalidate(self) -> None:
        """The derived condition is strict; only the resolution text satisfies it."""
        runtime = build_runtime()
        try:
            matter, _follow_up = self._follow_up(runtime)
            derived_only = CandidateIntent(
                candidate_id=new_id("candidate"),
                type="follow_up",
                intent=f"询问{matter.title}",
                target=matter.title,
                sources=[f"{candidate_module.UNFINISHED_SOURCE_PREFIX}{matter.unfinished_id}"],
                invalidate_when=candidate_module.matter_invalidation(matter),
            )
            assert (
                candidate_module.invalidated_by_situation(
                    derived_only, situation_text="", user_message="面试结果还没出来呢"
                )
                is None
            )
            assert (
                candidate_module.invalidated_by_situation(
                    derived_only,
                    situation_text=f"未尽之事已了结：{matter.title}",
                    user_message="",
                )
                == f"{candidate_module.MATTER_RESOLVED_PREFIX}{matter.title}"
            )
        finally:
            runtime.close()


class TestMemorySourcedInvalidation:
    """A candidate built from a memory dies when that memory leaves the pool."""

    def _share(self, runtime: Runtime):
        say(runtime, PREFERENCE, at=BASE_TIME, session=SESSION_B)
        memories = consolidate(runtime, now=BASE_TIME + timedelta(minutes=5))
        activate(runtime, memories)
        produced = generate(runtime, now=BASE_TIME + timedelta(minutes=6))
        shares = of_type(produced, "share")
        assert len(shares) == 1
        return memories[0], shares[0]

    def test_the_candidate_names_the_memory_that_invalidates_it(self) -> None:
        runtime = build_runtime()
        try:
            memory, share = self._share(runtime)
            assert candidate_module.memory_invalidation(memory) == [
                f"{candidate_module.MEMORY_ARCHIVED_PREFIX}{memory.memory_id}",
                f"{candidate_module.MEMORY_SUPERSEDED_PREFIX}{memory.memory_id}",
            ]
            for condition in candidate_module.memory_invalidation(memory):
                assert condition in share.invalidate_when
        finally:
            runtime.close()

    def test_archiving_the_memory_invalidates_the_candidate(self) -> None:
        runtime = build_runtime()
        try:
            memory, share = self._share(runtime)
            assert (
                candidate_module.invalidated_by_source_state(
                    share, memories=runtime.projections.memory.list_memories(status=None)
                )
                is None
            )
            with runtime.db.transaction() as conn:
                runtime.projections.memory.set_memory_status(
                    conn, memory.memory_id, MemoryStatus.ARCHIVED.value
                )
            matched = candidate_module.invalidated_by_source_state(
                share, memories=runtime.projections.memory.list_memories(status=None)
            )
            assert matched == f"{candidate_module.MEMORY_ARCHIVED_PREFIX}{memory.memory_id}"
        finally:
            runtime.close()

    def test_a_superseded_memory_invalidates_the_candidate(self) -> None:
        """A contradicted memory is withheld from retrieval, so the offer is stale."""
        runtime = build_runtime()
        try:
            memory, share = self._share(runtime)
            superseded = Memory(
                memory_id=memory.memory_id,
                kind=memory.kind,
                summary=memory.summary,
                topics=list(memory.topics),
                importance=memory.importance,
                status=memory.status,
                source_event_ids=list(memory.source_event_ids),
                structured={memory_module.SUPERSEDED_HINT_KEY: "用户其实不喜欢咖啡"},
                created_at=memory.created_at,
            )
            assert memory_module.is_superseded(superseded) is True
            matched = candidate_module.invalidated_by_source_state(share, memories=[superseded])
            assert matched == f"{candidate_module.MEMORY_SUPERSEDED_PREFIX}{memory.memory_id}"
        finally:
            runtime.close()

    def test_a_memory_that_is_only_demoted_does_not_invalidate(self) -> None:
        """``low_activation`` is still retrieved; only archival takes it out."""
        runtime = build_runtime()
        try:
            memory, share = self._share(runtime)
            with runtime.db.transaction() as conn:
                runtime.projections.memory.set_memory_status(
                    conn, memory.memory_id, MemoryStatus.LOW_ACTIVATION.value
                )
            assert (
                candidate_module.invalidated_by_source_state(
                    share, memories=runtime.projections.memory.list_memories(status=None)
                )
                is None
            )
        finally:
            runtime.close()

    def test_a_memory_that_no_longer_exists_invalidates_the_candidate(self) -> None:
        """A record the caller cannot find is a record that is gone."""
        runtime = build_runtime()
        try:
            _memory, share = self._share(runtime)
            assert candidate_module.invalidated_by_source_state(share, memories=[]) is not None
        finally:
            runtime.close()


class TestDerivedConditionsFromTheOtherSources:
    """The remaining derived conditions, and the generic fallback."""

    def test_a_boundary_derived_repair_dies_when_the_boundary_is_released(self) -> None:
        runtime = build_runtime()
        try:
            say(runtime, BOUNDARY_INTERROGATION, at=BASE_TIME, session=SESSION_B)
            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=1))
            repair = of_type(produced, "repair")[0]
            boundary = runtime.projections.boundaries.list_all()[0]
            assert (
                f"{candidate_module.BOUNDARY_RELEASED_PREFIX}{boundary.boundary_id}"
                in repair.invalidate_when
            )
            assert (
                candidate_module.invalidated_by_source_state(
                    repair,
                    boundaries=runtime.projections.boundaries.list_all(include_revoked=True),
                    now=BASE_TIME + timedelta(minutes=1),
                )
                is None
            )
            with runtime.db.transaction() as conn:
                runtime.projections.boundaries.upsert(
                    conn,
                    Boundary(
                        boundary_id=boundary.boundary_id,
                        type=boundary.type,
                        scope=boundary.scope,
                        allow_reply=boundary.allow_reply,
                        allow_proactive=boundary.allow_proactive,
                        starts_at=boundary.starts_at,
                        expires_at=boundary.expires_at,
                        source_event_id=boundary.source_event_id,
                        revoked_at=BASE_TIME + timedelta(minutes=2),
                        note=boundary.note,
                    ),
                )
            matched = candidate_module.invalidated_by_source_state(
                repair,
                boundaries=runtime.projections.boundaries.list_all(include_revoked=True),
                now=BASE_TIME + timedelta(minutes=3),
            )
            assert matched == f"{candidate_module.BOUNDARY_RELEASED_PREFIX}{boundary.boundary_id}"
        finally:
            runtime.close()

    def test_a_repair_dies_when_the_emotion_behind_it_fades(self) -> None:
        runtime = build_runtime()
        try:
            deliver_a_proactive_message(runtime, session=SESSION_B)
            say(
                runtime,
                DISLIKE,
                at=BASE_TIME + timedelta(minutes=30),
                session=SESSION_B,
                reason=BehaviourReaction(replied=True, explicit_negative=True),
            )
            emotions = runtime.projections.emotion.list_active()
            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=31))
            repair = of_type(produced, "repair")[0]
            condition = f"{candidate_module.EMOTION_FADED_PREFIX}{emotions[0].emotion_event_id}"
            assert condition in repair.invalidate_when
            assert (
                candidate_module.invalidated_by_source_state(
                    repair, emotions=emotions, now=BASE_TIME + timedelta(minutes=31)
                )
                is None
            )
            assert (
                candidate_module.invalidated_by_source_state(
                    repair, emotions=[], now=BASE_TIME + timedelta(minutes=31)
                )
                == condition
            )
        finally:
            runtime.close()

    def test_the_user_saying_they_are_over_it_invalidates_a_repair(self) -> None:
        """The one condition that is read from the user's words rather than a record."""
        runtime = build_runtime()
        try:
            say(runtime, BOUNDARY_INTERROGATION, at=BASE_TIME, session=SESSION_B)
            produced = generate(runtime, now=BASE_TIME + timedelta(minutes=1))
            repair = of_type(produced, "repair")[0]
            assert (
                candidate_module.invalidated_by_situation(
                    repair, situation_text="", user_message="没事了，我不怪你了。"
                )
                == "用户表示已经不再介意"
            )
        finally:
            runtime.close()

    def test_an_unknown_condition_is_no_longer_silently_dead(self) -> None:
        """Audit D4: a model-written condition used to match nothing at all."""
        candidate = CandidateIntent(
            candidate_id=new_id("candidate"),
            type="check_in",
            intent="问候",
            target="问候",
            sources=["internal_approach_drive"],
            invalidate_when=["用户已经知道体检结果了"],
        )
        assert (
            candidate_module.invalidated_by_situation(
                candidate, situation_text="", user_message="体检结果出来了，一切正常"
            )
            == "用户已经知道体检结果了"
        )
        # A single shared *character* is not evidence of anything.
        assert (
            candidate_module.invalidated_by_situation(
                candidate, situation_text="", user_message="体检有点紧张"
            )
            is None
        )
        # Nor is text that shares nothing.
        assert (
            candidate_module.invalidated_by_situation(
                candidate, situation_text="", user_message="今天天气不错"
            )
            is None
        )

    def test_a_collection_that_was_not_supplied_is_not_evidence(self) -> None:
        """``None`` means "no information", which is not the same as "nothing there"."""
        candidate = CandidateIntent(
            candidate_id=new_id("candidate"),
            type="follow_up",
            intent="询问等待面试结果",
            target="等待面试结果",
            sources=[f"{candidate_module.UNFINISHED_SOURCE_PREFIX}unf_missing"],
            invalidate_when=candidate_module.matter_invalidation(
                UnfinishedMatter(unfinished_id="unf_missing", title="等待面试结果")
            ),
        )
        assert (
            candidate_module.invalidated_by_source_state(candidate, now=BASE_TIME) is None
        ), "with no matters supplied, nothing can be said about the matter"
        assert (
            candidate_module.invalidated_by_source_state(candidate, unfinished=[], now=BASE_TIME)
            == f"{candidate_module.MATTER_RELEASED_PREFIX}等待面试结果"
        )

    def test_the_refresh_keeps_the_conditions_current(self) -> None:
        """An UPDATE from the generator refreshes the conditions it derived."""
        existing = CandidateIntent(
            candidate_id="cnd_live",
            type="follow_up",
            intent="询问等待面试结果",
            target="等待面试结果",
            sources=["unfinished:unf_1"],
            invalidate_when=["已经得知后续结果"],
            preconditions=[],
        )
        proposal = CandidateIntent(
            candidate_id="cnd_new",
            type="follow_up",
            intent="询问等待面试结果",
            target="等待面试结果",
            sources=["unfinished:unf_1"],
            invalidate_when=candidate_module.matter_invalidation(
                UnfinishedMatter(unfinished_id="unf_1", title="等待面试结果")
            ),
            preconditions=[],
        )
        operations = candidate_module.plan_operations(
            proposals=[proposal], existing=[existing], config=build_config()
        )
        assert len(operations) == 1
        patch = operations[0].patch or {}
        for condition in proposal.invalidate_when:
            assert condition in patch["invalidate_when"]
        assert "已经得知后续结果" in patch["invalidate_when"], (
            "a legacy condition on the live candidate must not be dropped by a refresh"
        )


class TestTheShapesSurviveThePacingRules:
    """The daily budget and the cooldown are downstream; generation must not defeat them."""

    def test_a_repair_is_proactive_so_a_hard_boundary_can_block_it(self) -> None:
        """An apology is initiated by the character, so the hard gate applies to it."""
        repair = CandidateIntent(
            candidate_id=new_id("candidate"),
            type="repair",
            intent="道歉",
            target="relationship_repair",
            sources=["internal_approach_drive"],
        )
        reply = CandidateIntent(
            candidate_id=new_id("candidate"),
            type="reply",
            intent="回答",
            target="问题",
            sources=["internal_approach_drive"],
        )
        assert candidate_module.is_candidate_proactive(repair) is True
        assert candidate_module.is_candidate_proactive(reply) is False

    def test_pool_deduplication_still_covers_the_new_shapes(self) -> None:
        """A live share about the same target is updated, not added twice."""
        existing = [
            CandidateIntent(
                candidate_id="cnd_live",
                type="share",
                intent="主动提起我记得的事",
                target="咖啡",
                sources=["memory:mem_1"],
            )
        ]
        proposals = [
            CandidateIntent(
                candidate_id="cnd_new",
                type="share",
                intent="主动提起我记得的事",
                target="咖啡",
                sources=["memory:mem_2"],
            )
        ]
        operations = candidate_module.plan_operations(
            proposals=proposals, existing=existing, config=build_config()
        )
        assert [operation.op for operation in operations] == ["update"]
        assert operations[0].candidate_id == "cnd_live"
