"""Optional Semantic Provider port (architecture patch v0.2, sections 16-21).

Patch v0.2 removes the local generative model from the standard dependency set: the
main LLM already understands the current turn, so the Runtime only needs *strong
semantics* for **low-frequency deep cognition**, never for the acting layer.

**The local route has been abandoned entirely.** There is no ``local_cpu`` or
``local_gpu`` provider: shipping a multi-gigabyte model beside a chat bot costs
more than it returns (measured: seconds per appraisal on CPU, ~2 GB resident),
and the acting layer never needed it in the first place. What remains is:

* :class:`SemanticProvider` - the frozen protocol the Runtime integrates against;
* :class:`DisabledProvider` - the default, model-free implementation, so the
  Runtime runs completely with no model at all;
* :class:`RemoteAPIProvider` - any OpenAI-compatible remote endpoint, for the
  optional low-frequency deep cognition;
* :func:`build_provider` - environment-driven selection that never raises and
  always falls back to :class:`DisabledProvider`.

Three contracts hold for every provider:

1. **Advisory only.** A provider proposes; the Reducer decides
   ``APPLY`` / ``REBASE`` / ``DISCARD``. Nothing here writes Runtime state.
2. **Fail-open.** Unavailable, timed out, unreachable or malformed-JSON calls
   return ``None`` or a ``degraded`` suggestion set - never an exception.
3. **Secret-safe.** An API key is read from ``CR_SEMANTIC_API_KEY`` only, is
   never persisted, and never appears in ``repr()``, logs or :meth:`health`.

Only the standard library is used, matching the sidecar's tiny dependency set.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

LOGGER = logging.getLogger("companion_runtime.providers")

__all__ = [
    "DEEP_REFRESH_FIELDS",
    "DISABLED_NAME",
    "PROVIDER_ENV_VAR",
    "REMOTE_API_NAME",
    "DEEP_REFRESH_SYSTEM_PROMPT",
    "DeepRefreshRequest",
    "DeepRefreshSuggestions",
    "DisabledProvider",
    "RemoteAPIProvider",
    "SemanticProvider",
    "Transport",
    "build_provider",
    "extract_json",
    "parse_deep_refresh",
    "parse_explanation",
    "resolve_provider_name",
]

#: Environment variable selecting the implementation.
PROVIDER_ENV_VAR = "CR_SEMANTIC_PROVIDER"

#: Environment variable holding the remote API key. Read from the environment
#: only; the Runtime never writes it back to disk or into a log.
API_KEY_ENV_VAR = "CR_SEMANTIC_API_KEY"

DISABLED_NAME = "disabled"
REMOTE_API_NAME = "remote_api"

#: Every provider name :func:`build_provider` accepts. Retired names such as
#: ``local_cpu`` / ``local_gpu`` are deliberately absent: an operator still
#: exporting one gets ``DisabledProvider`` plus a warning, never a half-working
#: local path that no longer exists.
KNOWN_PROVIDER_NAMES = frozenset({DISABLED_NAME, REMOTE_API_NAME})

#: Provider names that used to exist and are now removed. Kept so the factory can
#: say *why* a stale setting was ignored instead of silently degrading.
RETIRED_PROVIDER_NAMES = frozenset({"local_cpu", "local_gpu", "local", "cpu", "gpu", "llama_cpp"})

#: Deep-refresh suggestion field -> the Python type it must decode to.
DEEP_REFRESH_FIELDS: Mapping[str, type] = {
    "reinterpretations": list,
    "psychological_interpretation": dict,
    "candidate_intent_operations": list,
    "memory_suggestions": list,
    "unfinished_matter_suggestions": list,
    "user_model_evidence_suggestions": list,
}

#: Optional key a model may wrap the six collections under.
DEEP_REFRESH_WRAPPER_KEY = "suggestions"

DEEP_REFRESH_SYSTEM_PROMPT = (
    "你是长期陪伴角色的深层认知整理器，只在低频的后台刷新中被调用。"
    "只输出一个 JSON 对象，字段固定为 reinterpretations, psychological_interpretation, "
    "candidate_intent_operations, memory_suggestions, unfinished_matter_suggestions, "
    "user_model_evidence_suggestions。"
    "前五个中除 psychological_interpretation 是对象外，其余都是数组。"
    # The shape has to be spelled out, item keys included. Naming only the six
    # top-level fields is not enough: measured against a real backlog the model
    # answered with `event_id` where the grounding step requires `sources`, and every
    # reinterpretation was then discarded as `missing_sources` - the refresh ran,
    # applied nothing, and settled no event. `sources` is what ties a suggestion to
    # real events, so an item without it cannot be applied at all.
    "每个条目必须带 sources 数组，元素取自输入 unresolved_events 里的 event_id，"
    "不得编造 id。格式样例："
    "{\"reinterpretations\": [{\"sources\": [\"evt_x\"], \"content\": \"当时那句话的意思\", "
    "\"confidence\": 0.6}], "
    "\"psychological_interpretation\": {\"summary\": \"当前心理状态\"}, "
    "\"candidate_intent_operations\": [{\"sources\": [\"evt_x\"], \"operation\": \"add\", "
    "\"intent\": \"想做的事\", \"confidence\": 0.5}], "
    "\"memory_suggestions\": [{\"sources\": [\"evt_x\"], \"summary\": \"值得长期记住的事\", "
    "\"kind\": \"episodic\", \"importance\": 0.6}], "
    "\"unfinished_matter_suggestions\": [{\"sources\": [\"evt_x\"], \"title\": \"还没了结的事\"}], "
    "\"user_model_evidence_suggestions\": [{\"sources\": [\"evt_x\"], \"trait\": \"推断出的特征\", "
    "\"weight\": 0.3}]}"
    "你只提供建议，不决定任何状态变更，不生成台词，不创造输入中不存在的事件；"
    # The guardrail here has to be phrased as "do not invent", never as "stay silent
    # when unsure". The earlier wording ("return empty arrays when the evidence is
    # insufficient, do not guess") read as "if the intent is not explicit, say
    # nothing": measured against a real 10KB backlog, the model answered with 57
    # tokens of six empty collections, every time, while every rewriting that names
    # fabrication instead produced ~1000 tokens of usable suggestions. The whole
    # deferred-interpretation path depends on this call coming back with something,
    # because an event that is never reinterpreted is never settled and therefore
    # never becomes an emotion event.
    "不要编造输入中没有的事件、会话或时间戳。"
)

EXPLAIN_STATE_SYSTEM_PROMPT = (
    "你是长期陪伴角色的情绪解释器：把已有的结构化心理状态翻译成第一人称心理语言。"
    "只输出一个 JSON 对象，字段固定为 experience, focus, conflict, impulse, inhibition, expression。"
    "每个字段一句话，20 到 40 字，不出现数字，不生成台词，不创造输入中不存在的事件。"
    # The JSON Output mode requires the word "json" plus a shape example in the
    # prompt; this is that example.
    "样例：{\"experience\": \"……\", \"focus\": \"……\", \"conflict\": \"……\", "
    "\"impulse\": \"……\", \"inhibition\": \"……\", \"expression\": \"……\"}"
)


class Transport(Protocol):
    """Minimal HTTP transport seam used to keep tests offline.

    Injected into every provider so the whole suite runs with no network and no
    endpoint of any kind.
    """

    def __call__(
        self, url: str, body: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> Mapping[str, Any]:
        """Perform one POST and return the decoded JSON response."""
        ...


# --------------------------------------------------------------------------------------
# Wire contracts
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class DeepRefreshRequest:
    """Everything a deep refresh is allowed to see (patch v0.2, section 19).

    Attributes:
        unresolved_events: Events the Runtime declined to interpret yet.
        situation: Current working situation.
        mood: Long-horizon background mood.
        active_emotions: Active emotion impact events.
        memories: Activated memories.
        unfinished: Open unfinished matters.
        user_model_summary: Prose summary of the user interaction model.
        candidates: Candidate intents already in the pool.
        key_quotes: Raw quotes that must survive verbatim.
    """

    unresolved_events: list[dict[str, Any]] = field(default_factory=list)
    situation: dict[str, Any] = field(default_factory=dict)
    mood: dict[str, Any] = field(default_factory=dict)
    active_emotions: list[dict[str, Any]] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)
    unfinished: list[dict[str, Any]] = field(default_factory=list)
    user_model_summary: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    key_quotes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering of the request."""
        return {
            "unresolved_events": [dict(item) for item in self.unresolved_events],
            "situation": dict(self.situation),
            "mood": dict(self.mood),
            "active_emotions": [dict(item) for item in self.active_emotions],
            "memories": [dict(item) for item in self.memories],
            "unfinished": [dict(item) for item in self.unfinished],
            "user_model_summary": self.user_model_summary,
            "candidates": [dict(item) for item in self.candidates],
            "key_quotes": [dict(item) for item in self.key_quotes],
        }


