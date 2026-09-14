"""Tests for the simplified Bayesian user interaction model."""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import UserModelProjection
from companion_runtime.runtime import Runtime
from companion_runtime.user_model import (
    BEHAVIOUR_CLASSES,
    FEATURE_NAMES,
    BehaviourReaction,
    Prediction,
    UserInteractionModel,
    attribution_weight,
    behaviour_class_of,
    compute_weight,
    describe_absent_reply,
    extract_features,
    recency_weight,
    source_weight,
)

from conftest import BASE_TIME, build_config


def make_model(config: RuntimeConfig | None = None) -> tuple[UserInteractionModel, Database]:
    """Build a model over an in-memory database."""
    db = Database(":memory:")
    db.migrate()
    settings = config or build_config()
    return UserInteractionModel(UserModelProjection(db), settings), db


# --------------------------------------------------------------------------------------
# features and behaviour classes
# --------------------------------------------------------------------------------------


def test_feature_vector_is_complete_and_deterministic() -> None:
    """The feature vector always exposes the documented names in order."""
    features = extract_features(
        action={"type": "contact", "proactive": True, "question": True},
        context={"busy_probability": 0.5, "hours_since_contact": 12.0},
        config=RuntimeConfig().user_model,
    )
    assert tuple(features) == FEATURE_NAMES
    assert features["bias"] == 1.0
    assert features["proactive"] == 1.0
    assert features["question"] == 1.0
    assert features["busy"] == 0.5


def test_behaviour_class_mapping() -> None:
    """Candidate types map onto the hierarchical behaviour classes."""
    assert behaviour_class_of({"type": "follow_up"}) == "follow_up"
    assert behaviour_class_of({"type": "contact"}) == "proactive_contact"
    assert behaviour_class_of({"type": "unknown"}) == "proactive_contact"
    assert set(BEHAVIOUR_CLASSES) >= {"proactive_contact", "follow_up", "repair"}


def test_action_type_drives_different_predictions() -> None:
    """Different behaviour classes do not collapse to the same prediction."""
    model, db = make_model()
    try:
        context = {"busy_probability": 0.0, "hours_since_contact": 12.0}
        follow_up = model.predict(action={"type": "follow_up", "proactive": True}, context=context)
        contact = model.predict(action={"type": "contact", "proactive": True}, context=context)
        assert follow_up.reply_probability != contact.reply_probability
        assert follow_up.behaviour_class == "follow_up"
        assert contact.behaviour_class == "proactive_contact"
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# evidence weighting
# --------------------------------------------------------------------------------------


def test_no_reply_is_a_very_weak_signal() -> None:
    """Invariant 8: a missing reply must not dominate the model."""
    config = RuntimeConfig().user_model
    no_reply = BehaviourReaction(replied=False, reply_delay_seconds=21600)
    reply = BehaviourReaction(replied=True, reply_length=40, continued_topic=True)
    assert source_weight(no_reply, config) < source_weight(reply, config)
    assert source_weight(no_reply, config) == config.no_reply_weight


def test_busy_attribution_collapses_the_weight() -> None:
    """A busy user explains away a slow reply, so it barely updates beliefs."""
    config = RuntimeConfig().user_model
    surely_busy = BehaviourReaction(
        replied=False, reply_delay_seconds=21600, busy_probability=1.0
    )
    mostly_busy = BehaviourReaction(
        replied=False, reply_delay_seconds=21600, busy_probability=0.85
    )
    idle = BehaviourReaction(replied=False, reply_delay_seconds=21600, busy_probability=0.0)
    assert attribution_weight(surely_busy, config) == pytest.approx(config.busy_attribution_floor)
    assert attribution_weight(mostly_busy, config) < 0.2
    assert attribution_weight(idle, config) == 1.0


def test_recency_weight_decays_but_never_to_zero() -> None:
    """Old evidence keeps a floor of influence rather than vanishing."""
    fresh = recency_weight(BASE_TIME, BASE_TIME)
    stale = recency_weight(BASE_TIME - timedelta(days=365), BASE_TIME)
    assert fresh == 1.0
    assert 0.0 < stale < fresh


