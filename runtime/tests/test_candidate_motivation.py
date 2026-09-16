"""Tests for candidate intents, the pool manager and the motivation layer."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from companion_runtime import candidate as candidate_module
from companion_runtime import motivation
from companion_runtime import pool as pool_module
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.projections import Projections
from companion_runtime.runtime import Runtime
from companion_runtime.typing import (
    CandidateIntent,
    CandidateOp,
    CandidateStatus,
    Memory,
    RuntimeState,
    UnfinishedMatter,
    UnfinishedStatus,
)
from companion_runtime.user_model import Prediction
from companion_runtime.utility import softplus

from conftest import BASE_TIME, build_config


def make_candidate(
    *,
    candidate_id: str = "cnd_1",
    type: str = "contact",
    intent: str = "想联系用户",
    target: str = "relationship",
    internal_need: float = 0.5,
    unfinished_relevance: float = 0.0,
    sources: list[str] | None = None,
    invalidate_when: list[str] | None = None,
    preconditions: list[str] | None = None,
) -> CandidateIntent:
    """Build a candidate intent for unit tests."""
    return CandidateIntent(
        candidate_id=candidate_id,
        type=type,
        intent=intent,
        target=target,
        sources=sources if sources is not None else ["internal_approach_drive"],
        internal_need=internal_need,
        unfinished_relevance=unfinished_relevance,
        invalidate_when=invalidate_when or [],
        preconditions=preconditions or [],
    )


def make_prediction(**overrides) -> Prediction:
    """Build a prediction with sensible defaults."""
    payload = {
        "reply_probability": 0.55,
        "positive_probability": 0.6,
        "continue_probability": 0.6,
        "boundary_risk": 0.1,
        "uncertainty": 0.4,
    }
    payload.update(overrides)
    return Prediction(**payload)


# --------------------------------------------------------------------------------------
# candidate generation
# --------------------------------------------------------------------------------------


def test_contact_candidate_value_rises_with_impulse_and_pressure() -> None:
    """``V_contact = sigmoid(aI + bP - cR)`` behaves as documented."""
    config = RuntimeConfig()
    calm = RuntimeState()
    calm.approach_impulse = 0.0
    calm.pressure = 0.0
    calm.restraint = 0.9
    driven = RuntimeState()
    driven.approach_impulse = 0.9
    driven.pressure = 0.8
    driven.restraint = 0.1
    assert candidate_module.contact_candidate_value(calm, config) < (
        candidate_module.contact_candidate_value(driven, config)
    )
    assert 0.0 <= candidate_module.contact_candidate_value(calm, config) <= 1.0


def test_generation_creates_follow_up_for_live_matters() -> None:
    """A live unfinished matter yields a grounded follow-up candidate."""
    config = RuntimeConfig()
    produced = candidate_module.generate(
        state=RuntimeState(),
        config=config,
        unfinished=[
            UnfinishedMatter(
                unfinished_id="unf_1",
                title="等待面试结果",
                status=UnfinishedStatus.DUE.value,
                priority=0.8,
            )
        ],
        activated=[],
        existing=[],
        now=BASE_TIME,
    )
    types = {item.type for item in produced}
    assert "follow_up" in types
    follow_up = next(item for item in produced if item.type == "follow_up")
    assert follow_up.sources == ["unfinished:unf_1"]
    assert follow_up.unfinished_relevance > 0.0
    assert "已经得知后续结果" in follow_up.invalidate_when


def test_generation_always_offers_the_pure_contact_candidate() -> None:
    """The permanent "just want to be in contact" candidate is structural."""
    produced = candidate_module.generate(
        state=RuntimeState(), config=RuntimeConfig(), now=BASE_TIME
    )
    contacts = [item for item in produced if item.type == "contact"]
    assert len(contacts) == 1
    assert contacts[0].sources == [candidate_module.CONTACT_SOURCE]


def test_generation_skips_duplicates_of_existing_candidates() -> None:
    """An already-live matter does not produce a second identical candidate."""
    existing = [make_candidate(type="follow_up", target="等待面试结果")]
    produced = candidate_module.generate(
        state=RuntimeState(),
        config=RuntimeConfig(),
        unfinished=[
            UnfinishedMatter(unfinished_id="unf_1", title="等待面试结果", priority=0.9)
        ],
        existing=existing,
        now=BASE_TIME,
    )
    assert not [item for item in produced if item.type == "follow_up"]


def test_memory_candidates_require_enough_confidence() -> None:
    """Weak activation does not produce a curiosity candidate."""
    from companion_runtime.typing import ActivatedMemory

    config = RuntimeConfig()
    config.candidate.confidence_floor = 0.9
    produced = candidate_module.generate(
        state=RuntimeState(),
        config=config,
        activated=[
            (
                ActivatedMemory(memory_id="mem_1", activation=0.2),
                Memory(
                    memory_id="mem_1",
                    kind="episodic",
                    summary="用户以前提过的事",
                    importance=0.2,
                ),
            )
        ],
        existing=[],
        now=BASE_TIME,
    )
    assert not [item for item in produced if item.type == "curious_question"]


def test_candidate_validation_rejects_groundless_thoughts() -> None:
    """A thought that came from nowhere is invalid."""
    assert candidate_module.validate_candidate(make_candidate()) is None
    assert candidate_module.validate_candidate(make_candidate(sources=[])) == "missing_sources"
    assert candidate_module.validate_candidate(make_candidate(intent="")) == "empty_intent"
    assert (
        candidate_module.validate_candidate(make_candidate(type="nonsense"))
        == "unknown_type:nonsense"
    )


def test_plan_operations_prefers_update_over_add_for_duplicates() -> None:
    """The pool does not fill with near-identical thoughts."""
    existing = [make_candidate(candidate_id="cnd_live", type="contact", target="relationship")]
    proposals = [
        make_candidate(candidate_id="cnd_new", type="contact", target="relationship"),
        make_candidate(candidate_id="cnd_other", type="follow_up", target="面试"),
    ]
    operations = candidate_module.plan_operations(
        proposals=proposals, existing=existing, config=RuntimeConfig()
    )
    kinds = [operation.op for operation in operations]
    assert kinds == [CandidateOp.UPDATE.value, CandidateOp.ADD.value]
    assert operations[0].candidate_id == "cnd_live"


def test_should_refresh_logic() -> None:
    """Refreshing is reserved for an empty pool or an elapsed interval."""
    config = RuntimeConfig()
    config.candidate.empty_pool_refresh_seconds = 300.0
    config.candidate.refresh_min_seconds = 900.0
    assert candidate_module.should_refresh(
        existing=[], last_refresh_at=None, now=BASE_TIME, config=config
    )
    assert not candidate_module.should_refresh(
        existing=[], last_refresh_at=BASE_TIME, now=BASE_TIME + timedelta(minutes=1), config=config
    )
    assert candidate_module.should_refresh(
        existing=[], last_refresh_at=BASE_TIME, now=BASE_TIME + timedelta(minutes=10), config=config
    )
    assert not candidate_module.should_refresh(
        existing=[make_candidate()],
        last_refresh_at=BASE_TIME,
        now=BASE_TIME + timedelta(minutes=5),
        config=config,
    )


def test_invalidation_matching_for_known_conditions() -> None:
    """Natural-language invalidation conditions are matched coarsely but usefully."""
    candidate = make_candidate(
        type="follow_up", target="等待面试结果", invalidate_when=["已经得知面试结果"]
    )
    matched = candidate_module.invalidated_by_situation(
        candidate, situation_text="未尽之事：等待面试结果", user_message="面试过啦，结果是过了"
    )
    assert matched == "已经得知面试结果"
    assert (
        candidate_module.invalidated_by_situation(
            candidate, situation_text="", user_message="今天天气不错"
        )
        is None
    )
    # Only part of the condition is present: not enough to retire the candidate.
    assert (
        candidate_module.invalidated_by_situation(
            candidate, situation_text="", user_message="面试有点紧张"
        )
        is None
    )


def test_expires_in_seconds() -> None:
    """Candidate deadlines are reported in seconds."""
    candidate = make_candidate()
    candidate.expires_at = BASE_TIME + timedelta(hours=1)
    assert candidate_module.expires_in_seconds(candidate, BASE_TIME) == 3600.0
    candidate.expires_at = None
    assert candidate_module.expires_in_seconds(candidate, BASE_TIME) is None


# --------------------------------------------------------------------------------------
# pool manager
# --------------------------------------------------------------------------------------


def test_pool_manager_applies_all_four_operations() -> None:
    """ADD / UPDATE / RETIRE / REINTERPRET are all supported and validated."""
    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    config = build_config()
    context = pool_module.PoolContext(projections=projections, config=config)
    try:
        add = candidate_module.CandidateOperation(
            op="add",
            candidate={
                "type": "follow_up",
                "intent": "询问面试结果",
                "target": "面试",
                "sources": ["unfinished:unf_1"],
            },
        )
        with db.transaction() as conn:
            result = pool_module.apply_operations(context, conn, [add], now=BASE_TIME)
        assert len(result.changes) == 1 and not result.rejected
        candidate_id = result.changes[0].candidate_id

        with db.transaction() as conn:
            pool_module.apply_operations(
                context,
                conn,
                [
                    candidate_module.CandidateOperation(
                        op="update", candidate_id=candidate_id, patch={"confidence": 0.9}
                    ),
                    candidate_module.CandidateOperation(
                        op="reinterpret",
                        candidate_id=candidate_id,
                        interpretation="这其实是修复关系的尝试",
                        sources=["unfinished:unf_1"],
                    ),
                ],
                now=BASE_TIME,
            )
        updated = projections.candidates.get(candidate_id)
        assert updated.confidence == 0.9
        versions = projections.interpretations.list_for_target("candidate", candidate_id)
        assert len(versions) == 1 and versions[0]["interpretation_version"] == 1

        with db.transaction() as conn:
            pool_module.apply_operations(
                context,
                conn,
                [
                    candidate_module.CandidateOperation(
                        op="retire", candidate_id=candidate_id, reason="no longer relevant"
                    )
                ],
                now=BASE_TIME,
            )
        assert projections.candidates.get(candidate_id).status == CandidateStatus.RETIRED.value
    finally:
        db.close()


def test_pool_manager_rejects_bad_operations_without_aborting_the_batch() -> None:
    """One malformed operation never takes down the reducer."""
    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    context = pool_module.PoolContext(projections=projections, config=build_config())
    try:
        operations = [
            candidate_module.CandidateOperation(op="add", candidate={"intent": "no sources"}),
            candidate_module.CandidateOperation(op="retire", candidate_id="missing"),
            candidate_module.CandidateOperation(
                op="add",
                candidate={
                    "type": "contact",
                    "intent": "valid",
                    "sources": ["internal_approach_drive"],
                },
            ),
        ]
        with db.transaction() as conn:
            result = pool_module.apply_operations(context, conn, operations, now=BASE_TIME)
        assert len(result.changes) == 1
        assert len(result.rejected) == 2
        reasons = " ".join(str(item["reason"]) for item in result.rejected)
        assert "missing_sources" in reasons
        assert "unknown candidate" in reasons
    finally:
        db.close()


def test_pool_manager_ignores_non_writable_fields() -> None:
    """An update cannot smuggle arbitrary attributes onto a candidate."""
    db = Database(":memory:")
    db.migrate()
    projections = Projections(db)
    context = pool_module.PoolContext(projections=projections, config=build_config())
    try:
        with db.transaction() as conn:
            added = pool_module.apply_operations(
                context,
                conn,
                [
                    candidate_module.CandidateOperation(
                        op="add",
                        candidate={
                            "type": "contact",
                            "intent": "x",
                            "sources": ["internal_approach_drive"],
                        },
                    )
                ],
                now=BASE_TIME,
            )
        candidate_id = added.changes[0].candidate_id
        with db.transaction() as conn:
            pool_module.apply_operations(
                context,
                conn,
                [
                    candidate_module.CandidateOperation(
                        op="update",
                        candidate_id=candidate_id,
                        patch={"candidate_id": "hijacked", "confidence": 0.7},
                    )
                ],
                now=BASE_TIME,
            )
        assert projections.candidates.get(candidate_id) is not None
        assert projections.candidates.get("hijacked") is None
    finally:
        db.close()


def test_candidate_operation_parsing_rejects_unknown_ops() -> None:
    """The proposal-boundary parser rejects unknown operation kinds."""
    operation = candidate_module.CandidateOperation.from_mapping({"op": "ADD", "candidate": {}})
    assert operation.op == "add"
    with pytest.raises(ValueError):
        candidate_module.CandidateOperation.from_mapping({"op": "delete"})


def test_apply_candidate_operations_records_a_raw_event(runtime: Runtime) -> None:
    """The pool manager's work is visible in the append-only history."""
    before = runtime.events.count("candidate_proposal")
    runtime.apply_candidate_operations(
        [
            candidate_module.CandidateOperation(
                op="add",
                candidate={
                    "type": "contact",
                    "intent": "只是想联系",
                    "sources": ["internal_approach_drive"],
                },
            )
        ],
        now=BASE_TIME,
        source="test",
    )
    assert runtime.events.count("candidate_proposal") == before + 1
    assert runtime.projections.candidates.list_active()