@dataclass(slots=True)
class DeepRefreshSuggestions:
    """Advisory output of one deep refresh (patch v0.2, section 20).

    The Runtime still owns the decision: every entry here is a *suggestion* that
    goes through ``APPLY`` / ``REBASE`` / ``DISCARD`` in the Reducer.

    Attributes:
        reinterpretations: Suggested re-readings of old events.
        psychological_interpretation: Deeper first-person psychological language.
        candidate_intent_operations: Suggested candidate-intent operations.
        memory_suggestions: Suggested memory writes or edits.
        unfinished_matter_suggestions: Suggested unfinished-matter changes.
        user_model_evidence_suggestions: Suggested user-model evidence.
        provider: Name of the provider that produced (or failed to produce) this.
        degraded: ``True`` whenever the caller must not rely on the content.
        reason: Machine-readable degradation reason, empty on full success.
        latency_ms: Wall-clock cost of the attempt, in milliseconds.
    """

    reinterpretations: list[dict[str, Any]] = field(default_factory=list)
    psychological_interpretation: dict[str, Any] = field(default_factory=dict)
    candidate_intent_operations: list[dict[str, Any]] = field(default_factory=list)
    memory_suggestions: list[dict[str, Any]] = field(default_factory=list)
    unfinished_matter_suggestions: list[dict[str, Any]] = field(default_factory=list)
    user_model_evidence_suggestions: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    degraded: bool = True
    reason: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering of the suggestions."""
        return {
            "reinterpretations": [dict(item) for item in self.reinterpretations],
            "psychological_interpretation": dict(self.psychological_interpretation),
            "candidate_intent_operations": [
                dict(item) for item in self.candidate_intent_operations
            ],
            "memory_suggestions": [dict(item) for item in self.memory_suggestions],
            "unfinished_matter_suggestions": [
                dict(item) for item in self.unfinished_matter_suggestions
            ],
            "user_model_evidence_suggestions": [
                dict(item) for item in self.user_model_evidence_suggestions
            ],
            "provider": self.provider,
            "degraded": self.degraded,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
        }

    def is_empty(self) -> bool:
        """Return ``True`` when no suggestion of any kind was produced."""
        return not any(
            (
                self.reinterpretations,
                self.psychological_interpretation,
                self.candidate_intent_operations,
                self.memory_suggestions,
                self.unfinished_matter_suggestions,
                self.user_model_evidence_suggestions,
            )
        )


@runtime_checkable
class SemanticProvider(Protocol):
    """Optional port for low-frequency strong semantics.

    The Runtime must remain fully functional when the only implementation is
    :class:`DisabledProvider`, so every method is allowed to answer "nothing".
    """

    name: str

    def available(self) -> bool:
        """Return whether the provider is configured and usable right now."""
        ...

    def deep_refresh(
        self, request: DeepRefreshRequest, *, timeout_s: float | None = None
    ) -> DeepRefreshSuggestions | None:
        """Return advisory suggestions, or ``None`` when unavailable."""
        ...

    def explain_state(
        self, payload: Mapping[str, Any], *, state_key: str = ""
    ) -> dict[str, str] | None:
        """Return first-person psychological language, or ``None``."""
        ...

    def health(self) -> dict[str, Any]:
        """Return a JSON-serialisable, secret-free health snapshot."""
        ...


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def parse_deep_refresh(
    payload: Any, *, provider: str = "", latency_ms: int = 0
) -> DeepRefreshSuggestions | None:
    """Validate a decoded deep-refresh payload against the frozen contract.

    A model reply is untrusted input: each of the six suggestion fields is
    type-checked on its own. A field of the wrong type is **dropped** and named
    in ``reason`` rather than triggering a guess-repair, so a partially malformed
    reply still yields the suggestions that were well formed.

    Args:
        payload: Decoded JSON object from the model. The six collections may also
            be nested under a ``suggestions`` key.
        provider: Provider name to record on the result.
        latency_ms: Measured latency to record on the result.

    Returns:
        A :class:`DeepRefreshSuggestions`, or ``None`` when ``payload`` is not a
        mapping at all. A non-``None`` result with ``degraded`` set means the
        caller must not rely on the affected fields.
    """
    if not isinstance(payload, Mapping):
        return None
    body: Mapping[str, Any] = payload
    nested = payload.get(DEEP_REFRESH_WRAPPER_KEY)
    if isinstance(nested, Mapping) and any(key in nested for key in DEEP_REFRESH_FIELDS):
        body = nested

    suggestions = DeepRefreshSuggestions(provider=provider, latency_ms=latency_ms)
    invalid: list[str] = []
    for field_name, expected in DEEP_REFRESH_FIELDS.items():
        if field_name not in body:
            # An omitted field is allowed: absence is not corruption.
            continue
        value = body[field_name]
        if not isinstance(value, expected):
            invalid.append(field_name)
            continue
        if expected is list:
            if not all(isinstance(item, Mapping) for item in value):
                invalid.append(field_name)
                continue
            setattr(suggestions, field_name, [dict(item) for item in value])
        else:
            setattr(suggestions, field_name, dict(value))

    if invalid:
        suggestions.degraded = True
        suggestions.reason = "invalid_fields:" + ",".join(invalid)
    else:
        suggestions.degraded = False
        suggestions.reason = ""
    return suggestions


def resolve_provider_name(config: Any = None, env: Mapping[str, str] | None = None) -> str:
    """Return the requested provider name, normalised.

    This answers "what did the operator ask for", not "can we honour it". Retired
    names are therefore returned verbatim rather than collapsed to ``disabled``,
    so :func:`build_provider` can explain the removal instead of the operator
    silently getting no provider and no reason.

    Args:
        config: Optional Runtime configuration or mapping. A
            ``semantic_provider`` entry, ``config.semantic.provider`` (the field a
            :class:`~companion_runtime.config.SemanticConfig` actually carries) or
            ``extras['semantic']['provider']`` takes precedence over the
            environment.
        env: Environment mapping, defaults to :data:`os.environ`.

    Returns:
        A member of :data:`KNOWN_PROVIDER_NAMES`, a member of
        :data:`RETIRED_PROVIDER_NAMES`, or :data:`DISABLED_NAME` for anything
        unrecognised. This function never raises.
    """
    source = os.environ if env is None else env
    requested = _first_str(
        _safe_config_value(config, "semantic_provider", "provider"),
        _safe_config_value(_safe_read(config, "semantic"), "provider", "semantic_provider"),
        source.get(PROVIDER_ENV_VAR),
    )
    name = requested.strip().lower()
    if name in KNOWN_PROVIDER_NAMES or name in RETIRED_PROVIDER_NAMES:
        return name
    if name:
        LOGGER.warning(
            "Unknown semantic provider %r; falling back to %s", requested, DISABLED_NAME
        )
    return DISABLED_NAME


# --------------------------------------------------------------------------------------
# Implementations
# --------------------------------------------------------------------------------------


class DisabledProvider:
    """The default provider: no model, no network, no latency.

    Patch v0.2 makes "no strong semantics" a first-class configuration rather
    than an error path, so this provider is a complete, honest implementation:
    the Runtime keeps running on rules, statistics and the main LLM alone.
    """

    name = DISABLED_NAME

    def available(self) -> bool:
        """Return ``False``: there is deliberately no model behind this port."""
        return False

    def deep_refresh(
        self, request: DeepRefreshRequest, *, timeout_s: float | None = None
    ) -> DeepRefreshSuggestions | None:
        """Return ``None`` without touching the network."""
        return None

    def explain_state(
        self, payload: Mapping[str, Any], *, state_key: str = ""
    ) -> dict[str, str] | None:
        """Return ``None``; the caller uses its deterministic templates."""
        return None

    def health(self) -> dict[str, Any]:
        """Return a snapshot naming the disabled implementation."""
        return {
            "provider": self.name,
            "available": False,
            "enabled": False,
            "reason": "disabled",
        }

    def __repr__(self) -> str:
        """Return a stable, secret-free repr."""
        return "DisabledProvider(name='disabled')"


class _OpenAICompatibleProvider:
    """Shared OpenAI-compatible chat plumbing for the concrete providers.

    Args:
        name: Provider name reported through :meth:`health`.
        base_url: Base URL of the OpenAI-compatible endpoint.
        model: Model name sent to the endpoint.
        api_key: Bearer token, or an empty string for an unprotected endpoint.
        timeout_s: Default hard deadline for one call.
        deep_timeout_s: Hard deadline for a deep refresh. Deep cognition is
            low-frequency by design, so it may wait far longer than an
            interactive call; it defaults to ``timeout_s``.
        max_tokens: Completion cap.
        temperature: Sampling temperature; ``0`` keeps structured output stable.
        headers: Extra request headers.
        transport: Injectable transport used by tests.
        cache_ttl_s: How long one explanation stays usable in the provider's own
            cache. It is the same number the Runtime uses for its explanation
            cache, so the two caches cannot disagree about what "stale" means.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_s: float = 30.0,
        deep_timeout_s: float | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = True,
        headers: Mapping[str, str] | None = None,
        transport: Callable[[str, dict[str, Any], float, dict[str, str]], Mapping[str, Any]]
        | None = None,
        cache_ttl_s: float = 900.0,
    ) -> None:
        """Store the endpoint settings and the transport seam."""
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.deep_timeout_s = float(deep_timeout_s if deep_timeout_s is not None else timeout_s)
        self.max_tokens = int(max_tokens)
        #: Ask the endpoint to emit a JSON object (``response_format``). Both
        #: structured callers here parse JSON out of the reply, so the constraint
        #: only removes a failure mode; it is toggleable because not every
        #: OpenAI-compatible gateway accepts an unknown body key.
        self.json_mode = bool(json_mode)
        self.temperature = float(temperature)
        self.headers: dict[str, str] = dict(headers or {})
        self._transport = transport or self._http_transport
        # Secret hygiene: the token is deliberately NOT an instance attribute. It
        # lives only inside this closure's cell, so no ``vars()``/``__dict__`` dump,
        # ``repr`` or log record can reach it. Only ``_api_key_present`` (a bool) is
        # observable, which is exactly what ``health()`` is allowed to report.
        base_headers = dict(self.headers)
        if api_key:
            base_headers["Authorization"] = f"Bearer {api_key}"
        self._api_key_present = bool(api_key)

        def _headers() -> dict[str, str]:
            """Return the outgoing headers, including the bearer token if set."""
            return dict(base_headers)

        self._headers = _headers
        self._lock = threading.Lock()
        self.stats: dict[str, int] = {
            "deep_refresh_calls": 0,
            "deep_refresh_ok": 0,
            "deep_refresh_degraded": 0,
            "explain_calls": 0,
            "explain_ok": 0,
            "explain_degraded": 0,
            "cache_hits": 0,
        }
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._cache_ttl_s = max(0.0, float(cache_ttl_s))

    # ------------------------------------------------------------------ transport

    def _http_transport(
        self, url: str, body: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> Mapping[str, Any]:
        """Perform the HTTP call with the standard library only.

        Args:
            url: Fully qualified request URL.
            body: JSON request body.
            timeout: Deadline in seconds.
            headers: Request headers, including the bearer token when configured.

        Returns:
            The decoded JSON response.

        Raises:
            urllib.error.URLError: On any network failure. Callers degrade.
        """
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def _request_headers(self) -> dict[str, str]:
        """Return the outgoing headers, without ever logging the token."""
        return self._headers()

    def configured(self) -> bool:
        """Return whether enough settings exist to attempt a call."""
        return bool(self.base_url and self.model)

    def _api_key_configured(self) -> bool:
        """Return whether a bearer token is present, without exposing it."""
        return self._api_key_present

    def _chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        timeout: float,
        grammar: str | None = None,
        extra_body: Mapping[str, Any] | None = None,
        json_mode: bool = False,
    ) -> str:
        """Send one chat completion request and return the assistant text.

        Args:
            system_prompt: System message.
            user_content: User message.
            timeout: Hard deadline in seconds.
            grammar: Optional inline GBNF grammar for constrained decoding.
            extra_body: Extra request-body fields, e.g. ``chat_template_kwargs``.
            json_mode: Ask the endpoint for a JSON object (``response_format``).
                Both callers here want a JSON object, but only when the endpoint is
                known to implement the field - a gateway that rejects unknown body
                keys would fail every call.

        Returns:
            The assistant text.

        Raises:
            Exception: Propagated to the caller, which degrades to ``None``.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if grammar:
            body["grammar"] = grammar
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if extra_body:
            body.update(dict(extra_body))
        response = self._transport(
            f"{self.base_url}/chat/completions", body, timeout, self._request_headers()
        )
        return _first_message_text(response)

    def _parse_raw_json(self, raw: str) -> Any:
        """Extract and decode the JSON object from a model reply.

        Args:
            raw: Raw completion text, possibly fenced or wrapped in prose.

        Returns:
            The decoded object, or ``None`` when the reply carries no JSON.

        Raises:
            ValueError: When the reply is not decodable JSON.
        """
        return extract_json(raw)

    # ------------------------------------------------------------- deep refresh

    def deep_refresh(
        self, request: DeepRefreshRequest, *, timeout_s: float | None = None
    ) -> DeepRefreshSuggestions | None:
        """Ask the model for a deep cognitive refresh.

        Args:
            request: The bounded deep-refresh input.
            timeout_s: Override for the hard deadline.

        Returns:
            A :class:`DeepRefreshSuggestions` - ``degraded`` whenever the model
            could not be trusted - or ``None`` when the provider is unusable.
        """
        started = time.monotonic()
        if not self.available():
            return None
        deadline = float(timeout_s if timeout_s is not None else self.deep_timeout_s)
        with self._lock:
            self.stats["deep_refresh_calls"] += 1
        try:
            raw = self._chat(
                DEEP_REFRESH_SYSTEM_PROMPT,
                json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True),
                timeout=deadline,
                grammar=self._grammar_for("deep_refresh"),
                extra_body=self._extra_body(),
                json_mode=self.json_mode,
            )
            suggestions = parse_deep_refresh(
                self._parse_raw_json(raw), provider=self.name, latency_ms=_ms(started)
            )
        except Exception as exc:  # noqa: BLE001 - any failure means degradation
            with self._lock:
                self.stats["deep_refresh_degraded"] += 1
            LOGGER.debug("%s deep refresh degraded: %s", self.name, exc)
            return DeepRefreshSuggestions(
                provider=self.name,
                degraded=True,
                reason=f"error:{type(exc).__name__}",
                latency_ms=_ms(started),
            )

        if suggestions is None:
            with self._lock:
                self.stats["deep_refresh_degraded"] += 1
            return DeepRefreshSuggestions(
                provider=self.name,
                degraded=True,
                reason="invalid_json",
                latency_ms=_ms(started),
            )

        with self._lock:
            if suggestions.degraded:
                self.stats["deep_refresh_degraded"] += 1
            else:
                self.stats["deep_refresh_ok"] += 1
        return suggestions

    # ----------------------------------------------------------------- explain

    def explain_state(
        self, payload: Mapping[str, Any], *, state_key: str = ""
    ) -> dict[str, str] | None:
        """Ask the model for first-person psychological language.

        Args:
            payload: Structured psychological state (no long history).
            state_key: Stable cache key; any change to it invalidates the entry.
                An empty key disables caching for that call.

        Returns:
            A mapping of the six explanation fields, or ``None`` when the caller
            must use its deterministic template. Never raises.
        """
        if not self.available():
            return None
        if state_key:
            cached = self._cache_get(state_key)
            if cached is not None:
                with self._lock:
                    self.stats["cache_hits"] += 1
                return cached

        with self._lock:
            self.stats["explain_calls"] += 1
        try:
            raw = self._chat(
                EXPLAIN_STATE_SYSTEM_PROMPT,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                timeout=self.timeout_s,
                grammar=self._grammar_for("explanation"),
                extra_body=self._extra_body(),
                json_mode=self.json_mode,
            )
            explanation = parse_explanation(self._parse_raw_json(raw))
        except Exception as exc:  # noqa: BLE001 - any failure means degradation
            with self._lock:
                self.stats["explain_degraded"] += 1
            LOGGER.debug("%s explanation degraded: %s", self.name, exc)
            return None

        if explanation is None:
            with self._lock:
                self.stats["explain_degraded"] += 1
            return None

        with self._lock:
            self.stats["explain_ok"] += 1
        if state_key:
            self._cache_put(state_key, explanation)
        return explanation

    # ------------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        """Return a JSON-serialisable, secret-free snapshot.

        The API key is never echoed; only whether one is configured is reported.
        """
        with self._lock:
            return {
                "provider": self.name,
                "available": self.available(),
                "base_url": self.base_url,
                "model": self.model,
                "api_key": "configured" if self._api_key_configured() else "not configured",
                "stats": dict(self.stats),
                "cache_entries": len(self._cache),
            }

    def invalidate(self) -> None:
        """Drop every cached explanation."""
        with self._lock:
            self._cache.clear()

    # -------------------------------------------------------------------- cache

    def _cache_get(self, key: str) -> dict[str, str] | None:
        """Return a cached explanation when it is still fresh.

        An entry whose stored timestamp sits in the future is dropped rather than
        served: a wall-clock jump (NTP step, a replayed timeline, a hand-edited
        row) would otherwise make ``age`` negative, and a negative age is smaller
        than any TTL - so a corrupt entry would be trusted forever.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            stored_at, stored_wall, value = entry
            if time.monotonic() - stored_at > self._cache_ttl_s:
                self._cache.pop(key, None)
                return None
            skew = time.time() - stored_wall
            if skew < -_FUTURE_SKEW_TOLERANCE_S:
                self._cache.pop(key, None)
                return None
            return dict(value)

    def _cache_put(self, key: str, value: Mapping[str, str]) -> None:
        """Store one explanation, bounding the cache size."""
        with self._lock:
            self._cache[key] = (time.monotonic(), time.time(), dict(value))
            while len(self._cache) > 128:
                self._cache.pop(next(iter(self._cache)))

    # ------------------------------------------------------------------ internal

    def _extra_body(self) -> dict[str, Any] | None:
        """Return provider-specific request-body fields, if any."""
        return None

    def _grammar_for(self, name: str) -> str | None:
        """Return inline GBNF text for constrained decoding, or ``None``.

        The Runtime no longer bundles grammars, because the local route they
        existed for is gone. The hook stays so a self-hosted gateway that wants
        constrained decoding can be wrapped by subclassing and returning text here.

        Args:
            name: Grammar base name, e.g. ``deep_refresh``.

        Returns:
            ``None``, meaning unconstrained decoding.
        """
        return None

    def __repr__(self) -> str:
        """Return a stable, secret-free repr."""
        return (
            f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, api_key="
            f"{'configured' if self._api_key_configured() else 'not configured'})"
        )


