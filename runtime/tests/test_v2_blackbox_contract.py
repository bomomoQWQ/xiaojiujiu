"""Contract tests for the external-only v2 black-box harness.

These tests use a synthetic HTTP fixture.  They prove request construction and
result validation, not that the currently unwired Runtime passes live v2 E2E.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "v2_blackbox_e2e.py"
SPEC = importlib.util.spec_from_file_location("v2_blackbox_e2e", SCRIPT)
assert SPEC and SPEC.loader
blackbox = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = blackbox
SPEC.loader.exec_module(blackbox)

SCOPE = "onebot:private:contract-test"


class SyntheticHttpFixture:
    """In-memory public-wire fixture; it has no access to Runtime internals."""

    def __init__(self, evidence: Mapping[str, Any]) -> None:
        self.evidence = evidence
        self.exchanges: list[Any] = []

    def request(self, method: str, url: str, payload: Mapping[str, Any] | None = None) -> Any:
        if method == "GET":
            response: Any = copy.deepcopy(self.evidence)
        elif url.endswith("/events") and payload and "events" in payload:
            response = {"accepted": 1, "duplicates": 0}
        elif url.endswith("/decisions/run"):
            response = {"accepted": True, "decision_id": "bb-decision-1"}
        else:
            response = {"ok": True}
        self.exchanges.append(blackbox.HttpExchange(method, url, 200, payload, response))
        return response


def test_synthetic_http_fixture_covers_complete_public_contract() -> None:
    evidence = blackbox.synthetic_evidence(SCOPE)
    transport = SyntheticHttpFixture(evidence)
    endpoints = blackbox.EndpointConfig(
        runtime_base_url="http://runtime.test:8090",
        onebot_base_url="http://onebot.test:5700",
    )
    scenario = blackbox.BlackBoxScenario(
        endpoints,
        blackbox.ScenarioConfig(scope=SCOPE, dry_run=False),
        transport,
    )

    result = scenario.run()
    report = blackbox.ContractValidator().validate(
        result["evidence"], expected_scope=SCOPE, executed=True, source="synthetic_http"
    )

    assert report.passed
    assert {check.name for check in report.checks} == {
        "evidence_contract",
        "scope",
        "message_entered",
        "idempotent_replay",
        "active_decision_funnel",
        "send_ack_exposure",
        "reply_label",
        "late_and_no_reply",
        "boundary",
        "parameter_snapshot",
    }
    requests = transport.exchanges
    assert [row.method for row in requests] == ["POST", "POST", "POST", "POST", "GET"]
    assert requests[0].url == "http://onebot.test:5700/v2/test/events"
    assert requests[1].url == "http://runtime.test:8090/v2/events"
    assert requests[1].request_json == requests[2].request_json  # exact idempotency replay
    assert requests[3].url == "http://runtime.test:8090/v2/decisions/run"
    assert requests[4].url.endswith("/v2/blackbox/evidence?scope=onebot%3Aprivate%3Acontract-test")


@pytest.mark.parametrize(
    ("check_name", "mutate"),
    [
        ("message_entered", lambda value: value.update(events=[])),
        ("idempotent_replay", lambda value: value.update(duplicate_events=0)),
        (
            "active_decision_funnel",
            lambda value: value["decisions"][0].update(stages=[{"stage": "wake"}]),
        ),
        ("send_ack_exposure", lambda value: value["exposures"][0].update(send_ack_id="")),
        (
            "reply_label",
            lambda value: value["labels"][0].update(status="pending", value=None),
        ),
        (
            "late_and_no_reply",
            lambda value: value["labels"][1].update(status="observed_positive", value=True),
        ),
        ("boundary", lambda value: value.update(boundaries=[])),
        (
            "parameter_snapshot",
            lambda value: value["parameter_snapshots"][0].update(active=False),
        ),
    ],
)
def test_validator_rejects_each_missing_observable(check_name, mutate) -> None:
    evidence = blackbox.synthetic_evidence(SCOPE)
    mutate(evidence)
    report = blackbox.ContractValidator().validate(evidence, expected_scope=SCOPE)
    checks = {check.name: check.passed for check in report.checks}
    assert checks[check_name] is False
    assert report.passed is False


def test_dry_run_builds_requests_but_cannot_claim_live_success() -> None:
    transport = blackbox.DryRunTransport()
    scenario = blackbox.BlackBoxScenario(
        blackbox.EndpointConfig(),
        blackbox.ScenarioConfig(scope=SCOPE, dry_run=True),
        transport,
    )
    result = scenario.run()
    assert result["executed"] is False
    assert len(result["exchanges"]) == 5
    report = blackbox.ContractValidator().validate(
        blackbox.synthetic_evidence(SCOPE),
        expected_scope=SCOPE,
        executed=False,
        source="dry_run",
    )
    assert report.passed is False
    assert report.to_dict()["executed"] is False


def test_cli_defaults_to_dry_run_and_prints_no_dsn(capsys) -> None:
    secret_dsn = "postgresql://runtime:do-not-print@db/runtime"
    code = blackbox.main(["--dsn", secret_dsn, "--json", "--scope", SCOPE])
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert code == 0
    assert payload["live_contract_passed"] is False
    assert payload["status"] == "dry_run_not_executed"
    assert secret_dsn not in output
    assert "do-not-print" not in output


def test_report_redacts_nested_secret_fields() -> None:
    value = {
        "dsn": "postgresql://u:p@db/x",
        "nested": {"Authorization": "Bearer abc", "safe": "visible"},
        "items": [{"api_key": "sk-test"}],
    }
    assert blackbox.redact(value) == {
        "dsn": "<redacted>",
        "nested": {"Authorization": "<redacted>", "safe": "visible"},
        "items": [{"api_key": "<redacted>"}],
    }


def test_endpoint_and_scope_are_parameterized_and_validated() -> None:
    endpoints = blackbox.EndpointConfig(
        runtime_base_url="https://runtime.example/base",
        onebot_base_url="https://onebot.example",
        runtime_event_path="/public/events",
        decision_path="/public/decision",
        evidence_path="/public/evidence",
        onebot_event_path="/fixture/event",
    )
    assert endpoints.runtime_url(endpoints.runtime_event_path) == "https://runtime.example/base/public/events"
    with pytest.raises(ValueError, match="absolute HTTP"):
        blackbox.EndpointConfig(runtime_base_url="runtime.internal")
    with pytest.raises(ValueError, match="scope"):
        blackbox.ScenarioConfig(scope=" ")


def test_synthetic_mode_is_explicitly_not_a_live_pass(capsys) -> None:
    code = blackbox.main(["--synthetic", "--json", "--scope", SCOPE])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["synthetic_fixture_valid"] is True
    assert payload["live_contract_passed"] is False
    assert payload["validation"]["source"] == "synthetic_fixture"


def test_exchange_serialization_contains_only_public_wire_data() -> None:
    transport = SyntheticHttpFixture(blackbox.synthetic_evidence(SCOPE))
    blackbox.BlackBoxScenario(
        blackbox.EndpointConfig(), blackbox.ScenarioConfig(scope=SCOPE, dry_run=False), transport
    ).run()
    serialized = json.dumps([asdict(row) for row in transport.exchanges], default=str)
    assert "companion_runtime" not in serialized
    assert "_commit_attempt" not in serialized
    assert SCOPE in serialized
