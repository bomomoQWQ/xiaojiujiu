from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from companion_runtime.decision_v2_audit import CandidateAssessment
from companion_runtime.langchao_permission import PermissionProjection, PermissionVerdict
from companion_runtime.langchao_runtime_adapter import (
    FIXED_VALUE_TOTAL,
    RuntimeCandidateFacts,
    RuntimeCandidateInput,
    RuntimeFactSnapshot,
    build_shadow_round,
    value_profile_from_runtime,
)
from companion_runtime.langchao_types import GoalOwnership, MotivationDirection
from companion_runtime.motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
from companion_runtime.repeat_v2 import RepeatSubjectV2
from companion_runtime.runtime_v2 import CandidateV2, PredictionSetV2
from companion_runtime.user_model_v2_types import SupportStatus, Target, TargetPredictionV2

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
VALUES = {
    "autonomy": .72, "boundary_respect": .88, "emotional_expression": .46,
    "relationship_maintenance": .79, "user_care": .85, "conflict_directness": .41,
    "stability_commitment": .81, "curiosity": .76,
}


def prediction(target: Target, value: float = .5, *, support=SupportStatus.INFORMATIVE,
               prediction_id: str | None = None) -> TargetPredictionV2:
    available = support is not SupportStatus.UNAVAILABLE
    return TargetPredictionV2(prediction_id=prediction_id or f"p-{target.value}", scope_key="scope",
        target=target, point=value if available else None, lower=max(0, value-.1) if available else None,
        upper=min(1, value+.1) if available else None, interval_level=.9 if available else None,
        interval_kind="credible" if available else None, support=support, predicted_at=NOW,
        created_at=NOW, updated_at=NOW)


def source(candidate_id: str, *, internal=999.0, unavailable: Target | None = None,
           snapshot_id="prediction-snapshot", with_predictions=True,
           coefficients=(2, 3, 4)) -> RuntimeCandidateInput:
    candidate = CandidateV2(candidate_id=candidate_id, action={"type": "test"}, internal_utility=internal,
        coefficients=UserUtilityCoefficientsV2(v_reply=coefficients[0], v_continue=coefficients[1], c_negative=coefficients[2]),
        repeat_subject=RepeatSubjectV2(), policy=CandidatePolicyV2(), source_event_ids=("evt",))
    predictions = PredictionSetV2(snapshot_id=snapshot_id, parameter_version="model-v1",
        reply=prediction(Target.REPLY, .6, support=SupportStatus.UNAVAILABLE if unavailable is Target.REPLY else SupportStatus.INFORMATIVE),
        continuation=prediction(Target.CONTINUE, .5, support=SupportStatus.UNAVAILABLE if unavailable is Target.CONTINUE else SupportStatus.INFORMATIVE),
        negative=prediction(Target.NEGATIVE, .2, support=SupportStatus.UNAVAILABLE if unavailable is Target.NEGATIVE else SupportStatus.INFORMATIVE))
    assessment = CandidateAssessment(candidate_id=candidate_id, prediction_snapshot_id=snapshot_id,
        used_bounds={"acceptance": .99}, utility_terms={"legacy_total": internal}, repeat_key="repeat", reasons=())
    return RuntimeCandidateInput(candidate=candidate, predictions=predictions if with_predictions else None,
                                 assessment=assessment)


def facts(candidate_id: str, template="contact.v1", **kwargs) -> RuntimeCandidateFacts:
    base = dict(candidate_id=candidate_id, template_key=template, ownership=GoalOwnership.SELF_WISH,
                evidence_refs=("fact",))
    if template == "contact.v1": base["subject_ref"] = "relationship-continuity"
    elif template == "expression.v1": base["memory_ref"] = "memory:m1"
    elif template == "followup.v1": base["unfinished_id"] = "unfinished:u1"
    else:
        base["self_regulation_ref"] = "self:fatigue"
        base["rest_realized_policy"] = True
    base.update(kwargs)
    return RuntimeCandidateFacts(**base)


