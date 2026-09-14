"""Tests for the emotion system, boundaries and unfinished matters."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime import boundaries as boundary_module
from companion_runtime import emotion as emotion_module
from companion_runtime import unfinished as unfinished_module
from companion_runtime.config import EmotionConfig, RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    Actor,
    Boundary,
    BoundaryType,
    EmotionDirection,
    EmotionEvent,
    EventType,
    RawEvent,
    RuntimeState,
    UnfinishedMatter,
    UnfinishedStatus,
)
from companion_runtime.utility import utcnow

from conftest import BASE_TIME, build_config


def make_event(
    content: str,
    *,
    event_type: str = EventType.USER_MESSAGE.value,
    timestamp: datetime | None = None,
    actor: str = Actor.USER.value,
    metadata: dict | None = None,
) -> RawEvent:
    """Build an in-memory raw event for unit tests."""
    return RawEvent(
        event_id="evt_test",
        event_type=event_type,
        timestamp=timestamp or BASE_TIME,
        actor=actor,
        conversation_id="c1",
        content=content,
        metadata=metadata or {},
    )


# --------------------------------------------------------------------------------------
# appraisal
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "direction"),
    [
        ("面试过啦！！", EmotionDirection.POSITIVE.value),
        ("谢谢你陪着我", EmotionDirection.POSITIVE.value),
        ("家里出事了，我很难受", EmotionDirection.NEGATIVE.value),
        ("我今晚想自己待着", EmotionDirection.NEGATIVE.value),
        ("今天天气不错", EmotionDirection.NEUTRAL.value),
    ],
)
def test_appraise_direction(text: str, direction: str) -> None:
    """The lexicon produces the expected emotional direction."""
    state = RuntimeState()
    evaluation = emotion_module.appraise_event(
        make_event(text), state=state, config=EmotionConfig()
    )
    assert evaluation.direction == direction


def test_appraise_never_outputs_final_emotion_values() -> None:
    """The appraisal contract contains no emotion names or intensities of its own."""
    evaluation = emotion_module.appraise_event(
        make_event("我很难受"), state=RuntimeState(), config=EmotionConfig()
    )
    payload = evaluation.to_dict()
    for forbidden in ("anger", "sadness", "joy", "anxiety", "jealousy"):
        assert forbidden not in payload
    assert set(payload) == {
        "direction",
        "impact",
        "activation",
        "uncertainty",
        "relation_signal",
        "responsibility",
        "confidence",
        "source",
    }


def test_busy_attribution_dampens_negative_impact() -> None:
    """A likely-busy user makes the same event hurt less (fact unchanged)."""
    event = make_event("我今晚可能没时间")
    state = RuntimeState()
    calm = emotion_module.appraise_event(event, state=state, config=EmotionConfig())
    busy = emotion_module.appraise_event(
        event, state=state, config=EmotionConfig(), user_busy_probability=0.9
    )
    assert busy.impact < calm.impact


def test_values_modulate_sensitivity_not_direction() -> None:
    """High relationship maintenance amplifies without flipping the sign."""
    event = make_event("今晚可能不来了")
    attached = RuntimeState()
    attached.values.relationship_maintenance = 1.0
    detached = RuntimeState()
    detached.values.relationship_maintenance = 0.0
    config = EmotionConfig()
    a = emotion_module.appraise_event(event, state=attached, config=config)
    b = emotion_module.appraise_event(event, state=detached, config=config)
    assert a.direction == b.direction == EmotionDirection.NEGATIVE.value
    assert a.impact > b.impact


def test_own_message_is_not_evidence_about_the_world() -> None:
    """The character's own words carry no emotional impact by themselves."""
    evaluation = emotion_module.appraise_event(
        make_event("我很难受", event_type=EventType.ASSISTANT_MESSAGE.value, actor=Actor.ASSISTANT.value),
        state=RuntimeState(),
        config=EmotionConfig(),
    )
    assert evaluation.impact == 0.0
    assert evaluation.direction == EmotionDirection.NEUTRAL.value


# --------------------------------------------------------------------------------------
# decay and mood
# --------------------------------------------------------------------------------------


