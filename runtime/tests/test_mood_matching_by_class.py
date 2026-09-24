"""Mood-matching is decided by behaviour class, not by type spelling.

``Runtime._emotion_alignment`` answers "how appropriate is this behaviour given how the user
currently feels", and its answer goes into the candidate's utility (design §45), so it
decides **which candidate the character picks**. It kept two hand-written type sets that had
no relationship to :data:`~companion_runtime.user_model.TYPE_TO_BEHAVIOUR`:

==========================  ======================  =======
intention                   written as              score
==========================  ======================  =======
repair after a bad landing  ``repair``              **1.000**
the same apology            ``apology``             **0.440**
share good news             ``share``               **1.000**
the same thing              ``emotional_expression``  **0.440**
curious about them          ``curious_question``    **1.000**
the same question           ``question``            **0.440**
==========================  ======================  =======

The same family appeared in ``protocol.reconcile``, whose set omitted ``question`` entirely.
``docs/BUSINESS_LOGIC_AUDIT.md`` §4.

The tables are now stated per behaviour class, and these tests pin the *property* - synonyms
of one class must score identically - rather than re-listing types by hand, so a new type is
covered the day it is added to the vocabulary.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from companion_runtime import protocol as protocol_module
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    AttemptState,
    CandidateIntent,
    EventType,
    ReconcileAction,
    RuntimeState,
    new_id,
)
from companion_runtime.user_model import (
    MOOD_MATCH_NEGATIVE_CLASSES,
    MOOD_MATCH_POSITIVE_CLASSES,
    QUESTION_TYPES,
    TYPE_TO_BEHAVIOUR,
)

from conftest import BASE_TIME

#: The generic (no-match) alignment for a mood of intensity 0.8: 0.2 + 0.3 * 0.8.
GENERIC = pytest.approx(0.44)


@pytest.fixture()
def runtime() -> Runtime:
    instance = Runtime(created_at=BASE_TIME)
    try:
        yield instance
    finally:
        instance.close()


def _candidate(kind: str) -> CandidateIntent:
    return CandidateIntent(
        candidate_id=new_id("candidate"), type=kind, intent="想和你说话", goal="g", sources=["memory:mem_1"]
    )


def _mood(direction: str, intensity: float = 0.8):
    return [SimpleNamespace(direction=direction, intensity=intensity)]


def _by_class() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for kind, behaviour in TYPE_TO_BEHAVIOUR.items():
        grouped.setdefault(behaviour, []).append(kind)
    return grouped


def test_synonyms_of_one_behaviour_class_score_identically(runtime: Runtime) -> None:
    """The property that was missing, stated over the mapping rather than a hand list."""
    for direction, mood in (("-", _mood("-")), ("+", _mood("+"))):
        disagreements = {
            behaviour: sorted(
                {runtime._emotion_alignment(_candidate(kind), mood) for kind in kinds}
            )
            for behaviour, kinds in _by_class().items()
            if len({runtime._emotion_alignment(_candidate(kind), mood) for kind in kinds}) > 1
        }
        assert not disagreements, f"{direction} mood: {disagreements}"


def test_every_behaviour_class_has_a_decided_mood_rule(runtime: Runtime) -> None:
    """A new behaviour class must force a decision instead of defaulting into a branch.

    The tables are asserted literally for the same reason: this is the one place where the
    *content* is a design choice (a follow-up when the user is low, a question when they are
    up), and it should not change silently.
    """
    classes = set(TYPE_TO_BEHAVIOUR.values())
    assert MOOD_MATCH_NEGATIVE_CLASSES <= classes
    assert MOOD_MATCH_POSITIVE_CLASSES <= classes
    assert MOOD_MATCH_NEGATIVE_CLASSES == frozenset({"repair", "follow_up", "proactive_contact"})
    assert MOOD_MATCH_POSITIVE_CLASSES == frozenset(
        {"emotional_expression", "curious_question", "proactive_contact"}
    )
    decided = MOOD_MATCH_NEGATIVE_CLASSES | MOOD_MATCH_POSITIVE_CLASSES
    undecided = classes - decided
    assert undecided == {"reply"}, (
        "every class but `reply` should have a mood rule; a new one must not slip through "
        f"unnoticed: {sorted(undecided)}"
    )


def test_an_unknown_type_earns_no_mood_bonus(runtime: Runtime) -> None:
    """Unknown shapes get the generic value - the opposite decision from the boundary gate.

    The gate fails closed because it is a hard constraint; this is a matter of proportion,
    so an unrecognised shape must not be *rewarded* on a guess.
    """
    invented = _candidate("something_the_provider_invented")

    assert runtime._emotion_alignment(invented, _mood("-")) == GENERIC
    assert runtime._emotion_alignment(invented, _mood("+")) == GENERIC


def test_a_negative_mood_favours_repair_and_care_shapes(runtime: Runtime) -> None:
    """The design intent, asserted through synonyms: an apology counts as a repair."""
    bonus = pytest.approx(1.0)

    for kind in ("repair", "apology", "follow_up", "check_in", "contact"):
        assert runtime._emotion_alignment(_candidate(kind), _mood("-")) == bonus, kind

    for kind in ("share", "emotional_expression", "curious_question", "question", "reply"):
        assert runtime._emotion_alignment(_candidate(kind), _mood("-")) == GENERIC, kind


def test_a_positive_mood_favours_expression_and_curiosity(runtime: Runtime) -> None:
    """And the mirror image: sharing good news counts as emotional expression."""
    bonus = pytest.approx(1.0)

    for kind in ("share", "emotional_expression", "curious_question", "question", "contact"):
        assert runtime._emotion_alignment(_candidate(kind), _mood("+")) == bonus, kind

    for kind in ("repair", "apology", "follow_up", "reply"):
        assert runtime._emotion_alignment(_candidate(kind), _mood("+")) == GENERIC, kind


def _decide(runtime: Runtime, candidates, alignments):
    """Price these candidates the way an endogenous round would."""
    import random

    from companion_runtime import motivation
    from companion_runtime.user_model import Prediction

    predictions = {
        item.candidate_id: Prediction(
            reply_probability=0.55,
            positive_probability=0.6,
            continue_probability=0.6,
            boundary_risk=0.1,
            uncertainty=0.4,
        )
        for item in candidates
    }
    result = motivation.decide(
        motivation.MotivationInputs(
            state=RuntimeState(),
            candidates=list(candidates),
            predictions=predictions,
            boundary_allow_proactive=True,
            recent_contacts=0,
            hours_since_contact=30.0,
            cooldown_active=False,
            now=BASE_TIME,
            elapsed_seconds=3600.0,
        ),
        config=runtime.config,
        rng=random.Random(0),
        emotion_alignment=alignments,
    )
    return {item.candidate.candidate_id: item.breakdown for item in result.assessments}


def test_the_alignment_reaches_the_utility(runtime: Runtime) -> None:
    """It is not a cosmetic number: it is a term in the candidate's utility (design §45).

    Two assertions, both falsifiable: the two spellings of one intention must be priced
    identically, and the alignment term must actually move the price (feeding the generic
    value instead has to change it) - otherwise "they agree" would also be true of a term
    nobody reads.
    """
    mood = _mood("-")
    repair = _candidate("repair")
    apology = _candidate("apology")
    candidates = [repair, apology]
    alignments = {
        item.candidate_id: runtime._emotion_alignment(item, mood) for item in candidates
    }
    assert alignments[repair.candidate_id] == alignments[apology.candidate_id] == 1.0

    priced = _decide(runtime, candidates, alignments)
    assert priced[repair.candidate_id].total == pytest.approx(priced[apology.candidate_id].total)
    assert priced[repair.candidate_id].internal == pytest.approx(
        priced[apology.candidate_id].internal
    )

    # The same candidate with the generic alignment for its mood is priced *differently*.
    generic = {item.candidate_id: 0.44 for item in candidates}
    priced_generic = _decide(runtime, candidates, generic)
    assert priced_generic[repair.candidate_id].internal < priced[repair.candidate_id].internal, (
        "the emotion-alignment term must be consumed by the utility, not merely stored"
    )


def _event(content: str):
    return SimpleNamespace(
        event_id=new_id("evt"),
        event_type=EventType.USER_MESSAGE.value,
        content=content,
        timestamp=BASE_TIME,
    )


def test_the_protocol_classifier_now_covers_the_question_synonym() -> None:
    """``question`` was missing from the re-coordination set, though it is a question.

    ``protocol.reconcile`` uses the set to decide whether the user just answered the thing an
    in-flight intent was about to ask - and answering "面试过了" while a `question`-typed
    attempt is in flight should resolve it, exactly as it does for `curious_question`.
    """
    assert "question" in QUESTION_TYPES, "the shared vocabulary is the source of the set"

    arguments = {
        "attempt_state": AttemptState.COMMITTED.value,
        "attempt_intent": "询问面试结果",
        "attempt_goal": "了解结果并表达关心",
        "new_events": [_event("面试过了，结果是过了")],
        "now": BASE_TIME + timedelta(minutes=1),
    }
    canonical = protocol_module.reconcile(candidate_type="curious_question", **arguments)
    synonym = protocol_module.reconcile(candidate_type="question", **arguments)

    assert canonical.action == ReconcileAction.RESOLVED.value, canonical.to_dict()
    assert synonym.action == canonical.action, (
        f"a `question`-typed attempt must reconcile like a `curious_question` one: {synonym.to_dict()}"
    )