class RemoteAPIProvider(_OpenAICompatibleProvider):
    """Strong semantics from any OpenAI-compatible remote endpoint.

    The API key comes from ``CR_SEMANTIC_API_KEY`` only. It is never accepted from
    a config file, never written to disk, never logged, and never included in
    :meth:`health` - which reports only ``configured`` / ``not configured``.

    Args:
        base_url: Base URL, e.g. ``https://api.example.com/v1``.
        model: Remote model name.
        api_key: Bearer token. Defaults to ``CR_SEMANTIC_API_KEY`` when omitted.
        env: Environment mapping used for the default key lookup.
        timeout_s: Default hard deadline for one call.
        deep_timeout_s: Hard deadline for a deep refresh, which may be far longer
            than an interactive call; defaults to ``timeout_s``.
        max_tokens: Completion cap.
        temperature: Sampling temperature.
        grammar: Inline GBNF grammar, only meaningful for self-hosted gateways.
        json_mode: Ask for a JSON object via ``response_format``. On for every
            structured call; ``CR_SEMANTIC_JSON_MODE=0`` disables it.
        headers: Extra request headers.
        transport: Injectable transport used by tests.
        cache_ttl_s: TTL of the provider's own explanation cache. Callers pass the
            Runtime's configured interpretation age so the provider cannot serve
            an explanation the Runtime already considers stale.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        model: str = "",
        api_key: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
        deep_timeout_s: float | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        grammar: str | None = None,
        json_mode: bool = True,
        headers: Mapping[str, str] | None = None,
        transport: Callable[[str, dict[str, Any], float, dict[str, str]], Mapping[str, Any]]
        | None = None,
        cache_ttl_s: float = 900.0,
    ) -> None:
        """Store the endpoint settings; the key is read from the environment."""
        source = os.environ if env is None else env
        resolved_key = source.get(API_KEY_ENV_VAR, "") if api_key is None else api_key
        self._grammar = grammar
        super().__init__(
            name=REMOTE_API_NAME,
            base_url=base_url,
            model=model,
            api_key=str(resolved_key or ""),
            timeout_s=timeout_s,
            deep_timeout_s=deep_timeout_s,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            headers=headers,
            transport=transport,
            cache_ttl_s=cache_ttl_s,
        )

    def available(self) -> bool:
        """Return whether a base URL, a model and an API key are all present."""
        return bool(self.base_url and self.model and self._api_key_configured())

    def _grammar_for(self, name: str) -> str | None:
        """Return the explicit grammar override, else the bundled grammar."""
        return self._grammar if self._grammar else super()._grammar_for(name)


# --------------------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------------------


def build_provider(
    config: Any = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: Callable[[str, dict[str, Any], float, dict[str, str]], Mapping[str, Any]]
    | None = None,
) -> SemanticProvider:
    """Build the configured semantic provider, never raising.

    Selection order:

    1. ``config.semantic_provider`` / ``config.semantic.provider`` (or
       ``config.extras['semantic']['provider']``);
    2. ``CR_SEMANTIC_PROVIDER``;
    3. :data:`DISABLED_NAME`.

    Any unknown name and any construction failure falls back to
    :class:`DisabledProvider`, so the Runtime always has a working port. Retired
    local names are reported explicitly rather than degraded silently, so an
    operator who still exports ``CR_SEMANTIC_PROVIDER=local_cpu`` learns that the
    local route is gone instead of wondering why nothing happens.

    Args:
        config: Optional Runtime configuration (``RuntimeConfig`` or any object or
            mapping exposing the same names).
        env: Environment mapping, defaults to :data:`os.environ`.
        transport: Testing seam passed to the constructed provider's HTTP client.

    Returns:
        A :class:`SemanticProvider`; :class:`DisabledProvider` on every failure.
    """
    source = os.environ if env is None else env
    try:
        name = resolve_provider_name(config, source)
        if name in RETIRED_PROVIDER_NAMES:
            LOGGER.warning(
                "Semantic provider %r was removed when the local model route was abandoned; "
                "using the disabled provider. Use %r for remote strong semantics.",
                name,
                REMOTE_API_NAME,
            )
            return DisabledProvider()
        if name == DISABLED_NAME:
            return DisabledProvider()
        if name == REMOTE_API_NAME:
            return _build_remote(config, source, transport)
    except Exception as exc:  # noqa: BLE001 - the factory must never raise
        LOGGER.warning("Semantic provider construction failed (%s); using disabled", exc)
        return DisabledProvider()
    return DisabledProvider()


def _build_remote(
    config: Any,
    env: Mapping[str, str],
    transport: Callable[[str, dict[str, Any], float, dict[str, str]], Mapping[str, Any]] | None,
) -> SemanticProvider:
    """Build the remote API provider from config and environment settings.

    The API key is read from the environment exclusively; a key placed in the
    configuration object is deliberately ignored, because the Runtime's config is
    serialisable and must never be able to carry a credential.
    """
    base_url = _first_str(
        _config_value(config, "semantic_base_url", "base_url"), env.get("CR_SEMANTIC_BASE_URL")
    )
    model = _first_str(
        _config_value(config, "semantic_model", "model"), env.get("CR_SEMANTIC_MODEL")
    )
    return RemoteAPIProvider(
        base_url or "",
        model=model or "",
        env=env,
        timeout_s=_first_float(env.get("CR_SEMANTIC_TIMEOUT_S")) or 30.0,
        max_tokens=int(_first_float(env.get("CR_SEMANTIC_MAX_TOKENS")) or 1024),
        # JSON Output: the endpoint is asked for a JSON object, so a reply cannot
        # come back as prose that has to be scavenged for braces. Default on (both
        # callers here are structured); `CR_SEMANTIC_JSON_MODE=0` turns it off for a
        # gateway that rejects the field.
        json_mode=_bool_setting(_first_str(env.get("CR_SEMANTIC_JSON_MODE")), default=True),
        # The provider's own explanation cache is paced by the same number the
        # Runtime uses, so the two caches cannot disagree about staleness.
        cache_ttl_s=_first_float(_read_semantic(config, "interpretation_max_age_seconds"))
        or 900.0,
        transport=transport,
    )


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _config_value(config: Any, *names: str) -> str | None:
    """Read one string setting from a config object, mapping or ``extras``.

    Args:
        config: Runtime configuration, plain mapping, or ``None``.
        *names: Candidate attribute/key names, in priority order.

    Returns:
        The first non-empty string found, otherwise ``None``.

    Raises:
        ValueError: When reading one of the names raised. That is a broken
            configuration rather than an absent setting, and the provider factory
            turns it into its documented fallback.
    """
    if config is None:
        return None
    for name in names:
        value = _read_one(config, name)
        if value is _UNREADABLE:
            raise ValueError(f"configuration value {name!r} could not be read")
        if isinstance(value, str) and value.strip():
            return value.strip()
    extras = _read_one(config, "extras")
    if isinstance(extras, Mapping):
        semantic = extras.get("semantic")
        if isinstance(semantic, Mapping):
            for name in names:
                value = semantic.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _read_semantic(config: Any, name: str) -> Any:
    """Read one non-string setting from the ``semantic`` section, or ``None``.

    A helper rather than a call to :func:`_config_value`, which only ever returns
    strings: numeric settings such as the interpretation age must keep their type.

    Raises:
        ValueError: When reading the setting raised.
    """
    semantic = _read_one(config, "semantic")
    if semantic is _UNREADABLE:
        raise ValueError("the semantic configuration section could not be read")
    value = _read_one(semantic, name)
    if value is _UNREADABLE:
        raise ValueError(f"semantic.{name} could not be read")
    if value is not None:
        return value
    extras = _safe_read(config, "extras")
    if isinstance(extras, Mapping):
        section = extras.get("semantic")
        if isinstance(section, Mapping):
            return section.get(name)
    return None


def _read_one(config: Any, name: str) -> Any:
    """Return ``config[name]`` or ``config.name``, or ``None``.

    A raising lookup is reported as :data:`_UNREADABLE` rather than as absent: the
    provider factory treats "this setting could not be read" as a construction
    failure and falls back to the disabled provider, instead of building a remote
    client pointed at nothing. :func:`resolve_provider_name` deliberately swallows
    the marker, because picking a name must never raise.
    """
    try:
        if isinstance(config, Mapping):
            return config.get(name)
        return getattr(config, name, None)
    except Exception:  # noqa: BLE001 - report the failure, do not propagate it
        return _UNREADABLE


def _safe_read(config: Any, name: str) -> Any:
    """Return a configuration value, or ``None`` when it cannot be read at all."""
    value = _read_one(config, name)
    return None if value is _UNREADABLE else value


def _safe_config_value(config: Any, *names: str) -> str | None:
    """Like :func:`_config_value`, but report an unreadable setting as absent.

    Used by name resolution, which promises never to raise: the caller has not
    asked for anything to be built yet, so "I could not read the name" is best
    answered with the documented default.
    """
    try:
        return _config_value(config, *names)
    except ValueError:
        return None


def _first_str(*values: Any) -> str:
    """Return the first non-empty string among ``values``, else ``''``."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_float(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not numeric.

    A raising attribute is treated as absent: the provider factory promises never to
    raise, and a broken endpoint setting must degrade to the disabled provider
    rather than propagate out of configuration resolution.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, AttributeError):
        return None