# --------------------------------------------------------------------------------------
# silence utility and utility decomposition
# --------------------------------------------------------------------------------------


def test_silence_becomes_more_painful_as_pressure_rises() -> None:
    """High pressure erodes the value of staying quiet, quadratically."""
    config = RuntimeConfig()
    state = RuntimeState()
    state.approach_impulse = 0.7
    state.restraint = 0.5
    state.pressure = 0.0
    calm = motivation.silence_utility(
        state=state, config=config, boundary_risk=0.0, cooldown_active=False, hours_since_contact=24.0
    )
    state.pressure = 0.9
    tense = motivation.silence_utility(
        state=state, config=config, boundary_risk=0.0, cooldown_active=False, hours_since_contact=24.0
    )
    assert tense < calm


def test_utility_components_follow_the_documented_formula() -> None:
    """``U = V_internal + V_user + V_relation - C_boundary - C_interrupt - C_repeat - C_risk``."""
    config = RuntimeConfig()
    state = RuntimeState()
    breakdown = motivation.candidate_utility(
        candidate=make_candidate(unfinished_relevance=0.7, internal_need=0.8),
        state=state,
        prediction=make_prediction(),
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=30.0,
        emotion_alignment=0.5,
    )
    expected = (
        breakdown.internal
        + breakdown.user
        + breakdown.relation
        - breakdown.boundary_cost
        - breakdown.interrupt_cost
        - breakdown.repeat_cost
        - breakdown.risk_cost
        - config.utility.uncertainty_penalty * breakdown.uncertainty
    )
    assert breakdown.total == pytest.approx(expected, abs=1e-9)


