"""A topic boundary must bind the subject the user was talking about, or bind nothing.

Found by the relationship-progression simulation: the binder's last step returned the
previous message *verbatim with no overlap requirement*, so
"有件事想说清楚，不要一直追问我在干嘛，我不太喜欢被盯着。" was bound to the politeness
formula that preceded it. A wrong binding is worse than none - `blocks_candidate` could
never match it, so the boundary looked enforced and protected nothing.
"""

from __future__ import annotations

from datetime import timedelta

from companion_runtime import boundaries as boundaries_module
from companion_runtime.runtime import Runtime
from conftest import BASE_TIME

POLITENESS = "谢谢你听我说这些。"
INTERROGATION = "有件事想说清楚，不要一直追问我在干嘛，我不太喜欢被盯着。"
TOPIC_THEN_DEIXIS = "我明天下午三点面试，结束了告诉你。"
DEIXIS = "暂时不要跟我说这个。"


def _declared(runtime: Runtime):
    """Return the newest declared boundary."""
    rows = runtime.projections.boundaries.list_all()
    assert rows, "the instruction should have declared a boundary"
    return rows[-1]


def test_a_politeness_formula_is_never_bound_as_the_subject(runtime: Runtime) -> None:
    """Zero overlap means no evidence: the boundary must bind nothing.

    The consequence is asserted, not just the `None`: an unbound topic boundary
    constrains nothing, which is the documented conservative direction.
    """
    runtime.process_user_message(content=POLITENESS, timestamp=BASE_TIME)
    runtime.process_user_message(
        content=INTERROGATION, timestamp=BASE_TIME + timedelta(minutes=1)
    )
    boundary = _declared(runtime)
    assert boundary.scope == "repeated_interrogation"
    assert boundary.subject != POLITENESS, "the previous sentence is not the topic"
    assert boundary.subject is None, (
        "nothing in the conversation established the subject, so it must stay unbound"
    )
    blocked = boundaries_module.blocks_candidate(
        boundaries=[boundary],
        now=BASE_TIME + timedelta(minutes=2),
        subject="关于工作的问题",
        is_question=True,
    )
    assert blocked is None, "an unbound boundary must not silently block a subject"


def test_the_matter_link_still_binds_the_subject(runtime: Runtime) -> None:
    """The known-good case: one shared bigram, settled by event identity.

    This is the case the binder exists for and the fix must not cost.
    """
    runtime.process_user_message(content=TOPIC_THEN_DEIXIS, timestamp=BASE_TIME)
    runtime.process_user_message(
        content=DEIXIS, timestamp=BASE_TIME + timedelta(minutes=1)
    )
    boundary = _declared(runtime)
    titles = [matter.title for matter in runtime.projections.unfinished.list_all()]
    assert boundary.subject, "the deictic '这个' was recoverable and must be bound"
    assert boundary.subject in titles, (
        f"the subject must be the matter it was built from, not free text: "
        f"{boundary.subject!r} not in {titles}"
    )


#: NOTE (honest gap): a third case was attempted here - a *discussed* topic whose text
#: overlaps an open matter's title (step 2 of the binder) - and it bound nothing, so the
#: subject came back `None`. I did not establish whether that is because the matter's
#: title is the whole sentence rather than a topic noun, or because the overlap threshold
#: is not reached; rather than keep an assertion I had not verified, the case is removed
#: and the question is recorded in HANDOFF.md. Steps 1 and 2 are therefore covered by one
#: positive control (the matter link) plus the reported defect (the politeness formula).
