"""Tests for the optional local CPU model integration.

Every test runs offline: the HTTP transport is replaced by an injected fake, so
the suite proves the degradation contract without a llama.cpp server.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from companion_runtime.local_llm import (
    LocalModelClient,
    LocalModelConfig,
    load_grammar,
    parse_appraisal,
    parse_explanation,
)

GOOD_APPRAISAL = {
    "direction": "-",
    "impact": 0.45,
    "activation": 0.30,
    "uncertainty": 0.60,
    "relation_signal": "slight_distance",
    "responsibility": "unclear",
    "confidence": 0.70,
}


def _reply(payload: Any) -> Mapping[str, Any]:
    """Wrap a payload the way an OpenAI-compatible endpoint would."""
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}


def _client(handler, **overrides: Any) -> LocalModelClient:
    """Build a client whose transport is ``handler``."""
    config = LocalModelConfig(enabled=True, **overrides)
    calls: list[dict[str, Any]] = []

    def transport(url: str, body: dict[str, Any], timeout: float, headers: dict[str, str]):
        calls.append({"url": url, "body": body, "timeout": timeout, "headers": headers})
        return handler(url, body, timeout, headers)

    client = LocalModelClient(config, transport=transport)
    client.calls = calls  # type: ignore[attr-defined]
    return client


class TestParsing:
    """The contract parser is the last line of defence before state changes."""

    def test_valid_payload_is_accepted(self) -> None:
        evaluation = parse_appraisal(GOOD_APPRAISAL)
        assert evaluation is not None
        assert evaluation.direction == "-"
        assert evaluation.source == "local_model"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda payload: payload.pop("confidence"),
            lambda payload: payload.update(direction="?"),
            lambda payload: payload.update(relation_signal="unknown_label"),
            lambda payload: payload.update(responsibility="nobody"),
            lambda payload: payload.update(impact=1.4),
            lambda payload: payload.update(impact="0.5"),
            lambda payload: payload.update(direction="0", impact=0.9),
            lambda payload: payload.update(direction="+", impact=0.0),
        ],
    )
    def test_contract_violations_are_rejected(self, mutate) -> None:
        payload = dict(GOOD_APPRAISAL)
        mutate(payload)
        assert parse_appraisal(payload) is None

    def test_explanation_requires_six_fields(self) -> None:
        payload = {name: "一句话" for name in ("experience", "focus", "conflict", "impulse", "inhibition", "expression")}
        assert parse_explanation(payload) is not None
        payload["focus"] = 3
        assert parse_explanation(payload) is None


class TestAppraise:
    """Appraisal must be strictly fail-open."""

    def test_successful_call_is_not_degraded(self) -> None:
        client = _client(lambda *_: _reply(GOOD_APPRAISAL))
        result = client.appraise("今晚可能不来了。")
        assert result.degraded is False
        assert result.evaluation is not None
        assert result.evaluation.source == "local_model"

    def test_disabled_client_degrades_without_calling_out(self) -> None:
        client = LocalModelClient(LocalModelConfig(enabled=False))
        result = client.appraise("你好")
        assert result.degraded is True
        assert result.reason == "disabled"

    def test_network_failure_degrades(self) -> None:
        def boom(*_: Any) -> Mapping[str, Any]:
            raise OSError("connection refused")

        result = _client(boom).appraise("你好")
        assert result.degraded is True
        assert result.reason.startswith("error:")

    def test_malformed_reply_degrades(self) -> None:
        client = _client(lambda *_: {"choices": [{"message": {"content": "不是 JSON"}}]})
        assert client.appraise("你好").degraded is True

    def test_contract_violation_degrades(self) -> None:
        client = _client(lambda *_: _reply({**GOOD_APPRAISAL, "impact": 9}))
        result = client.appraise("你好")
        assert result.degraded is True
        assert result.reason == "contract_violation"

    def test_empty_event_degrades_immediately(self) -> None:
        client = _client(lambda *_: _reply(GOOD_APPRAISAL))
        assert client.appraise("   ").reason == "empty_event"

    def test_grammar_is_sent_as_inline_gbnf(self) -> None:
        client = _client(lambda *_: _reply(GOOD_APPRAISAL))
        client.appraise("你好")
        body = client.calls[0]["body"]  # type: ignore[attr-defined]
        assert "::=" in body["grammar"]

    def test_authorization_header_only_when_key_present(self) -> None:
        client = _client(lambda *_: _reply(GOOD_APPRAISAL), api_key="local-token")
        client.appraise("你好")
        headers = client.calls[0]["headers"]  # type: ignore[attr-defined]
        assert headers["Authorization"] == "Bearer local-token"


class TestExplain:
    """Explanations are cached by state key and degrade to the template."""

    def test_explanation_is_cached(self) -> None:
        calls = {"n": 0}

        def handler(*_: Any) -> Mapping[str, Any]:
            calls["n"] += 1
            return _reply(
                {
                    "experience": "有点失落。",
                    "focus": "在想对方是不是累了。",
                    "conflict": "",
                    "impulse": "想确认一下。",
                    "inhibition": "先不打扰。",
                    "expression": "语气放轻。",
                }
            )

        client = _client(handler)
        first, cached_first = client.explain({"valence": -0.2}, state_key="k1")
        second, cached_second = client.explain({"valence": -0.2}, state_key="k1")
        assert first is not None
        assert cached_first is False
        assert second == first
        assert cached_second is True
        assert calls["n"] == 1

    def test_changed_state_key_is_not_cached(self) -> None:
        client = _client(
            lambda *_: _reply(
                {
                    "experience": "平静。",
                    "focus": "没什么特别的。",
                    "conflict": "",
                    "impulse": "",
                    "inhibition": "",
                    "expression": "平常。",
                }
            )
        )
        client.explain({"valence": 0.0}, state_key="a")
        _second, cached = client.explain({"valence": 0.1}, state_key="b")
        assert cached is False

    def test_failure_returns_none(self) -> None:
        def boom(*_: Any) -> Mapping[str, Any]:
            raise TimeoutError("deadline")

        assert _client(boom, explain_timeout_s=0.01).explain({"valence": 0.0})[0] is None


class TestGrammarFiles:
    """Bundled grammars must exist and be loadable."""

    @pytest.mark.parametrize("name", ["appraisal", "explanation"])
    def test_grammar_loads(self, name: str) -> None:
        text = load_grammar(name)
        assert text is not None
        assert "root" in text

    def test_missing_grammar_is_not_fatal(self) -> None:
        assert load_grammar("does-not-exist") is None