def build(items, fact_items=None, **kwargs):
    fact_items = fact_items or tuple(facts(item.candidate.candidate_id) for item in items)
    snapshot = RuntimeFactSnapshot(scope_key="scope", episode_id="episode", source_cursor="cursor",
        values=VALUES, candidates=tuple(fact_items))
    return build_shadow_round(snapshot=snapshot, inputs=tuple(items), advanced_at=NOW, **kwargs)


@pytest.mark.parametrize("template", ["contact.v1", "expression.v1", "followup.v1", "internal_rest.v1"])
def test_fixed_templates_build_and_enforce_subject_facts(template):
    result = build((source("c"),), (facts("c", template),))
    assert result.contracts[0].candidate.action_template == template
    assert result.contracts[0].goal.ownership is GoalOwnership.SELF_WISH


def test_template_validation_and_ownership_are_not_inferred():
    with pytest.raises(ValueError):
        facts("c", "contact.v1", subject_ref="user-wants-contact")
    with pytest.raises(ValueError):
        RuntimeCandidateFacts(candidate_id="c", template_key="expression.v1",
                              ownership=GoalOwnership.SELF_WISH, evidence_refs=())
    with pytest.raises(TypeError):
        facts("c", ownership="user_request")


def test_different_legacy_ids_with_same_semantics_share_stable_ids_and_one_slot():
    first = build((source("legacy-a"),), (facts("legacy-a"),))
    second = build((source("legacy-b"),), (facts("legacy-b"),))
    a, b = first.contracts[0], second.contracts[0]
    assert (a.goal.goal_id, a.reward.reward_contract_id, a.candidate.candidate_id) == (
        b.goal.goal_id, b.reward.reward_contract_id, b.candidate.candidate_id)
    merged = build((source("legacy-b"), source("legacy-a")),
                   (facts("legacy-b"), facts("legacy-a")))
    assert len(merged.contracts) == len(merged.state.working_set) == 1
    assert merged.contracts[0].source_candidate_id == "legacy-a"
    assert merged.dropped[0].reasons == ("duplicate_semantic_candidate",)
    assert a.candidate.to_dict() == b.candidate.to_dict()
    assert a.provenance.candidate_id != b.provenance.candidate_id
    assert dict(a.candidate.envelope) == {"subject_ref": "relationship-continuity"}


def test_revisions_are_explicit_and_independent_of_state_version():
    revisions = dict(initial_goal_revision=3, reward_revision=6,
                     bound_goal_revision=8, candidate_revision=4)
    first = build((source("c"),), (facts("c", **revisions),), based_on_state_version=0)
    later = build((source("c"),), (facts("c", **revisions),), based_on_state_version=7)
    assert first.contracts[0].initial_goal.revision == 3
    assert first.contracts[0].reward.revision == 6
    assert first.contracts[0].goal.revision == 8
    assert first.contracts[0].candidate.semantic_revision == 4
    assert later.contracts[0].candidate.semantic_revision == 4
    assert later.contracts[0].candidate.based_on_state_version == 7
    changed = revisions | {"candidate_revision": 5}
    advanced = build((source("c"),), (facts("c", **changed),), based_on_state_version=7)
    assert advanced.contracts[0].candidate.semantic_revision == 5


def test_default_sequence_is_goal1_reward1_goal2_candidate1():
    built = build((source("c"),)).contracts[0]
    assert (built.initial_goal.revision, built.reward.revision,
            built.goal.revision, built.candidate.semantic_revision) == (1, 1, 2, 1)


def test_revision_resolver_values_must_be_positive_and_bound_goal_later():
    with pytest.raises(ValueError):
        facts("c", initial_goal_revision=0)
    with pytest.raises(ValueError):
        facts("c", candidate_revision=True)
    with pytest.raises(ValueError):
        facts("c", initial_goal_revision=3, bound_goal_revision=3)


def test_identity_ignores_time_decision_and_prediction_identity():
    first = build((source("c", snapshot_id="pred-A"),))
    later = datetime(2026, 2, 2, tzinfo=timezone.utc)
    snap = RuntimeFactSnapshot(scope_key="scope", episode_id="episode", source_cursor="cursor",
        values=VALUES, candidates=(facts("c"),))
    second = build_shadow_round(snapshot=snap, inputs=(source("c", snapshot_id="pred-B"),), advanced_at=later)
    a, b = first.contracts[0], second.contracts[0]
    assert (a.goal.goal_id, a.reward.reward_contract_id, a.candidate.candidate_id) == (
        b.goal.goal_id, b.reward.reward_contract_id, b.candidate.candidate_id)


