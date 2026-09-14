"""End-to-end tests for the Level 2 (local CPU model) appraisal path.

These tests drive the *whole* Runtime, not the client in isolation, so they pin
the two properties that matter operationally:

* when the model answers inside its contract, its appraisal is what reaches the
  emotion layer (``appraisal_source == "local_model"``);
* when it is unreachable, slow or contract-violating, the Runtime silently keeps
  working on the deterministic Level 0 appraiser and never raises.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Mapping

import pytest

from companion_runtime.local_llm import LocalModelClient, LocalModelConfig
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME, build_config

GOOD_APPRAISAL = {
    "direction": "-",
    "impact": 0.6,
    "activation": 0.4,
    "uncertainty": 0.5,
    "relation_signal": "slight_distance",
    "responsibility": "unclear",
    "confidence": 0.8,
}


def _runtime_with_model(handler, **config_overrides: Any) -> Runtime:
    """Build a Runtime whose local model is served by ``handler``."""
    runtime = Runtime(config=build_config())
    config = LocalModelConfig(enabled=True, **config_overrides)
    runtime.local_model = LocalModelClient(
        config,
        transport=lambda url, body, timeout, headers: handler(),
    )
    return runtime


def _reply(payload: Any) -> Mapping[str, Any]:
    """Wrap a payload the way an OpenAI-compatible endpoint would."""
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}


class TestModelBackedAppraisal:
    """The model is an accelerator, never a dependency."""

    def test_model_appraisal_reaches_the_emotion_layer(self) -> None:
        runtime = _runtime_with_model(lambda: _reply(GOOD_APPRAISAL))
        try:
            outcome = runtime.process_user_message(content="今晚可能不来了。", timestamp=BASE_TIME)
            assert outcome.appraisal_source == "local_model"
            assert outcome.emotion_event_ids, "a negative appraisal must create impact"
        finally:
            runtime.close()

    def test_unreachable_model_degrades_to_rules(self) -> None:
        def boom() -> Mapping[str, Any]:
            raise OSError("connection refused")

        runtime = _runtime_with_model(boom)
        try:
            outcome = runtime.process_user_message(content="今晚可能不来了。", timestamp=BASE_TIME)
            assert outcome.appraisal_source == "rule"
            assert outcome.emotion_event_ids
        finally:
            runtime.close()

    def test_contract_violation_degrades_to_rules(self) -> None:
        runtime = _runtime_with_model(lambda: _reply({**GOOD_APPRAISAL, "relation_signal": "made_up"}))
        try:
            outcome = runtime.process_user_message(content="今晚可能不来了。", timestamp=BASE_TIME)
            assert outcome.appraisal_source == "rule"
        finally:
            runtime.close()

    def test_disabled_model_never_changes_behaviour(self) -> None:
        """A default Runtime is byte-for-byte the deterministic Level 0 path."""
        runtime = Runtime(config=build_config())
        try:
            assert runtime.local_model.config.enabled is False
            outcome = runtime.process_user_message(content="谢谢你，今天很开心。", timestamp=BASE_TIME)
            assert outcome.appraisal_source == "rule"
        finally:
            runtime.close()

    def test_model_outage_does_not_stop_later_rounds(self) -> None:
        """A flapping model must not wedge the ingress path."""
        state = {"fail": True}

        def flaky() -> Mapping[str, Any]:
            if state["fail"]:
                raise TimeoutError("deadline")
            return _reply(GOOD_APPRAISAL)

        runtime = _runtime_with_model(flaky)
        try:
            first = runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
            assert first.appraisal_source == "rule"
            state["fail"] = False
            second = runtime.process_user_message(
                content="今天面试没过，好难受。", timestamp=BASE_TIME + timedelta(minutes=5)
            )
            assert second.appraisal_source == "local_model"
        finally:
            runtime.close()


class TestHealthSurface:
    """Operators must be able to see which level is active."""

    def test_health_reports_level_and_counters(self) -> None:
        runtime = _runtime_with_model(lambda: _reply(GOOD_APPRAISAL))
        try:
            runtime.process_user_message(content="今晚可能不来了。", timestamp=BASE_TIME)
            health = runtime.local_model.health()
            assert health["enabled"] is True
            assert health["stats"]["appraise_calls"] == 1
            assert health["stats"]["appraise_ok"] == 1
            assert health["stats"]["appraise_degraded"] == 0
        finally:
            runtime.close()

    def test_api_health_endpoint_includes_the_local_model(self) -> None:
        from fastapi.testclient import TestClient

        from companion_runtime.api import create_app

        runtime = _runtime_with_model(lambda: _reply(GOOD_APPRAISAL))
        try:
            client = TestClient(create_app(runtime, runtime.config))
            body = client.get("/health").json()
            assert body["local_model"]["enabled"] is True
        finally:
            runtime.close()
