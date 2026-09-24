"""Tests for the OpenAI-compatible endpoint that feeds the program's strong semantics.

These run without the program: the endpoint is exercised as a plain HTTP service,
which is how the program sees it too.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from cf.logbook import Logbook
from cf.mock_openai import (
    DEEP_REFRESH_FIELDS,
    EXPLANATION_FIELDS,
    KIND_DEEP_REFRESH,
    KIND_EXPLAIN,
    KIND_UNKNOWN,
    MockOpenAIServer,
    MockReply,
    MockScript,
    build_deep_refresh_payload,
    build_explanation_payload,
    classify_prompt,
)

DEEP_SYSTEM = "你是长期陪伴角色的深层认知整理器，只在低频的后台刷新中被调用。"
EXPLAIN_SYSTEM = "你是长期陪伴角色的情绪解释器：把已有的结构化心理状态翻译成第一人称心理语言。"


def post(url: str, payload: dict, *, token: str | None = "test") -> tuple[int, dict]:
    """POST JSON and return ``(status, decoded)``, tolerating error statuses."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def completion(url: str, system: str, body: dict, **kwargs) -> tuple[int, dict]:
    """Send one chat completion in the shape the program sends."""
    return post(
        f"{url}/chat/completions",
        {
            "model": "framework-mock",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
            ],
            "temperature": 0.0,
            "max_tokens": 1024,
            "stream": False,
        },
        **kwargs,
    )


@pytest.fixture()
def server(tmp_path):
    """A running mock endpoint with its own logbook."""
    book = Logbook(tmp_path, echo=False)
    instance = MockOpenAIServer(book)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()
        book.close()


class TestPromptRouting:
    """The two contracts share one wire and are told apart by the system prompt."""

    def test_classifies_both_contracts(self) -> None:
        """Each real prompt is routed to its own reply builder."""
        assert classify_prompt(DEEP_SYSTEM) == KIND_DEEP_REFRESH
        assert classify_prompt(EXPLAIN_SYSTEM) == KIND_EXPLAIN
        assert classify_prompt("something else entirely") == KIND_UNKNOWN

    def test_deep_refresh_reply_has_all_six_fields(self, server) -> None:
        """A deep-refresh answer carries every field the parser looks for."""
        status, response = completion(server.base_url, DEEP_SYSTEM, {"unresolved_events": []})
        assert status == 200
        content = json.loads(response["choices"][0]["message"]["content"])
        assert set(content) == set(DEEP_REFRESH_FIELDS)

    def test_explanation_reply_has_all_six_fields(self, server) -> None:
        """An explanation answer carries the six short strings, within the limit."""
        status, response = completion(server.base_url, EXPLAIN_SYSTEM, {"mood": {"valence": 0.5}})
        assert status == 200
        content = json.loads(response["choices"][0]["message"]["content"])
        assert set(content) == set(EXPLANATION_FIELDS)
        for field in EXPLANATION_FIELDS:
            assert isinstance(content[field], str)
            assert len(content[field]) <= 120, field