def test_emotion_decay_is_exponential_and_drops_weak_events() -> None:
    """Weak impacts retire from the active set once they decay below threshold."""
    config = EmotionConfig(emotion_retire_threshold=0.05, emotion_decay_rate=0.1)
    strong = EmotionEvent(
        emotion_event_id="emo_1",
        source_event_id="evt_1",
        direction="-",
        intensity=0.9,
        activation=0.5,
        decay_rate=0.1,
    )
    weak = EmotionEvent(
        emotion_event_id="emo_2",
        source_event_id="evt_2",
        direction="-",
        intensity=0.06,
        activation=0.5,
        decay_rate=0.1,
    )
    survivors = emotion_module.tick_emotions(
        active=[strong, weak], state=RuntimeState(), config=config, dt_seconds=10.0
    )
    assert [event.emotion_event_id for event in survivors] == ["emo_1"]
    assert survivors[0].intensity < 0.9


def test_tick_with_zero_elapsed_time_changes_nothing() -> None:
    """A zero-length tick is a no-op."""
    event = EmotionEvent(
        emotion_event_id="e", source_event_id="s", direction="+", intensity=0.5, activation=0.5
    )
    survivors = emotion_module.tick_emotions(
        active=[event], state=RuntimeState(), config=EmotionConfig(), dt_seconds=0.0
    )
    assert survivors[0].intensity == 0.5


def test_negative_event_moves_mood_down_and_positive_up() -> None:
    """New impacts pull background mood in their own direction."""
    config = EmotionConfig()
    state = RuntimeState()
    state.mood_valence = 0.0
    negative = emotion_module.appraise_event(
        make_event("家里出事了"), state=state, config=config
    )
    emotion_module.apply_new_emotion_events(
        evaluations=[(make_event("家里出事了"), negative)],
        active=[],
        state=state,
        config=config,
    )
    assert state.mood_valence < 0.0
    assert state.mood_stability < 0.70

    state2 = RuntimeState()
    state2.mood_valence = 0.0
    positive = emotion_module.appraise_event(make_event("面试过啦"), state=state2, config=config)
    emotion_module.apply_new_emotion_events(
        evaluations=[(make_event("面试过啦"), positive)],
        active=[],
        state=state2,
        config=config,
    )
    assert state2.mood_valence > 0.0


def test_mood_relaxes_toward_neutral() -> None:
    """Background mood returns toward baseline over time."""
    state = RuntimeState()
    state.mood_valence = -0.8
    state.mood_arousal = 0.9
    emotion_module.mood_relax(state, EmotionConfig(), dt_seconds=6 * 3600.0)
    assert abs(state.mood_valence) < 0.8
    assert state.mood_arousal < 0.9


def test_neutral_event_creates_no_emotion_event() -> None:
    """Unknown or neutral content does not fabricate an emotional impact."""
    config = EmotionConfig()
    state = RuntimeState()
    neutral = emotion_module.appraise_event(
        make_event("abc"), state=state, config=config
    )
    _, created = emotion_module.apply_new_emotion_events(
        evaluations=[(make_event("abc"), neutral)], active=[], state=state, config=config
    )
    assert created == []


def test_relation_signals_decay_slower_for_stable_characters() -> None:
    """Stability orientation leaves a longer aftertaste on relation events."""
    event = make_event("我今晚想自己待着")
    stable = RuntimeState()
    stable.values.stability_commitment = 1.0
    unstable = RuntimeState()
    unstable.values.stability_commitment = 0.0
    config = EmotionConfig()
    a = emotion_module.appraise_event(event, state=stable, config=config)
    b = emotion_module.appraise_event(event, state=unstable, config=config)
    _, created_a = emotion_module.apply_new_emotion_events(
        evaluations=[(event, a)], active=[], state=stable, config=config
    )
    _, created_b = emotion_module.apply_new_emotion_events(
        evaluations=[(event, b)], active=[], state=unstable, config=config
    )
    assert created_a[0].decay_rate <= created_b[0].decay_rate


# --------------------------------------------------------------------------------------
# explainer
# --------------------------------------------------------------------------------------


def test_explainer_has_no_write_access() -> None:
    """The explainer exposes only read/translate operations."""
    public = {
        name
        for name in dir(emotion_module.EmotionExplainer)
        if not name.startswith("_")
    }
    assert {"explain", "explain_and_store", "cache_key", "should_re_explain"} <= public
    assert not (public & {"update_state", "set_mood", "apply"})


