"""The question-frame decision, as executable acceptance criteria.

The product owner decided how a user's *question* should be treated by long-term memory,
and this file is that decision written down where it cannot rot:

1. the **proposition** inside the question is what gets stored - the interrogative frame
   ("你还记得…吗？") is removed from the summary, so the fact is readable as a fact;
2. the **frame is kept as its own relationship evidence** - a user checking whether the
   character remembers is evidence about the relationship, and the owner wants it kept;
3. that evidence must **not compete for the four slots** of the 必要记忆 section, because
   question memories share their characters with each other by construction and were
   measured filling 4 of 10 probe turns while the disclosure the user asked about - which
   retrieval *did* return, rank 4 of 10 - lost the budget.

All three were written as ``xfail(strict=True)`` before the behaviour existed, so that an
"expected failure" could not quietly outlive the work: implementing it turns the marker
into a failure and forces the implementer to delete it. That is what happened -- the three
markers were removed in the commit that implemented the decision, and these are now
standing regression tests.

One caveat worth recording, because it nearly went unnoticed: the third test drove
retrieval with a bare string where ``MemoryStore.retrieve`` wants a ``RetrievalCue``, so it
raised ``AttributeError`` and could never have passed however correct the implementation
was. ``strict`` catches an *unexpected pass*; it says nothing about a test that is
unsatisfiable, and a long-lived expected failure is exactly where such a defect hides.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime import context as context_module
from companion_runtime import memory as memory_module
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME

#: A question that carries a fact, with the frame in the middle.
QUESTION_WITH_FACT = "我喜欢你这件事情，你还记得我说过吗？"
#: A question that carries an update, frame at the end.
QUESTION_WITH_UPDATE = "我妈身体好一些了，你还记得我每周跑医院那阵子吗？"
#: A disclosure with no frame at all: it must pass through untouched.
PLAIN_DISCLOSURE = "我以前养过一只猫，叫团子，后来送人了，我现在还经常想起它。"
#: The frame with nothing before it - stripping must not produce an empty summary.
FRAME_ONLY = "你还记得我跟你讲过它吗？"


def _summaries(runtime: Runtime) -> list[str]:
    """Return every stored memory summary."""
    return [memory.summary for memory in runtime.projections.memory.list_memories(limit=50)]


def test_a_question_is_stored_as_its_proposition(runtime: Runtime) -> None:
    """The fact survives without the interrogative frame riding along with it."""
    runtime.process_user_message(content=QUESTION_WITH_FACT, timestamp=BASE_TIME)
    runtime.consolidate(now=BASE_TIME + timedelta(hours=2))
    summaries = _summaries(runtime)

    assert summaries, "the sentence carried a fact and should have been remembered"
    assert "我喜欢你" in " ".join(summaries), "the fact must survive the stripping"
    for summary in summaries:
        assert "你还记得" not in summary, f"the frame is still stored as a fact: {summary!r}"


def test_the_frame_is_kept_as_relationship_evidence(runtime: Runtime) -> None:
    """Removing it from the summary must not remove it from the record.

    Both halves are asserted deliberately, because either one alone is satisfiable today:
    the frame is *in* the summary right now, so "the frame is somewhere in the record" is
    already true and would be a vacuous acceptance test. The pair - gone from the summary,
    still present in the memory's own record - is what only the decided shape satisfies.
    """
    import json

    runtime.process_user_message(content=QUESTION_WITH_UPDATE, timestamp=BASE_TIME)
    runtime.consolidate(now=BASE_TIME + timedelta(hours=2))
    memories = list(runtime.projections.memory.list_memories(limit=50))
    assert memories

    summaries = " ".join(memory.summary for memory in memories)
    records = json.dumps(
        [memory.structured for memory in memories], ensure_ascii=False, default=str
    )
    assert "你还记得" not in summaries, f"the frame is still a fact: {summaries!r}"
    assert "你还记得" in records, (
        "the frame was dropped instead of kept as relationship evidence"
    )


def test_question_memories_do_not_crowd_out_a_disclosure(runtime: Runtime) -> None:
    """The measured defect: four of ten sections were filled by the user's own questions."""
    for index, content in enumerate(
        [
            "你还记得我跟你讲过它吗？",
            "我说过我喜欢你，你还记得吗？",
            "你还记得我每周跑医院那阵子吗？",
            "我讲过团子的事，你还记得吗？",
        ]
    ):
        runtime.process_user_message(
            content=content, timestamp=BASE_TIME + timedelta(minutes=index)
        )
    runtime.process_user_message(
        content=PLAIN_DISCLOSURE, timestamp=BASE_TIME + timedelta(minutes=10)
    )
    runtime.consolidate(now=BASE_TIME + timedelta(hours=2))
    runtime.lazy_tick(BASE_TIME + timedelta(hours=3))

    selected = context_module.select_memories(
        runtime.projections,
        limit=4,
        # A plain string cannot drive retrieval: ``MemoryStore.retrieve`` reads
        # ``cue.query_text`` and ``cue.topics``, so passing the sentence itself raised
        # ``AttributeError`` and the test could never pass no matter what the code did.
        # ``xfail(strict=True)`` did not catch that -- it only fails on an *unexpected
        # pass*, never on an unsatisfiable test -- which is worth remembering the next
        # time an expected failure sits in the tree for a while.
        cue=memory_module.RetrievalCue(
            query_text="团子最近怎么样？",
            now=BASE_TIME + timedelta(hours=3),
        ),
        store=runtime.memory_store,
        now=BASE_TIME + timedelta(hours=3),
    )
    # ``select_memories`` returns plain dicts, so this must read the key -- and it reads
    # it with ``[...]``, not ``.get(key, "")``. The original used
    # ``getattr(item, "summary", "")``: on a dict that always yields the default, so
    # ``shown`` was the empty string and the assertion below was true no matter what the
    # runtime did. A mutation (disabling the recall-check exclusion in
    # ``context.select_memories``) did not turn it red, which is how the vacuous
    # assertion was found.
    #
    # ``.get(key, "")`` would have the same failure shape: rename the key and the default
    # silently comes back. Subscripting raises ``KeyError`` instead, so the next person to
    # change the item's shape gets told rather than getting a green test that checks
    # nothing.
    shown = " ".join(str(item["summary"]) for item in selected)
    assert "你还记得" not in shown, (
        f"a question memory took one of the four slots: {shown!r}"
    )
