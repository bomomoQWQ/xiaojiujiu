"""What the cold-start priors actually claim (design §31, §23, §29).

``DEFAULT_THETA`` is where behaviour is decided before any evidence arrives, so it is
where an unexamined number silently becomes a personality. The comment above it used to
claim the vectors were "symmetric where the Runtime should stay agnostic", which was false
for every entry: a belief that multiplies a feature is never neutral.

These tests do not re-assert the comment. They check the properties that must hold
whatever the numbers are, and they refuse to let a *new* column acquire an opinion that
nobody wrote down.
"""

from __future__ import annotations

import math

import pytest

from companion_runtime.runtime import Runtime
from companion_runtime.user_model import DEFAULT_THETA, FEATURE_NAMES

from conftest import BASE_TIME

#: Every feature that carries a non-zero prior, and the claim it encodes. This is the
#: readable form of the table in :data:`DEFAULT_THETA`; the test below keeps the two in
#: step, so adding a feature (or giving a neutral one an opinion) forces a sentence here.
DOCUMENTED_PRIORS: dict[str, str] = {
    "bias": "the intercept: how receptive a person is to an unprompted contact",
    "proactive": "an unprompted message is answered less often than a reply is",
    "follow_up": "a message about something concrete fares better than a vague one",
    "emotional_expression": "showing feeling is a small negative for reply (design §23 情绪表达接受度)",
    "question": "a specific question is answered more often than a statement",
    "topic_shift": "changing the subject is close to neutral, very slightly positive",
    "busy": "a busy user replies less, is less positive and risks more (design §23)",
    "recent_contact_ratio": "contact fatigue is the strongest negative signal (design §29)",
    "hours_since_contact": "a longer silence makes a contact slightly more welcome",
    "collision": "writing while the user is already active is mildly unwelcome",
    "after_boundary": "a known boundary raises risk sharply and lowers warmth",
    "novelty": "novelty is a small positive on every target",
    "explicit_permission": "stated permission is the strongest positive signal (design §26.1)",
}


def _runtime() -> Runtime:
    """A fresh Runtime (kept so the fixture reads as one line)."""
    return Runtime(created_at=BASE_TIME)


@pytest.fixture()
def runtime() -> Runtime:
    """A fresh Runtime whose user model has seen nothing."""
    instance = _runtime()
    try:
        yield instance
    finally:
        instance.close()


def test_every_prior_vector_matches_the_feature_layout() -> None:
    """A vector of the wrong length is silently replaced by the prior on load."""
    for target, values in DEFAULT_THETA.items():
        assert len(values) == len(FEATURE_NAMES), (
            f"{target} has {len(values)} entries for {len(FEATURE_NAMES)} features"
        )
        assert all(math.isfinite(float(value)) for value in values), target


def test_every_feature_with_an_opinion_has_a_written_reason() -> None:
    """No column may acquire a prior that nobody documented.

    This is the check the old comment could not pass in either direction: it described a
    symmetry the table never had. Here the table and the prose are the same object.
    """
    opinionated = {
        name
        for index, name in enumerate(FEATURE_NAMES)
        if any(float(vector[index]) != 0.0 for vector in DEFAULT_THETA.values())
    }
    assert opinionated == set(DOCUMENTED_PRIORS), (
        "a feature gained or lost a prior without its reason being written down"
    )


def test_a_first_contact_is_not_judged_likely_to_touch_a_boundary(
    runtime: Runtime,
) -> None:
    """Design §31's safe exploration, read as a prior (design §53).

    Cold start must not be *suspicious*: if the model already thought an unprompted hello
    was likely to violate a boundary, the character would never explore, which is the
    failure §31 names first ("没数据 → 不确定性高 → 永远不主动").
    """
    prediction = runtime.user_model.predict(
        action={"type": "contact", "proactive": True}, context={}
    )

    assert prediction.cold_start is True, "this test is about the cold-start path"
    threshold = runtime.config.utility.conservative_risk_threshold
    assert prediction.boundary_risk < threshold, (
        f"cold-start risk {prediction.boundary_risk:.3f} is not below the gate {threshold}"
    )
    assert prediction.boundary_risk < 0.5, "a first contact must not read as a likely violation"


def test_risk_rises_only_for_a_known_boundary_or_a_provably_busy_user(
    runtime: Runtime,
) -> None:
    """The one property the comment above ``DEFAULT_THETA`` asserts, checked.

    Cold start is low-risk; it is not *flat*. Risk moves for exactly two reasons, and both
    are evidence rather than suspicion: a boundary the user actually declared, and a user
    who is provably busy.
    """
    action = {"type": "contact", "proactive": True}
    cold = runtime.user_model.predict(action=action, context={}).boundary_risk
    after_boundary = runtime.user_model.predict(
        action=action, context={"ever_boundary": True}
    ).boundary_risk
    busy = runtime.user_model.predict(
        action=action, context={"busy_probability": 1.0}
    ).boundary_risk

    assert cold < after_boundary, "a declared boundary must raise risk"
    assert cold < busy, "a provably busy user must raise risk"


def test_permission_and_contact_fatigue_are_the_two_load_bearing_priors(
    runtime: Runtime,
) -> None:
    """The strongest claims in the table, checked behaviourally (design §26.1 / §29).

    "以后可以多主动找我" is the design's canonical extreme positive evidence and repeated
    unprompted contact its canonical fatigue signal. Both are encoded here before any
    observation exists, so they decide how the character behaves on day one - which is
    exactly why they are worth an assertion rather than a comment.
    """
    action = {"type": "contact", "proactive": True}
    baseline = runtime.user_model.predict(action=action, context={}).reply_probability
    permitted = runtime.user_model.predict(
        action=action, context={"explicit_permission": True}
    ).reply_probability
    fatigued = runtime.user_model.predict(
        action=action, context={"recent_contact_count": 10}
    ).reply_probability

    assert permitted > baseline, "stated permission must raise the expected reply rate"
    assert fatigued < baseline, "contact fatigue must lower it"
    assert baseline - fatigued > permitted - baseline, (
        "the design rates fatigue as the stronger signal (design §29's baseline rule)"
    )
