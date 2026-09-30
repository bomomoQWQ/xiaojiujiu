"""MOT-01..09 pure-function coverage for the isolated decision-v2 consumer."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from companion_runtime.motivation_v2 import (
    CandidatePolicyV2,
    UserUtilityCoefficientsV2,
    user_utility,
)
from companion_runtime.user_model_v2_types import SupportStatus, Target, TargetPredictionV2

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SAFE = CandidatePolicyV2(low_pressure=True, low_frequency=True, easy_to_ignore=True)
COEFFICIENTS = UserUtilityCoefficientsV2(
    v_reply=1.2,
    v_continue=0.8,
    c_negative=2.0,
)


def prediction(
    target: Target,
    *,
    point: float,
    lower: float,
    upper: float,
    support: SupportStatus = SupportStatus.INFORMATIVE,
) -> TargetPredictionV2:
    return TargetPredictionV2(
        prediction_id=f"pred_{target.value}",
        scope_key="user:42/channel:direct",
        target=target,
        point=point,
        lower=lower,
        upper=upper,
        interval_level=0.9,
        interval_kind="credible",
        support=support,
        predicted_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def inputs(
    *,
    reply: TargetPredictionV2 | None = None,
    continuation: TargetPredictionV2 | None = None,
    negative: TargetPredictionV2 | None = None,
    coefficients: UserUtilityCoefficientsV2 = COEFFICIENTS,
    candidate: CandidatePolicyV2 = SAFE,
) -> dict[str, object]:
    return {
        "reply": reply
        or prediction(Target.REPLY, point=0.6, lower=0.5, upper=0.7),
        "continuation": continuation
        or prediction(Target.CONTINUE, point=0.4, lower=0.3, upper=0.5),
        "negative": negative
        or prediction(Target.NEGATIVE, point=0.15, lower=0.1, upper=0.2),
        "coefficients": coefficients,
        "candidate": candidate,
    }


def test_mot_01_uses_independent_observable_heads_not_complement_bad_semantics() -> None:
    result = user_utility(
        **inputs(
            continuation=prediction(Target.CONTINUE, point=0.2, lower=0.2, upper=0.2),
            negative=prediction(Target.NEGATIVE, point=0.0, lower=0.0, upper=0.0),
        )
    )

    assert result.decomposition.negative_cost == 0.0
    assert result.used_bounds.p_continue_given_reply_lower == 0.2
    assert result.utility == pytest.approx(0.5 * 1.2 + 0.5 * 0.2 * 0.8)
    assert "bad_probability" not in result.to_dict()


def test_mot_02_uncertainty_never_discounts_negative_cost() -> None:
    narrow = user_utility(
        **inputs(negative=prediction(Target.NEGATIVE, point=0.3, lower=0.3, upper=0.3))
    )
    uncertain = user_utility(
        **inputs(negative=prediction(Target.NEGATIVE, point=0.3, lower=0.0, upper=0.9))
    )

    assert narrow.decomposition.negative_cost == pytest.approx(0.6)
    assert uncertain.decomposition.negative_cost == pytest.approx(1.8)
    assert uncertain.utility < narrow.utility


def test_mot_03_wider_conservative_intervals_cannot_increase_utility() -> None:
    narrow = user_utility(**inputs())
    wider = user_utility(
        **inputs(
            reply=prediction(Target.REPLY, point=0.6, lower=0.35, upper=0.85),
            continuation=prediction(Target.CONTINUE, point=0.4, lower=0.1, upper=0.7),
            negative=prediction(Target.NEGATIVE, point=0.15, lower=0.0, upper=0.4),
        )
    )

    assert wider.utility <= narrow.utility


def test_mot_04_one_shot_notification_can_set_continuation_value_to_zero() -> None:
    one_shot = UserUtilityCoefficientsV2(v_reply=1.0, v_continue=0.0, c_negative=1.0)
    low = user_utility(
        **inputs(
            coefficients=one_shot,
            continuation=prediction(Target.CONTINUE, point=0.0, lower=0.0, upper=0.0),
        )
    )
    high = user_utility(
        **inputs(
            coefficients=one_shot,
            continuation=prediction(Target.CONTINUE, point=1.0, lower=1.0, upper=1.0),
        )
    )

    assert low.utility == high.utility
    assert low.decomposition.continuation_benefit == 0.0
    assert low.decomposition.negative_cost >= 0.0


def test_mot_05_unconditional_negative_probability_is_not_multiplied_by_reply() -> None:
    result = user_utility(
        **inputs(
            reply=prediction(Target.REPLY, point=0.1, lower=0.1, upper=0.1),
            negative=prediction(Target.NEGATIVE, point=0.5, lower=0.5, upper=0.5),
        )
    )

    assert result.decomposition.negative_cost == pytest.approx(2.0 * 0.5)
    assert result.decomposition.negative_cost != pytest.approx(2.0 * 0.5 * 0.1)


def test_mot_06_acceptance_path_is_disabled_and_misuse_is_rejected() -> None:
    acceptance = prediction(Target.ACCEPTANCE, point=0.9, lower=0.8, upper=1.0)

    with pytest.raises(ValueError, match="acceptance consumption is disabled"):
        user_utility(**inputs(), acceptance=acceptance)


def test_mot_07_hard_boundary_cannot_be_overridden_by_arbitrary_benefit() -> None:
    huge = UserUtilityCoefficientsV2(v_reply=1e12, v_continue=1e12, c_negative=0.0)
    result = user_utility(
        **inputs(coefficients=huge),
        boundary_blocked=True,
        boundary_reasons=("user_forbids_proactive_contact",),
    )

    assert result.utility > 0.0
    assert result.blocked is True
    assert result.eligible is False
    assert "hard_boundary_blocked" in result.reasons
    assert "boundary:user_forbids_proactive_contact" in result.reasons


@pytest.mark.parametrize("support", [SupportStatus.PRIOR_ONLY, SupportStatus.SPARSE])
def test_mot_08_limited_support_blocks_pressure_followups_and_sensitive_candidates(
    support: SupportStatus,
) -> None:
    cold_reply = replace(inputs()["reply"], support=support)  # type: ignore[arg-type]
    risky = CandidatePolicyV2(
        low_pressure=False,
        low_frequency=True,
        easy_to_ignore=True,
        continuous_follow_up=True,
        sensitive=True,
    )
    result = user_utility(**inputs(reply=cold_reply, candidate=risky))

    assert result.blocked is True
    assert "limited_support_continuous_follow_up_blocked" in result.reasons
    assert "limited_support_sensitive_candidate_blocked" in result.reasons
    assert "limited_support_high_or_unspecified_pressure_blocked" in result.reasons


@pytest.mark.parametrize("support", [SupportStatus.PRIOR_ONLY, SupportStatus.SPARSE])
def test_mot_09_limited_support_explicitly_allows_safe_ignorable_candidate(
    support: SupportStatus,
) -> None:
    result = user_utility(
        **inputs(
            reply=replace(inputs()["reply"], support=support),  # type: ignore[arg-type]
            candidate=SAFE,
        )
    )

    assert result.blocked is False
    assert result.eligible is True
    assert "limited_support_safe_exploration_allowed" in result.reasons
    rendered = result.to_dict()
    assert rendered["decomposition"] == {
        "reply_benefit": result.decomposition.reply_benefit,
        "continuation_benefit": result.decomposition.continuation_benefit,
        "negative_cost": result.decomposition.negative_cost,
        "total": result.utility,
    }
    assert rendered["used_bounds"] == {
        "p_reply_lower": 0.5,
        "p_continue_given_reply_lower": 0.3,
        "p_negative_upper": 0.2,
    }
    assert rendered["reasons"]
