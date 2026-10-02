from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_runtime.capability_witness import (
    ArtifactStatus,
    ArtifactWitness,
    InMemoryWitnessRegistry,
    TaskStatus,
    WitnessValidationError,
)
from companion_runtime.config import RuntimeConfig
from companion_runtime.langchao_shadow_wiring import (
    DEFAULT_LANGCHAO_PARAMETERS,
    CandidateAdmission,
    LangchaoShadowRunner,
    _BorrowedConnection,
    _admit_candidate,
    _facts_for,
)
from companion_runtime.runtime_v2 import EndogenousDecisionV2
from test_langchao_runtime_adapter import source


def test_langchao_shadow_is_default_off_and_requires_explicit_switch():
    config = RuntimeConfig()
    assert config.langchao.shadow_enabled is False
    assert config.langchao.attention_recipe_for_scope("scope") == "off"
    config.langchao.shadow_enabled = True
    assert config.langchao.shadow_enabled is True


def test_attention_recipe_b3_requires_exact_scope_allowlist_and_invalid_fails_closed():
    config = RuntimeConfig()
    config.langchao.attention_recipe = "B3"
    assert config.langchao.attention_recipe_for_scope("scope") == "off"
    config.langchao.attention_b3_scope_allowlist.append("scope")
    assert config.langchao.attention_recipe_for_scope("scope") == "B3"
    config.langchao.attention_recipe = "unknown"
    import pytest
    with pytest.raises(ValueError, match="recipe"):
        config.langchao.attention_recipe_for_scope("scope")


HASH = "a" * 64
SCOPE = "scope"


def _exploration_action(*, result_kind="artifact", artifact_type="exploration_artifact"):
    refs = ("issue:parser", f"artifact:sha256:{HASH}")
    action = {
        "type": "internal_exploration", "internal": True, "segment_completed": True,
        "segment_id": "segment:parser", "problem_ref": "issue:parser",
        "question": "Which parser preserves offsets?",
        "executable_steps": ["run A", "run B", "compare"],
        "result_kind": result_kind, "result_ref": f"artifact:sha256:{HASH}",
        "evidence_refs": list(refs), "elapsed_seconds": 999999,
        "exploration_witness": {
            "capability": "bounded_exploration", "operation": "internal_exploration",
            "task_run_id": "task:parser", "artifact_sha256": HASH,
            "artifact_type": artifact_type, "source_refs": list(refs),
        },
    }
    return action


def _exploration_registry(*, artifact_type="exploration_artifact", scope=SCOPE):
    refs = ("issue:parser", f"artifact:sha256:{HASH}")
    return InMemoryWitnessRegistry(ArtifactWitness(
        witness_id="witness:parser", scope_key=scope,
        capability="bounded_exploration", operation="internal_exploration",
        task_run_id="task:parser", task_status=TaskStatus.SUCCEEDED,
        artifact_sha256=HASH, artifact_type=artifact_type,
        artifact_status=ArtifactStatus.ACTIVE,
        created_at=datetime(2026, 10, 2, tzinfo=timezone.utc), source_refs=refs,
    ))


def _exploration_decision(action):
    decision = _decision("explore", "internal_exploration")
    return replace(decision, candidate=replace(decision.candidate, action=action))


def test_runtime_fact_builder_emits_only_registry_verified_internal_exploration():
    decision = _exploration_decision(_exploration_action())
    built = _facts_for(
        decision, scope_key=SCOPE, witness_reader=_exploration_registry(),
    )
    assert built is not None and built.template_key == "exploration.v1"
    assert built.capability_refs == ()
    assert built.exploration_segment.segment_id == "segment:parser"
    assert not hasattr(built.exploration_segment, "elapsed_seconds")
    # A refusal is typed and auditable rather than a silent ``None``: the two cases that
    # must not become exploration contracts report *why* they were dropped.
    disabled = _admit_candidate(decision, internal_exploration_enabled=False)
    assert disabled.facts is None
    assert disabled.drop_reason == "exploration_disabled_or_incomplete"
    assert disabled.stage == "admission"
    vague = _admit_candidate(_decision("vague", "exploration_work_segment"))
    assert vague.facts is None
    assert vague.drop_reason == "exploration_disabled_or_incomplete"


def test_forged_exploration_action_fields_are_rejected_without_local_reward():
    decision = _exploration_decision(_exploration_action())
    with pytest.raises(WitnessValidationError, match="trusted witness reader"):
        _facts_for(decision, scope_key=SCOPE)

    forged = _exploration_action()
    forged["result_ref"] = "artifact:sha256:" + "b" * 64
    with pytest.raises(WitnessValidationError, match="result reference"):
        _facts_for(
            _exploration_decision(forged), scope_key=SCOPE,
            witness_reader=_exploration_registry(),
        )

    forged = _exploration_action()
    forged["evidence_refs"] = ["issue:parser", forged["result_ref"], "invented:evidence"]
    with pytest.raises(WitnessValidationError, match="evidence"):
        _facts_for(
            _exploration_decision(forged), scope_key=SCOPE,
            witness_reader=_exploration_registry(),
        )


