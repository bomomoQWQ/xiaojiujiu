"""Cold start must be escapable: safe exploration is wired, bounded, and never overrides safety."""
from __future__ import annotations

import math
import random
from datetime import timedelta

from companion_runtime.runtime_v2 import (
    DecisionConfigV2,
    PredictionSetV2,
    SendAckV2,
    V2RuntimeCoordinator,
)
from companion_runtime.user_model_v2_service import UserModelV2Service
from companion_runtime.user_model_v2_types import SupportStatus, Target, TargetPredictionV2

from test_runtime_v2 import NOW, SCOPE, FakeLegacyBridge, FakePostgresRepository


def cold_predictions() -> PredictionSetV2:
    """Exactly what production sees before any exposure exists: three cold heads."""

    def head(target: Target) -> TargetPredictionV2:
        return TargetPredictionV2(
            prediction_id=f"prediction:cold:{target.value}",
            scope_key=SCOPE,
            target=target,
            point=None,
            lower=None,
            upper=None,
            interval_level=None,
            interval_kind=None,
            support=SupportStatus.UNAVAILABLE,
            predicted_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )

    return PredictionSetV2(
        snapshot_id="prediction:snapshot:cold",
        parameter_version="none",
        reply=head(Target.REPLY),
        continuation=head(Target.CONTINUE),
        negative=head(Target.NEGATIVE),
    )


class TrackingRepository(FakePostgresRepository):
    """Adds the exploration counter the coordinator reads through the protocol."""

    def __init__(self, order, *, spent=0, last_spent_at=None):
        super().__init__(order)
        self.spent = spent
        self.last_spent_at = last_spent_at
        self.recorded: list[tuple[str, bool]] = []

    def count_cold_start_explorations(self, *, scope_key, since):
        assert scope_key == SCOPE
        return self.spent

    def last_cold_start_exploration_at(self, *, scope_key):
        assert scope_key == SCOPE
        return self.last_spent_at

    def record_acknowledged_exposure(
        self, *, exposure_id, acknowledged_at, concern_id=None, action_goal_id=None,
        cold_start_exploration=False,
    ):
        self.recorded.append((exposure_id, cold_start_exploration))


def build(*, spent=0, hazard_base=1.0, limit=1, advantage=1.0, spacing=timedelta(0),
          last_spent_at=None):
    order: list[str] = []
    legacy = FakeLegacyBridge(order)
    repository = TrackingRepository(order, spent=spent, last_spent_at=last_spent_at)
    repository.predictions = cold_predictions()
    runtime = V2RuntimeCoordinator(
        scope_key=SCOPE,
        legacy=legacy,
        user_model=UserModelV2Service(repository),
        repository=repository,
        config=DecisionConfigV2(
            hazard_base=hazard_base,
            hazard_beta=4.0,
            cold_start_exploration_limit=limit,
            cold_start_exploration_window=timedelta(hours=24),
            cold_start_exploration_min_spacing=spacing,
            cold_start_exploration_advantage=advantage,
        ),
        rng=random.Random(0),
    )
    return runtime, legacy, repository


def stages(repository, decision_id):
    return [row["stage"] for row in repository.audits[decision_id]["events"]]


def test_cold_start_without_exploration_wiring_would_never_send():
    """Pin the arithmetic that caused the production deadlock."""

    runtime, _legacy, repository = build(spent=1)
    decision = runtime.decide_endogenous(
        decision_id="decision:no-budget", now=NOW, elapsed_allowed_seconds=600.0
    )
    assert decision.acted is False
    assert decision.reason == "no_eligible_candidate"
    assessment = decision.assessments[0]
    # Benefits are structurally zero and the full negative range is charged, so the
    # conservative utility is negative and can never clear a zero threshold.
    assert assessment.user_utility.used_bounds.p_reply_lower == 0.0
    assert assessment.user_utility.used_bounds.p_negative_upper == 1.0
    assert assessment.net_utility < runtime.config.utility_threshold
    assert decision.audit["run"]["lambda"] == 0.0


