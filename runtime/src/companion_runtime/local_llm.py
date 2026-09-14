"""Local CPU model integration (Level 2) with strict degradation.

The Runtime must keep working when no model is available, so this module is
written as an *optional accelerator* in front of the deterministic rule-based
appraiser:

* :class:`LocalModelClient` talks to any OpenAI-compatible chat endpoint - in
  practice a ``llama.cpp`` server hosting the fine-tuned 2B model on the CPU.
* Every call has a hard deadline, and every failure (unreachable, timeout,
  malformed JSON, schema violation, invariant violation) degrades to the
  rule-based path instead of raising.

The model only ever *proposes* an appraisal. It never writes state, never emits
final emotion values, and never decides whether to speak: those stay with the
Reducer and the motivation layer.

Only the standard library is used so the sidecar keeps its tiny dependency set.
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
from typing import Any, Callable, Mapping

from .typing import EmotionDirection, EmotionEvaluation
from .utility import clamp

LOGGER = logging.getLogger("companion_runtime.local_llm")

#: Directions the appraisal contract allows. ``+-`` marks a mixed event.
ALLOWED_DIRECTIONS = frozenset({"+", "-", "0", "+-"})

#: Relation signals, ordered from approach to distance.
ALLOWED_RELATION_SIGNALS = frozenset(
    {
        "closeness",
        "strong_approach",
        "slight_approach",
        "approach",
        "neutral",
        "slight_distance",
        "distance",
        "strong_distance",
        "boundary_declare",
        "repair",
        "busy",
        "good_news",
        "bad_news",
        "apology",
        "appreciation",
        "amusement",
        "crisis",
        "loss",
        "worry",
        "sorrow",
        "fatigue",
        "guilt",
        "uncertain",
        "contact",
        "other",
        "self",
    }
)

ALLOWED_RESPONSIBILITY = frozenset(
    {"self", "user", "third_party", "other", "circumstance", "shared", "unclear"}
)

#: Appraisal field -> (minimum, maximum) for numeric fields.
NUMERIC_BOUNDS: Mapping[str, tuple[float, float]] = {
    "impact": (0.0, 1.0),
    "activation": (0.0, 1.0),
    "uncertainty": (0.0, 1.0),
    "confidence": (0.0, 1.0),
}

APPRAISAL_FIELDS = (
    "direction",
    "impact",
    "activation",
    "uncertainty",
    "relation_signal",
    "responsibility",
    "confidence",
)

#: Emotion-explanation fields (architecture section 11.3).
EXPLANATION_FIELDS = ("experience", "focus", "conflict", "impulse", "inhibition", "expression")

APPRAISAL_SYSTEM_PROMPT = (
    "你是长期陪伴角色的内部评价器。只输出一个 JSON 对象，字段固定为 "
    "direction, impact, activation, uncertainty, relation_signal, responsibility, confidence。"
    "direction 取 '+' / '-' / '0' / '+-'；数值取 0 到 1，步长 0.05；"
    "relation_signal 取 closeness / slight_approach / neutral / slight_distance / distance "
    "/ boundary_declare / repair；responsibility 取 self / user / third_party / shared / unclear。"
    "只评价事件本身，不输出任何情绪值，不给行为建议，不加解释文字。"
)

EXPLANATION_SYSTEM_PROMPT = (
    "你是长期陪伴角色的情绪解释器：把已有的结构化心理状态翻译成第一人称心理语言。"
    "只输出一个 JSON 对象，字段固定为 experience, focus, conflict, impulse, inhibition, expression。"
    "每个字段一句话，20 到 40 字，不出现数字，不生成台词，不创造输入中不存在的事件，"
    "不放大也不压低底层情绪强度，没有冲突就不要虚构冲突。"
)


@dataclass(slots=True)
class LocalModelConfig:
    """Connection and budget settings for the local CPU model.

    Attributes:
        enabled: Master switch; when ``False`` the Runtime stays rule-based.
        base_url: OpenAI-compatible base URL, e.g. ``http://127.0.0.1:8080/v1``.
        model: Model name sent to the endpoint (llama.cpp ignores it mostly).
        api_key: Optional bearer token for a protected local endpoint.
        appraise_timeout_s: Hard deadline for an appraisal call.
        explain_timeout_s: Hard deadline for an explanation call.
        max_tokens: Completion cap.
        temperature: Sampling temperature; 0 keeps structured output stable.
        grammar_appraisal: GBNF grammar name for constrained appraisal decoding.
        grammar_explanation: GBNF grammar name for constrained explanation decoding.
        cache_ttl_s: How long an explanation stays valid for an unchanged state.
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "qboss-2b"
    api_key: str = ""
    appraise_timeout_s: float = 1.2
    explain_timeout_s: float = 6.0
    max_tokens: int = 256
    temperature: float = 0.0
    grammar_appraisal: str = "appraisal"
    grammar_explanation: str = "explanation"
    cache_ttl_s: float = 900.0
    #: Extra request headers, e.g. a reverse-proxy token.
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LocalModelConfig":
        """Build a config from environment variables.

        Recognised variables: ``CR_LOCAL_MODEL_ENABLED``,
        ``CR_LOCAL_MODEL_BASE_URL``, ``CR_LOCAL_MODEL_NAME`` and
        ``CR_LOCAL_MODEL_API_KEY``. The API key is read from the environment and
        never written back to disk or logs.

        Args:
            env: Environment mapping, defaults to :data:`os.environ`.

        Returns:
            A populated configuration.
        """
        source = os.environ if env is None else env
        enabled = str(source.get("CR_LOCAL_MODEL_ENABLED", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            enabled=enabled,
            base_url=str(source.get("CR_LOCAL_MODEL_BASE_URL", cls.base_url)).rstrip("/"),
            model=str(source.get("CR_LOCAL_MODEL_NAME", cls.model)),
            api_key=str(source.get("CR_LOCAL_MODEL_API_KEY", "")),
        )


@dataclass(slots=True)
class AppraisalResult:
    """Outcome of one semantic appraisal attempt."""

    evaluation: EmotionEvaluation | None
    degraded: bool
    reason: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "degraded": self.degraded,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
            "evaluation": self.evaluation.to_dict() if self.evaluation else None,
        }