def test_no_conclusion_requires_real_work_segment_record_witness():
    action = _exploration_action(
        result_kind="no_conclusion", artifact_type="exploration_work_segment",
    )
    decision = _exploration_decision(action)
    built = _facts_for(
        decision, scope_key=SCOPE,
        witness_reader=_exploration_registry(artifact_type="exploration_work_segment"),
    )
    assert built is not None
    assert built.exploration_segment.records_progress is False

    with pytest.raises(WitnessValidationError, match="artifact type|result kind"):
        _facts_for(
            decision, scope_key=SCOPE,
            witness_reader=_exploration_registry(artifact_type="exploration_artifact"),
        )


def _decision_with_action(candidate_id: str, action: dict):
    decision = _decision(candidate_id, str(action["type"]))
    return replace(decision, candidate=replace(decision.candidate, action=action))


def test_candidate_admission_is_explicit_for_every_formal_type():
    memory_question = _admit_candidate(_decision_with_action("q", {
        "type": "curious_question", "question": True, "topic_shift": True,
        "memory_ref": "memory:m1",
    }))
    assert memory_question.admitted
    assert memory_question.facts.template_key == "question.v1"
    assert memory_question.facts.memory_ref == "memory:m1"
    assert memory_question.facts.question is True
    assert memory_question.facts.topic_shift is True

    social_question = _admit_candidate(_decision_with_action("qs", {
        "type": "curious_question", "question": True, "topic_shift": True,
        "social_ref": "social:s1",
    }))
    assert social_question.facts.social_ref == "social:s1"

    assert _admit_candidate(_decision("contact", "contact")).facts.template_key == "contact.v1"
    assert _admit_candidate(_decision_with_action("share", {
        "type": "share", "memory_ref": "memory:m2",
    })).facts.template_key == "expression.v1"
    assert _admit_candidate(_decision_with_action("follow", {
        "type": "follow_up", "unfinished_id": "unfinished:u1",
    })).facts.template_key == "followup.v1"

    check_in_contact = _admit_candidate(_decision("check", "check_in"))
    assert check_in_contact.facts.template_key == "contact.v1"
    check_in_followup = _admit_candidate(_decision_with_action("check-u", {
        "type": "check_in", "unfinished_id": "unfinished:u2",
    }))
    assert check_in_followup.facts.template_key == "followup.v1"

    rest = _admit_candidate(_decision("rest", "internal_rest"))
    assert rest.admitted and rest.facts.template_key == "internal_rest.v1"

    for kind in ("repair", "apology", "confront", "reply"):
        dropped = _admit_candidate(_decision(kind, kind))
        assert dropped.facts is None
        assert dropped.drop_reason == "permission_semantics_unimplemented"
        assert dropped.stage == "admission"


def test_unreferenced_and_unknown_candidates_are_typed_drops_not_silent_none():
    expression = _admit_candidate(_decision("expression", "share"))
    assert expression.facts is None
    assert expression.drop_reason == "missing_memory_or_social_ref"
    assert expression.source_type == "share"

    followup = _admit_candidate(_decision("followup", "follow_up"))
    assert followup.facts is None
    assert followup.drop_reason == "missing_unfinished_ref"

    unknown = _admit_candidate(_decision("mystery", "totally_new_type"))
    assert unknown.facts is None
    assert unknown.drop_reason == "unsupported_candidate_type"

    with pytest.raises(ValueError, match="admission"):
        _facts_for(_decision("mystery", "totally_new_type"))


def test_candidate_ttl_is_preserved_by_question_contract():
    expires = datetime(2026, 10, 2, 6, tzinfo=timezone.utc)
    item = _decision_with_action("q-ttl", {
        "type": "curious_question", "question": True, "topic_shift": True,
        "memory_ref": "memory:m1", "expires_at": expires.isoformat(),
    })
    facts = _admit_candidate(item).facts
    assert facts.expires_at == expires


def test_contact_diagnostic_reasons_do_not_become_block_reasons_or_drop():
    item = _decision("contact", "contact")
    item = replace(
        item,
        reasons=(
            "limited_support:reply,continue,negative",
            "limited_support_safe_exploration_allowed",
        ),
    )
    admission = _admit_candidate(item)
    assert admission.admitted
    assert admission.facts.blocked is False
    assert admission.facts.block_reasons == ()
    assert admission.facts.diagnostic_reasons == item.reasons


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("decision_threshold", 0.70),
        ("leak", 0.05),
        ("time_scale_seconds", 3600.0),
        ("competition_gain", 0.0),
    ],
)
def test_default_langchao_parameters_match_audited_recommendation(name, expected):
    assert getattr(DEFAULT_LANGCHAO_PARAMETERS, name) == expected


