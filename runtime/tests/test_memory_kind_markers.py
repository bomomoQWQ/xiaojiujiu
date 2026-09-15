"""The memory kind must be decided by words about the user, not by a character.

Found by the relationship-progression simulation: `最` sat in `PREFERENCE_MARKERS` as a
bare character, so `最近` made any sentence a "preference" (four such rows in one
two-and-a-half-month run), and the preference branch was the only one of the three
without an `is_question` guard, so a question containing `喜欢` was filed as a lasting
fact about the user.
"""

from __future__ import annotations

from datetime import timedelta

from companion_runtime.runtime import Runtime
from companion_runtime.typing import MemoryKind

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