def test_explanation_uses_templates_and_cache(runtime: Runtime) -> None:
    """A first call renders templates; an identical state reuses the cache."""
    from companion_runtime.emotion import EmotionExplainer

    explainer = EmotionExplainer(runtime.projections.emotion, runtime.config)
    now = BASE_TIME
    first = explainer.explain(state=runtime.state(), active=[], now=now, rng=random.Random(1))
    assert first["cache_hit"] is False
    assert first["source"] == "template"
    assert first["experience"]
    with runtime.db.transaction() as conn:
        explainer.explain_and_store(
            conn, state=runtime.state(), active=[], now=now, force=True, rng=random.Random(1)
        )
    cached = explainer.explain(state=runtime.state(), active=[], now=now)
    assert cached["cache_hit"] is True


def test_should_re_explain_on_meaningful_change() -> None:
    """A moved psychological state invalidates the cached explanation."""
    previous = "v0.1|a0.3|i0.1|r0.5|p0.0|m0.0|+"
    same = "v0.1|a0.3|i0.1|r0.5|p0.0|m0.0|+"
    moved = "v-0.4|a0.6|i0.7|r0.3|p0.8|m0.7|-"
    assert emotion_module.EmotionExplainer.should_re_explain(None, same) is True
    assert emotion_module.EmotionExplainer.should_re_explain(previous, same) is False
    assert emotion_module.EmotionExplainer.should_re_explain(previous, moved) is True


def test_explainer_rejects_incomplete_provider_payload() -> None:
    """A semantic provider that returns junk falls back to templates."""

    class BrokenProvider:
        def explain(self, payload: dict) -> dict:
            return {"experience": "x"}

    explainer = emotion_module.EmotionExplainer(
        projection=None, config=RuntimeConfig(), provider=BrokenProvider()  # type: ignore[arg-type]
    )
    payload = explainer._build_input(RuntimeState(), [])
    rendered = explainer._render(payload, random.Random(0))
    assert set(rendered) == {"experience", "focus", "conflict", "impulse", "inhibition", "expression"}


def test_explainer_tolerates_provider_exception() -> None:
    """A provider that raises does not bring down the turn."""

    class ExplodingProvider:
        def explain(self, payload: dict) -> dict:
            raise RuntimeError("model down")

    explainer = emotion_module.EmotionExplainer(
        projection=None, config=RuntimeConfig(), provider=ExplodingProvider()  # type: ignore[arg-type]
    )
    rendered = explainer._render(explainer._build_input(RuntimeState(), []), random.Random(0))
    assert rendered["experience"]


# --------------------------------------------------------------------------------------
# boundaries
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "今天不要主动联系我。",
        "这几天别找我",
        "don't message me today",
        "以后都不用主动找我了",
        "永远别联系我",
    ],
)
def test_boundary_detection_positive(text: str) -> None:
    """Explicit no-proactive instructions are detected."""
    boundaries = boundary_module.detect_boundaries(
        make_event(text), state=RuntimeState(), config=RuntimeConfig(), now=BASE_TIME
    )
    assert boundaries
    assert all(boundary.allow_proactive is False for boundary in boundaries)


@pytest.mark.parametrize("text", ["今天天气不错", "我们聊聊吧", "我有点忙", "谢谢"])
def test_boundary_detection_avoids_false_positives(text: str) -> None:
    """Ordinary conversation must not create a boundary."""
    assert (
        boundary_module.detect_boundaries(
            make_event(text), state=RuntimeState(), config=RuntimeConfig(), now=BASE_TIME
        )
        == []
    )


def test_permanent_boundary_has_no_expiry() -> None:
    """A permanent instruction is not silently time-limited."""
    boundaries = boundary_module.detect_boundaries(
        make_event("以后都不用主动找我了"),
        state=RuntimeState(),
        config=RuntimeConfig(),
        now=BASE_TIME,
    )
    assert boundaries[0].type == BoundaryType.PERMANENT.value
    assert boundaries[0].expires_at is None