def test_boundary_risk_raises_cost_and_lowers_total() -> None:
    """Predicted boundary risk is where the caution actually lives."""
    config = RuntimeConfig()
    state = RuntimeState()
    calm = motivation.candidate_utility(
        candidate=make_candidate(),
        state=state,
        prediction=make_prediction(boundary_risk=0.02),
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=24.0,
    )
    risky = motivation.candidate_utility(
        candidate=make_candidate(),
        state=state,
        prediction=make_prediction(boundary_risk=0.85),
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=24.0,
    )
    assert risky.boundary_cost > calm.boundary_cost
    assert risky.total < calm.total


def test_blocked_candidate_has_negative_infinite_utility() -> None:
    """A hard-blocked candidate is removed from the comparison entirely."""
    breakdown = motivation.candidate_utility(
        candidate=make_candidate(),
        state=RuntimeState(),
        prediction=make_prediction(),
        config=RuntimeConfig(),
        boundary_risk_baseline=1.0,
        recent_contacts=0,
        hours_since_contact=24.0,
        blocked=True,
        block_reason="boundary_blocks_proactive",
    )
    assert breakdown.total == float("-inf")
    assert breakdown.blocked is True


def test_concrete_reason_beats_pure_loneliness() -> None:
    """Urgency is what separates a reason to speak from mere loneliness."""
    config = RuntimeConfig()
    state = RuntimeState()
    plain = motivation.candidate_utility(
        candidate=make_candidate(unfinished_relevance=0.0, internal_need=0.5),
        state=state,
        prediction=make_prediction(),
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=24.0,
    )
    grounded = motivation.candidate_utility(
        candidate=make_candidate(unfinished_relevance=0.8, internal_need=0.5),
        state=state,
        prediction=make_prediction(),
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=24.0,
    )
    assert grounded.total > plain.total


