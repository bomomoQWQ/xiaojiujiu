"""A behaviour must be described the same way when it is predicted and when it is observed.

Design §22.1 fixes ``A_i`` as a record - 是否主动 / 意图类型 / 话题 / 是否追问 / 表达强度 /
情绪暴露程度 / 消息长度 / 是否允许用户退出 - and §25 features a candidate as
``x = phi(A, C, Z)``. Section §27 then reuses that same ``X_i`` in the posterior:

    p(Theta|D) ∝ p(Theta) ∏ p(Y_i|X_i, Theta)^{w_i}

So the ``A`` handed to ``phi`` must describe the behaviour identically on both sides. It
did not: the prediction path passed ``emotional_expression``/``question``/``topic_shift``
while every observation path passed only ``type``/``proactive`` (and two of them derived
``question`` from an ASCII ``"?"`` in the intent text - which no candidate template
contains). The model therefore learned from a feature vector it had never scored, and one
of the design document's own examples (``{"type": "follow_up", "intent": "询问用户今天的
面试结果"}``, design §39) was affected.

These tests pin the canonical ``A`` per type and the parity between the two paths. The
expected values are absolute rather than "whatever the prediction used", so a change that
moves both sides together still fails.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime.api import create_app
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    AttemptState,
    CandidateIntent,
    OutboxKind,
    OutboxStatus,
    new_id,
)
from companion_runtime.user_model import (
    ACTION_FEATURE_NAMES,
    TYPE_TO_BEHAVIOUR,
    extract_features,
)

from conftest import BASE_TIME

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

#: The features ``phi`` derives from ``A`` alone (design §22.1), imported from the source
#: so the expectations below cannot drift from the encoder they describe. The rest of the
#: vector comes from ``C``/``Z`` and is out of scope: comparing it would measure the
#: situation, not the behaviour.
ACTION_FEATURES: tuple[str, ...] = ACTION_FEATURE_NAMES


@pytest.fixture()
def client(runtime: Runtime):
    """A TestClient bound to a fresh Runtime."""
    app = create_app(runtime, runtime.config)
    with TestClient(app) as test_client:
        yield test_client

#: ``type -> (proactive, follow_up, emotional_expression, question, topic_shift)``.
#:
#: ``proactive`` is not invented here: it is ``candidate.is_candidate_proactive``, the
#: same predicate the hard-boundary gate uses, so a behaviour blocked as proactive is
#: also learned about as proactive. ``question`` is 是否追问 (a probe the character
#: chose), ``emotional_expression`` is 情绪暴露程度 and ``topic_shift`` is 话题. None of
#: them may depend on the characters of the rendered sentence.
#: NOTE: the three ``proactive`` values for ``apology`` / ``question`` /
#: ``emotional_expression`` moved from 0 to 1 when the predicate stopped keeping its own
#: type list and started deriving from the behaviour class - see
#: ``docs/BUSINESS_LOGIC_AUDIT.md`` §3 and ``tests/test_boundary_synonyms.py``. This table
#: had encoded the old classification; it was never asserting a design truth, and the
#: parity assertions above it were unaffected by the change.
CANONICAL_ACTION_FEATURES: dict[str, tuple[float, float, float, float, float]] = {
    "contact": (1.0, 0.0, 0.0, 0.0, 0.0),
    "check_in": (1.0, 1.0, 0.0, 1.0, 0.0),
    "follow_up": (1.0, 1.0, 0.0, 1.0, 0.0),
    "question": (1.0, 0.0, 0.0, 1.0, 0.0),
    "curious_question": (1.0, 0.0, 0.0, 1.0, 1.0),
    "share": (1.0, 0.0, 1.0, 0.0, 0.0),
    "emotional_expression": (1.0, 0.0, 1.0, 0.0, 0.0),
    "repair": (1.0, 0.0, 0.0, 0.0, 0.0),
    "apology": (1.0, 0.0, 0.0, 0.0, 0.0),
    "reply": (0.0, 0.0, 0.0, 0.0, 0.0),
}

#: The intent the design document itself uses for a follow-up (design §39). It carries
#: no question mark in either script, which is exactly why a punctuation test is empty.
FOLLOW_UP_INTENT = "询问用户今天的面试结果"


def _canonical(kind: str) -> dict[str, float]:
    """Return the expected action features for ``kind``."""
    values = CANONICAL_ACTION_FEATURES[kind]
    return dict(zip(ACTION_FEATURES, values))


def _action_features(runtime: Runtime, action: dict, context: dict) -> dict[str, float]:
    """Return the action-derived features of one recorded (or proposed) behaviour."""
    extracted = extract_features(
        action=action, context=context, config=runtime.config.user_model
    )
    return {name: extracted[name] for name in ACTION_FEATURES}


def _delivered_attempt(runtime: Runtime, *, kind: str, intent: str) -> tuple[CandidateIntent, str]:
    """Commit, render and deliver one proactive message; return its candidate and attempt."""
    candidate = CandidateIntent(
        candidate_id=new_id("candidate"),
        type=kind,
        intent=intent,
        goal="表达关心",
        sources=["unfinished:unf_parity"],
    )
    with runtime.db.transaction() as conn:
        runtime.projections.candidates.upsert(conn, candidate)
        state = runtime.projections.runtime.ensure()
        attempt_id, _outbox_id = runtime._commit_attempt(
            conn, chosen=candidate, state=state, now=BASE_TIME
        )
    def _pending(kind: str) -> list:
        return [
            item
            for item in runtime.projections.outbox.list_items(
                status=OutboxStatus.PENDING.value, limit=50
            )
            if item.kind == kind and item.payload.get("attempt_id") == attempt_id
        ]

    render_row = _pending(OutboxKind.RENDER.value)[0]
    runtime.reducer.complete_render(outbox_id=render_row.outbox_id, text="在吗", now=BASE_TIME)
    send_row = _pending(OutboxKind.SEND.value)[0]
    runtime.reducer.claim_outbox(owner="w", now=BASE_TIME, limit=1, kinds=["send"])
    runtime.reducer.mark_delivered(outbox_id=send_row.outbox_id, now=BASE_TIME)
    assert runtime.projections.attempts.get(attempt_id).state == AttemptState.SENT.value
    return candidate, attempt_id


def _observed_action_features(runtime: Runtime, attempt_id: str) -> dict[str, float]:
    """Return the action features of the observation recorded for a delivered attempt."""
    row = runtime.projections.user_model.observation_for_attempt(attempt_id)
    assert row is not None, "the outcome must leave an observation behind"
    return _action_features(runtime, row["action_json"], row["context_json"])


def test_every_mapped_type_is_covered_by_these_expectations() -> None:
    """A new candidate type must not appear without an expected encoding.

    ``TYPE_TO_BEHAVIOUR`` is the authority on which types exist, so it is the authority
    on which types these tests must account for.
    """
    assert set(CANONICAL_ACTION_FEATURES) == set(TYPE_TO_BEHAVIOUR), (
        "the expectation table and the type mapping have drifted apart"
    )


@pytest.mark.parametrize("kind", sorted(CANONICAL_ACTION_FEATURES))
def test_the_prediction_path_encodes_each_type_canonically(runtime: Runtime, kind: str) -> None:
    """The behaviour the character *considers* carries the design's §22.1 fields.

    This is the half that already worked for most types; it is pinned here so the fix on
    the observation side cannot be made by moving both sides to a wrong value.
    """
    candidate, _attempt_id = _delivered_attempt(runtime, kind=kind, intent=FOLLOW_UP_INTENT)

    prediction = runtime.user_model.predict(
        action=runtime._action_spec(candidate),
        context=runtime._situation_context(BASE_TIME),
    )

    assert {
        name: prediction.features[name] for name in ACTION_FEATURES
    } == _canonical(kind), f"the prediction path mis-encodes a {kind!r} candidate"


@pytest.mark.parametrize("kind", sorted(CANONICAL_ACTION_FEATURES))
def test_the_user_reply_path_observes_the_same_features_it_predicted(
    runtime: Runtime, kind: str
) -> None:
    """The most common production path: the user answers a delivered message.

    Both halves are asserted - the canonical value *and* the equality with what was
    predicted - because either one alone can be satisfied by the wrong fix.
    """
    candidate, attempt_id = _delivered_attempt(runtime, kind=kind, intent=FOLLOW_UP_INTENT)
    predicted = _action_features(
        runtime,
        runtime._action_spec(candidate),
        runtime._situation_context(BASE_TIME),
    )

    runtime.process_user_message(
        content="嗯，还行。", timestamp=BASE_TIME + timedelta(minutes=5)
    )

    observed = _observed_action_features(runtime, attempt_id)
    assert observed == _canonical(kind), f"a {kind!r} candidate is observed with the wrong A"
    assert observed == predicted, f"a {kind!r} candidate is observed with a different A"


def test_the_silence_path_observes_the_same_features_it_predicted(runtime: Runtime) -> None:
    """A message that is never answered is evidence too (design §22.3), and it must
    describe the behaviour the same way the scoring did."""
    candidate, attempt_id = _delivered_attempt(
        runtime, kind="share", intent="想起你上次说的咖啡，我也有点想喝。"
    )
    predicted = _action_features(
        runtime,
        runtime._action_spec(candidate),
        runtime._situation_context(BASE_TIME),
    )

    horizon = runtime.config.user_model.silence_after_hours
    runtime.lazy_tick(BASE_TIME + timedelta(hours=horizon + 1.0))

    observed = _observed_action_features(runtime, attempt_id)
    assert observed == _canonical("share")
    assert observed == predicted, "being ignored must not change what was being learned about"


def test_the_delivery_receipt_path_observes_the_same_features_it_predicted(
    runtime: Runtime,
) -> None:
    """The host-reported outcome path (``observe_reply``) is a third producer of the
    same observation and must agree with the other two."""
    from companion_runtime.user_model import BehaviourReaction

    candidate, attempt_id = _delivered_attempt(
        runtime, kind="curious_question", intent="之前记过的事，还想再聊聊吗"
    )
    predicted = _action_features(
        runtime,
        runtime._action_spec(candidate),
        runtime._situation_context(BASE_TIME),
    )

    runtime.observe_reply(
        attempt_id=attempt_id,
        reaction=BehaviourReaction(replied=True, reply_delay_seconds=120.0, reply_length=8),
        now=BASE_TIME + timedelta(minutes=3),
    )

    observed = _observed_action_features(runtime, attempt_id)
    assert observed == _canonical("curious_question")
    assert observed == predicted


def test_punctuation_does_not_enter_the_behaviour_vector(runtime: Runtime) -> None:
    """Two identical behaviours must encode identically whatever their wording.

    The intent is generated text; 是否追问 is a property of the behaviour, not of the
    sentence. Deriving it from a question mark made the feature depend on the script -
    and on nothing at all, because no candidate template contains one. A ``contact``
    candidate is used because its canonical ``question`` is ``0``: that is the only case
    in which a punctuation heuristic *changes* the vector, so it is the only case that
    can fail when the heuristic comes back.
    """
    observed: dict[str, dict[str, float]] = {}
    for index, intent in enumerate(("只是想问问", "只是想问问?", "只是想问问？")):
        candidate, attempt_id = _delivered_attempt(runtime, kind="contact", intent=intent)
        runtime.process_user_message(
            content="嗯，在的。", timestamp=BASE_TIME + timedelta(minutes=5 * (index + 1))
        )
        assert candidate.type == "contact"
        observed[intent] = _observed_action_features(runtime, attempt_id)

    assert all(features == _canonical("contact") for features in observed.values()), (
        f"a contact candidate must carry no probe flag, whatever its punctuation: {observed}"
    )
    distinct = {tuple(sorted(features.items())) for features in observed.values()}
    assert len(distinct) == 1, f"the same behaviour encoded differently by wording alone: {observed}"


def test_the_public_observation_endpoint_canonicalises_the_action(
    client: TestClient, runtime: Runtime
) -> None:
    """A host may report *what* happened, not how it maps onto features.

    ``phi`` (design §25) belongs to the Runtime, so the endpoint must not accept a
    behaviour description that contradicts its own ``type``. Passing the payload through
    verbatim meant ``{"type": "share", "proactive": true}`` - a perfectly reasonable thing
    for a host to send - was learned as if nothing had been shown.
    """
    response = client.post(
        "/observations",
        json={
            "action": {"type": "share", "proactive": True, "note": "kept"},
            "context": {},
            "reaction": {"replied": True, "reply_length": 12},
            "now": BASE_TIME.isoformat(),
        },
    )
    assert response.status_code == 200

    row = runtime.projections.user_model.list_observations(limit=5)[0]
    assert row["action_json"]["note"] == "kept", "unknown keys must survive canonicalisation"
    features = _action_features(runtime, row["action_json"], row["context_json"])
    assert features == _canonical("share"), (
        f"the endpoint let a host describe a share without the exposure it implies: {features}"
    )


def test_the_public_observation_endpoint_refuses_to_be_told_the_features(
    client: TestClient, runtime: Runtime
) -> None:
    """The derived flags must be recomputed, not trusted.

    The endpoint's contract is "``type`` and ``proactive`` say what the character did; how
    that maps onto features is the Runtime's business". Trusting a supplied flag reopened
    the same divergence the Runtime's own paths were fixed for, only arriving through the
    front door: a caller could teach the model that a ``reply`` was a probe.
    """
    response = client.post(
        "/observations",
        json={
            "action": {
                "type": "reply",
                "proactive": False,
                "emotional_expression": True,
                "question": True,
                "topic_shift": True,
                "follow_up": True,
            },
            "context": {},
            "reaction": {"replied": True, "reply_length": 12},
            "now": BASE_TIME.isoformat(),
        },
    )
    assert response.status_code == 200

    row = runtime.projections.user_model.list_observations(limit=5)[0]
    features = _action_features(runtime, row["action_json"], row["context_json"])
    assert features == _canonical("reply"), f"a forged action was believed: {features}"


def test_the_public_observation_endpoint_keeps_the_callers_proactive_claim(
    client: TestClient, runtime: Runtime
) -> None:
    """``proactive`` is the caller's claim about the behaviour, and it is honoured.

    It cannot be derived from ``type`` inside the encoder without inverting the module
    layering (``candidate.is_candidate_proactive`` is the gate's authority), so the
    endpoint passes it through - but it *is* canonicalised to a bool, and the derived
    flags are still recomputed from the type.
    """
    response = client.post(
        "/observations",
        json={
            "action": {"type": "share", "proactive": False},
            "context": {},
            "reaction": {"replied": True, "reply_length": 12},
            "now": BASE_TIME.isoformat(),
        },
    )
    assert response.status_code == 200

    row = runtime.projections.user_model.list_observations(limit=5)[0]
    features = _action_features(runtime, row["action_json"], row["context_json"])
    expected = _canonical("share") | {"proactive": 0.0}
    assert features == expected, f"a caller's proactive claim was dropped: {features}"