def test_prediction_mapping_unknown_negative_and_acceptance_unused():
    result = build((source("c"),))
    forecasts = {t.outcome_key: f for t, f in zip(result.contracts[0].reward.outcome_tokens,
                                                  result.contracts[0].shadow_input.forecasts)}
    assert forecasts["reply"].probability == pytest.approx(.5)
    assert forecasts["continuation"].probability == pytest.approx(.5 * .4)
    assert forecasts["negative"].probability == pytest.approx(.3)
    negative = next(t for t in result.contracts[0].reward.outcome_tokens if t.outcome_key == "negative")
    assert negative.base_amount == -1.5
    assert all("accept" not in token.outcome_key for token in result.contracts[0].reward.outcome_tokens)
    unknown = build((source("c", unavailable=Target.REPLY),)).contracts[0].shadow_input.forecasts
    assert {x.probability for x in unknown if x.token_id} >= {None}
    assert unknown[0].probability is None and unknown[1].probability is None


def test_internal_legacy_fields_only_enter_provenance_and_do_not_change_contracts():
    low = build((source("c", internal=1, coefficients=(1, 2, 3)),),
                (facts("c", legacy_internal_need=2, legacy_relevance=3),))
    high = build((source("c", internal=999, coefficients=(9, 8, 7)),),
                 (facts("c", legacy_internal_need=8, legacy_relevance=9),))
    assert low.contracts[0].reward == high.contracts[0].reward
    assert low.contracts[0].shadow_input.forecasts == high.contracts[0].shadow_input.forecasts
    assert low.contracts[0].shadow_input.costs == high.contracts[0].shadow_input.costs
    assert low.contracts[0].provenance != high.contracts[0].provenance


def test_repeat_soft_cost_explicit_and_blocked_or_hard_repeat_dropped():
    result = build((source("soft"), source("blocked"), source("hard")), (
        facts("soft", repeat_soft_cost=.75, repeat_cost_refs=("repeat:a",)),
        facts("blocked", blocked=True), facts("hard", hard_repeat=True)))
    assert result.contracts[0].shadow_input.costs[0].kind == "repeat_soft_cost"
    assert {item.source_candidate_id for item in result.dropped} == {"blocked", "hard"}
    assert len(result.state.working_set) == 1


def test_internal_rest_has_only_rest_outcome_and_needs_no_user_predictions():
    rest = build((source("r", with_predictions=False),),
                 (facts("r", "internal_rest.v1"),)).contracts[0]
    assert [token.outcome_key for token in rest.reward.outcome_tokens] == ["rest_realized"]
    assert rest.shadow_input.template_probability_policy[0][1] == 1.0


def test_local_deterministic_outcomes_require_explicit_policy():
    no = build((source("e"),), (facts("e", "expression.v1"),)).contracts[0]
    yes = build((source("e"),), (facts("e", "expression.v1", expression_delivered_policy=True),)).contracts[0]
    assert "expression_delivered" not in {t.outcome_key for t in no.reward.outcome_tokens}
    assert yes.shadow_input.template_probability_policy


def test_value_profile_fixed_total_all_one_attention_and_zero_definition():
    profile = value_profile_from_runtime(VALUES)
    assert sum(dict(profile.direction_weights).values()) == pytest.approx(FIXED_VALUE_TOTAL)
    zero = value_profile_from_runtime({key: 0 for key in VALUES})
    assert set(dict(zero.direction_weights).values()) == {1.0}
    result = build((source("c"),))
    assert set(dict(result.attention_profile.direction_weights).values()) == {1.0}