class TestGrounding:
    """A suggestion that cites nothing real is thrown away by the program."""

    def test_sources_come_from_the_request(self) -> None:
        """Every emitted operation cites an event id the caller actually sent."""
        payload = build_deep_refresh_payload(
            {"unresolved_events": [{"event_id": "evt_a"}, {"event_id": "evt_b"}], "mood": {}, "candidates": []}
        )
        cited = {source for item in payload["reinterpretations"] for source in item["sources"]}
        assert cited == {"evt_a", "evt_b"}
        assert all(source in {"evt_a", "evt_b"} for source in cited)

    def test_candidate_operation_shape_matches_the_pool_contract(self) -> None:
        """The candidate operation uses ``op`` and repeats ``sources`` inside the candidate.

        Both details are load-bearing and both were wrong in the first version:
        the pool reads ``op`` (not ``operation``), and ``validate_candidate``
        rejects a candidate whose own ``sources`` is empty.
        """
        payload = build_deep_refresh_payload(
            {"unresolved_events": [{"event_id": "evt_a"}], "mood": {}, "candidates": []}
        )
        operation = payload["candidate_intent_operations"][0]
        assert operation["sources"] == ["evt_a"]
        body = operation["payload"]
        assert body["op"] in {"add", "update", "retire", "reinterpret"}
        assert body["candidate"]["sources"] == ["evt_a"]
        assert body["candidate"]["type"] in {
            "contact",
            "check_in",
            "follow_up",
            "curious_question",
            "share",
            "repair",
            "reply",
        }

    def test_no_unresolved_events_means_no_invented_operations(self) -> None:
        """With nothing to reason about, the mock proposes nothing.

        The alternative -- emitting a plausible-sounding operation anyway --
        would make the framework manufacture evidence, which is exactly what the
        program's grounding pass exists to prevent.
        """
        payload = build_deep_refresh_payload({"unresolved_events": [], "mood": {}, "candidates": []})
        assert payload["reinterpretations"] == []
        assert payload["candidate_intent_operations"] == []
        assert payload["memory_suggestions"] == []

    def test_existing_candidates_suppress_the_add(self) -> None:
        """A pool that already has something does not get a duplicate proposal."""
        payload = build_deep_refresh_payload(
            {
                "unresolved_events": [{"event_id": "evt_a"}],
                "mood": {},
                "candidates": [{"candidate_id": "cnd_1"}],
            }
        )
        assert payload["candidate_intent_operations"] == []

    def test_interpretation_reflects_the_mood_it_was_given(self) -> None:
        """The reply is derived from the request, not constant.

        This is the property that makes the log readable: the mock's answer can be
        traced back to the numbers the program sent.
        """
        calm = build_deep_refresh_payload({"mood": {"valence": 0.5, "pressure": 0.0}})
        tense = build_deep_refresh_payload({"mood": {"valence": -0.5, "pressure": 0.9, "restraint": 0.1}})
        assert calm["psychological_interpretation"]["experience"] != tense["psychological_interpretation"]["experience"]

    def test_explanation_is_non_empty(self) -> None:
        """At least one explanation field must be filled or the parser rejects it."""
        payload = build_explanation_payload({"mood": {"valence": 0.0}})
        assert any(payload.values())


class TestScripting:
    """The endpoint has to be able to fail on purpose."""

    def test_scripted_payload_wins(self, server) -> None:
        """A scripted reply replaces the grounded one."""
        server.script = MockScript([MockReply(payload={"reinterpretations": [], "note": "scripted"})])
        _, response = completion(server.base_url, DEEP_SYSTEM, {"unresolved_events": [{"event_id": "evt_a"}]})
        assert json.loads(response["choices"][0]["message"]["content"])["note"] == "scripted"

    def test_http_error_is_returned(self, server) -> None:
        """A scripted 500 is returned as a 500, so the provider's degrade path runs."""
        server.script = MockScript([MockReply.http_error(500)])
        status, _ = completion(server.base_url, DEEP_SYSTEM, {})
        assert status == 500

    def test_malformed_json_is_returned_verbatim(self, server) -> None:
        """Non-JSON content goes out untouched, to exercise the extractor."""
        server.script = MockScript([MockReply.malformed("这不是 JSON。")])
        _, response = completion(server.base_url, DEEP_SYSTEM, {})
        assert response["choices"][0]["message"]["content"] == "这不是 JSON。"

    def test_script_advances_and_repeats_last(self, server) -> None:
        """Replies are consumed in order and the last one sticks."""
        server.script = MockScript([MockReply(payload={"n": 1}), MockReply(payload={"n": 2})])
        seen = [
            json.loads(completion(server.base_url, DEEP_SYSTEM, {})[1]["choices"][0]["message"]["content"])["n"]
            for _ in range(3)
        ]
        assert seen == [1, 2, 2]

    def test_repeat_last_false_falls_back_to_grounded(self, server) -> None:
        """With ``repeat_last`` off, the script runs out and normal service resumes."""
        server.script = MockScript([MockReply(payload={"n": 1})], repeat_last=False)
        first = json.loads(completion(server.base_url, DEEP_SYSTEM, {})[1]["choices"][0]["message"]["content"])
        second = json.loads(completion(server.base_url, DEEP_SYSTEM, {})[1]["choices"][0]["message"]["content"])
        assert first["n"] == 1
        assert set(second) == set(DEEP_REFRESH_FIELDS)


