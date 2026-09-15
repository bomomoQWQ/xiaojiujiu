"""The main LLM: the acting layer the Runtime is a sidecar to.

The architecture splits the work in two (patch v0.2 §5): the **host's main LLM**
does the moment-to-moment acting -- it is the one that actually talks -- while the
Runtime holds the persistent cognition and only ever hands over a temporary
background block. Until now this framework had no acting layer at all: it drove
the Runtime directly and nothing ever spoke. This module supplies the missing
half, as a plain OpenAI-compatible chat client so any endpoint works.

Where it plugs in
-----------------
The host pipeline calls one method for both paths::

    generate(provider_id=..., prompt=..., session=...) -> str

* a **user turn** passes the user's words with the Runtime's background block
  already appended by the plugin's ``on_llm_request`` hook;
* a **proactive render** passes the prompt the Runtime composed, which contains a
  ``- 想做的事：`` line -- that line is the whole point of the render call.

The client does not distinguish them. It sends what it is given and returns what
comes back, which is exactly the contract the shipped black-box simulation's
deterministic stand-in implements.

Secret discipline
-----------------
The key is read from the environment only (``CF_MAIN_LLM_API_KEY`` by default).
It is never written to a config file, never included in a trace record, and never
echoed in :meth:`describe`. The logbook records the prompt and the reply -- both
of which can be long and personal -- so the endpoint and model are recorded
instead, and the key only as a boolean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .logbook import Logbook

LOGGER = logging.getLogger("cf.main_llm")

#: Environment variables the endpoint is read from.
BASE_URL_ENV = "CF_MAIN_LLM_BASE_URL"
MODEL_ENV = "CF_MAIN_LLM_MODEL"
API_KEY_ENV = "CF_MAIN_LLM_API_KEY"

#: A default persona, used when the operator supplies none.
#:
#: It is deliberately thin. The architecture document is explicit that the host's
#: character setting is the highest personality source and that the Runtime only
#: supplies background -- so this text says "you are talking to someone you know"
#: and little else. Anything more specific belongs in the operator's own file.
DEFAULT_SYSTEM_PROMPT = (
    "你是一个长期陪伴用户的角色，正在和一个你熟悉的人聊天。"
    "用自然、口语化的中文回复，长度和对方的话相称，不要长篇大论，不要像助手或客服。"
    "如果下面附带了背景信息，那是你此刻心里的状态，不是这一轮的任务，也不要照抄。"
)

#: Marker the Runtime puts in a render prompt. Used only for labelling, never for
#: changing behaviour: the acting layer must render whatever it is asked to.
RENDER_MARKER = "- 想做的事："


class MainLLMError(Exception):
    """Raised when the endpoint cannot answer at all."""


class MainLLM(Protocol):
    """The acting layer's only method."""

    async def generate(self, *, provider_id: str, prompt: str, session: str = "") -> str:
        """Return the text the character says."""
        ...


@dataclass
class LLMCall:
    """One recorded call, for the trace."""

    session: str
    kind: str
    model: str
    prompt_chars: int
    reply_chars: int
    latency_ms: int
    prompt: str = ""
    reply: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "session": self.session,
            "llm_call_kind": self.kind,
            "model": self.model,
            "prompt_chars": self.prompt_chars,
            "reply_chars": self.reply_chars,
            "latency_ms": self.latency_ms,
            "usage": dict(self.usage),
            "error": self.error,
        }