def test_total_weight_is_the_product_of_components() -> None:
    """The four evidence weights multiply into the total."""
    config = RuntimeConfig().user_model
    reaction = BehaviourReaction(replied=True, reply_length=5, busy_probability=0.2)
    weight = compute_weight(
        reaction, config=config, observed_at=BASE_TIME, now=BASE_TIME, semantic_confidence=0.8
    )
    assert weight.total == pytest.approx(
        weight.source * weight.attribution * weight.semantic * weight.recency
    )


def test_explicit_feedback_outweighs_implicit_behaviour() -> None:
    """A direct statement about preferences is the strongest evidence."""
    config = RuntimeConfig().user_model
    explicit = BehaviourReaction(replied=True, explicit_positive=True, reply_length=8)
    implicit = BehaviourReaction(replied=True, reply_length=8, continued_topic=True)
    assert source_weight(explicit, config) > source_weight(implicit, config)


# --------------------------------------------------------------------------------------
# prediction
# --------------------------------------------------------------------------------------


def test_cold_start_prediction_is_neutral_and_uncertain() -> None:
    """With no evidence the model is agnostic and reports high uncertainty."""
    model, db = make_model()
    try:
        prediction = model.predict(
            action={"type": "contact", "proactive": True},
            context={"busy_probability": 0.0, "hours_since_contact": 12.0},
        )
        assert prediction.cold_start is True
        assert 0.3 < prediction.reply_probability < 0.8
        assert prediction.boundary_risk < 0.25
        assert prediction.uncertainty > 0.4
    finally:
        db.close()


def test_busy_context_lowers_reply_probability() -> None:
    """The fast variable ``Z_t`` shifts the prediction."""
    model, db = make_model()
    try:
        action = {"type": "contact", "proactive": True}
        free = model.predict(action=action, context={"busy_probability": 0.0})
        busy = model.predict(action=action, context={"busy_probability": 0.95})
        assert busy.reply_probability < free.reply_probability
    finally:
        db.close()


def test_conservative_bound_is_below_the_mean() -> None:
    """High-risk candidates use a lower quantile instead of the mean."""
    model, db = make_model()
    try:
        prediction = model.predict(
            action={"type": "contact", "proactive": True},
            context={"busy_probability": 0.5},
        )
        bound = model.conservative_bound(prediction)
        assert bound <= prediction.reply_probability
    finally:
        db.close()


def test_uncertainty_shrinks_with_evidence() -> None:
    """More weighted evidence means a tighter estimate."""
    model, db = make_model()
    try:
        action = {"type": "contact", "proactive": True}
        context = {"busy_probability": 0.0, "hours_since_contact": 12.0}
        before = model.predict(action=action, context=context).uncertainty
        with db.transaction() as conn:
            for _ in range(12):
                model.observe(
                    conn,
                    action=action,
                    context=context,
                    reaction=BehaviourReaction(
                        replied=True, reply_length=30, continued_topic=True, asked_back=True
                    ),
                    now=BASE_TIME,
                    observed_at=BASE_TIME,
                    semantic_confidence=1.0,
                )
        after = model.predict(action=action, context=context).uncertainty
        assert after < before
        assert model.effective_count > 0.0
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# learning
# --------------------------------------------------------------------------------------


def test_repeated_positive_reaction_raises_reply_probability() -> None:
    """Consistent positive evidence moves the model in the right direction."""
    model, db = make_model()
    try:
        action = {"type": "contact", "proactive": True}
        context = {"busy_probability": 0.0, "hours_since_contact": 12.0}
        before = model.predict(action=action, context=context).reply_probability
        with db.transaction() as conn:
            for _ in range(15):
                model.observe(
                    conn,
                    action=action,
                    context=context,
                    reaction=BehaviourReaction(
                        replied=True, reply_length=40, continued_topic=True, explicit_positive=True
                    ),
                    now=BASE_TIME,
                    observed_at=BASE_TIME,
                    semantic_confidence=1.0,
                )
        after = model.predict(action=action, context=context).reply_probability
        assert after > before
    finally:
        db.close()