class TestSurface:
    """Standard OpenAI shape, and secret hygiene."""

    def test_models_endpoint(self, server) -> None:
        """``/v1/models`` lists the advertised model."""
        with urllib.request.urlopen(f"{server.base_url}/models", timeout=5) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
        assert body["object"] == "list"
        assert body["data"][0]["id"] == "framework-mock"

    def test_response_looks_like_a_chat_completion(self, server) -> None:
        """The envelope has the fields the program's extractor reads."""
        _, response = completion(server.base_url, DEEP_SYSTEM, {})
        assert response["object"] == "chat.completion"
        choice = response["choices"][0]
        assert choice["message"]["role"] == "assistant"
        assert isinstance(choice["message"]["content"], str)
        assert "usage" in response

    def test_unknown_path_is_404(self, server) -> None:
        """Anything else is a clean 404, not a hang."""
        request = urllib.request.Request(f"{server.base_url}/embeddings", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)  # noqa: S310
        assert excinfo.value.code == 404

    def test_require_auth_rejects_missing_token(self, tmp_path) -> None:
        """When auth is required, a tokenless call is refused."""
        book = Logbook(tmp_path, echo=False)
        instance = MockOpenAIServer(book, require_auth=True)
        instance.start()
        try:
            status, _ = completion(instance.base_url, DEEP_SYSTEM, {}, token=None)
            assert status == 401
            status_ok, _ = completion(instance.base_url, DEEP_SYSTEM, {}, token="anything")
            assert status_ok == 200
        finally:
            instance.stop()
            book.close()

    def test_token_value_never_reaches_the_log(self, server) -> None:
        """Only the presence of a bearer token is recorded, never its value.

        The framework logs a great deal, and a log is exactly where a credential
        leaks. The program keeps its key out of every dump; the mock has to hold
        the same line.
        """
        completion(server.base_url, DEEP_SYSTEM, {}, token="sk-super-secret-value")
        trace_text = (server.logbook.trace_path).read_text(encoding="utf-8")
        assert "sk-super-secret-value" not in trace_text
        call = server.logbook.read_trace(kinds=("mock_openai_call",))[-1]
        assert call["auth_present"] is True

    def test_call_records_the_variables_the_program_sent(self, server) -> None:
        """The trace keeps the request, which is what makes the log diagnostic."""
        completion(
            server.base_url,
            DEEP_SYSTEM,
            {"unresolved_events": [{"event_id": "evt_a"}], "mood": {"pressure": 0.75}},
        )
        call = server.logbook.read_trace(kinds=("mock_openai_call",))[-1]
        assert call["prompt_kind"] == KIND_DEEP_REFRESH
        assert call["request"]["mood"]["pressure"] == 0.75
        assert call["request"]["unresolved_events"][0]["event_id"] == "evt_a"
        assert call["reply"]["reinterpretations"][0]["sources"] == ["evt_a"]

    def test_stats_counts_by_prompt_kind(self, server) -> None:
        """The stats block separates deep refreshes from explanations."""
        completion(server.base_url, DEEP_SYSTEM, {})
        completion(server.base_url, EXPLAIN_SYSTEM, {})
        completion(server.base_url, EXPLAIN_SYSTEM, {})
        assert server.stats()["by_kind"] == {KIND_DEEP_REFRESH: 1, KIND_EXPLAIN: 2}
