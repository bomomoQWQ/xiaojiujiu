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
    sse_content,
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


class StreamTransport:
    """Replays an SSE stream, and records the request body."""

    def __init__(self, chunks: list[str], *, fail: bool = False) -> None:
        """Store the fragments to emit."""
        self.chunks = list(chunks)
        self.fail = fail
        self.requests: list[dict] = []

    def __call__(self, url, body, timeout, headers):
        """Not used: streaming goes through ``stream``."""
        raise AssertionError("the streaming tests must use the stream transport")

    def stream(self, url, body, timeout, headers):
        """Yield content fragments, which is what ``_transport_stream`` yields.

        Not SSE frames: the wire format is parsed inside the real transport, and
        substituting a fake that emitted frames would be testing a different
        contract than the one the client depends on.
        """
        self.requests.append({"url": url, "body": body, "headers": headers})
        if self.fail:
            raise ConnectionError("stream refused")
        for piece in self.chunks:
            if piece is None:
                return
            yield piece


def streamed_client(transport, **kwargs):
    """Build a client whose streaming transport is the fake."""
    client = OpenAICompatibleMainLLM(DEEPSEEK, model="m", **kwargs)
    client._transport_stream = transport.stream
    return client


class TestStreaming:
    """Showing the answer while it is still being written."""

    def test_fragments_arrive_in_order(self) -> None:
        """Every fragment reaches the callback, and the return value is the whole text."""
        transport = StreamTransport(["你", "好", "呀", None])
        client = streamed_client(transport)
        seen: list[str] = []
        text = asyncio.run(
            client.generate(provider_id="p", prompt="hi", session="s", on_delta=seen.append)
        )
        assert seen == ["你", "好", "呀"]
        assert text == "你好呀"

    def test_streaming_is_requested_only_when_someone_is_watching(self) -> None:
        """No callback means a plain request: a render has no audience."""
        transport = StreamTransport(["x", None])
        client = streamed_client(transport)
        asyncio.run(client.generate(provider_id="p", prompt="hi", session="s"))
        assert transport.requests == [], "stream was requested with no observer"

    def test_the_body_asks_for_a_stream(self) -> None:
        """``stream: true`` goes on the wire."""
        transport = StreamTransport(["x", None])
        client = streamed_client(transport)
        asyncio.run(client.generate(provider_id="p", prompt="hi", session="s", on_delta=lambda _p: None))
        assert transport.requests[0]["body"]["stream"] is True

    def test_time_to_first_token_is_recorded(self, tmp_path) -> None:
        """TTFB is the number that decides whether a chat feels responsive."""
        book = Logbook(tmp_path, echo=False)
        transport = StreamTransport(["x", "y", None])
        client = streamed_client(transport, logbook=book)
        asyncio.run(client.generate(provider_id="p", prompt="hi", session="s", on_delta=lambda _p: None))
        book.close()
        record = book.read_trace(kinds=("main_llm_call",))[-1]
        assert record["streamed"] is True
        assert isinstance(record["ttfb_ms"], int)
        assert record["ttfb_ms"] <= record["latency_ms"]

    def test_a_failing_callback_does_not_lose_the_reply(self) -> None:
        """A display bug must not cost the answer."""
        transport = StreamTransport(["好", "的", None])
        client = streamed_client(transport)

        def explode(_piece: str) -> None:
            raise RuntimeError("display broke")

        text = asyncio.run(
            client.generate(provider_id="p", prompt="hi", session="s", on_delta=explode)
        )
        assert text == "好的"

    def test_a_failed_stream_falls_back_to_a_plain_request(self) -> None:
        """A gateway that refuses ``stream=true`` must not cost the answer.

        The fallback keeps the plumbing honest: an endpoint that cannot stream is
        still usable, it just arrives all at once.
        """
        failing = StreamTransport([], fail=True)
        client = streamed_client(failing)
        client._transport = FakeTransport([completion("整条回复")])
        seen: list[str] = []
        text = asyncio.run(
            client.generate(provider_id="p", prompt="hi", session="s", on_delta=seen.append)
        )
        assert text == "整条回复"
        assert seen == ["整条回复"], "the fallback must still hand the text to the display"
        assert client.stream_fallbacks == 1

    def test_an_empty_stream_also_falls_back(self) -> None:
        """A stream that yields nothing usable is retried, not reported as silence."""
        client = streamed_client(StreamTransport([None]))
        client._transport = FakeTransport([completion("重试得到的回复")])
        text = asyncio.run(client.generate(provider_id="p", prompt="hi", session="s", on_delta=lambda _p: None))
        assert text == "重试得到的回复"

    def test_malformed_frames_are_skipped(self) -> None:
        """A keep-alive or a truncated frame mid-stream is not fatal."""
        pieces = list(
            sse_content(
                [
                    "data: {not json",
                    ": keep-alive comment",
                    "",
                    'data: {"choices": [{"delta": {"content": "好"}}]}',
                    'data: {"choices": [{"delta": {}}]}',
                    'data: {"choices": []}',
                    "data: 42",
                    'data: {"choices": [{"delta": {"content": "的"}}]}',
                    "data: [DONE]",
                    'data: {"choices": [{"delta": {"content": "after-done"}}]}',
                ]
            )
        )
        assert pieces == ["好", "的"], pieces

    def test_a_stream_with_no_content_yields_nothing(self) -> None:
        """An immediately-terminated stream is empty, not an error."""
        assert list(sse_content(["data: [DONE]"])) == []

    def test_frames_without_a_trailing_newline_still_parse(self) -> None:
        """The reader tolerates lines with and without their terminator."""
        assert list(sse_content(['data: {"choices":[{"delta":{"content":"x"}}]}'])) == ["x"]

    def test_stats_count_streamed_calls(self) -> None:
        """The stats block says how many calls actually streamed."""
        transport = StreamTransport(["x", None])
        client = streamed_client(transport)
        asyncio.run(client.generate(provider_id="p", prompt="a", session="s", on_delta=lambda _p: None))
        stats = client.stats()
        assert stats["streamed"] == 1
        assert stats["stream_fallbacks"] == 0


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

    def test_it_honours_on_delta(self) -> None:
        """The stand-in calls the callback too, so the display path is exercised."""
        llm = ScriptedMainLLM(replies=["一整句"])
        seen: list[str] = []
        text = asyncio.run(llm.generate(provider_id="p", prompt="a", session="s", on_delta=seen.append))
        assert seen == ["一整句"]
        assert text == "一整句"
