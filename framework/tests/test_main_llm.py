"""Tests for the acting layer: the OpenAI-compatible main LLM client.

These are pure -- the transport is injected, so nothing here touches a network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cf.logbook import Logbook
from cf.main_llm import (
    RENDER_MARKER,
    ScriptedMainLLM,
    _first_message_text,
    _render_from_prompt,
    MainLLMError,
    OpenAICompatibleMainLLM,
)

DEEPSEEK = "https://api.example.com/v1"


class FakeTransport:
    """Records requests and replays canned responses."""

    def __init__(self, replies: list[object] | None = None) -> None:
        """Store the replies to replay."""
        self.replies = list(replies or [])
        self.requests: list[dict] = []

    def __call__(self, url, body, timeout, headers):
        """Record one request and return the next reply."""
        self.requests.append({"url": url, "body": body, "timeout": timeout, "headers": headers})
        if not self.replies:
            return {"choices": [{"message": {"role": "assistant", "content": "嗯。"}}]}
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def generate(client: OpenAICompatibleMainLLM, prompt: str, session: str = "s") -> str:
    """Run one generate call to completion."""
    return asyncio.run(client.generate(provider_id="p::s", prompt=prompt, session=session))


def completion(text: str) -> dict:
    """Build an OpenAI-shaped completion response."""
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class TestRequestShape:
    """The endpoint has to receive a standard OpenAI request."""

    def test_posts_to_chat_completions(self) -> None:
        """The URL is ``{base_url}/chat/completions``."""
        transport = FakeTransport()
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", transport=transport)
        generate(client, "你好")
        assert transport.requests[0]["url"] == f"{DEEPSEEK}/chat/completions"

    def test_body_is_a_chat_completion(self) -> None:
        """System prompt, user prompt, model and the non-streaming flag."""
        transport = FakeTransport()
        client = OpenAICompatibleMainLLM(
            DEEPSEEK, model="deepseek-chat", system_prompt="你是小九九", temperature=0.5, transport=transport
        )
        generate(client, "在吗")
        body = transport.requests[0]["body"]
        assert body["model"] == "deepseek-chat"
        assert body["messages"] == [
            {"role": "system", "content": "你是小九九"},
            {"role": "user", "content": "在吗"},
        ]
        assert body["temperature"] == 0.5
        assert body["stream"] is False

    def test_trailing_slash_is_normalised(self) -> None:
        """A base URL with a trailing slash does not produce a doubled slash."""
        transport = FakeTransport()
        client = OpenAICompatibleMainLLM("https://api.example.com/v1/", model="m", transport=transport)
        generate(client, "hi")
        assert transport.requests[0]["url"] == "https://api.example.com/v1/chat/completions"

    def test_bearer_token_is_sent_when_configured(self) -> None:
        """The Authorization header carries the key."""
        transport = FakeTransport()
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", api_key="secret-key", transport=transport)
        generate(client, "hi")
        assert transport.requests[0]["headers"]["Authorization"] == "Bearer secret-key"

    def test_no_authorization_header_without_a_key(self) -> None:
        """An unprotected endpoint is called without an Authorization header."""
        transport = FakeTransport()
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", api_key="", transport=transport)
        generate(client, "hi")
        assert "Authorization" not in transport.requests[0]["headers"]


class TestKeyHygiene:
    """The acting layer logs everything else, so the key must not ride along."""

    def test_key_never_appears_in_the_trace(self, tmp_path) -> None:
        """A full call leaves the key out of the trace file."""
        book = Logbook(tmp_path, echo=False)
        transport = FakeTransport([completion("你好呀")])
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", api_key="sk-topsecret", logbook=book, transport=transport)
        generate(client, "在吗")
        book.close()
        assert "sk-topsecret" not in book.trace_path.read_text(encoding="utf-8")

    def test_describe_reports_presence_only(self) -> None:
        """``describe`` says whether a key exists, never what it is."""
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", api_key="sk-topsecret")
        described = client.describe()
        assert described["api_key"] == "configured"
        assert "sk-topsecret" not in json.dumps(described)

    def test_key_is_read_from_the_environment(self, monkeypatch) -> None:
        """``from_env`` picks the key up without it being an argument."""
        monkeypatch.setenv("CF_MAIN_LLM_API_KEY", "sk-fromenv")
        monkeypatch.setenv("CF_MAIN_LLM_BASE_URL", DEEPSEEK)
        monkeypatch.setenv("CF_MAIN_LLM_MODEL", "m")
        client = OpenAICompatibleMainLLM.from_env()
        assert client.configured is True
        assert client.key_present is True


class TestDegradation:
    """A dead endpoint must not take the chat window with it."""

    def test_transport_failure_returns_empty_text(self) -> None:
        """An unreachable endpoint yields an empty reply, not an exception."""
        transport = FakeTransport([ConnectionError("boom")])
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", transport=transport)
        assert generate(client, "hi") == ""

    def test_failure_is_recorded_with_its_reason(self, tmp_path) -> None:
        """The trace says why the call produced nothing."""
        book = Logbook(tmp_path, echo=False)
        transport = FakeTransport([ConnectionError("boom")])
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", logbook=book, transport=transport)
        generate(client, "hi")
        book.close()
        record = book.read_trace(kinds=("main_llm_call",))[-1]
        assert record["error"].startswith("ConnectionError")
        assert record["reply_chars"] == 0

    def test_missing_choices_is_an_error(self) -> None:
        """A malformed response body is named, not a KeyError from a dict lookup."""
        with pytest.raises(MainLLMError):
            _first_message_text({"nothing": "here"})

    def test_non_text_content_is_an_error(self) -> None:
        """A non-string content is refused."""
        with pytest.raises(MainLLMError):
            _first_message_text({"choices": [{"message": {"content": 42}}]})


class TestCallKinds:
    """The two paths through the acting layer are labelled apart in the trace."""

    def test_render_is_detected_by_the_intent_line(self) -> None:
        """A prompt carrying ``- 想做的事：`` is a proactive render."""
        assert _render_from_prompt(f"{RENDER_MARKER}问他面试结果") != ""

    def test_reply_and_render_are_recorded_separately(self, tmp_path) -> None:
        """A user turn and a render are distinguishable in the trace."""
        book = Logbook(tmp_path, echo=False)
        transport = FakeTransport([completion("在的"), completion("面试怎么样？")])
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", logbook=book, transport=transport)
        generate(client, "在吗")
        generate(client, f"{RENDER_MARKER}问他面试结果")
        book.close()
        kinds = [r["llm_call_kind"] for r in book.read_trace(kinds=("main_llm_call",))]
        assert kinds == ["reply", "render"]
        assert client.stats()["by_kind"] == {"reply": 1, "render": 1}

    def test_prompt_and_reply_are_both_kept(self, tmp_path) -> None:
        """The trace keeps the full prompt -- without it there is no way to tell
        "the Runtime injected nothing" from "the model ignored what it injected"."""
        book = Logbook(tmp_path, echo=False)
        transport = FakeTransport([completion("好的")])
        client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", logbook=book, transport=transport)
        generate(client, "用户原话\n\n<companion_runtime_context>x</companion_runtime_context>")
        book.close()
        record = book.read_trace(kinds=("main_llm_call",))[-1]
        assert "companion_runtime_context" in record["prompt"]
        assert record["reply"] == "好的"


class TestScriptedStandIn:
    """The no-endpoint mode must be usable but never mistakable for a model."""

    def test_render_answer_comes_from_the_intent_line(self) -> None:
        """A render prompt produces a message built from the Runtime's own intent."""
        assert "面试结果" in _render_from_prompt(f"{RENDER_MARKER}问他面试结果")

    def test_render_without_an_intent_line_still_answers(self) -> None:
        """A render prompt with no usable intent yields something rather than ''."""
        assert _render_from_prompt(f"{RENDER_MARKER}") != ""

    def test_scripted_replies_are_consumed_in_order(self) -> None:
        """Supplied replies are used first."""
        llm = ScriptedMainLLM(replies=["第一句", "第二句"])
        assert asyncio.run(llm.generate(provider_id="p", prompt="a", session="s")) == "第一句"
        assert asyncio.run(llm.generate(provider_id="p", prompt="b", session="s")) == "第二句"

    def test_it_admits_it_is_not_configured(self) -> None:
        """``configured`` is false and the description says so in words."""
        llm = ScriptedMainLLM()
        assert llm.configured is False
        assert llm.describe()["configured"] is False
        assert "替身" in llm.describe()["note"]

    def test_stats_mirror_the_real_client(self) -> None:
        """``stats`` has the same shape, so the banner and teardown do not branch."""
        llm = ScriptedMainLLM()
        asyncio.run(llm.generate(provider_id="p", prompt="a", session="s"))
        asyncio.run(llm.generate(provider_id="p", prompt=f"{RENDER_MARKER}x", session="s"))
        assert llm.stats()["by_kind"] == {"reply": 1, "render": 1}
