"""T23/T25: finite exploration has honest evidence and bounded process value."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion_runtime.langchao_exploration import (
    EXPLORATION_PROCESS_CAP,
    EXPLORATION_PROCESS_VALUE,
    ExplorationResultKind,
    ExplorationWorkSegment,
    claim_exploration_process,
)
from companion_runtime.langchao_live import LangchaoLiveService
from companion_runtime.langchao_runtime_adapter import RuntimeCandidateFacts
from companion_runtime.langchao_types import CandidateKind, GoalKind, GoalOwnership, MotivationDirection

from test_langchao_runtime_adapter import build, facts, source


def segment(
    *,
    segment_id: str = "segment:compare-two-parsers",
    kind: ExplorationResultKind = ExplorationResultKind.ARTIFACT,
    result_ref: str = "artifact:comparison.json",
) -> ExplorationWorkSegment:
    return ExplorationWorkSegment(
        segment_id=segment_id,
        problem_ref="issue:parser-disagreement",
        question="Which parser preserves the source offsets?",
        executable_steps=("run parser A on fixture 7", "run parser B on fixture 7", "compare offsets"),
        result_kind=kind,
        result_ref=result_ref,
        evidence_refs=("issue:parser-disagreement", "fixture:7", result_ref),
    )


def exploration_facts(item: ExplorationWorkSegment) -> RuntimeCandidateFacts:
    return RuntimeCandidateFacts(
        candidate_id="explore",
        template_key="exploration.v1",
        ownership=GoalOwnership.SELF_INTEREST,
        evidence_refs=(item.problem_ref, item.result_ref),
        exploration_segment=item,
    )


def test_positive_artifact_segment_builds_internal_open_activity_and_reward() -> None:
    item = segment()
    built = build(
        (source("explore", with_predictions=False),),
        (exploration_facts(item),),
    ).contracts[0]

    assert built.goal.kind is GoalKind.OPEN_ACTIVITY
    assert built.candidate.kind is CandidateKind.INTERNAL_PROCESS
    assert built.candidate.action_template == "exploration.v1"
    assert built.reward.total_cap == EXPLORATION_PROCESS_CAP
    assert [token.outcome_key for token in built.reward.outcome_tokens] == ["work_segment_completed"]
    token = built.reward.outcome_tokens[0]
    assert token.base_amount == EXPLORATION_PROCESS_VALUE
    assert token.direction_weights == ((MotivationDirection.EXPLORATION, 1.0),)
    assert item.result_ref in token.evidence_refs
    assert built.shadow_input.template_probability_policy == ((token.token_id, 1.0),)
    assert dict(built.candidate.envelope)["records_progress"] is True


def test_unknown_or_missing_witness_is_rejected_instead_of_rewarded() -> None:
    with pytest.raises(ValueError, match="result_ref must be included"):
        ExplorationWorkSegment(
            segment_id="segment:bad",
            problem_ref="issue:real",
            question="What happens?",
            executable_steps=("run bounded check",),
            result_kind=ExplorationResultKind.CAPABILITY,
            result_ref="capability:unwitnessed",
            evidence_refs=("issue:real",),
        )
    with pytest.raises(ValueError, match="requires exploration_segment"):
        facts("explore", "exploration.v1")
    with pytest.raises(ValueError, match="must not be empty"):
        replace(segment(), executable_steps=())


def test_no_result_is_an_honest_record_not_fake_progress() -> None:
    item = segment(
        kind=ExplorationResultKind.NO_CONCLUSION,
        result_ref="record:no-conclusion:fixture-7",
    )
    built = build(
        (source("explore", with_predictions=False),),
        (exploration_facts(item),),
    ).contracts[0]

    assert item.records_progress is False
    envelope = dict(built.candidate.envelope)
    assert envelope["result_kind"] == "no_conclusion"
    assert envelope["records_progress"] is False
    # Honest completion of the finite segment may receive the same fixed process
    # allowance, but no capability/artifact or discovery outcome is minted.
    assert {token.outcome_key for token in built.reward.outcome_tokens} == {"work_segment_completed"}
    assert all("progress" not in token.outcome_key and "discovery" not in token.outcome_key
               for token in built.reward.outcome_tokens)


def test_process_cap_is_idempotent_and_cannot_grow_with_replays_or_more_segments() -> None:
    first = segment(segment_id="segment:1")
    second = segment(segment_id="segment:2", kind=ExplorationResultKind.NO_CONCLUSION,
                     result_ref="record:no-conclusion:2")
    claim = claim_exploration_process((first, first, second))
    assert claim.admitted_segment_ids == ("segment:1", "segment:2")
    assert claim.duplicate_segment_ids == ("segment:1",)
    assert claim.amount == claim.cap == EXPLORATION_PROCESS_CAP

    replay = claim_exploration_process((first, second), already_settled_segment_ids=("segment:1", "segment:2"))
    assert replay.admitted_segment_ids == ()
    assert replay.amount == 0.0


def test_live_selected_exploration_stays_internal_and_never_calls_send_bridge() -> None:
    round_ = build(
        (source("explore", with_predictions=False),),
        (exploration_facts(segment()),),
    )
    selected = round_.contracts[0]

    class NeverSend:
        def __getattr__(self, name):
            raise AssertionError(f"live bridge must not be called for internal exploration: {name}")

    service = LangchaoLiveService(
        scope_key="scope",
        authority_reader=SimpleNamespace(
            get_active=lambda: {"engine_key": "langchao", "mode": "live", "may_dispatch": True, "revision": 1}
        ),
        legacy_bridge=NeverSend(),
    )
    result = service.execute_in_transaction(
        object(),
        built=round_,
        decision_candidate_id=selected.candidate.candidate_id,
        assessed_candidates={},
        now=selected.candidate.available_from,
    )
    assert result.committed is False
    assert result.reason == "internal_candidate"