def test_repeat_pressure_raises_cost() -> None:
    """Repeated contact within the window is penalised."""
    config = RuntimeConfig()
    cheap = motivation.candidate_utility(
        candidate=make_candidate(),
        state=RuntimeState(),
        prediction=make_prediction(),
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=0,
        hours_since_contact=24.0,
    )
    expensive = motivation.candidate_utility(
        candidate=make_candidate(),
        state=RuntimeState(),
        prediction=make_prediction(),
        config=config,
        boundary_risk_baseline=0.0,
        recent_contacts=5,
        hours_since_contact=24.0,
    )
    assert expensive.repeat_cost > cheap.repeat_cost
    assert expensive.total < cheap.total


# --------------------------------------------------------------------------------------
# hazard rate
# --------------------------------------------------------------------------------------


def test_hazard_is_zero_for_non_positive_advantage() -> None:
    """A negative advantage means silence: no chance to act at all."""
    config = RuntimeConfig()
    assert motivation.hazard_rate(-1.0, config=config) < config.utility.hazard_base * 1.05
    assert motivation.hazard_rate(0.0, config=config) == pytest.approx(
        config.utility.hazard_base * softplus(0.0), rel=1e-6
    )
    # The bound above cannot detect a missing sign guard: ``hazard_rate`` is a
    # strictly positive softplus, so it stays under ``hazard_base * 1.05`` for every
    # advantage below ~+0.13. What actually keeps a losing candidate silent is the
    # eligibility filter in ``decide``, so that is what is asserted: no matter how
    # long the exposure, a negative advantage never accumulates a chance to act.
    state = RuntimeState()
    state.approach_impulse = 0.0
    state.pressure = 0.0
    state.restraint = 0.9
    result = _decide(
        state=state,
        candidates=[make_candidate(internal_need=0.1, unfinished_relevance=0.0)],
        elapsed_seconds=1e9,
    )
    assert result.outcome.advantage < 0
    assert result.outcome.hazard == 0.0
    assert result.outcome.action_probability == 0.0
    assert result.outcome.acted is False
    assert result.outcome.reason == "no_candidate_beats_silence"