def test_safe_cold_start_candidate_may_spend_one_bounded_exploration():
    runtime, _legacy, repository = build(spent=0)
    decision = runtime.decide_endogenous(
        decision_id="decision:explore", now=NOW, elapsed_allowed_seconds=600.0
    )
    assert decision.acted is True
    assert decision.reason == "committed"
    eligible = [
        row for row in repository.audits["decision:explore"]["events"]
        if row["stage"] == "candidate_eligible"
    ]
    assert eligible[-1]["details"]["cold_start_exploration"] is True
    # The exploration is not left at the hazard floor; the budget bounds its frequency.
    assert decision.audit["run"]["D"] >= runtime.config.cold_start_exploration_advantage
    assert decision.audit["run"]["lambda"] > 0.0
    assert stages(repository, "decision:explore")[-2:] == ["committed", "rendered"] or (
        "committed" in stages(repository, "decision:explore")
    )


def test_exploration_is_recorded_as_spend_only_on_delivery():
    runtime, legacy, repository = build(spent=0)
    runtime.decide_endogenous(
        decision_id="decision:spend", now=NOW, elapsed_allowed_seconds=600.0
    )
    runtime.mark_rendered(
        decision_id="decision:spend", outbox_id="outbox:render:1",
        now=NOW + timedelta(seconds=1),
    )
    from companion_runtime.runtime_v2 import SendAckV2

    runtime.acknowledge_send(
        SendAckV2(
            decision_id="decision:spend",
            attempt_id="attempt:1",
            send_outbox_id="outbox:send:1",
            acknowledged_at=NOW + timedelta(seconds=2),
            sent=True,
            action=legacy.candidate.action,
            context_provider=lambda: {},
        )
    )
    assert repository.recorded[-1][1] is True


def test_exploration_never_overrides_a_hard_boundary():
    from companion_runtime.legacy_bridge_v2 import BoundaryVerdictV2

    runtime, legacy, _repository = build(spent=0, hazard_base=1.0)
    legacy.boundary_verdict = lambda *, candidate, scope_key, now: BoundaryVerdictV2(
        blocked=True, reasons=("user_said_stop",)
    )
    decision = runtime.decide_endogenous(
        decision_id="decision:boundary", now=NOW, elapsed_allowed_seconds=600.0
    )
    assert decision.acted is False
    assert decision.reason == "no_eligible_candidate"


def test_exploration_never_authorises_an_unsafe_candidate():
    from companion_runtime.motivation_v2 import CandidatePolicyV2

    runtime, legacy, _repository = build(spent=0, hazard_base=1.0)
    legacy.candidate = legacy.candidate.__class__(
        **{
            **{
                field: getattr(legacy.candidate, field)
                for field in (
                    "candidate_id", "action", "internal_utility", "coefficients",
                    "repeat_subject", "source_event_ids",
                )
            },
            "policy": CandidatePolicyV2(
                low_pressure=False, low_frequency=True, easy_to_ignore=False
            ),
        }
    )
    decision = runtime.decide_endogenous(
        decision_id="decision:unsafe", now=NOW, elapsed_allowed_seconds=600.0
    )
    assert decision.acted is False
    assert decision.reason == "no_eligible_candidate"
    assert "limited_support_high_or_unspecified_pressure_blocked" in decision.assessments[0].reasons


def test_zero_limit_disables_cold_start_exploration():
    runtime, _legacy, _repository = build(spent=0, limit=0)
    decision = runtime.decide_endogenous(
        decision_id="decision:off", now=NOW, elapsed_allowed_seconds=600.0
    )
    assert decision.acted is False
    assert decision.reason == "no_eligible_candidate"


def test_missing_counter_degrades_to_unlimited_not_to_silence():
    """A test double without the counter still explores once; production always has it."""

    order: list[str] = []
    legacy = FakeLegacyBridge(order)
    repository = FakePostgresRepository(order)
    repository.predictions = cold_predictions()
    runtime = V2RuntimeCoordinator(
        scope_key=SCOPE,
        legacy=legacy,
        user_model=UserModelV2Service(repository),
        repository=repository,
        config=DecisionConfigV2(hazard_base=1.0, hazard_beta=4.0),
        rng=random.Random(0),
    )
    assert runtime._exploration_available(now=NOW) is True
    decision = runtime.decide_endogenous(
        decision_id="decision:legacy-fake", now=NOW, elapsed_allowed_seconds=600.0
    )
    assert decision.acted is True


