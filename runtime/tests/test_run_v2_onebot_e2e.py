"""Fixture/unit tests for the guarded live OneBot-v2 E2E runner.

These tests model public HTTP only. They do not claim that a deployment passed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_v2_onebot_e2e.py"
SPEC = importlib.util.spec_from_file_location("run_v2_onebot_e2e", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

SCOPE = "default:FriendMessage:20001"
PARAMETER = "parameter:one"


def evidence(*, ingress=False, complete=False):
    body: dict[str, Any] = {
        "scope": SCOPE,
        "read_only": True,
        "evidence": {"events": [], "exposures": [], "labels": [], "parameter_snapshots": []},
        "decision_audits": [],
    }
    if ingress:
        body["evidence"]["events"] = [{"event_id": "evt-one", "kind": "user_message", "scope_key": SCOPE}]
    if complete:
        body["decision_audits"] = [{
            "decision_id": "decision:one", "scope_key": SCOPE,
            "parameter_snapshot_id": PARAMETER,
            "stages": [{"stage": name} for name in runner.REQUIRED_STAGES],
        }]
        body["evidence"].update({
            "exposures": [{
                "exposure_id": "exposure:one", "scope_key": SCOPE,
                "decision_id": "decision:one", "delivery_basis": "delivered", "send_ack_id": "ack:one",
            }],
            "labels": [{
                "target_label_id": "label:one", "scope_key": SCOPE, "target_name": "reply",
                "status": "observed_positive", "target_value": True,
            }],
            "parameter_snapshots": [{
                "parameter_snapshot_id": PARAMETER, "scope_key": SCOPE,
                "parameter_version": 1, "parameters": {"reply_horizon_seconds": 21600},
            }],
        })
    return body


class PublicFixture:
    """A deterministic xxj-onebot/AstrBot/Runtime public-wire fixture."""
    def __init__(self, *, automatic_decision: bool = False) -> None:
        self.exchanges = []
        self.sends = 0
        self.decision_called = automatic_decision
        self.evidence_reads = 0
        self.reply_seen = False
        self.state_reads = 0

    def request(self, method: str, url: str, payload: Mapping[str, Any] | None = None):
        response: Any
        if url.endswith("/state"):
            self.state_reads += 1
            sent = self.decision_called and self.state_reads > 1
            response = {
                "connected": True, "counts": {"sent": 1 if sent else 0},
                "calls": ([{"action": "send_private_msg", "params": {"user_id": "20001"}}]
                          if sent else []),
            }
        elif "blackbox/evidence" in url:
            self.evidence_reads += 1
            if self.reply_seen:
                response = evidence(ingress=True, complete=True)
            elif self.evidence_reads == 1:
                response = evidence()
            elif self.decision_called:
                response = evidence(ingress=True, complete=True)
            else:
                response = evidence(ingress=True)
        elif url.endswith("/decisions/run"):
            assert payload and payload["simulate"] is True and payload["scope"] == SCOPE
            self.decision_called = True
            response = {"scope": SCOPE, "simulated": True, "result": {"acted": True}}
        elif url.endswith("/send"):
            self.sends += 1
            assert payload and payload["user_id"] == "20001"
            self.reply_seen = self.sends == 2
            response = {"ok": True, "event": {"message_id": self.sends}}
        else:  # AstrBot reachability root
            response = "AstrBot"
        self.exchanges.append(runner.Exchange(method, url, 200, payload, response))
        return response


def config(**overrides):
    values = dict(
        onebot_url="http://onebot.test:6300", astrbot_url="http://astrbot.test:6185",
        runtime_url="http://runtime.test:8090", pg_dsn=None, scope=SCOPE,
        confirm_napcat_stopped=True, wait_seconds=2, poll_seconds=0.001,
    )
    values.update(overrides)
    return runner.Config(**values)


def test_full_public_fixture_uses_v1_ingress_then_simulation_and_strict_evidence() -> None:
    fixture = PublicFixture()
    result = runner.Runner(config(), fixture, sleeper=lambda _: None).run()
    assert result["passed"] is True
    assert result["decision_simulation_used"] is True
    assert result["checks"] == {
        "v2_exposure": True, "v2_reply_label": True,
        "v2_complete_audit": True, "v2_parameter_reference": True,
    }
    sends = [row for row in fixture.exchanges if row.url.endswith("/send")]
    assert len(sends) == 2
    assert all(row.request["user_id"] == "20001" for row in sends)


def test_simulation_endpoint_is_not_called_when_complete_decision_already_exists() -> None:
    fixture = PublicFixture(automatic_decision=True)
    result = runner.Runner(config(), fixture, sleeper=lambda _: None).run()
    assert result["passed"] is True
    assert result["decision_simulation_used"] is False


def test_default_refuses_traffic_without_explicit_napcat_stopped_confirmation() -> None:
    fixture = PublicFixture()
    with pytest.raises(runner.E2EFailure, match="NapCat is stopped"):
        runner.Runner(config(confirm_napcat_stopped=False), fixture).run()
    assert fixture.exchanges == []


def test_disconnected_onebot_is_rejected_before_message_injection() -> None:
    fixture = PublicFixture()
    original = fixture.request
    def disconnected(method, url, payload=None):
        if url.endswith("/state"):
            fixture.exchanges.append(runner.Exchange(method, url, 200, payload, {"connected": False}))
            return {"connected": False}
        return original(method, url, payload)
    fixture.request = disconnected
    with pytest.raises(runner.E2EFailure, match="not connected"):
        runner.Runner(config(), fixture).run()
    assert not any(row.url.endswith("/send") for row in fixture.exchanges)


def test_runner_is_pinned_to_fake_user_20001() -> None:
    with pytest.raises(ValueError, match="20001"):
        config(user_id="123456789")


@pytest.mark.parametrize("missing", ["v2_exposure", "v2_reply_label", "v2_complete_audit", "v2_parameter_reference"])
def test_strict_checks_fail_each_required_v2_observable(missing: str) -> None:
    before, after = evidence(), evidence(ingress=True, complete=True)
    if missing == "v2_exposure":
        after["evidence"]["exposures"] = []
    elif missing == "v2_reply_label":
        after["evidence"]["labels"] = []
    elif missing == "v2_complete_audit":
        after["decision_audits"][0]["stages"] = [{"stage": "wake"}]
    else:
        after["evidence"]["parameter_snapshots"] = []
    checks = runner.strict_checks(before, after, scope=SCOPE)
    assert checks[missing] is False
    assert not all(checks.values())


def test_scope_mismatch_is_rejected_even_with_otherwise_complete_payload() -> None:
    payload = evidence(ingress=True, complete=True)
    payload["scope"] = "other"
    class WrongScope(PublicFixture):
        def request(self, method, url, request_payload=None):
            if "blackbox/evidence" in url:
                self.exchanges.append(runner.Exchange(method, url, 200, request_payload, payload))
                return payload
            return super().request(method, url, request_payload)
    with pytest.raises(runner.E2EFailure, match="scope-bound"):
        runner.Runner(config(), WrongScope()).run()


def test_dsn_is_parameterized_but_never_emitted() -> None:
    fixture = PublicFixture()
    secret = "postgresql://runtime:do-not-print@db/runtime"
    result = runner.Runner(config(pg_dsn=secret), fixture, sleeper=lambda _: None).run()
    serialized = str(runner.redact(result)) + repr(fixture.exchanges)
    assert secret not in serialized
    assert "do-not-print" not in serialized
    assert result["pg_dsn_mode"] == "configured_not_printed_public_evidence_authoritative"
