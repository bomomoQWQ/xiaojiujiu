"""The real Jev adapter is deferred; disabled/fake v2 ports stay safe and grounded."""

from __future__ import annotations

from companion_runtime.semantic_judge_v2 import (
    DisabledSemanticJudgeV2,
    FakeSemanticJudgeV2,
    JudgementProposalV2,
    JudgementRequestV2,
    JudgementStatus,
)


def request() -> JudgementRequestV2:
    return JudgementRequestV2(
        request_id="judge-1",
        question="Did the user explicitly accept this interaction style?",
        scope_key="scope:a",
        source_event_ids=("evt-1",),
        evidence_fragments=("谢谢你来问我",),
        contract_version="targets-v2",
        state_version=7,
    )


def test_disabled_is_unavailable_not_false_or_neutral() -> None:
    judge = DisabledSemanticJudgeV2()
    result = judge.judge(request())
    assert judge.available() is False
    assert result.status is JudgementStatus.UNAVAILABLE
    assert result.proposed_value is None
    assert result.source_event_ids == ()


def test_fake_unknown_is_not_a_label() -> None:
    judge = FakeSemanticJudgeV2()
    result = judge.judge(request())
    assert result.status is JudgementStatus.UNKNOWN
    assert result.proposed_value is None
    assert judge.calls == ["judge-1"]


def test_fake_proposal_must_stay_inside_allowed_evidence() -> None:
    valid = JudgementProposalV2(
        request_id="judge-1",
        status=JudgementStatus.PROPOSED,
        detector_version="fixture-1",
        source_event_ids=("evt-1",),
        proposed_value=True,
    )
    assert FakeSemanticJudgeV2((valid,)).judge(request()) == valid

    invented = JudgementProposalV2(
        request_id="judge-1",
        status=JudgementStatus.PROPOSED,
        detector_version="fixture-1",
        source_event_ids=("evt-invented",),
        proposed_value=True,
    )
    rejected = FakeSemanticJudgeV2((invented,)).judge(request())
    assert rejected.status is JudgementStatus.REJECTED
    assert rejected.proposed_value is None
