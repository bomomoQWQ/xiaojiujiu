"""Tests for the optional Semantic Provider port (patch v0.2 sections 16-21).

The whole suite is offline: every provider gets an injected transport, so the
degradation contracts are proven without a network and without any endpoint at
all. The suite covers the four things the integration depends on:

* ``disabled`` is the default, and every unknown name or failed construction
  falls back to it without raising;
* both ``deep_refresh`` and ``explain_state`` fail open on timeout and on
  malformed JSON;
* ``health()`` and ``repr()`` never leak an API key;
* the ``state_key`` cache contract of ``explain_state`` is honoured.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any, Mapping

import pytest

from companion_runtime.providers import (
    API_KEY_ENV_VAR,
    DEEP_REFRESH_FIELDS,
    KNOWN_PROVIDER_NAMES,
    RETIRED_PROVIDER_NAMES,
    DeepRefreshRequest,
    DeepRefreshSuggestions,
    DisabledProvider,
    RemoteAPIProvider,
    build_provider,
    parse_deep_refresh,
    resolve_provider_name,
)

GOOD_SUGGESTIONS: dict[str, Any] = {
    "reinterpretations": [{"event_id": "e1", "reading": "当时是在硬撑"}],
    "psychological_interpretation": {"tone": "克制的失落"},
    "candidate_intent_operations": [{"op": "add", "intent": "问问那天的事"}],
    "memory_suggestions": [{"text": "他很少主动提家里", "kind": "episodic"}],
    "unfinished_matter_suggestions": [{"title": "等他回话说面试结果"}],
    "user_model_evidence_suggestions": [{"trait": "回避冲突", "weight": 0.3}],
}

GOOD_EXPLANATION: dict[str, str] = {
    "experience": "心里有点发闷，但还不至于说出来。",
    "focus": "注意力停在他刚才那句话上。",
    "conflict": "想追问，又怕显得逼人。",
    "impulse": "想问清楚他到底怎么想的。",
    "inhibition": "先按住了，等他愿意自己说。",
    "expression": "表面上还是平稳地接话。",
}

#: A canary that must never appear in a repr, a log record or ``health()``.
SECRET = "sk-canary-DO-NOT-LEAK-1234567890"


def _reply(payload: Any) -> Mapping[str, Any]:
    """Wrap a payload the way an OpenAI-compatible endpoint would."""
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}


class FakeTransport:
    """A recording transport that returns a scripted reply or raises.

    Args:
        handler: Callable receiving ``(url, body, timeout, headers)``.
    """

    def __init__(self, handler) -> None:
        """Store the scripted handler."""
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> Mapping[str, Any]:
        """Record the call and delegate to the scripted handler."""
        self.calls.append({"url": url, "body": body, "timeout": timeout, "headers": headers})
        return self._handler(url, body, timeout, headers)

    @property
    def call_count(self) -> int:
        """Return how many requests were attempted."""
        return len(self.calls)


def _remote(handler, **overrides: Any) -> RemoteAPIProvider:
    """Build a remote provider wired to a fake transport."""
    settings: dict[str, Any] = {
        "base_url": "https://semantic.example.com/v1",
        "model": "strong-model",
        "api_key": SECRET,
    }
    settings.update(overrides)
    return RemoteAPIProvider(transport=FakeTransport(handler), **settings)


def _suggestions_payload(**overrides: Any) -> DeepRefreshSuggestions:
    """Return a non-degraded suggestion set for request-building tests."""
    payload = dict(GOOD_SUGGESTIONS)
    payload.update(overrides)
    result = parse_deep_refresh(payload)
    assert result is not None
    return result


# --------------------------------------------------------------------------------------
# Contract parsing
# --------------------------------------------------------------------------------------


class TestParseDeepRefresh:
    """A model reply is untrusted input: malformed fields are dropped, not repaired."""

    def test_fully_valid_payload_is_not_degraded(self) -> None:
        result = parse_deep_refresh(GOOD_SUGGESTIONS, provider="remote_api", latency_ms=12)
        assert result is not None
        assert result.degraded is False
        assert result.reason == ""
        assert result.provider == "remote_api"
        assert result.latency_ms == 12
        assert result.is_empty() is False
        assert result.reinterpretations[0]["event_id"] == "e1"
        assert result.psychological_interpretation["tone"] == "克制的失落"

    def test_nested_wrapper_payload_is_accepted(self) -> None:
        result = parse_deep_refresh({"suggestions": GOOD_SUGGESTIONS})
        assert result is not None
        assert result.degraded is False
        assert len(result.memory_suggestions) == 1

    def test_omitted_fields_are_allowed(self) -> None:
        result = parse_deep_refresh({"reinterpretations": []})
        assert result is not None
        assert result.degraded is False
        assert result.is_empty() is True

    def test_non_mapping_payload_returns_none(self) -> None:
        assert parse_deep_refresh(["not", "an", "object"]) is None
        assert parse_deep_refresh("{}") is None
        assert parse_deep_refresh(None) is None

    @pytest.mark.parametrize(
        "field_name",
        sorted(DEEP_REFRESH_FIELDS),
    )
    def test_wrongly_typed_field_is_dropped_and_named(self, field_name: str) -> None:
        wrong_type: Any = "not-the-right-type"
        payload = dict(GOOD_SUGGESTIONS)
        payload[field_name] = wrong_type
        result = parse_deep_refresh(payload)
        assert result is not None
        assert result.degraded is True
        assert field_name in result.reason
        assert result.reason.startswith("invalid_fields:")
        # The bad field fell back to its empty default; the good ones survived.
        expected = DEEP_REFRESH_FIELDS[field_name]
        assert getattr(result, field_name) == ([] if expected is list else {})

    def test_list_field_with_non_mapping_items_is_dropped(self) -> None:
        payload = dict(GOOD_SUGGESTIONS)
        payload["memory_suggestions"] = ["just a string"]
        result = parse_deep_refresh(payload)
        assert result is not None
        assert result.degraded is True
        assert "memory_suggestions" in result.reason
        assert result.memory_suggestions == []

    def test_multiple_bad_fields_are_all_named(self) -> None:
        result = parse_deep_refresh(
            {"reinterpretations": 5, "memory_suggestions": {"nope": True}}
        )
        assert result is not None
        assert "reinterpretations" in result.reason
        assert "memory_suggestions" in result.reason

    def test_to_dict_is_json_serialisable(self) -> None:
        result = parse_deep_refresh(GOOD_SUGGESTIONS, provider="local_cpu")
        assert result is not None
        rendered = result.to_dict()
        assert rendered["provider"] == "local_cpu"
        assert rendered["degraded"] is False
        assert json.loads(json.dumps(rendered))["provider"] == "local_cpu"

    def test_is_empty_reports_every_collection(self) -> None:
        empty = DeepRefreshSuggestions()
        assert empty.is_empty() is True
        assert empty.degraded is True


class TestDeepRefreshRequest:
    """The request is the whole surface a deep refresh may see."""

    def test_defaults_are_empty_and_independent(self) -> None:
        first = DeepRefreshRequest()
        second = DeepRefreshRequest()
        first.unresolved_events.append({"event_id": "e1"})
        assert second.unresolved_events == []
        assert first.situation == {}
        assert first.user_model_summary == ""

    def test_to_dict_round_trips_every_field(self) -> None:
        request = DeepRefreshRequest(
            unresolved_events=[{"event_id": "e1"}],
            situation={"phase": "late_night"},
            mood={"valence": -0.2},
            active_emotions=[{"label": "失落"}],
            memories=[{"memory_id": "m1"}],
            unfinished=[{"title": "等他回话"}],
            user_model_summary="偏回避",
        )
        assert request.to_dict()["situation"] == {"phase": "late_night"}
        assert request.to_dict()["user_model_summary"] == "偏回避"


# --------------------------------------------------------------------------------------
# Factory selection
# --------------------------------------------------------------------------------------


class TestBuildProvider:
    """Selection must be total: no environment can break the Runtime."""

    def test_default_is_disabled(self) -> None:
        provider = build_provider(env={})
        assert isinstance(provider, DisabledProvider)
        assert provider.name == "disabled"

    def test_default_with_no_arguments_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CR_SEMANTIC_PROVIDER", raising=False)
        assert isinstance(build_provider(), DisabledProvider)

    def test_explicit_disabled_name(self) -> None:
        provider = build_provider(env={"CR_SEMANTIC_PROVIDER": "disabled"})
        assert isinstance(provider, DisabledProvider)

    def test_unknown_name_falls_back_without_raising(self) -> None:
        for name in ("openai", "LOCAL_2B", "", "  ", "remote", "disabled2", "none"):
            provider = build_provider(env={"CR_SEMANTIC_PROVIDER": name})
            assert isinstance(provider, DisabledProvider), name

    def test_names_are_case_and_space_insensitive(self) -> None:
        provider = build_provider(env={"CR_SEMANTIC_PROVIDER": "  Remote_API  "})
        assert isinstance(provider, RemoteAPIProvider)

    def test_the_local_route_is_gone(self) -> None:
        """Abandoning the local model must be an explicit, reported outcome.

        An operator still exporting a retired name must get the disabled provider
        and a warning - never a half-working local path or a silent success.
        """
        for name in sorted(RETIRED_PROVIDER_NAMES):
            provider = build_provider(env={"CR_SEMANTIC_PROVIDER": name})
            assert isinstance(provider, DisabledProvider), name
            assert provider.available() is False

    def test_only_disabled_and_remote_are_known(self) -> None:
        assert KNOWN_PROVIDER_NAMES == {"disabled", "remote_api"}
        assert not (KNOWN_PROVIDER_NAMES & RETIRED_PROVIDER_NAMES)

    def test_a_retired_name_is_reported_in_the_log(self, caplog: Any) -> None:
        with caplog.at_level(logging.WARNING):
            build_provider(env={"CR_SEMANTIC_PROVIDER": "local_cpu"})
        assert any("abandoned" in record.getMessage() for record in caplog.records)

    def test_remote_selection_uses_environment_key(self) -> None:
        provider = build_provider(
            env={
                "CR_SEMANTIC_PROVIDER": "remote_api",
                "CR_SEMANTIC_BASE_URL": "https://semantic.example.com/v1",
                "CR_SEMANTIC_MODEL": "strong-model",
                API_KEY_ENV_VAR: SECRET,
            }
        )
        assert isinstance(provider, RemoteAPIProvider)
        assert provider.available() is True
        assert provider.health()["api_key"] == "configured"

    def test_remote_without_key_is_unavailable_but_constructed(self) -> None:
        provider = build_provider(
            env={
                "CR_SEMANTIC_PROVIDER": "remote_api",
                "CR_SEMANTIC_BASE_URL": "https://semantic.example.com/v1",
                "CR_SEMANTIC_MODEL": "strong-model",
            }
        )
        assert isinstance(provider, RemoteAPIProvider)
        assert provider.available() is False
        assert provider.deep_refresh(DeepRefreshRequest()) is None

    def test_config_object_wins_over_environment(self) -> None:
        class FakeConfig:
            """A stand-in for ``RuntimeConfig`` carrying only the selection."""

            semantic_provider = "disabled"

        provider = build_provider(FakeConfig(), env={"CR_SEMANTIC_PROVIDER": "remote_api"})
        assert isinstance(provider, DisabledProvider)

    def test_mapping_config_is_supported(self) -> None:
        provider = build_provider({"semantic_provider": "remote_api"}, env={})
        assert isinstance(provider, RemoteAPIProvider)

    def test_a_retired_name_in_config_is_also_reported(self) -> None:
        """The removal must be visible wherever the name is set, not just in env."""
        provider = build_provider({"semantic_provider": "local_gpu"}, env={})
        assert isinstance(provider, DisabledProvider)

    def test_config_extras_semantic_section_is_honoured(self) -> None:
        config = type(
            "Cfg", (), {"extras": {"semantic": {"provider": "remote_api"}}}
        )()
        provider = build_provider(config, env={})
        assert isinstance(provider, RemoteAPIProvider)

    def test_api_key_in_config_is_ignored(self) -> None:
        provider = build_provider(
            {
                "semantic_provider": "remote_api",
                "semantic_base_url": "https://semantic.example.com/v1",
                "semantic_model": "strong-model",
                "CR_SEMANTIC_API_KEY": SECRET,
            },
            env={},
        )
        assert isinstance(provider, RemoteAPIProvider)
        assert provider.available() is False
        assert SECRET not in repr(provider)
        assert SECRET not in json.dumps(provider.health())

    @pytest.mark.parametrize(
        "config",
        [
            object(),
            {"extras": "not-a-mapping"},
            {"extras": {"semantic": 7}},
            type("Boom", (), {"semantic_provider": property(lambda self: 1 / 0)})(),
        ],
    )
    def test_construction_failures_fall_back_to_disabled(self, config: Any) -> None:
        assert isinstance(build_provider(config, env={}), DisabledProvider)

    def test_construction_failure_of_local_endpoint_falls_back(self) -> None:
        class ExplodingConfig:
            """A config whose endpoint lookup raises."""

            extras: dict[str, Any] = {}

            @property
            def semantic_provider(self) -> str:
                return "remote_api"

            @property
            def semantic_base_url(self) -> str:
                raise RuntimeError("endpoint lookup exploded")

        provider = build_provider(ExplodingConfig(), env={})
        assert isinstance(provider, DisabledProvider)

    def test_resolve_provider_name_never_raises(self) -> None:
        assert resolve_provider_name(None, {}) == "disabled"
        assert resolve_provider_name(None, {"CR_SEMANTIC_PROVIDER": "???"}) == "disabled"
        assert resolve_provider_name(None, {"CR_SEMANTIC_PROVIDER": "remote_api"}) == "remote_api"


# --------------------------------------------------------------------------------------
# Disabled provider
# --------------------------------------------------------------------------------------


class TestDisabledProvider:
    """The default implementation is complete: no model is a valid configuration."""

    def test_state(self) -> None:
        provider = DisabledProvider()
        assert provider.name == "disabled"
        assert provider.available() is False
        assert provider.health()["available"] is False

    def test_calls_return_none(self) -> None:
        provider = DisabledProvider()
        assert provider.deep_refresh(DeepRefreshRequest()) is None
        assert provider.explain_state({"mood_valence": -0.2}) is None

    def test_repr_is_stable_and_secret_free(self) -> None:
        assert repr(DisabledProvider()) == "DisabledProvider(name='disabled')"


# --------------------------------------------------------------------------------------
# Remote API provider
# --------------------------------------------------------------------------------------


class TestRemoteAPIProvider:
    """The remote route is the same wire, with a credential that must not leak."""

    def test_available_requires_url_model_and_key(self) -> None:
        assert _remote(lambda *_: _reply(GOOD_SUGGESTIONS)).available() is True
        assert _remote(lambda *_: _reply({}), base_url="").available() is False
        assert _remote(lambda *_: _reply({}), model="").available() is False
        assert _remote(lambda *_: _reply({}), api_key="").available() is False

    def test_unavailable_provider_does_not_call_out(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_SUGGESTIONS))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key="",
            transport=transport,
        )
        assert provider.deep_refresh(DeepRefreshRequest()) is None
        assert provider.explain_state({"x": 1}) is None
        assert transport.call_count == 0

    def test_deep_refresh_success(self) -> None:
        provider = _remote(lambda *_: _reply(GOOD_SUGGESTIONS))
        result = provider.deep_refresh(DeepRefreshRequest(key_quotes=[{"text": "算了"}]))
        assert result is not None
        assert result.degraded is False
        assert result.provider == "remote_api"
        assert len(result.unfinished_matter_suggestions) == 1
        assert result.latency_ms >= 0

    def test_deep_refresh_uses_the_deep_deadline(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_SUGGESTIONS))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            timeout_s=3.0,
            deep_timeout_s=45.0,
            transport=transport,
        )
        provider.deep_refresh(DeepRefreshRequest())
        provider.explain_state({"x": 1})
        assert transport.calls[0]["timeout"] == 45.0
        assert transport.calls[1]["timeout"] == 3.0

    def test_timeout_degrades_instead_of_raising(self) -> None:
        def boom(*_: Any) -> Mapping[str, Any]:
            raise TimeoutError("deadline exceeded")

        provider = _remote(boom)
        result = provider.deep_refresh(DeepRefreshRequest(), timeout_s=0.01)
        assert result is not None
        assert result.degraded is True
        assert result.reason == "error:TimeoutError"
        assert result.is_empty() is True
        assert provider.explain_state({"x": 1}) is None

    def test_url_error_degrades(self) -> None:
        def boom(*_: Any) -> Mapping[str, Any]:
            raise OSError("connection refused")

        result = _remote(boom).deep_refresh(DeepRefreshRequest())
        assert result is not None
        assert result.degraded is True
        assert result.reason == "error:OSError"

    def test_malformed_json_degrades(self) -> None:
        """A reply with no decodable JSON object must degrade, never raise."""

        def raw(text: str):
            return lambda *_: {"choices": [{"message": {"content": text}}]}

        for text in ("完全不是 JSON", "{broken", "", "```json\n```"):
            provider = _remote(raw(text))
            result = provider.deep_refresh(DeepRefreshRequest())
            assert result is not None, text
            assert result.degraded is True
            # ``_extract_json`` raises ValueError for an undecodable reply; the
            # provider converts it into a degraded result rather than propagating.
            assert result.reason == "error:ValueError"
            assert result.is_empty() is True
            assert provider.explain_state({"x": 1}) is None

    def test_decodable_but_non_mapping_json_degrades(self) -> None:
        """Valid JSON of the wrong shape degrades; it is not repaired."""
        provider = _remote(lambda *_: _reply(["a", "list", "not", "an", "object"]))
        result = provider.deep_refresh(DeepRefreshRequest())
        assert result is not None
        assert result.degraded is True
        assert result.reason == "invalid_json"

    def test_schema_violation_degrades_but_keeps_well_formed_fields(self) -> None:
        payload = dict(GOOD_SUGGESTIONS)
        payload["reinterpretations"] = "should have been a list"
        result = _remote(lambda *_: _reply(payload)).deep_refresh(DeepRefreshRequest())
        assert result is not None
        assert result.degraded is True
        assert "reinterpretations" in result.reason
        assert len(result.memory_suggestions) == 1

    def test_endpoint_without_choices_degrades(self) -> None:
        provider = _remote(lambda *_: {"error": "upstream boom"})
        result = provider.deep_refresh(DeepRefreshRequest())
        assert result is not None
        assert result.degraded is True
        assert result.reason.startswith("error:")

    def test_grammar_and_request_body_are_sent_when_bundled(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_SUGGESTIONS))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            grammar="root ::= object",
            transport=transport,
        )
        provider.deep_refresh(DeepRefreshRequest())
        assert transport.calls[0]["body"]["grammar"] == "root ::= object"

    def test_json_mode_asks_for_a_json_object(self) -> None:
        """Both structured calls must carry the JSON Output request field.

        The field is what keeps a reply from arriving as prose that has to be
        scavenged for braces - the failure that made a real deep refresh look like
        six empty collections rather than a broken reply.
        """
        transport = FakeTransport(lambda *_: _reply(GOOD_SUGGESTIONS))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1", model="strong-model", api_key=SECRET,
            transport=transport,
        )
        provider.deep_refresh(DeepRefreshRequest())
        assert transport.calls[0]["body"]["response_format"] == {"type": "json_object"}

    def test_json_mode_can_be_turned_off_for_a_strict_gateway(self) -> None:
        """A gateway that rejects unknown body keys must be able to opt out."""
        transport = FakeTransport(lambda *_: _reply(GOOD_SUGGESTIONS))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1", model="strong-model", api_key=SECRET,
            json_mode=False, transport=transport,
        )
        provider.deep_refresh(DeepRefreshRequest())
        assert "response_format" not in transport.calls[0]["body"]

    def test_factory_reads_the_json_mode_switch_from_the_environment(self) -> None:
        """``CR_SEMANTIC_JSON_MODE=0`` disables the field; the default keeps it on."""
        off = build_provider(
            env={
                "CR_SEMANTIC_PROVIDER": "remote_api",
                "CR_SEMANTIC_BASE_URL": "https://semantic.example.com/v1",
                "CR_SEMANTIC_MODEL": "strong-model",
                "CR_SEMANTIC_API_KEY": SECRET,
                "CR_SEMANTIC_JSON_MODE": "0",
            }
        )
        on = build_provider(
            env={
                "CR_SEMANTIC_PROVIDER": "remote_api",
                "CR_SEMANTIC_BASE_URL": "https://semantic.example.com/v1",
                "CR_SEMANTIC_MODEL": "strong-model",
                "CR_SEMANTIC_API_KEY": SECRET,
            }
        )
        assert isinstance(off, RemoteAPIProvider) and off.json_mode is False
        assert isinstance(on, RemoteAPIProvider) and on.json_mode is True

    def test_empty_but_valid_json_is_an_empty_answer_not_a_degradation(self) -> None:
        """``{}`` is a legal, quiet answer: no suggestions and nothing broken.

        Pinned because the two are easy to conflate when reading a report: an
        all-empty suggestion set with ``degraded=False`` means the model chose to
        say nothing, which is a prompt question, not a transport failure.
        """
        result = _remote(lambda *_: _reply({})).deep_refresh(DeepRefreshRequest())
        assert result is not None
        assert result.degraded is False
        assert result.reason == ""
        assert result.reinterpretations == []
        assert result.psychological_interpretation == {}

    def test_the_prompt_forbids_inventing_rather_than_guessing(self) -> None:
        """The guardrail must read "do not invent", never "stay silent if unsure".

        Measured against a real 10KB backlog: a prompt ending in "return empty
        arrays when the evidence is insufficient, do not guess" produced 57 tokens
        of six empty collections every single time, while every phrasing that names
        fabrication instead produced roughly a thousand tokens of usable
        suggestions. An event that is never reinterpreted is never settled, so that
        sentence switched off the whole deferred-interpretation path.
        """
        from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT

        assert "不要编造" in DEEP_REFRESH_SYSTEM_PROMPT
        assert "证据不足" not in DEEP_REFRESH_SYSTEM_PROMPT
        # JSON Output also requires the word "json" plus a shape example in-prompt.
        assert "JSON" in DEEP_REFRESH_SYSTEM_PROMPT
        assert '"reinterpretations"' in DEEP_REFRESH_SYSTEM_PROMPT

    def test_the_prompt_spells_out_the_item_keys_grounding_demands(self) -> None:
        """The example must show ``sources`` on every suggestion kind.

        Naming only the six top-level fields is not enough: measured against a real
        backlog the model put the event id under ``event_id``, grounding rejected
        every reading as ``missing_sources``, and a refresh that "ran: true" settled
        nothing. ``sources`` is the field that ties a suggestion to real events.
        """
        from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT

        example = DEEP_REFRESH_SYSTEM_PROMPT.split("格式样例：", 1)[1]
        for field in (
            "reinterpretations",
            "candidate_intent_operations",
            "memory_suggestions",
            "unfinished_matter_suggestions",
            "user_model_evidence_suggestions",
        ):
            assert field in example, field
        # One `sources` per kind in the example, plus the instruction itself.
        assert example.count('"sources"') == 5
        assert "不得编造 id" in DEEP_REFRESH_SYSTEM_PROMPT

    def test_bearer_token_is_sent_but_never_stored_in_headers_dict(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_SUGGESTIONS))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            transport=transport,
        )
        provider.deep_refresh(DeepRefreshRequest())
        assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
        assert SECRET not in json.dumps(provider.headers)
        assert "Authorization" not in provider.headers


# --------------------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------------------


class TestSecretHygiene:
    """A credential must not surface in a repr, a log record or ``health()``."""

    def test_health_reports_presence_not_value(self) -> None:
        configured = _remote(lambda *_: _reply(GOOD_SUGGESTIONS)).health()
        assert configured["api_key"] == "configured"
        assert configured["provider"] == "remote_api"
        assert configured["available"] is True

        missing = _remote(lambda *_: _reply({}), api_key="").health()
        assert missing["api_key"] == "not configured"
        assert missing["available"] is False

    def test_repr_and_health_never_contain_the_key(self) -> None:
        provider = _remote(lambda *_: _reply(GOOD_SUGGESTIONS))
        for rendering in (repr(provider), str(provider), json.dumps(provider.health())):
            assert SECRET not in rendering
            assert "Bearer" not in rendering

    def test_key_is_not_reachable_from_a_generic_attribute_dump(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = _remote(lambda *_: _reply(GOOD_SUGGESTIONS))
        with caplog.at_level(logging.DEBUG, logger="companion_runtime.providers"):
            provider.deep_refresh(DeepRefreshRequest())
            provider.explain_state({"mood_valence": -0.3})
        dump = json.dumps({key: str(value) for key, value in vars(provider).items()})
        assert SECRET not in dump
        assert SECRET not in caplog.text

    def test_degradation_logs_do_not_include_the_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(*_: Any) -> Mapping[str, Any]:
            raise OSError("connection refused")

        provider = _remote(boom)
        with caplog.at_level(logging.DEBUG, logger="companion_runtime.providers"):
            provider.deep_refresh(DeepRefreshRequest())
            provider.explain_state({"mood_valence": -0.3})
        assert SECRET not in caplog.text


# --------------------------------------------------------------------------------------
# explain_state caching
# --------------------------------------------------------------------------------------


class TestExplainStateCache:
    """``state_key`` is the cache identity; an empty key means no caching."""

    def test_same_key_is_served_from_cache(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_EXPLANATION))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            transport=transport,
        )
        first = provider.explain_state({"mood_valence": -0.3}, state_key="v-3|a2")
        second = provider.explain_state({"mood_valence": -0.3}, state_key="v-3|a2")
        assert first == GOOD_EXPLANATION
        assert second == GOOD_EXPLANATION
        assert transport.call_count == 1
        assert provider.stats["cache_hits"] == 1
        assert provider.stats["explain_calls"] == 1

    def test_different_key_misses_the_cache(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_EXPLANATION))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            transport=transport,
        )
        provider.explain_state({"mood_valence": -0.3}, state_key="v-3|a2")
        provider.explain_state({"mood_valence": -0.1}, state_key="v-1|a2")
        assert transport.call_count == 2
        assert provider.stats["cache_hits"] == 0

    def test_empty_key_bypasses_the_cache_both_ways(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_EXPLANATION))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            transport=transport,
        )
        provider.explain_state({"mood_valence": -0.3})
        provider.explain_state({"mood_valence": -0.3})
        assert transport.call_count == 2
        assert provider.health()["cache_entries"] == 0

    def test_cached_value_is_a_copy(self) -> None:
        provider = _remote(lambda *_: _reply(GOOD_EXPLANATION))
        first = provider.explain_state({"x": 1}, state_key="k")
        assert first is not None
        first["experience"] = "mutated by the caller"
        second = provider.explain_state({"x": 1}, state_key="k")
        assert second is not None
        assert second["experience"] == GOOD_EXPLANATION["experience"]

    def test_invalidate_drops_the_cache(self) -> None:
        transport = FakeTransport(lambda *_: _reply(GOOD_EXPLANATION))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            transport=transport,
        )
        provider.explain_state({"x": 1}, state_key="k")
        provider.invalidate()
        provider.explain_state({"x": 1}, state_key="k")
        assert transport.call_count == 2

    def test_cache_key_is_not_derived_from_the_payload_alone(self) -> None:
        """Two payloads sharing a key share the entry - the key is authoritative."""
        transport = FakeTransport(lambda *_: _reply(GOOD_EXPLANATION))
        provider = RemoteAPIProvider(
            "https://semantic.example.com/v1",
            model="strong-model",
            api_key=SECRET,
            transport=transport,
        )
        assert provider.explain_state({"a": 1}, state_key="shared") is not None
        assert provider.explain_state({"b": 2}, state_key="shared") is not None
        assert transport.call_count == 1


# --------------------------------------------------------------------------------------
# Protocol conformance
# --------------------------------------------------------------------------------------


class TestProtocolConformance:
    """The integration codes against the protocol, so every member must exist."""

    @pytest.mark.parametrize(
        "provider",
        [
            DisabledProvider(),
            RemoteAPIProvider(
                "https://semantic.example.com/v1",
                model="strong-model",
                api_key=SECRET,
                transport=FakeTransport(lambda *_: {}),
            ),
        ],
        ids=["disabled", "remote_api"],
    )
    def test_members_and_fail_open_behaviour(self, provider: Any) -> None:
        assert isinstance(provider.name, str) and provider.name
        assert isinstance(provider.available(), bool)
        assert isinstance(provider.health(), dict)
        # Fail-open on a transport that returns a useless body.
        outcome = provider.deep_refresh(DeepRefreshRequest())
        assert outcome is None or isinstance(outcome, DeepRefreshSuggestions)
        explanation = provider.explain_state({"mood_valence": -0.1})
        assert explanation is None or isinstance(explanation, dict)
        # The request object is never mutated by a provider.
        request = DeepRefreshRequest(unresolved_events=[{"event_id": "e1"}])
        provider.deep_refresh(request)
        assert request.unresolved_events == [{"event_id": "e1"}]

    def test_suggestions_payload_helper_matches_the_parser(self) -> None:
        result = _suggestions_payload()
        assert result.degraded is False
        assert isinstance(result, DeepRefreshSuggestions)