def parse_appraisal(payload: Any) -> EmotionEvaluation | None:
    """Validate a decoded appraisal payload against the frozen contract.

    Args:
        payload: Decoded JSON object from the model.

    Returns:
        A validated :class:`EmotionEvaluation`, or ``None`` when the payload
        violates the contract. A ``None`` result must be treated as a
        degradation trigger, never repaired by guessing.
    """
    if not isinstance(payload, Mapping):
        return None
    if any(field_name not in payload for field_name in APPRAISAL_FIELDS):
        return None
    direction = payload.get("direction")
    if direction not in ALLOWED_DIRECTIONS:
        return None
    relation = payload.get("relation_signal")
    if not isinstance(relation, str) or relation not in ALLOWED_RELATION_SIGNALS:
        return None
    responsibility = payload.get("responsibility")
    if not isinstance(responsibility, str) or responsibility not in ALLOWED_RESPONSIBILITY:
        return None

    numbers: dict[str, float] = {}
    for name, (low, high) in NUMERIC_BOUNDS.items():
        raw = payload.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        value = float(raw)
        if value < low or value > high:
            return None
        numbers[name] = clamp(value)

    # A neutral direction with a large impact is the classic self-contradiction
    # the training data is explicitly built to avoid; reject instead of trusting.
    if direction == "0" and numbers["impact"] > 0.25:
        return None
    if direction in {"+", "-", "+-"} and numbers["impact"] <= 0.05:
        return None

    return EmotionEvaluation(
        direction=str(direction),
        impact=numbers["impact"],
        activation=numbers["activation"],
        uncertainty=numbers["uncertainty"],
        relation_signal=str(relation),
        responsibility=str(responsibility),
        confidence=numbers["confidence"],
        source="local_model",
    )