def test_default_parameters_are_reachable_and_threshold_one_is_dead_for_bounded_utility():
    weak = .01639344262295082
    equilibrium = weak / (weak + DEFAULT_LANGCHAO_PARAMETERS.leak)
    assert equilibrium < DEFAULT_LANGCHAO_PARAMETERS.decision_threshold

    medium = .2
    equilibrium = medium / (medium + DEFAULT_LANGCHAO_PARAMETERS.leak)
    assert equilibrium > DEFAULT_LANGCHAO_PARAMETERS.decision_threshold

    impossible = replace(DEFAULT_LANGCHAO_PARAMETERS, decision_threshold=1.0)
    assert medium / (medium + impossible.leak) < impossible.decision_threshold


def test_enabled_runner_builds_contact_plus_synthetic_rest_and_state_cursor():
    captured = {}

    class Service:
        def run(self, built, **kwargs):
            captured["built"] = built
            captured["kwargs"] = kwargs
            return SimpleNamespace(sent_count=0, reward_count=0, training_count=0,
                                   quota_count=0, outbox_id=None)

    class Revisions:
        def resolve(self, kind, identity, dto):
            return (1 if kind != "goal" or dto.reward_contract_id is None else 2), None

    state = SimpleNamespace(version=7, values=RuntimeConfig().values)
    runtime = SimpleNamespace(
        state=lambda: state,
        projections=SimpleNamespace(boundaries=SimpleNamespace(list_all=lambda **_kwargs: [])),
    )
    runner = LangchaoShadowRunner(
        scope_key="scope", runtime=runtime, service=Service(),
        state_repository=SimpleNamespace(load_active_state=lambda: None), revisions=Revisions(),
    )
    item = _decision("contact", "contact")
    result = runner.run(EndogenousDecisionV2(decision_id="d1", acted=False, reason="defer",
                        assessments=(item,)), now=item.predictions.reply.predicted_at)
    assert result.sent_count == result.reward_count == result.training_count == result.quota_count == 0
    assert result.outbox_id is None
    built = captured["built"]
    assert {contract.candidate.action_template for contract in built.contracts} == {
        "contact.v1", "internal_rest.v1"
    }
    assert built.state.event_cursor == "runtime-version:7"
    assert built.state.parameter_version == "langchao.parameters.v1"
    assert set(dict(built.attention_profile.direction_weights).values()) == {1.0}
    assert captured["kwargs"]["edges"] == ()
    assert captured["kwargs"]["idempotency_key"] == "runtime-v2:d1"


def test_b3_runner_passes_explicit_attention_edges_and_audit_versions_to_service():
    captured = {}

    class Service:
        def run(self, built, **kwargs):
            captured["built"] = built
            captured["kwargs"] = kwargs
            return SimpleNamespace()

    class Revisions:
        def resolve(self, kind, identity, dto):
            return (1 if kind != "goal" or dto.reward_contract_id is None else 2), None

    state = SimpleNamespace(version=7, values=RuntimeConfig().values)
    runtime = SimpleNamespace(
        state=lambda: state,
        projections=SimpleNamespace(boundaries=SimpleNamespace(list_all=lambda **_kwargs: [])),
    )
    runner = LangchaoShadowRunner(
        scope_key="scope", runtime=runtime, service=Service(),
        state_repository=SimpleNamespace(load_active_state=lambda: None), revisions=Revisions(),
        attention_recipe="B3",
    )
    item = _decision("contact", "contact")
    from dataclasses import replace
    item = replace(item, candidate=replace(item.candidate, action={
        "type": "contact", "goal_urgency": 1.0,
        "source_freshness": 1.0, "resource_availability": 1.0,
    }))
    runner.run(EndogenousDecisionV2(decision_id="d-b3", acted=False, reason="defer",
               assessments=(item,)), now=item.predictions.reply.predicted_at)
    built = captured["built"]
    assert built.attention_profile.version == "langchao.attention.b3-explicit-signals.v1"
    assert "langchao.attention-recipe-audit.v1" in built.state.parameter_version
    assert "langchao.recipe.b3-explicit-attention-full-competition.v1" in built.state.parameter_version
    assert captured["kwargs"]["parameters"].competition_gain == 1.0
    assert len(captured["kwargs"]["edges"]) == 2