def test_hazard_grows_with_advantage_and_is_smooth() -> None:
    """No threshold cliff: the hazard is continuous in the advantage."""
    config = RuntimeConfig()
    low = motivation.hazard_rate(0.2, config=config)
    high = motivation.hazard_rate(2.0, config=config)
    assert 0 < low < high
    # A hair's difference in advantage must not change the decision materially.
    nearly = motivation.hazard_rate(0.8001, config=config)
    almost = motivation.hazard_rate(0.7999, config=config)
    assert abs(nearly - almost) / max(nearly, 1e-12) < 0.01


def test_action_probability_is_a_survival_curve() -> None:
    """``P = 1 - exp(-lambda dt)`` grows with elapsed time, not with heartbeats."""
    hazard = 0.001
    short = motivation.action_probability(hazard, 60.0)
    long = motivation.action_probability(hazard, 600.0)
    assert 0.0 < short < long < 1.0
    assert motivation.action_probability(hazard, 0.0) == 0.0
    assert motivation.action_probability(0.0, 600.0) == 0.0
    assert motivation.probability_of_silence(hazard=hazard, delta_t=600.0) == pytest.approx(
        1.0 - long
    )


def test_two_short_ticks_match_one_long_tick() -> None:
    """The hazard is frequency-independent, which is the point of using it."""
    hazard = 0.0005
    one_long = motivation.action_probability(hazard, 1200.0)
    survival_two = (1.0 - motivation.action_probability(hazard, 600.0)) ** 2
    assert one_long == pytest.approx(1.0 - survival_two, rel=1e-9)


# --------------------------------------------------------------------------------------
# drive dynamics
# --------------------------------------------------------------------------------------