def parse_explanation(payload: Any) -> dict[str, str] | None:
    """Validate a decoded emotion-explanation payload.

    Args:
        payload: Decoded JSON object from the model.

    Returns:
        A mapping of the six explanation fields, or ``None`` when malformed.
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


def _extract_json(text: str) -> Any:
    """Extract the first JSON object from a model reply.

    Args:
        text: Raw completion text, possibly fenced or wrapped in prose.

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


class LocalModelClient:
    """A tiny, dependency-free OpenAI-compatible client with hard deadlines.

    Args:
        config: Connection settings.
        transport: Optional injectable transport used by tests. It receives the
            request URL, the JSON body, the timeout and the headers, and returns
            the decoded response mapping.
    """

    def __init__(
        self,
        config: LocalModelConfig | None = None,
        *,
        transport: Callable[[str, dict[str, Any], float, dict[str, str]], Mapping[str, Any]]
        | None = None,
    ) -> None:
        """Store the configuration and transport."""
        self.config = config or LocalModelConfig()
        self._transport = transport or self._http_transport
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        self.stats: dict[str, int] = {
            "appraise_calls": 0,
            "appraise_ok": 0,
            "appraise_degraded": 0,
            "explain_calls": 0,
            "explain_ok": 0,
            "explain_degraded": 0,
            "cache_hits": 0,
        }

    # ------------------------------------------------------------------ transport

    def _http_transport(
        self, url: str, body: dict[str, Any], timeout: float, headers: dict[str, str]
    ) -> Mapping[str, Any]:
        """Perform the HTTP call.

        Args:
            url: Fully qualified request URL.
            body: JSON request body.
            timeout: Deadline in seconds.
            headers: Extra headers.

        Returns:
            The decoded JSON response.

        Raises:
            urllib.error.URLError: On any network failure.
        """
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def _chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        timeout: float,
        grammar: str | None,
    ) -> str:
        """Send one chat completion request and return the text.

        Args:
            system_prompt: System message.
            user_content: User message.
            timeout: Hard deadline.
            grammar: Optional GBNF grammar name exposed through ``grammar``.

        Returns:
            The assistant text.

        Raises:
            Exception: Propagated to the caller, which degrades.
        """
        headers = dict(self.config.headers)
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if grammar:
            # llama.cpp accepts either an inline GBNF string or a server-side
            # grammar name. A value containing ``::=`` is treated as inline GBNF;
            # anything else is resolved against the bundled grammar directory.
            resolved = grammar if "::=" in grammar else load_grammar(grammar)
            if resolved:
                body["grammar"] = resolved
        url = f"{self.config.base_url}/chat/completions"
        response = self._transport(url, body, timeout, headers)
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

    # ----------------------------------------------------------------- appraisal

    def appraise(
        self,
        event_text: str,
        *,
        context_summary: str = "",
        boundary_state: str = "",
        timeout_s: float | None = None,
    ) -> AppraisalResult:
        """Ask the local model to appraise one event.

        Args:
            event_text: The event to appraise, already normalised.
            context_summary: Short working-situation summary.
            boundary_state: Human-readable boundary state.
            timeout_s: Override for the hard deadline.

        Returns:
            An :class:`AppraisalResult`; ``degraded`` is ``True`` whenever the
            caller must fall back to the rule-based appraiser.
        """
        started = time.monotonic()
        if not self.config.enabled:
            return AppraisalResult(None, True, "disabled")
        if not (event_text or "").strip():
            return AppraisalResult(None, True, "empty_event")

        user_content = json.dumps(
            {
                "event": {"speaker": "user", "text": event_text},
                "context": context_summary,
                "boundary_state": boundary_state,
            },
            ensure_ascii=False,
        )
        deadline = float(timeout_s if timeout_s is not None else self.config.appraise_timeout_s)

        with self._lock:
            self.stats["appraise_calls"] += 1
        try:
            raw = self._chat(
                APPRAISAL_SYSTEM_PROMPT,
                user_content,
                timeout=deadline,
                grammar=self.config.grammar_appraisal,
            )
            evaluation = parse_appraisal(_extract_json(raw))
        except Exception as exc:  # noqa: BLE001 - any failure means degradation
            with self._lock:
                self.stats["appraise_degraded"] += 1
            LOGGER.debug("local appraisal degraded: %s", exc)
            return AppraisalResult(None, True, f"error:{type(exc).__name__}", _ms(started))

        if evaluation is None:
            with self._lock:
                self.stats["appraise_degraded"] += 1
            return AppraisalResult(None, True, "contract_violation", _ms(started))

        with self._lock:
            self.stats["appraise_ok"] += 1
        return AppraisalResult(evaluation, False, "", _ms(started))

    # --------------------------------------------------------------- explanation

    def explain(
        self,
        state_payload: Mapping[str, Any],
        *,
        state_key: str = "",
        timeout_s: float | None = None,
    ) -> tuple[dict[str, str] | None, bool]:
        """Ask the local model for a first-person explanation of a state.

        Args:
            state_payload: Structured psychological state (no long history).
            state_key: Stable key used for caching; the cache is invalidated by
                any change to the key.
            timeout_s: Override for the hard deadline.

        Returns:
            ``(explanation_or_None, cached)``. A ``None`` explanation means the
            caller must use the deterministic template.
        """
        if not self.config.enabled:
            return None, False

        if state_key:
            cached = self._cache_get(state_key)
            if cached is not None:
                with self._lock:
                    self.stats["cache_hits"] += 1
                return cached, True

        deadline = float(timeout_s if timeout_s is not None else self.config.explain_timeout_s)
        with self._lock:
            self.stats["explain_calls"] += 1
        try:
            raw = self._chat(
                EXPLANATION_SYSTEM_PROMPT,
                json.dumps(state_payload, ensure_ascii=False, sort_keys=True),
                timeout=deadline,
                grammar=self.config.grammar_explanation,
            )
            explanation = parse_explanation(_extract_json(raw))
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.stats["explain_degraded"] += 1
            LOGGER.debug("local explanation degraded: %s", exc)
            return None, False

        if explanation is None:
            with self._lock:
                self.stats["explain_degraded"] += 1
            return None, False

        with self._lock:
            self.stats["explain_ok"] += 1
        if state_key:
            self._cache_put(state_key, explanation)
        return explanation, False

    # ---------------------------------------------------------------------- cache

    def _cache_get(self, key: str) -> dict[str, str] | None:
        """Return a cached explanation when it is still fresh."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if time.monotonic() - stored_at > self.config.cache_ttl_s:
                self._cache.pop(key, None)
                return None
            return dict(value)

    def _cache_put(self, key: str, value: Mapping[str, str]) -> None:
        """Store one explanation, bounding the cache size."""
        with self._lock:
            self._cache[key] = (time.monotonic(), dict(value))
            while len(self._cache) > 128:
                self._cache.pop(next(iter(self._cache)))

    def invalidate(self) -> None:
        """Drop every cached explanation."""
        with self._lock:
            self._cache.clear()

    def health(self) -> dict[str, Any]:
        """Return a JSON-serialisable health snapshot."""
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "base_url": self.config.base_url,
                "model": self.config.model,
                "stats": dict(self.stats),
                "cache_entries": len(self._cache),
            }


def _ms(started: float) -> int:
    """Return elapsed milliseconds since ``started``."""
    return int((time.monotonic() - started) * 1000)


def load_grammar(name: str, directory: str | None = None) -> str | None:
    """Load a GBNF grammar file shipped with the Runtime.

    Args:
        name: Grammar base name, e.g. ``appraisal``.
        directory: Override for the grammar directory.

    Returns:
        The grammar text, or ``None`` when the file is absent. A missing grammar
        only removes constrained decoding; it never breaks the Runtime.
    """
    from pathlib import Path

    base = Path(directory) if directory else Path(__file__).resolve().parents[2] / "grammars"
    path = base / f"{name}.gbnf"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")