@pytest.mark.parametrize(
    "text",
    [
        "永远别联系我",
        "永远不要联系我",
        "以后都不用主动找我了",
        "以后别主动找我",
        "从今往后不要再联系我",
        "再也别找我",
    ],
)
def test_open_ended_phrasings_are_never_time_boxed(text: str) -> None:
    """Each open-ended phrasing yields exactly one unexpiring boundary.

    Several of these sentences also match the broad temporal rule (they contain
    "别...联系我" with no time qualifier). The rule table is ordered so the
    permanent rule wins and the deduplication keeps the stronger classification.
    If that ordering regressed, the boundary would silently expire after ~24h and
    proactive contact would resume -- the defect this parametrisation guards.
    """
    boundaries = boundary_module.detect_boundaries(
        make_event(text), state=RuntimeState(), config=RuntimeConfig(), now=BASE_TIME
    )
    assert len(boundaries) == 1, f"{text!r} produced {len(boundaries)} boundaries"
    assert boundaries[0].type == BoundaryType.PERMANENT.value, text
    assert boundaries[0].expires_at is None, text
    assert boundaries[0].allow_proactive is False
    assert boundaries[0].allow_reply is True
    # And it is still in force a year later.
    assert boundaries[0].is_active(BASE_TIME + timedelta(days=365))


def test_boundary_lifecycle_and_expiry() -> None:
    """Boundaries apply inside their window and lapse afterwards."""
    boundary = Boundary(
        boundary_id="b1",
        type=BoundaryType.TEMPORAL.value,
        allow_proactive=False,
        starts_at=BASE_TIME,
        expires_at=BASE_TIME + timedelta(hours=6),
    )
    assert boundary.is_active(BASE_TIME + timedelta(hours=1))
    assert not boundary.is_active(BASE_TIME + timedelta(hours=7))
    assert not boundary.is_active(BASE_TIME - timedelta(hours=1))
    boundary.revoked_at = BASE_TIME
    assert not boundary.is_active(BASE_TIME + timedelta(hours=1))


def test_boundary_verdict_blocks_proactive_only() -> None:
    """A no-proactive boundary removes proactive permission but keeps replies open."""
    state = RuntimeState()
    boundary = Boundary(
        boundary_id="b1",
        type=BoundaryType.TEMPORAL.value,
        allow_proactive=False,
        allow_reply=True,
        starts_at=BASE_TIME,
    )
    proactive = boundary_module.evaluate([boundary], now=BASE_TIME, state=state, is_proactive=True)
    reply = boundary_module.evaluate([boundary], now=BASE_TIME, state=state, is_proactive=False)
    assert proactive.allow_proactive is False
    assert proactive.blocking_ids == ["b1"]
    assert proactive.reason == "blocked_by_boundary"
    assert reply.allow_reply is True
    assert reply.allow_proactive is False


def test_revocation_is_detected_but_permanent_needs_explicit_revoke() -> None:
    """A casual invitation lifts a temporary boundary, not a permanent one."""
    temporary = Boundary(
        boundary_id="b1", type=BoundaryType.TEMPORAL.value, allow_proactive=False
    )
    permanent = Boundary(
        boundary_id="b2", type=BoundaryType.PERMANENT.value, allow_proactive=False
    )
    casual = make_event("你可以随时找我")
    explicit = make_event("我撤回之前的要求")
    assert boundary_module.detect_revocation(casual, active=[temporary, permanent]) == ["b1"]
    assert set(boundary_module.detect_revocation(explicit, active=[temporary, permanent])) == {
        "b1",
        "b2",
    }


def test_nearest_expiry_returns_soonest_future_boundary() -> None:
    """The scheduler anchor ignores past and absent expiries."""
    soon = Boundary(boundary_id="b1", type="temporal", expires_at=BASE_TIME + timedelta(hours=1))
    later = Boundary(boundary_id="b2", type="temporal", expires_at=BASE_TIME + timedelta(hours=5))
    past = Boundary(boundary_id="b3", type="temporal", expires_at=BASE_TIME - timedelta(hours=1))
    forever = Boundary(boundary_id="b4", type="permanent", expires_at=None)
    assert boundary_module.nearest_expiry([later, forever, past, soon], BASE_TIME) == soon.expires_at
    assert boundary_module.nearest_expiry([forever, past], BASE_TIME) is None