def test_absence_raises_impulse_target() -> None:
    """Nothing pushes the character toward speaking like long silence."""
    config = RuntimeConfig()
    state = RuntimeState()
    fresh = motivation.target_drives(
        motivation.DriveInputs(hours_since_contact=1.0), state=state, config=config
    )
    lonely = motivation.target_drives(
        motivation.DriveInputs(hours_since_contact=48.0), state=state, config=config
    )
    assert lonely.impulse > fresh.impulse


def test_boundary_and_busyness_raise_restraint_and_lower_impulse() -> None:
    """Explicit constraints are respected by the dynamics, not just the gate."""
    config = RuntimeConfig()
    state = RuntimeState()
    free = motivation.target_drives(
        motivation.DriveInputs(hours_since_contact=48.0), state=state, config=config
    )
    constrained = motivation.target_drives(
        motivation.DriveInputs(
            hours_since_contact=48.0, boundary_pressure=1.0, user_busy=0.9
        ),
        state=state,
        config=config,
    )
    assert constrained.impulse < free.impulse
    assert constrained.restraint > free.restraint


def test_values_compile_into_different_dynamics() -> None:
    """The same situation produces different drives for different personalities."""
    config = RuntimeConfig()
    inputs = motivation.DriveInputs(hours_since_contact=36.0)
    cautious = RuntimeState()
    cautious.values.boundary_respect = 1.0
    cautious.values.stability_commitment = 1.0
    careless = RuntimeState()
    careless.values.boundary_respect = 0.0
    careless.values.stability_commitment = 0.0
    cautious_targets = motivation.target_drives(inputs, state=cautious, config=config)
    careless_targets = motivation.target_drives(inputs, state=careless, config=config)
    # Impulse is driven by care and relationship maintenance; restraint by
    # boundary respect and stability. A cautious character ends up with a much
    # smaller approachable gap, which is the quantity that builds pressure.
    assert (cautious_targets.restraint - cautious_targets.impulse) > (
        careless_targets.restraint - careless_targets.impulse
    )

    caring = RuntimeState()
    caring.values.user_care = 1.0
    indifferent = RuntimeState()
    indifferent.values.user_care = 0.0
    unfinished = motivation.DriveInputs(hours_since_contact=36.0, unfinished=1.0)
    assert motivation.target_drives(
        unfinished, state=caring, config=config
    ).impulse > motivation.target_drives(unfinished, state=indifferent, config=config).impulse


def test_impulse_inertia_approaches_the_target() -> None:
    """``dI/dt = (I_target - I) / tau`` is applied with inertia, not instantly."""
    config = RuntimeConfig()
    state = RuntimeState()
    state.approach_impulse = 0.0
    state.restraint = 0.5
    targets = motivation.DriveTargets(impulse=0.9, restraint=0.2)
    motivation.step_drives(state=state, targets=targets, config=config, dt_seconds=600.0)
    assert 0.0 < state.approach_impulse < 0.9
    motivation.step_drives(state=state, targets=targets, config=config, dt_seconds=10 * 3600.0)
    assert state.approach_impulse == pytest.approx(0.9, rel=1e-2)


def test_zero_dt_is_a_no_op() -> None:
    """A tick with no elapsed time changes nothing."""
    state = RuntimeState()
    state.approach_impulse = 0.3
    motivation.step_drives(
        state=state,
        targets=motivation.DriveTargets(impulse=0.9, restraint=0.1),
        config=RuntimeConfig(),
        dt_seconds=0.0,
    )
    assert state.approach_impulse == 0.3


def test_pressure_accumulates_when_impulse_exceeds_restraint() -> None:
    """``I > R`` builds pressure; ``R > I`` releases it."""
    config = RuntimeConfig()
    tense = RuntimeState()
    tense.approach_impulse = 0.9
    tense.restraint = 0.1
    motivation.step_drives(
        state=tense,
        targets=motivation.DriveTargets(impulse=0.9, restraint=0.1),
        config=config,
        dt_seconds=3600.0,
    )
    assert tense.pressure > 0.0

    calm = RuntimeState()
    calm.approach_impulse = 0.1
    calm.restraint = 0.9
    calm.pressure = 0.5
    motivation.step_drives(
        state=calm,
        targets=motivation.DriveTargets(impulse=0.1, restraint=0.9),
        config=config,
        dt_seconds=3600.0,
    )
    assert calm.pressure < 0.5


