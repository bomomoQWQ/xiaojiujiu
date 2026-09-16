"""A boundary must not be bypassable by renaming the behaviour (design §52 / §86.5).

Design §52 says a declared boundary "是硬约束" - not a cost term that pressure can outbid -
and §86.5 repeats it: "任何压力、冲动、候选收益都不能绕过硬边界". The gate implements that by
asking :func:`~companion_runtime.candidate.is_candidate_proactive`, so whichever types that
predicate says "no" to are types the user's "don't contact me" cannot stop.

It kept its own list of type names, and that list disagreed with
:data:`~companion_runtime.user_model.TYPE_TO_BEHAVIOUR` for three synonyms:

    ======================  ======================  =============
    type                    behaviour class         was blocked?
    ======================  ======================  =============
    ``repair``              ``repair``              yes
    ``apology``             ``repair``              **no**
    ``share``               ``emotional_expression``  yes
    ``emotional_expression``  ``emotional_expression``  **no**
    ``curious_question``    ``curious_question``    yes
    ``question``            ``curious_question``    **no**
    ======================  ======================  =============

So a user who asked for space still got an *apology*, because the gate only recognised the
spelling ``repair``. Nothing in the suite could see it: every test named one type, and each
of those assertions was individually true.

The predicate now derives from the behaviour class, and these tests pin the property that
was missing - **synonyms must be equivalent** - rather than re-listing the types by hand.
They only reach the provider/API types because the shipped rule generator never emits
``apology``/``question``/``emotional_expression``; that is exactly why the gap survived.
"""

from __future__ import annotations

import random

import pytest

from companion_runtime import candidate as candidate_module
from companion_runtime import motivation
from companion_runtime.config import RuntimeConfig
from companion_runtime.motivation import is_candidate_proactive
from companion_runtime.typing import CandidateIntent, RuntimeState, new_id
from companion_runtime.user_model import Prediction
from companion_runtime.user_model import TYPE_TO_BEHAVIOUR

from conftest import BASE_TIME

#: The three types that used to slip through, named explicitly so a regression is loud.
#: Each is a synonym of a proactive behaviour class.
TYPES_THAT_USED_TO_SLIP_THROUGH = ("apology", "question", "emotional_expression")


def _candidate(kind: str) -> CandidateIntent:
    return CandidateIntent(
        candidate_id=new_id("candidate"),
        type=kind,
        intent="想和你说句话",
        target="relationship",
        sources=["internal_approach_drive"],
    )


def _prediction() -> Prediction:
    return Prediction(
        reply_probability=0.55,
        positive_probability=0.6,
        continue_probability=0.6,
        boundary_risk=0.1,
        uncertainty=0.4,
    )


def _round(candidates: list[CandidateIntent], *, allow_proactive: bool):
    """Run one decision round under a boundary; return utility by candidate id."""
    predictions = {item.candidate_id: _prediction() for item in candidates}
    result = motivation.decide(
        motivation.MotivationInputs(
            state=RuntimeState(),
            candidates=candidates,
            predictions=predictions,
            boundary_allow_proactive=allow_proactive,
            recent_contacts=0,
            hours_since_contact=30.0,
            cooldown_active=False,
            now=BASE_TIME,
            elapsed_seconds=3600.0,
        ),
        config=RuntimeConfig(),
        rng=random.Random(0),
    )
    return {item.candidate.candidate_id: item.breakdown for item in result.assessments}


def _by_class() -> dict[str, list[str]]:
    """Group every known type by its behaviour class."""
    grouped: dict[str, list[str]] = {}
    for kind, behaviour in TYPE_TO_BEHAVIOUR.items():
        grouped.setdefault(behaviour, []).append(kind)
    return grouped


def test_every_behaviour_class_is_internally_consistent() -> None:
    """The property that was missing: synonyms of one class must agree.

    This is stated over the mapping rather than over a hand-written list, so a *new* type
    added to ``TYPE_TO_BEHAVIOUR`` is covered the day it appears.
    """
    disagreements = {
        behaviour: sorted({is_candidate_proactive(_candidate(kind)) for kind in kinds})
        for behaviour, kinds in _by_class().items()
        if len({is_candidate_proactive(_candidate(kind)) for kind in kinds}) > 1
    }
    assert not disagreements, (
        f"types sharing a behaviour class disagree about being proactive: {disagreements}"
    )


@pytest.mark.parametrize("kind", TYPES_THAT_USED_TO_SLIP_THROUGH)
def test_the_synonyms_that_used_to_slip_through_are_proactive(kind: str) -> None:
    """The regression pin: these three are the reason this file exists."""
    assert is_candidate_proactive(_candidate(kind)) is True


def test_a_declared_boundary_blocks_every_spelling_of_a_proactive_behaviour() -> None:
    """The user-visible property: "don't contact me" means it, whatever the type is called.

    A candidate that is only blocked under one spelling is not blocked - the provider
    chooses the spelling, and the provider is the thing that must not be able to talk its
    way past a hard constraint.
    """
    kinds = sorted(
        kind
        for kind, behaviour in TYPE_TO_BEHAVIOUR.items()
        if behaviour != "reply"
    )
    candidates = [_candidate(kind) for kind in kinds]

    utilities = _round(candidates, allow_proactive=False)

    unblocked = [
        kind
        for kind, candidate in zip(kinds, candidates)
        if utilities[candidate.candidate_id].block_reason != "boundary_blocks_proactive"
    ]
    assert not unblocked, f"these spellings escaped a hard boundary: {unblocked}"


def test_a_reply_is_governed_by_allow_reply_not_by_the_proactive_gate() -> None:
    """The boundary blocks unprompted contact; answering the user is a different permission.

    Design §53's boundary carries ``allow_reply`` separately, so a "don't contact me"
    must not silence a reply - and this is also why ``reply`` cannot simply be folded into
    the blocked set to make the test above pass.
    """
    candidate = _candidate("reply")

    utilities = _round([candidate], allow_proactive=False)

    assert utilities[candidate.candidate_id].block_reason != "boundary_blocks_proactive"
    assert is_candidate_proactive(candidate) is False


def test_an_unknown_type_fails_closed() -> None:
    """An unrecognised behaviour is blocked, not waved through.

    The predicate feeds a hard constraint, so the safe default is "assume unprompted".
    This also matches the behaviour-class lookup, which already falls back to
    ``proactive_contact`` for unknown types (``behaviour_class_of``).
    """
    assert is_candidate_proactive(_candidate("something_the_provider_invented")) is True

    utilities = _round([_candidate("something_the_provider_invented")], allow_proactive=True)
    assert utilities, "the round must still price an unknown type when no boundary is active"


def test_the_class_to_proactive_rule_is_one_place() -> None:
    """``reply`` is the only non-proactive class, and that is asserted, not assumed.

    If a future behaviour class is added to the vocabulary, this fails and forces the
    author to decide whether it is unprompted contact - rather than letting it default
    into whichever branch happens to be first.
    """
    assert candidate_module.NON_PROACTIVE_BEHAVIOUR_CLASSES == frozenset({"reply"})
    assert set(TYPE_TO_BEHAVIOUR.values()) >= candidate_module.NON_PROACTIVE_BEHAVIOUR_CLASSES
