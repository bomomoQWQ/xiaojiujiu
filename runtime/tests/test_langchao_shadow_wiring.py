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
    LangchaoShadowRunner,
    _BorrowedConnection,
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
    assert _facts_for(decision, internal_exploration_enabled=False) is None
    assert _facts_for(_decision("vague", "exploration_work_segment")) is None


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


def test_fixed_template_mapping_drops_unreferenced_expression_and_followup():
    expression = source("expression").assessment
    expression_decision = _decision("expression", "share")
    assert _facts_for(expression_decision) is None
    followup_decision = _decision("followup", "follow_up")
    assert _facts_for(followup_decision) is None
    contact = _facts_for(_decision("contact", "contact"))
    assert contact is not None and contact.template_key == "contact.v1"
    rest = _facts_for(_decision("rest", "internal_rest"))
    assert rest is not None and rest.template_key == "internal_rest.v1"
    assert _facts_for(_decision("repair", "repair")) is None
    assert _facts_for(_decision("apology", "apology")) is None


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
