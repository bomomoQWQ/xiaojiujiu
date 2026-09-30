"""Unavailable prediction heads fail closed without crashing decision v2."""
from datetime import datetime, timezone
from companion_runtime.motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2, user_utility
from companion_runtime.user_model_v2_types import Target, TargetPredictionV2, SupportStatus

NOW=datetime(2026,9,30,tzinfo=timezone.utc)
def unavailable(target):
    return TargetPredictionV2(prediction_id='p-'+target.value,scope_key='s',target=target,
        point=None,lower=None,upper=None,interval_level=None,interval_kind=None,
        support=SupportStatus.UNAVAILABLE,predicted_at=NOW,created_at=NOW,updated_at=NOW)

def test_unavailable_heads_use_zero_benefit_full_negative_bound_and_cold_gate():
    result=user_utility(reply=unavailable(Target.REPLY),continuation=unavailable(Target.CONTINUE),
        negative=unavailable(Target.NEGATIVE),coefficients=UserUtilityCoefficientsV2(v_reply=1,v_continue=1,c_negative=1),
        candidate=CandidatePolicyV2(low_pressure=True,low_frequency=True,easy_to_ignore=True))
    assert result.used_bounds.p_reply_lower==0
    assert result.used_bounds.p_continue_given_reply_lower==0
    assert result.used_bounds.p_negative_upper==1
    assert result.utility==-1
    assert result.eligible is True
    assert any(reason.startswith('limited_support:') for reason in result.reasons)