def test_pressure_is_bounded_and_saturating() -> None:
    """The ``(1 - P)`` factor prevents runaway pressure."""
    config = RuntimeConfig()
    state = RuntimeState()
    state.approach_impulse = 1.0
    state.restraint = 0.0
    for _ in range(50):
        motivation.step_drives(
            state=state,
            targets=motivation.DriveTargets(impulse=1.0, restraint=0.0),
            config=config,
            dt_seconds=3600.0,
        )
    assert 0.0 <= state.pressure <= 1.0


def test_release_after_contact_transitions_state() -> None:
    """Committing contact releases impulse and pressure and raises restraint."""
    config = RuntimeConfig()
    state = RuntimeState()
    state.approach_impulse = 0.8
    state.pressure = 0.8
    state.restraint = 0.2
    motivation.release_after_contact(state, config=config, now=BASE_TIME)
    assert state.approach_impulse < 0.8
    assert state.pressure < 0.8
    assert state.restraint > 0.2
    assert state.cooldown_until is not None
    assert motivation.cooldown_remaining(state, BASE_TIME) > 0.0


# --------------------------------------------------------------------------------------
# the decision round
# --------------------------------------------------------------------------------------


def _decide(
    *,
    config: RuntimeConfig | None = None,
    state: RuntimeState | None = None,
    candidates: list[CandidateIntent] | None = None,
    allow_proactive: bool = True,
    cooldown_active: bool = False,
    elapsed_seconds: float = 3600.0,
    rng: random.Random | None = None,
) -> motivation.MotivationResult:
    """Run one decision round with compact arguments."""
    settings = config or RuntimeConfig()
    runtime_state = state or RuntimeState()
    items = candidates if candidates is not None else [make_candidate()]
    predictions = {item.candidate_id: make_prediction() for item in items}
    return motivation.decide(
        motivation.MotivationInputs(
            state=runtime_state,
            candidates=items,
            predictions=predictions,
            boundary_allow_proactive=allow_proactive,
            recent_contacts=0,
            hours_since_contact=30.0,
            cooldown_active=cooldown_active,
            now=BASE_TIME,
            elapsed_seconds=elapsed_seconds,
        ),
        config=settings,
        rng=rng or random.Random(0),
    )


def test_no_candidate_beats_silence_when_the_character_is_relaxed() -> None:
    """A calm, well-restrained character stays quiet."""
    state = RuntimeState()
    state.approach_impulse = 0.0
    state.pressure = 0.0
    state.restraint = 0.9
    result = _decide(
        state=state,
        candidates=[make_candidate(internal_need=0.1, unfinished_relevance=0.0)],
    )
    assert result.outcome.acted is False
    assert result.outcome.reason == "no_candidate_beats_silence"
    assert result.outcome.advantage < 0


def test_blocked_by_boundary_is_reported_and_never_acts() -> None:
    """Invariant 5 at the decision layer."""
    result = _decide(allow_proactive=False)
    assert result.outcome.acted is False
    assert result.outcome.reason == "blocked_by_boundary"
    # Guarded before being quantified: ``all([])`` is True, so without this the line
    # below would pass on an empty assessment list and the test's own claim -- that the
    # block *is reported* -- would go unchecked.
    assert result.assessments, "the block must be reported per candidate"
    assert all(item.breakdown.total == float("-inf") for item in result.assessments)


def test_cooldown_suppresses_action() -> None:
    """After contact, acting again immediately is not allowed."""
    result = _decide(cooldown_active=True)
    assert result.outcome.acted is False
    assert result.assessments, "the block must be reported per candidate"
    assert all(
        assessment.breakdown.block_reason == "cooldown_active"
        for assessment in result.assessments
    )