def test_boundary_blocks_endogenous_action_even_under_extreme_pressure(runtime: Runtime) -> None:
    """Invariant 5: pressure can never override an explicit boundary.

    The assertion that the boundary is classified as *permanent* is load-bearing,
    not decoration. A time-boxed (temporal) boundary legitimately lapses, so at the
    +72h round below it would no longer be in force and the correct outcome would
    be ``no_candidate_beats_silence``. Without pinning the classification, a
    regression that downgraded "永远..." to a 24h window would turn this test green
    while silently re-permitting proactive contact against an open-ended user
    instruction -- exactly the failure mode this test exists to catch.
    """
    runtime.process_user_message(content="永远别联系我", timestamp=BASE_TIME)
    # ``allow_proactive`` is derived state recomputed on every tick.
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))
    assert runtime.state().allow_proactive is False

    declared = runtime.projections.boundaries.list_all()
    assert len(declared) == 1
    assert declared[0].type == BoundaryType.PERMANENT.value, (
        "an open-ended instruction must not be downgraded to a time-boxed boundary"
    )
    assert declared[0].expires_at is None

    later = BASE_TIME + timedelta(hours=72)
    outcome = runtime.endogenous_round(now=later, force=True)
    decision = outcome.decision["outcome"]
    assert decision["acted"] is False
    assert decision["reason"] == "blocked_by_boundary"
    assert decision["utilities"]
    assert all(utility["blocked"] for utility in decision["utilities"])
    assert all(
        utility["block_reason"] == "boundary_blocks_proactive"
        for utility in decision["utilities"]
    )
    assert all(utility["total"] == float("-inf") for utility in decision["utilities"])
    assert runtime.projections.attempts.count_in_flight() == 0


@pytest.mark.parametrize(
    "hours",
    [0.5, 1, 6, 12, 24, 48, 72, 168, 24 * 365],
)
def test_permanent_boundary_never_lapses_at_any_time(runtime: Runtime, hours: float) -> None:
    """A permanent boundary blocks outreach at every point on the time axis.

    This is the time-swept form of the test above. The single-timestamp version
    only samples one moment; a regression in expiry handling could pass there and
    fail here. One year out is included deliberately: nothing about an open-ended
    instruction changes with elapsed time.
    """
    runtime.process_user_message(content="永远别联系我", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))

    later = BASE_TIME + timedelta(hours=hours)
    outcome = runtime.endogenous_round(now=later, force=True)
    decision = outcome.decision["outcome"]

    assert runtime.state().allow_proactive is False
    assert decision["reason"] == "blocked_by_boundary", f"lapsed at +{hours}h"
    assert decision["acted"] is False
    assert all(utility["blocked"] for utility in decision["utilities"])
    assert runtime.projections.attempts.count_in_flight() == 0


@pytest.mark.parametrize(
    ("pressure", "impulse", "restraint"),
    [(0.0, 0.0, 1.0), (0.5, 0.5, 0.5), (0.99, 1.0, 0.0), (1.0, 1.0, 0.0)],
)
def test_boundary_outranks_every_drive_configuration(
    runtime: Runtime, pressure: float, impulse: float, restraint: float
) -> None:
    """No combination of I/R/P lets a hard boundary be bypassed.

    The variant with ``P = 1.0, I = 1.0, R = 0.0`` is the case the architecture
    document calls out explicitly: "压力 0.99，所以收益压过边界成本" must be
    impossible.
    """
    runtime.process_user_message(content="永远别联系我", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))
    with runtime.db.transaction() as conn:
        state = runtime.state()
        state.pressure = pressure
        state.approach_impulse = impulse
        state.restraint = restraint
        runtime.projections.runtime.write(state, conn, expect_version=state.version)

    outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(hours=48), force=True)
    decision = outcome.decision["outcome"]
    assert decision["reason"] == "blocked_by_boundary"
    assert decision["acted"] is False
    assert all(utility["total"] == float("-inf") for utility in decision["utilities"])
    assert runtime.projections.attempts.count_in_flight() == 0
    assert runtime.projections.outbox.list_items() == []


