"""The memory kind must be decided by words about the user, not by a character.

Two runs of the same audit found two versions of one mistake. First `最` sat in
`PREFERENCE_MARKERS` as a bare character, so `最近` made any sentence a "preference" (four
such rows in one two-and-a-half-month run), and the preference branch was the only one of
the three without an `is_question` guard, so a question containing `喜欢` was filed as a
lasting fact about the user. Then, with the marker tables cleaned up, the *subject* of the
sentence was still never checked: `我一哥们很喜欢玩柚子社…` became the user's taste,
`因为我 QQ 一直在响！` a habit and `那你就试试呗，就当陪我` a relational fact, because a
substring test cannot see who a sentence is about.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime.config import RuntimeConfig
from companion_runtime.memory import propose_from_event
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    Actor,
    EventType,
    MemoryKind,
    RawEvent,
    RuntimeState,
)

from conftest import BASE_TIME

#: A sentence with no preference in it at all - the `最近` in it is why this is here.
NOT_A_PREFERENCE = "其实我最近挺难的，我妈身体不太好，我每周都要跑医院。"
#: A question that contains a preference word.
QUESTION_WITH_PREFERENCE = "我喜欢你这件事情，你还记得我说过吗？"
#: A real preference: it must keep being stored as one.
STILL_A_PREFERENCE = "记住，我平时只喝手冲咖啡，不加糖。"
#: An instruction addressed to the character, with no statement about the user in it.
#: Two corrections while writing this: the first version also said "我不太喜欢被盯着"
#: (which *is* a preference about the user, so calling it one was right), and the second
#: still said "不要一直追问" - and `一直` is a *habit* marker ("我总是失眠"), so a directive
#: that contains one still reads as a habit. That limitation is real and is recorded in
#: HANDOFF.md rather than papered over here.
INSTRUCTION = "有件事想说清楚，别再追问我的工作了。"


def _kinds(runtime: Runtime, text: str) -> list[str]:
    """Ingest one sentence, consolidate, and return the kinds it produced."""
    runtime.process_user_message(content=text, timestamp=BASE_TIME)
    runtime.consolidate(now=BASE_TIME + timedelta(hours=2))
    return [
        memory.kind for memory in runtime.projections.memory.list_memories(limit=50)
    ]


def test_recently_is_not_a_preference(runtime: Runtime) -> None:
    """`最` is not a word; it cannot decide what the user prefers."""
    kinds = _kinds(runtime, NOT_A_PREFERENCE)
    assert kinds, "the sentence should still be worth remembering"
    assert MemoryKind.USER_PREFERENCE.value not in kinds, (
        f"a sentence with no preference in it was filed as a preference: {kinds}"
    )


def test_a_question_is_not_a_durable_preference(runtime: Runtime) -> None:
    """The preference branch is guarded against questions, like its two siblings."""
    kinds = _kinds(runtime, QUESTION_WITH_PREFERENCE)
    assert MemoryKind.USER_PREFERENCE.value not in kinds, (
        f"a question was stored as a lasting preference: {kinds}"
    )


def test_an_instruction_is_not_a_preference(runtime: Runtime) -> None:
    """`不要…` tells the character what to do; it says nothing about what the user likes."""
    kinds = _kinds(runtime, INSTRUCTION)
    assert MemoryKind.USER_PREFERENCE.value not in kinds, (
        f"an instruction to the character was stored as the user's preference: {kinds}"
    )


def test_a_real_preference_is_still_a_preference(runtime: Runtime) -> None:
    """The fix must not cost the classification it was protecting (mutation guard)."""
    kinds = _kinds(runtime, STILL_A_PREFERENCE)
    assert MemoryKind.USER_PREFERENCE.value in kinds, (
        f"a plainly stated preference stopped being one: {kinds}"
    )


# --------------------------------------------------------------------------------------
# who the sentence is about
# --------------------------------------------------------------------------------------

#: The three kinds that claim something lasting about the user.
DURABLE_KINDS = (
    MemoryKind.USER_PREFERENCE.value,
    MemoryKind.STABLE_KNOWLEDGE.value,
    MemoryKind.RELATIONSHIP.value,
)


def _kind_of(text: str) -> str:
    """Classify one sentence through the rule path alone.

    ``candidate_min_value`` is removed so that the answer is the *classification* and
    never the value floor: a test that reads "no durable memory was stored" would also
    pass if nothing at all had been stored, which is how a rule that always answers "no"
    would keep this file green.

    Args:
        text: The user's message.

    Returns:
        The kind of the candidate the rule path proposes.
    """
    config = RuntimeConfig()
    config.memory.candidate_min_value = 0.0
    event = RawEvent(
        event_id="evt_kind",
        event_type=EventType.USER_MESSAGE.value,
        timestamp=BASE_TIME,
        actor=Actor.USER.value,
        content=text,
    )
    candidate = propose_from_event(
        event,
        state=RuntimeState(),
        unfinished=[],
        emotion_salience=0.0,
        config=config,
        created_at=BASE_TIME,
    )
    assert candidate is not None, f"{text!r} produced no candidate at all"
    return candidate.kind


#: Messages from the first beta's own archive that were stored as lasting facts about the
#: user. Each one names its own hole; the marker it hit is in the comment.
NOT_ABOUT_THE_USER = (
    "我一哥们很喜欢玩柚子社，天天在那喊柚子社天下第一",  # 喜欢, a friend's taste
    "他总是这样，我受不了",  # 总是, somebody else's habit
    "我们班同学都喜欢打游戏",  # 喜欢, the whole class's taste
    "因为我 QQ 一直在响！",  # 一直, and 我 belongs to 因为 rather than to the claim
    "今天上班好累",  # 上班, and the day's tiredness is not a lasting fact
    "他昨天陪我吃饭",  # 陪我, with somebody else doing the accompanying
    "那你就试试呗，就当陪我",  # 陪我, as a request
    "你不在乎我",  # 在乎, and the user is the object of the attitude
    "那我以后再说吧",  # 以后, about the pair rather than about the user
    "安能辨我是雌雄",  # 我是, inside a quotation
)

#: Self-disclosures from the same archive, with the kind each one must keep. This is the
#: mutation guard: a subject rule that only ever answers "no" would pass every row above.
ABOUT_THE_USER = (
    ("我喜欢打夜羊社的", MemoryKind.USER_PREFERENCE.value),
    ("我平时喜欢手冲咖啡。", MemoryKind.USER_PREFERENCE.value),
    ("记住，我平时只喝手冲咖啡，不加糖。", MemoryKind.USER_PREFERENCE.value),
    ("我是魔爪战士，我喝白魔爪", MemoryKind.STABLE_KNOWLEDGE.value),
    ("我生日是三月三号。", MemoryKind.STABLE_KNOWLEDGE.value),
    ("我在腾讯上班", MemoryKind.STABLE_KNOWLEDGE.value),
    ("面试过了！谢谢你那天惦记我。", MemoryKind.RELATIONSHIP.value),
    ("我很想你", MemoryKind.RELATIONSHIP.value),
)


@pytest.mark.parametrize("text", NOT_ABOUT_THE_USER)
def test_a_sentence_about_somebody_else_is_not_a_durable_fact(text: str) -> None:
    """A marker table is a substring test; only the clause can say who it is about."""
    kind = _kind_of(text)
    assert kind not in DURABLE_KINDS, f"filed as a lasting fact about the user: {text!r}"


@pytest.mark.parametrize(("text", "expected"), ABOUT_THE_USER)
def test_a_disclosure_about_the_user_keeps_its_kind(text: str, expected: str) -> None:
    """...and the rule must not cost a single real disclosure (mutation guard)."""
    kind = _kind_of(text)
    assert kind == expected, f"{text!r} -> {kind}"


def test_the_ingest_path_applies_the_subject_rule(runtime: Runtime) -> None:
    """A rule that is right in isolation and unwired in production is the usual defect.

    The distinction from the unit rows above: this one goes through ingest and
    consolidation, so it also proves the sentence is still worth remembering at all - the
    failure mode being guarded against is not "she forgot it" but "she remembered it as
    the user's own taste".
    """
    kinds = _kinds(runtime, NOT_ABOUT_THE_USER[0])
    assert kinds, "the sentence should still be worth remembering"
    assert MemoryKind.USER_PREFERENCE.value not in kinds, (
        f"a friend's taste was stored as the user's preference: {kinds}"
    )