def test_strong_advantage_leads_to_action() -> None:
    """A grounded, urgent candidate wins when the hazard fires."""
    state = RuntimeState()
    state.approach_impulse = 0.9
    state.pressure = 0.85
    state.restraint = 0.3
    result = _decide(
        state=state,
        candidates=[
            make_candidate(
                type="follow_up",
                intent="询问面试结果",
                target="面试",
                unfinished_relevance=0.95,
                internal_need=0.95,
            )
        ],
        rng=random.Random(1),
    )
    assert result.outcome.advantage > 0
    assert result.outcome.acted is True
    assert result.outcome.reason == "hazard_triggered"
    assert result.outcome.chosen_candidate_id == "cnd_1"


def test_no_action_when_elapsed_time_is_zero() -> None:
    """With no time elapsed there is no hazard exposure, so nothing happens."""
    state = RuntimeState()
    state.approach_impulse = 0.9
    state.pressure = 0.9
    state.restraint = 0.2
    result = _decide(
        state=state,
        candidates=[make_candidate(unfinished_relevance=0.9, internal_need=0.9)],
        elapsed_seconds=0.0,
    )
    assert result.outcome.acted is False
    assert result.outcome.reason == "hazard_not_triggered"
    assert result.outcome.action_probability == 0.0


def test_candidate_selection_uses_softmax_with_temperature() -> None:
    """Higher utility candidates win more often, but not deterministically."""
    state = RuntimeState()
    state.approach_impulse = 0.9
    state.pressure = 0.9
    state.restraint = 0.2
    config = RuntimeConfig()
    config.utility.temperature = 0.25
    candidates = [
        make_candidate(
            candidate_id="cnd_strong",
            type="follow_up",
            target="面试",
            unfinished_relevance=0.95,
            internal_need=0.95,
        ),
        make_candidate(candidate_id="cnd_weak", type="curious_question", target="coffee"),
    ]
    wins = {"cnd_strong": 0, "cnd_weak": 0}
    for seed in range(60):
        result = _decide(
            config=config, state=state, candidates=candidates, rng=random.Random(seed)
        )
        if result.outcome.acted and result.outcome.chosen_candidate_id:
            wins[result.outcome.chosen_candidate_id] += 1
    assert wins["cnd_strong"] > wins["cnd_weak"]


def test_distribution_sums_to_one() -> None:
    """The inspection helper exposes a proper distribution."""
    weights = motivation.tie_break_softmax([0.5, 0.2, -0.1], config=RuntimeConfig())
    assert sum(weights) == pytest.approx(1.0)


def test_reproduce_advantage_ignores_blocked_candidates() -> None:
    """Blocked candidates cannot contribute to the advantage."""
    assert motivation.reproduce_advantage(
        totals=[0.1, 0.9], blocked=[False, True], silence=0.4
    ) == pytest.approx(-0.3)
    assert motivation.reproduce_advantage(totals=[0.0], blocked=[True], silence=0.4) == float(
        "-inf"
    )


def test_precondition_failure_blocks_a_candidate() -> None:
    """Preconditions gate a candidate independently of utility."""
    candidate = make_candidate(preconditions=["需要用户在线"])
    holds, failed = motivation.precondition_holds(candidate, situation_text="用户在线，正在聊天")
    assert holds and failed is None
    # No overlapping content token at all: the precondition cannot be shown to
    # hold, so the candidate is blocked.
    holds, failed = motivation.precondition_holds(candidate, situation_text="nothing relevant")
    assert not holds and failed == "需要用户在线"
    assert motivation.precondition_holds(make_candidate(), situation_text="") == (True, None)


def test_is_candidate_proactive_classification() -> None:
    """Replies are not proactive; outreach is."""
    assert motivation.is_candidate_proactive(make_candidate(type="contact"))
    assert motivation.is_candidate_proactive(make_candidate(type="follow_up"))
    assert not motivation.is_candidate_proactive(make_candidate(type="reply"))


def test_decision_outcome_is_serialisable() -> None:
    """The decision payload is JSON-safe for the HTTP API."""
    import json

    result = _decide()
    json.dumps(result.to_dict())


def test_advantage_never_reported_as_infinity() -> None:
    """The API must never emit an infinite number."""
    result = _decide(allow_proactive=False)
    assert result.outcome.advantage == -99.0
    assert result.to_dict()["outcome"]["advantage"] == -99.0