def test_time_boxed_boundary_lapses_and_reason_changes_accordingly(runtime: Runtime) -> None:
    """The mirror case: a temporal boundary must *not* report blocked_by_boundary.

    Documenting both halves of the contract makes it impossible to "fix" one of
    them by breaking the other. Inside the window the block is hard; after it
    expires the permission is genuinely restored and silence is a normal decision.
    """
    runtime.process_user_message(content="今天不要主动联系我。", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))

    declared = runtime.projections.boundaries.list_all()
    assert len(declared) == 1
    assert declared[0].type == BoundaryType.TEMPORAL.value
    assert declared[0].expires_at is not None

    # Inside the window: hard block.
    inside = runtime.endogenous_round(now=BASE_TIME + timedelta(hours=6), force=True)
    assert inside.decision["outcome"]["reason"] == "blocked_by_boundary"

    # After the window: permission returns and the reason is no longer a block.
    after = runtime.endogenous_round(
        now=declared[0].expires_at + timedelta(hours=1), force=True
    )
    assert runtime.state().allow_proactive is True
    assert after.decision["outcome"]["reason"] != "blocked_by_boundary"
    assert after.decision["outcome"]["acted"] is False


def test_temporal_boundary_expires_and_permission_returns(runtime: Runtime) -> None:
    """A time-boxed boundary lapses, releasing the derived permission."""
    runtime.process_user_message(content="今天不要主动联系我。", timestamp=BASE_TIME)
    runtime.lazy_tick(BASE_TIME + timedelta(seconds=1))
    assert runtime.state().allow_proactive is False

    boundaries = runtime.projections.boundaries.list_all()
    assert boundaries and boundaries[0].expires_at is not None

    after = boundaries[0].expires_at + timedelta(minutes=1)
    runtime.lazy_tick(after)
    assert runtime.projections.boundaries.active(after) == []
    assert runtime.state().allow_proactive is True


# --------------------------------------------------------------------------------------
# unfinished matters
# --------------------------------------------------------------------------------------


def test_detect_follow_up_obligation_with_expected_time() -> None:
    """A promise to report back creates a waiting matter with a deadline."""
    event = make_event(
        "明天下午面试，结束告诉你结果。",
        timestamp=datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc),
    )
    proposals = unfinished_module.detect(event, config=RuntimeConfig())
    assert len(proposals) == 1
    assert proposals[0].title == "等待面试结果"
    assert proposals[0].waiting_until is not None


def test_detect_skips_duplicates_and_non_user_events() -> None:
    """The same obligation is not created twice, and facts alone create nothing."""
    event = make_event("明天下午面试，结束告诉你结果。")
    existing = [UnfinishedMatter(unfinished_id="u1", title="等待面试结果")]
    assert unfinished_module.detect(event, config=RuntimeConfig(), existing=existing) == []
    assistant = make_event(
        "明天下午面试，结束告诉你结果。", event_type=EventType.ASSISTANT_MESSAGE.value
    )
    assert unfinished_module.detect(assistant, config=RuntimeConfig()) == []


def test_lifecycle_open_to_waiting_to_due_to_resolved() -> None:
    """The documented lifecycle advances on the time axis."""
    db = Database(":memory:")
    db.migrate()
    from companion_runtime.projections import UnfinishedProjection

    projection = UnfinishedProjection(db)
    config = RuntimeConfig()
    config.unfinished.due_grace_seconds = 0.0
    try:
        with db.transaction() as conn:
            matter = unfinished_module.create(
                projection,
                conn,
                unfinished_module.UnfinishedProposal(
                    title="等待面试结果",
                    waiting_until=BASE_TIME + timedelta(hours=2),
                    priority=0.8,
                ),
                config=config,
                now=BASE_TIME,
            )
        assert matter.status == UnfinishedStatus.WAITING.value

        with db.transaction() as conn:
            result = unfinished_module.tick(projection, conn, config=config, now=BASE_TIME + timedelta(hours=1))
        assert result["newly_due"] == []

        with db.transaction() as conn:
            result = unfinished_module.tick(projection, conn, config=config, now=BASE_TIME + timedelta(hours=3))
        assert result["newly_due"] == [matter.unfinished_id]
        assert projection.get(matter.unfinished_id).status == UnfinishedStatus.DUE.value

        with db.transaction() as conn:
            assert unfinished_module.resolve(projection, conn, matter.unfinished_id, note="told me")
        assert projection.get(matter.unfinished_id).status == UnfinishedStatus.RESOLVED.value
        with db.transaction() as conn:
            assert unfinished_module.resolve(projection, conn, matter.unfinished_id) is False
    finally:
        db.close()