def _bool_setting(value: Any, *, default: bool) -> bool:
    """Read one boolean setting, keeping ``default`` when the text says nothing.

    Environment values arrive as text, and the two ways an operator turns a switch
    off - ``0`` and ``false`` - both have to work, so the recognised spellings are
    listed rather than left to ``bool(value)`` (where the string ``"0"`` is truthy
    and the switch could never be turned off).
    """
    if not isinstance(value, str) or not value.strip():
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    LOGGER.warning("Unrecognised boolean setting %r; keeping %s", value, default)
    return default


def _first_message_text(response: Any) -> str:
    """Return the assistant text of an OpenAI-compatible response.

    Args:
        response: Decoded response mapping.

    Returns:
        The assistant message content.

    Raises:
        ValueError: When the response carries no usable message.
    """
    choices = response.get("choices") if isinstance(response, Mapping) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("endpoint returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise ValueError("endpoint returned no message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("endpoint returned non-text content")
    return content


def extract_json(text: str) -> Any:
    """Extract the first JSON object from a completion reply.

    Remote endpoints fence their output, prefix it with prose, or wrap it in a
    ``suggestions`` envelope, so the reply must be located rather than assumed.

    Args:
        text: Raw completion text.

    Returns:
        The decoded object.

    Raises:
        ValueError: When no decodable JSON object is present.
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        if stripped.startswith("json"):
            stripped = stripped[4:]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return json.loads(stripped[start : end + 1])
    raise ValueError("no JSON object in model reply")


#: Explanation fields a provider must return to be considered usable.
EXPLANATION_FIELDS = ("experience", "focus", "conflict", "impulse", "inhibition", "expression")

#: How far ahead of the local clock a cached explanation's timestamp may sit
#: before it is treated as corrupt rather than as ordinary clock skew.
_FUTURE_SKEW_TOLERANCE_S = 60.0

#: Marker for a configuration read that raised instead of returning a value.
#:
#: The distinction matters: "the setting is absent" is a normal answer that means
#: "use the default", while "reading the setting blew up" means the configuration
#: is broken and the caller asked for something that cannot be built. Collapsing the
#: two would silently start a provider with an endpoint that was never readable.
_UNREADABLE = object()


def parse_explanation(payload: Any) -> dict[str, str] | None:
    """Validate a decoded emotion-explanation payload.

    Args:
        payload: Decoded JSON object from the model.

    Returns:
        A mapping of the six explanation fields, or ``None`` when malformed. A
        long field is rejected rather than truncated: a truncated psychological
        sentence is worse than none.
    """
    if not isinstance(payload, Mapping):
        return None
    result: dict[str, str] = {}
    for field_name in EXPLANATION_FIELDS:
        value = payload.get(field_name)
        if value is None:
            value = ""
        if not isinstance(value, str):
            return None
        text = value.strip()
        if len(text) > 120:
            return None
        result[field_name] = text
    if not any(result.values()):
        return None
    return result


def _ms(started: float) -> int:
    """Return elapsed milliseconds since ``started``."""
    return int((time.monotonic() - started) * 1000)