def test_reconcile_same_set_recovers_and_changed_set_intersects_readiness():
    initial_facts = (facts("a"), facts("b", "expression.v1"))
    first = build((source("a"), source("b")), initial_facts)
    old = replace(first.state, readiness=tuple((key, value) for key, value in zip(first.state.working_set, (.2, .8))))
    same = build((source("b"), source("a")), previous_state=old,
                 fact_items=(facts("b", "expression.v1"), facts("a")))
    assert same.state is not old and same.state.decision_round_id == old.decision_round_id
    assert same.state.readiness == old.readiness and same.state.advanced_at == old.advanced_at
    original_ids = {m.source_candidate_id: m.langchao_candidate_id for m in first.id_mapping}
    expected_b = dict(old.readiness)[original_ids["b"]]
    changed = build((source("b"), source("c")), previous_state=old,
                    fact_items=(facts("b", "expression.v1"), facts("c", "followup.v1")))
    by_source = {m.source_candidate_id: m.langchao_candidate_id for m in changed.id_mapping}
    readiness = dict(changed.state.readiness)
    assert readiness[by_source["b"]] == expected_b and readiness[by_source["c"]] == 0
    assert set(readiness) == {by_source["b"], by_source["c"]}
    assert changed.state.advanced_at == NOW


def test_permission_version_change_forces_fresh_round_without_backlog_readiness():
    first = build((source("a"),), permission_version="permission:allow:v1")
    old = replace(first.state, readiness=((first.state.working_set[0], 0.9),))
    revoked = build(
        (source("a"),), previous_state=old,
        permission_version="permission:revoked:v2",
    )
    assert revoked.state.decision_round_id != old.decision_round_id
    assert dict(revoked.state.readiness) == {old.working_set[0]: 0.0}
    assert revoked.state.advanced_at == NOW


def test_exact_permission_projection_binds_external_candidate_and_denial_keeps_internal_rest():
    allowed = PermissionProjection(scope_key="scope", verdict=PermissionVerdict.ALLOW,
        permission_version="permission:allow:exact", event_id="allow", occurred_at=NOW,
        ingested_at=NOW, revision=1)
    snapshot = RuntimeFactSnapshot(scope_key="scope", episode_id="episode", source_cursor="cursor",
        values=VALUES, candidates=(facts("a"), facts("rest", "internal_rest.v1")),
        permission=allowed)
    built = build_shadow_round(snapshot=snapshot,
        inputs=(source("a"), source("rest", with_predictions=False)), advanced_at=NOW)
    by_source = {item.source_candidate_id: item for item in built.contracts}
    assert built.state.permission_version == allowed.permission_version
    assert by_source["a"].candidate.permission_ref == allowed.permission_version
    assert by_source["rest"].candidate.permission_ref == "runtime.permission.v2"

    denied = replace(allowed, verdict=PermissionVerdict.DENY,
                     permission_version="permission:deny:exact", event_id="deny")
    denied_round = build_shadow_round(snapshot=replace(snapshot, permission=denied),
        inputs=(source("a"), source("rest", with_predictions=False)), advanced_at=NOW,
        previous_state=built.state)
    assert [item.source_candidate_id for item in denied_round.contracts] == ["rest"]
    assert dict(denied_round.state.readiness) == {denied_round.state.working_set[0]: 0.0}
    assert any(item.source_candidate_id == "a" and "permission_denied" in item.reasons
               for item in denied_round.dropped)


def test_round_hashes_are_order_independent_and_cursor_sensitive():
    a = build((source("a"), source("b")), (facts("a"), facts("b", "expression.v1")))
    b = build((source("b"), source("a")), fact_items=(facts("b", "expression.v1"), facts("a")))
    assert (a.candidate_snapshot_version, a.prediction_snapshot_version, a.state.decision_round_id) == (
        b.candidate_snapshot_version, b.prediction_snapshot_version, b.state.decision_round_id)
    snap = RuntimeFactSnapshot(scope_key="scope", episode_id="episode", source_cursor="next",
        values=VALUES, candidates=(facts("a"), facts("b", "expression.v1")))
    c = build_shadow_round(snapshot=snap, inputs=(source("a"), source("b")), advanced_at=NOW)
    assert c.state.decision_round_id != a.state.decision_round_id


def test_adapter_source_contains_no_send_or_wave_api():
    import companion_runtime.langchao_runtime_adapter as module
    from pathlib import Path
    text = Path(module.__file__).read_text(encoding="utf-8")
    assert "send(" not in text and "Wave" not in text and "wave" not in text