def test_negative_feedback_raises_boundary_risk_and_lowers_reward() -> None:
    """An explicit rebuke teaches the model that the behaviour is risky."""
    model, db = make_model()
    try:
        action = {"type": "follow_up", "proactive": True, "question": True}
        context = {"busy_probability": 0.0}
        before = model.predict(action=action, context=context)
        with db.transaction() as conn:
            for _ in range(10):
                model.observe(
                    conn,
                    action=action,
                    context=context,
                    reaction=BehaviourReaction(
                        replied=True,
                        explicit_negative=True,
                        boundary_touched=True,
                        reply_length=4,
                    ),
                    now=BASE_TIME,
                    observed_at=BASE_TIME,
                    semantic_confidence=1.0,
                )
        after = model.predict(action=action, context=context)
        assert after.boundary_risk > before.boundary_risk
        assert after.positive_probability < before.positive_probability
    finally:
        db.close()


def test_absent_reply_barely_moves_the_model() -> None:
    """Invariant 8 again, measured: 6 hours of silence is nearly no evidence."""
    model, db = make_model()
    try:
        action = {"type": "contact", "proactive": True}
        context = {"busy_probability": 0.8}
        before = model.predict(action=action, context=context).reply_probability
        with db.transaction() as conn:
            model.observe(
                conn,
                action=action,
                context=context,
                reaction=BehaviourReaction(replied=False, reply_delay_seconds=21600),
                now=BASE_TIME,
                observed_at=BASE_TIME,
                busy_probability=0.85,
            )
        after = model.predict(action=action, context=context).reply_probability
        assert abs(after - before) < 0.02
    finally:
        db.close()


def test_observation_is_recorded_before_interpretation() -> None:
    """The raw observation is stored even though it barely changes beliefs."""
    model, db = make_model()
    try:
        with db.transaction() as conn:
            observation = model.observe(
                conn,
                action={"type": "contact", "proactive": True},
                context={"busy_probability": 0.8},
                reaction=BehaviourReaction(replied=False, reply_delay_seconds=21600),
                now=BASE_TIME,
                observed_at=BASE_TIME,
                busy_probability=0.85,
            )
        stored = model.projection.list_observations()
        assert stored
        payload = stored[0]
        assert payload["outcome_json"]["replied"] is False
        assert payload["outcome_json"]["reply_delay_seconds"] == 21600
        # The stored fact is an observation, never a label like "negative".
        assert "negative" not in payload["outcome_json"]
        assert payload["weight"] == pytest.approx(observation.weight)
    finally:
        db.close()


def test_absent_reply_description_is_neutral() -> None:
    """The helper wording records a fact, not an attribution."""
    text = describe_absent_reply(BehaviourReaction(replied=False, reply_delay_seconds=21600))
    assert text == "no_reply=true, reply_delay=21600"
    assert "negative" not in text


def test_parameters_persist_across_instances() -> None:
    """Learning survives a process restart."""
    db = Database(":memory:")
    db.migrate()
    config = build_config()
    first = UserInteractionModel(UserModelProjection(db), config)
    action = {"type": "contact", "proactive": True}
    context = {"busy_probability": 0.0}
    try:
        with db.transaction() as conn:
            for _ in range(5):
                first.observe(
                    conn,
                    action=action,
                    context=context,
                    reaction=BehaviourReaction(replied=True, reply_length=30, explicit_positive=True),
                    now=BASE_TIME,
                    observed_at=BASE_TIME,
                    semantic_confidence=1.0,
                )
        reloaded = UserInteractionModel(UserModelProjection(db), config)
        assert reloaded.observations == first.observations
        assert reloaded.effective_count == pytest.approx(first.effective_count)
        assert reloaded.predict(action=action, context=context).reply_probability == pytest.approx(
            first.predict(action=action, context=context).reply_probability
        )
    finally:
        db.close()


