from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from companion_runtime.config import RuntimeConfig
from companion_runtime.langchao_shadow_wiring import LangchaoShadowRunner, _facts_for
from companion_runtime.runtime_v2 import EndogenousDecisionV2
from test_langchao_runtime_adapter import source


def test_langchao_shadow_is_default_off_and_requires_explicit_switch():
    config = RuntimeConfig()
    assert config.langchao.shadow_enabled is False
    config.langchao.shadow_enabled = True
    assert config.langchao.shadow_enabled is True


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
    runner = LangchaoShadowRunner(
        scope_key="scope", runtime=SimpleNamespace(state=lambda: state), service=Service(),
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
    assert captured["kwargs"]["idempotency_key"] == "runtime-v2:d1"


def test_cli_shadow_failure_isolation_is_after_baseline_decision():
    source_text = (Path(__file__).parents[1] / "src" / "companion_runtime" / "cli.py").read_text(encoding="utf-8")
    body = source_text.split("def run_v2_round", 1)[1].split("def simulate_v2_decision", 1)[0]
    assert body.index("decide_endogenous(") < body.index("langchao_shadow_runner.run")
    assert "except Exception" in body and "LOGGER.exception" in body


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