class OpenAICompatibleMainLLM:
    """A chat client for any OpenAI-compatible endpoint.

    Args:
        base_url: Endpoint root, e.g. ``https://api.example.com/v1``.
        model: Model name.
        api_key: Bearer token. Defaults to ``CF_MAIN_LLM_API_KEY``.
        system_prompt: The character setting; see :data:`DEFAULT_SYSTEM_PROMPT`.
        timeout_s: Per-call deadline. A companion that types slowly is better
            than one that hangs the terminal, so this is enforced.
        temperature: Sampling temperature.
        max_tokens: Completion cap.
        logbook: Where to record calls (prompts and replies included).
        transport: Injectable transport, so tests never touch the network.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        model: str = "",
        api_key: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        timeout_s: float = 60.0,
        temperature: float = 0.8,
        max_tokens: int = 800,
        logbook: Logbook | None = None,
        transport: Any = None,
    ) -> None:
        """Store the endpoint settings; the key comes from the environment."""
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.timeout_s = float(timeout_s)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.logbook = logbook
        self._transport = transport or self._http_transport
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
        self.calls: list[LLMCall] = []

    # ------------------------------------------------------------------ config

    @classmethod
    def from_env(cls, *, logbook: Logbook | None = None, **overrides: Any) -> OpenAICompatibleMainLLM:
        """Build from the environment, with keyword overrides on top.

        Args:
            logbook: Where to record calls.
            **overrides: Any constructor argument; ``None`` values are ignored so
                a caller can pass unset CLI flags straight through.

        Returns:
            The configured client.
        """
        settings: dict[str, Any] = {
            "base_url": os.environ.get(BASE_URL_ENV, ""),
            "model": os.environ.get(MODEL_ENV, ""),
            "logbook": logbook,
        }
        settings.update({key: value for key, value in overrides.items() if value not in (None, "")})
        return cls(**settings)

    @property
    def configured(self) -> bool:
        """Whether enough settings exist to attempt a call."""
        return bool(self.base_url and self.model)

    @property
    def key_present(self) -> bool:
        """Whether a bearer token is configured. The value is never exposed."""
        return bool(self._api_key)

    def describe(self) -> dict[str, Any]:
        """Return a secret-free description, for the banner and the trace."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": "configured" if self.key_present else "not configured",
            "system_prompt_chars": len(self.system_prompt),
            "configured": self.configured,
        }

    # ------------------------------------------------------------------- calls

    async def generate(self, *, provider_id: str, prompt: str, session: str = "") -> str:
        """Answer one prompt.

        The blocking HTTP call runs in a worker thread: the host pipeline calls
        this from its own asyncio loop, and a synchronous ``urlopen`` inside that
        loop would stall every other task -- including the plugin's outbox poller,
        which is what delivers proactive messages.

        Args:
            provider_id: Provider id the host resolved (recorded, not used).
            prompt: The prompt the host pipeline built.
            session: Session the call belongs to.

        Returns:
            The generated text. Empty string when the endpoint failed, because a
            companion that says nothing is better than a chat window that dies.

        Raises:
            MainLLMError: Only when ``strict`` is set on the transport result.
        """
        del provider_id
        return await asyncio.to_thread(self._generate_sync, prompt, session)

    def _generate_sync(self, prompt: str, session: str) -> str:
        """Perform the blocking call and record it."""
        import time

        started = time.monotonic()
        kind = "render" if RENDER_MARKER in (prompt or "") else "reply"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        error = ""
        text = ""
        usage: dict[str, Any] = {}
        try:
            response = self._transport(
                f"{self.base_url}/chat/completions", body, self.timeout_s, self._headers()
            )
            text = _first_message_text(response)
            usage = dict(response.get("usage") or {}) if isinstance(response, Mapping) else {}
        except Exception as exc:  # noqa: BLE001 - the acting layer degrades, never crashes the chat
            error = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("main LLM call failed: %s", error)

        latency_ms = int((time.monotonic() - started) * 1000)
        call = LLMCall(
            session=session,
            kind=kind,
            model=self.model,
            prompt_chars=len(prompt or ""),
            reply_chars=len(text),
            latency_ms=latency_ms,
            prompt=prompt or "",
            reply=text,
            error=error,
            usage=usage,
        )
        self.calls.append(call)
        if self.logbook is not None:
            self.logbook.event(
                "main_llm_call",
                {
                    "session": session,
                    "llm_call_kind": kind,
                    "model": self.model,
                    "latency_ms": latency_ms,
                    "prompt_chars": call.prompt_chars,
                    "reply_chars": call.reply_chars,
                    "usage": usage,
                    "error": error,
                    # The full prompt and reply are kept: without them there is no
                    # way to tell "the Runtime injected nothing" apart from "the
                    # model ignored what it injected".
                    "prompt": prompt or "",
                    "reply": text,
                },
                message=(
                    f"[llm {kind}] {latency_ms}ms prompt={call.prompt_chars}c "
                    f"reply={call.reply_chars}c" + (f" ERROR {error}" if error else "")
                ),
            )
        return text

    def _headers(self) -> dict[str, str]:
        """Return the request headers, including the bearer token when set."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _http_transport(
        self, url: str, body: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> Mapping[str, Any]:
        """Perform one POST with the standard library only."""
        request = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    # ------------------------------------------------------------------ report

    def stats(self) -> dict[str, Any]:
        """Return a secret-free summary of what has been called."""
        by_kind: dict[str, int] = {}
        errors = 0
        for call in self.calls:
            by_kind[call.kind] = by_kind.get(call.kind, 0) + 1
            if call.error:
                errors += 1
        return {
            "calls": len(self.calls),
            "by_kind": by_kind,
            "errors": errors,
            "endpoint": self.base_url,
            "model": self.model,
        }


class ScriptedMainLLM:
    """A deterministic stand-in, for tests and for running with no endpoint.

    It is not a fallback that pretends to be a model: it echoes enough structure
    to prove the *plumbing* (context injection, render prompts, delivery) without
    consuming anything, and it labels itself ``scripted`` so nothing downstream
    can mistake its output for a real reply.
    """

    def __init__(self, *, logbook: Logbook | None = None, replies: list[str] | None = None) -> None:
        """Store the scripted replies."""
        self.logbook = logbook
        self.replies = list(replies or [])
        self.calls: list[LLMCall] = []

    @property
    def configured(self) -> bool:
        """Whether a real endpoint backs this stand-in. Always false.

        The banner and the status line branch on this, so a stand-in that claimed
        to be configured would let an operator read a scripted reply as a model's.
        """
        return False

    def describe(self) -> dict[str, Any]:
        """Return a description that cannot be mistaken for a real endpoint."""
        return {
            "base_url": "",
            "model": "(scripted stand-in)",
            "api_key": "not configured",
            "configured": False,
            "note": "没有配置主 LLM 端点，回复由确定性替身产生，不代表真模型",
        }

    def stats(self) -> dict[str, Any]:
        """Return a summary of what has been called."""
        by_kind: dict[str, int] = {}
        for call in self.calls:
            by_kind[call.kind] = by_kind.get(call.kind, 0) + 1
        return {"calls": len(self.calls), "by_kind": by_kind, "errors": 0, "model": "scripted"}

    async def generate(self, *, provider_id: str, prompt: str, session: str = "") -> str:
        """Return the next scripted reply, or one derived from the prompt."""
        del provider_id
        kind = "render" if RENDER_MARKER in (prompt or "") else "reply"
        if self.replies:
            text = self.replies.pop(0)
        elif kind == "render":
            text = _render_from_prompt(prompt)
        else:
            text = "嗯，我在听。"
        self.calls.append(
            LLMCall(
                session=session,
                kind=kind,
                model="scripted",
                prompt_chars=len(prompt or ""),
                reply_chars=len(text),
                latency_ms=0,
                prompt=prompt or "",
                reply=text,
            )
        )
        if self.logbook is not None:
            self.logbook.event(
                "main_llm_call",
                {
                    "session": session,
                    "llm_call_kind": kind,
                    "model": "scripted",
                    "prompt": prompt or "",
                    "reply": text,
                },
                message=f"[llm {kind}] scripted",
            )
        return text


def _render_from_prompt(prompt: str) -> str:
    """Build a message from the Runtime's own intent line.

    Mirrors what the shipped black-box simulation's stand-in does, so the
    no-endpoint mode still exercises the render path end to end.
    """
    for line in (prompt or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(RENDER_MARKER):
            intent = stripped[len(RENDER_MARKER) :].strip()
            if intent:
                return f"刚才忽然想起{intent}，现在怎么样了？"
    return "在忙吗？忽然想起你了。"


def _first_message_text(response: Any) -> str:
    """Return the assistant text of an OpenAI-compatible response.

    Raises:
        MainLLMError: When the response carries no usable message. Callers
            degrade; this exists so the *reason* is a sentence rather than a
            ``TypeError`` from deep inside a dict lookup.
    """
    choices = response.get("choices") if isinstance(response, Mapping) else None
    if not isinstance(choices, list) or not choices:
        raise MainLLMError("endpoint returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise MainLLMError("endpoint returned no message")
    content = message.get("content")
    if not isinstance(content, str):
        raise MainLLMError("endpoint returned non-text content")
    return content