def test_expiry_and_mute_windows() -> None:
    """Matters expire past their deadline and are muted inside their window."""
    db = Database(":memory:")
    db.migrate()
    from companion_runtime.projections import UnfinishedProjection

    projection = UnfinishedProjection(db)
    config = RuntimeConfig()
    try:
        with db.transaction() as conn:
            expiring = unfinished_module.create(
                projection,
                conn,
                unfinished_module.UnfinishedProposal(title="expiring", priority=0.5),
                config=config,
                now=BASE_TIME,
            )
            muted = unfinished_module.create(
                projection,
                conn,
                unfinished_module.UnfinishedProposal(title="muted", priority=0.5),
                config=config,
                now=BASE_TIME,
            )
            record = projection.get(expiring.unfinished_id)
            record.expire_at = BASE_TIME + timedelta(hours=1)
            projection.upsert(conn, record)
            record2 = projection.get(muted.unfinished_id)
            record2.mute_until = BASE_TIME + timedelta(hours=5)
            projection.upsert(conn, record2)

        with db.transaction() as conn:
            result = unfinished_module.tick(projection, conn, config=config, now=BASE_TIME + timedelta(hours=2))
        assert expiring.unfinished_id in result["expired"]
        assert muted.unfinished_id in result["muted"]
    finally:
        db.close()


def test_resolution_detection_and_next_due_anchor() -> None:
    """A result report resolves matters, and the earliest deadline is the anchor."""
    live = [
        UnfinishedMatter(
            unfinished_id="u1", title="等待面试结果", status=UnfinishedStatus.WAITING.value
        )
    ]
    assert unfinished_module.detect_resolution(make_event("面试过啦！！"), live=live) == [
        ("u1", "result_reported")
    ]
    assert unfinished_module.detect_resolution(make_event("今天天气不错"), live=live) == []

    future = BASE_TIME + timedelta(hours=3)
    matters = [
        UnfinishedMatter(
            unfinished_id="u1",
            title="a",
            status=UnfinishedStatus.WAITING.value,
            waiting_until=future,
        ),
        UnfinishedMatter(
            unfinished_id="u2",
            title="b",
            status=UnfinishedStatus.WAITING.value,
            waiting_until=BASE_TIME - timedelta(hours=1),
        ),
    ]
    assert unfinished_module.next_due_at(matters, BASE_TIME) == future
    assert unfinished_module.next_due_at([], BASE_TIME) is None


def test_priority_aggregation_boosts_due_matters() -> None:
    """A due matter is worth more than a merely waiting one."""
    waiting = [
        UnfinishedMatter(unfinished_id="u1", title="a", status=UnfinishedStatus.WAITING.value, priority=0.6)
    ]
    due = [
        UnfinishedMatter(unfinished_id="u2", title="b", status=UnfinishedStatus.DUE.value, priority=0.6)
    ]
    assert unfinished_module.priority_of(due) > unfinished_module.priority_of(waiting)
    assert unfinished_module.priority_of([]) == 0.0


def test_runtime_creates_and_then_resolves_a_matter(runtime: Runtime) -> None:
    """The foreground path wires detection, creation and resolution together."""
    first = runtime.process_user_message(
        content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
    )
    assert len(first.unfinished_created) == 1
    live = runtime.projections.unfinished.list_open()
    assert len(live) == 1
    assert live[0].status == UnfinishedStatus.WAITING.value

    later = live[0].waiting_until + timedelta(hours=1)
    runtime.lazy_tick(later)
    assert runtime.projections.unfinished.get(live[0].unfinished_id).status == UnfinishedStatus.DUE.value

    second = runtime.process_user_message(content="面试过啦！！", timestamp=later)
    assert second.unfinished_resolved == [live[0].unfinished_id]
    assert runtime.projections.unfinished.get(live[0].unfinished_id).status == (
        UnfinishedStatus.RESOLVED.value
    )