def test_slow_drift_forgets_precision_not_mean() -> None:
    """``Theta_t ~ N(Theta_{t-1}, Q dt)``: means survive, confidence relaxes."""
    db = Database(":memory:")
    db.migrate()
    config = build_config()
    config.user_model.forgetting_rate = 0.5
    model = UserInteractionModel(UserModelProjection(db), config)
    action = {"type": "contact", "proactive": True}
    context = {"busy_probability": 0.0}
    try:
        with db.transaction() as conn:
            model.observe(
                conn,
                action=action,
                context=context,
                reaction=BehaviourReaction(replied=True, reply_length=30, explicit_positive=True),
                now=BASE_TIME,
                observed_at=BASE_TIME,
                semantic_confidence=1.0,
            )
        precision = list(model._precision["reply_probability"])
        theta = list(model._theta["reply_probability"])
        with db.transaction() as conn:
            model.observe(
                conn,
                action=action,
                context=context,
                reaction=BehaviourReaction(replied=True, reply_length=30, explicit_positive=True),
                now=BASE_TIME,
                observed_at=BASE_TIME,
                semantic_confidence=1.0,
            )
        # Heavy forgetting pulls precision back toward the prior while the mean
        # stays where the evidence put it.
        assert model._precision["reply_probability"][0] <= precision[0] + 1.0
        assert model._theta["reply_probability"][0] != theta[0]
    finally:
        db.close()


def test_semantic_view_is_honest_about_ignorance() -> None:
    """The prose view must not invent knowledge it does not have."""
    model, db = make_model()
    try:
        view = model.semantic_view()
        assert "没有证据" in view["summary"]
        assert view["confidence"] == 0.0
        with db.transaction() as conn:
            for _ in range(10):
                model.observe(
                    conn,
                    action={"type": "contact", "proactive": True},
                    context={"busy_probability": 0.0},
                    reaction=BehaviourReaction(replied=True, reply_length=20, explicit_positive=True),
                    now=BASE_TIME,
                    observed_at=BASE_TIME,
                    semantic_confidence=1.0,
                )
        informed = model.semantic_view()
        assert informed["confidence"] > 0.0
        assert informed["summary"] != view["summary"]
    finally:
        db.close()


def test_numeric_view_is_json_serialisable() -> None:
    """Both views can be returned by the HTTP API directly."""
    import json

    model, db = make_model()
    try:
        with db.transaction() as conn:
            model.observe(
                conn,
                action={"type": "contact", "proactive": True},
                context={},
                reaction=BehaviourReaction(replied=True, reply_length=10),
                now=BASE_TIME,
                observed_at=BASE_TIME,
            )
        json.dumps(model.numeric_view())
        json.dumps(model.semantic_view())
    finally:
        db.close()


def test_busy_probability_estimator() -> None:
    """The fast variable responds to silence length and explicit signals."""
    model, db = make_model()
    try:
        assert model.busy_probability(hours_since_contact=0.5, replied_recently=True) < 0.3
        assert model.busy_probability(hours_since_contact=10.0, replied_recently=False) > 0.5
        assert (
            model.busy_probability(
                hours_since_contact=1.0, replied_recently=True, context={"stated_busy": True}
            )
            >= 0.85
        )
    finally:
        db.close()


def test_prediction_serialisation_is_json_safe() -> None:
    """The prediction object is directly serialisable for proposals."""
    import json

    model, db = make_model()
    try:
        prediction = model.predict(action={"type": "contact", "proactive": True}, context={})
        assert isinstance(prediction, Prediction)
        json.dumps(prediction.to_dict())
    finally:
        db.close()


def test_runtime_exposes_a_working_user_model(runtime: Runtime) -> None:
    """The Runtime wires the model to its projections."""
    before = runtime.user_model.effective_count
    outcome = runtime.process_user_message(content="在的，我挺好的", timestamp=BASE_TIME)
    assert outcome.version > 0
    assert runtime.user_model.numeric_view()["effective_count"] >= before