def test_borrowed_connection_forwards_sql_but_never_opens_nested_transaction():
    calls = []

    class Connection:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return "cursor"

        def transaction(self):
            raise AssertionError("repository must not own a nested transaction")

    borrowed = _BorrowedConnection(Connection())
    with borrowed.transaction():
        assert borrowed.execute("SELECT 1") == "cursor"
    assert calls == [("SELECT 1", ())]


def test_cli_routes_authority_before_any_engine_can_commit():
    source_text = (Path(__file__).parents[1] / "src" / "companion_runtime" / "cli.py").read_text(encoding="utf-8")
    body = source_text.split("def run_v2_round", 1)[1].split("def simulate_v2_decision", 1)[0]
    assert "authority_round_router.run(" in body
    assert "decide_endogenous(" not in body
    assert "langchao_shadow_runner.run" not in body


def test_cold_start_drive_is_granted_only_to_a_safe_low_pressure_candidate():
    """The drive needs the safety reason, and never overrides a block or a repeat limit."""
    from companion_runtime.langchao_shadow_wiring import COLD_START_EXPLORATION_REASON

    def question(candidate_id: str):
        return _decision_with_action(candidate_id, {
            "type": "curious_question", "question": True, "topic_shift": True,
            "memory_ref": "memory:m1",
        })

    allowed = replace(question("c1"), reasons=(COLD_START_EXPLORATION_REASON,))
    facts = _admit_candidate(allowed, cold_start_drive=2.0).facts
    assert facts is not None and facts.cold_start_drive == 2.0

    # Without the policy's own allowance the drive is not a licence to reach out.
    plain = _admit_candidate(question("c2"), cold_start_drive=2.0).facts
    assert plain is not None and plain.cold_start_drive == 0.0

    # A hard block or a spent repeat budget still wins over the drive.
    blocked = _admit_candidate(replace(allowed, blocked=True), cold_start_drive=2.0).facts
    assert blocked is not None and blocked.cold_start_drive == 0.0

    spent_repeat = type("Repeat", (), {
        "total_cost": 0.0, "policy_version": "repeat-v2.0",
        "hard_limit_reasons": ("hard_repeat",),
    })()
    repeated = _admit_candidate(
        replace(allowed, repeat=spent_repeat), cold_start_drive=2.0
    ).facts
    assert repeated is not None and repeated.cold_start_drive == 0.0


def test_cold_start_drive_is_refused_for_templates_that_already_own_a_local_reward():
    """Expression and rest already carry a local outcome; a second one would double-count."""
    from companion_runtime.langchao_shadow_wiring import COLD_START_EXPLORATION_REASON

    cases = (
        {"type": "share", "memory_ref": "memory:m1"},
        {"type": "internal_rest", "self_regulation_ref": "runtime:self-regulation"},
    )
    for action in cases:
        item = replace(
            _decision_with_action("c", action), reasons=(COLD_START_EXPLORATION_REASON,)
        )
        facts = _admit_candidate(item, cold_start_drive=2.0).facts
        assert facts is not None, action["type"]
        assert facts.cold_start_drive == 0.0, action["type"]


def test_round_drive_is_withheld_when_the_durable_budget_is_spent():
    """A budget that says no must zero the round's drive before any candidate is admitted."""
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)

    class SpentBudget:
        def available(self, *, now):
            return False

    runner = LangchaoShadowRunner(
        scope_key="scope", runtime=None, service=None, state_repository=None,
        revisions=None, cold_start_drive=2.0, cold_start_budget=SpentBudget(),
    )
    assert runner._cold_start_drive_for_round(now) == 0.0

    class OpenBudget:
        def available(self, *, now):
            return True

    runner.cold_start_budget = OpenBudget()
    assert runner._cold_start_drive_for_round(now) == 2.0

    runner.cold_start_drive = 0.0
    assert runner._cold_start_drive_for_round(now) == 0.0


def _decision(candidate_id: str, action_type: str):
    from dataclasses import replace
    from companion_runtime.runtime_v2 import CandidateDecisionV2

    item = source(candidate_id)
    candidate = replace(item.candidate, action={"type": action_type})
    repeat = type("Repeat", (), {
        "total_cost": 0.0,
        "policy_version": "repeat-v2.0",
        "hard_limit_reasons": (),
    })()
    utility = type("Utility", (), {
        "used_bounds": type("Bounds", (), {"to_dict": lambda self: {}})(),
        "decomposition": type("Terms", (), {"to_dict": lambda self: {"total": 0.0}})(),
    })()
    return CandidateDecisionV2(
        candidate=candidate,
        predictions=item.predictions,
        user_utility=utility,
        repeat=repeat,
        net_utility=1.0,
        blocked=False,
        reasons=(),
    )