def test_daily_limit_caps_exploration_spending():
    """Three acknowledged explorations exhaust the day; two do not."""

    runtime, _legacy, _repository = build(spent=2, limit=3)
    assert runtime._exploration_available(now=NOW) is True
    assert runtime.decide_endogenous(
        decision_id="decision:two-spent", now=NOW, elapsed_allowed_seconds=600.0
    ).acted is True

    runtime, _legacy, _repository = build(spent=3, limit=3)
    assert runtime._exploration_available(now=NOW) is False
    blocked = runtime.decide_endogenous(
        decision_id="decision:three-spent", now=NOW, elapsed_allowed_seconds=600.0
    )
    assert blocked.acted is False
    assert blocked.reason == "no_eligible_candidate"


def test_spacing_stops_three_per_day_becoming_three_in_a_row():
    """The daily cap alone would let the allowance spend as a burst."""

    spacing = timedelta(hours=8)
    runtime, _legacy, _repository = build(
        spent=1, limit=3, spacing=spacing, last_spent_at=NOW - timedelta(hours=1)
    )
    assert runtime._exploration_available(now=NOW) is False
    assert runtime.decide_endogenous(
        decision_id="decision:too-soon", now=NOW, elapsed_allowed_seconds=600.0
    ).acted is False

    runtime, _legacy, _repository = build(
        spent=1, limit=3, spacing=spacing, last_spent_at=NOW - timedelta(hours=9)
    )
    assert runtime._exploration_available(now=NOW) is True
    assert runtime.decide_endogenous(
        decision_id="decision:spaced", now=NOW, elapsed_allowed_seconds=600.0
    ).acted is True


def test_production_exploration_policy_is_three_per_day_eight_hours_apart():
    config = DecisionConfigV2()
    assert config.cold_start_exploration_limit == 3
    assert config.cold_start_exploration_window == timedelta(hours=24)
    assert config.cold_start_exploration_min_spacing == timedelta(hours=8)
    assert config.cold_start_exploration_advantage == 2.0
    # softplus(4 * 2.0) * 3e-5, i.e. materially above the calibrated floor.
    expected_lambda = 0.000030 * math.log(1.0 + math.exp(4.0 * 2.0))
    assert expected_lambda > 2.0e-4


def test_exploration_stops_once_a_head_becomes_informative():
    """The allowance is a cold-start bridge, not a permanent second policy."""

    runtime, _legacy, repository = build(spent=0, limit=3)
    repository.predictions = PredictionSetV2(
        snapshot_id="prediction:snapshot:warm",
        parameter_version="parameters:1",
        reply=TargetPredictionV2(
            prediction_id="p:reply", scope_key=SCOPE, target=Target.REPLY,
            point=0.8, lower=0.7, upper=0.9, interval_level=0.9,
            interval_kind="laplace", support=SupportStatus.INFORMATIVE,
            predicted_at=NOW, created_at=NOW, updated_at=NOW,
        ),
        continuation=TargetPredictionV2(
            prediction_id="p:continue", scope_key=SCOPE, target=Target.CONTINUE,
            point=0.7, lower=0.6, upper=0.8, interval_level=0.9,
            interval_kind="laplace", support=SupportStatus.INFORMATIVE,
            predicted_at=NOW, created_at=NOW, updated_at=NOW,
        ),
        negative=TargetPredictionV2(
            prediction_id="p:negative", scope_key=SCOPE, target=Target.NEGATIVE,
            point=0.05, lower=0.01, upper=0.1, interval_level=0.9,
            interval_kind="laplace", support=SupportStatus.INFORMATIVE,
            predicted_at=NOW, created_at=NOW, updated_at=NOW,
        ),
    )
    decision = runtime.decide_endogenous(
        decision_id="decision:warm", now=NOW, elapsed_allowed_seconds=600.0
    )
    eligible = [
        row for row in repository.audits["decision:warm"]["events"]
        if row["stage"] == "candidate_eligible"
    ]
    assert eligible[-1]["details"]["cold_start_exploration"] is False
