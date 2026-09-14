"""OpenAI 兼容的 DeepSeek 客户端（异步、可注入、可离线测试）。

设计要点
--------
* **凭据只来自环境变量**：``base_url`` 默认 ``https://api.deepseek.com``，
  key 从 ``DEEPSEEK_API_KEY`` 读取。key 绝不写入配置、日志、断点、报告。
* **可注入 HTTP 传输层**：默认用 httpx.AsyncClient；测试可以传入
  ``httpx.MockTransport``，从而在**无网络**下完整跑通生成器。
* **不依赖 openai SDK**：只用 httpx 发标准 ``/chat/completions`` 请求，
  降低依赖面，也便于锁死重试语义。
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

import httpx

from ..config import BudgetConfig, ClientConfig, RetryConfig
from ..errors import BudgetExceeded, GeneratorError, MissingCredentialError
from ..utils.secrets import getenv_secret, redact


@dataclass
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Usage:
    """一次调用的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "Usage":
        if not payload:
            return cls()
        prompt = int(payload.get("prompt_tokens") or 0)
        completion = int(payload.get("completion_tokens") or 0)
        total = int(payload.get("total_tokens") or (prompt + completion))
        return cls(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass
class CompletionResult:
    """一次成功调用的结果。"""

    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    request_id: str = ""
    attempts: int = 1


class AsyncTransport(Protocol):
    """最小传输层协议，便于测试注入。"""

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_s: float,
    ) -> tuple[int, dict[str, Any]]: ...


class HttpxTransport:
    """基于 httpx.AsyncClient 的默认传输层。

    Args:
        max_connections: 连接池上限。
        transport: 可注入的 ``httpx.AsyncBaseTransport``。
            测试用 ``httpx.MockTransport`` 注入假服务端，
            这样**真实 HTTP 代码路径**（含 JSON 解析与状态码处理）
            可以在完全无网络的情况下被覆盖，而不是只测一个假的协议实现。
    """

    def __init__(
        self,
        *,
        max_connections: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._limits = httpx.Limits(
            max_connections=max_connections, max_keepalive_connections=max_connections
        )
        self._injected_transport = transport
        self._client: httpx.AsyncClient | None = client

    async def __aenter__(self) -> "HttpxTransport":
        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=self._limits, transport=self._injected_transport
            )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_s: float,
    ) -> tuple[int, dict[str, Any]]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=self._limits, transport=self._injected_transport
            )
        response = await self._client.post(
            url, headers=headers, json=payload, timeout=timeout_s
        )
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            body = {"raw_text": response.text[:2000]}
        return response.status_code, body


class BudgetGuard:
    """预算守卫：请求数 / token / 美元三重上限。

    任一超限即抛 :class:`BudgetExceeded`；调用方负责保留断点并优雅退出。
    """

    def __init__(self, config: BudgetConfig) -> None:
        self.config = config
        self.usage = Usage()
        self.requests = 0

    def estimated_usd(self, usage: Usage | None = None) -> float:
        data = usage or self.usage
        return (
            data.prompt_tokens / 1_000_000 * self.config.usd_per_million_prompt_tokens
            + data.completion_tokens
            / 1_000_000
            * self.config.usd_per_million_completion_tokens
        )

    def check_can_start(self) -> None:
        """发起请求前检查。"""
        if self.requests >= self.config.max_requests:
            raise BudgetExceeded(
                f"已达请求上限 {self.config.max_requests}，停止生成（断点已保留）"
            )
        if self.usage.prompt_tokens >= self.config.max_prompt_tokens:
            raise BudgetExceeded(
                f"已达 prompt token 上限 {self.config.max_prompt_tokens}，停止生成"
            )
        if self.usage.total_tokens >= self.config.max_total_tokens:
            raise BudgetExceeded(
                f"已达总 token 上限 {self.config.max_total_tokens}，停止生成"
            )
        if self.estimated_usd() >= self.config.max_usd:
            raise BudgetExceeded(
                f"已达预算上限 ${self.config.max_usd:.2f}（估算已用 "
                f"${self.estimated_usd():.4f}），停止生成"
            )

    def record(self, usage: Usage) -> None:
        self.requests += 1
        self.usage = self.usage + usage

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
            "estimated_usd": round(self.estimated_usd(), 6),
            "limits": {
                "max_requests": self.config.max_requests,
                "max_prompt_tokens": self.config.max_prompt_tokens,
                "max_completion_tokens": self.config.max_completion_tokens,
                "max_total_tokens": self.config.max_total_tokens,
                "max_usd": self.config.max_usd,
            },
        }


class DeepSeekClient:
    """OpenAI 兼容 chat completions 客户端。

    Args:
        config: 客户端配置（model / base_url / 采样参数）。
        retry: 重试与退避配置。
        transport: 可注入传输层；None 时使用 httpx。
        api_key: 显式注入的 key（测试用）。生产路径应留空，从环境变量读。
    """

    def __init__(
        self,
        config: ClientConfig | None = None,
        retry: RetryConfig | None = None,
        *,
        transport: AsyncTransport | None = None,
        api_key: str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or ClientConfig()
        self.retry = retry or RetryConfig()
        self._transport = transport
        self._owns_transport = transport is None
        self._api_key = api_key
        self._rng = rng or random.Random(0)

    # -- 凭据 ---------------------------------------------------------------

    def resolve_api_key(self) -> str:
        """解析 API key：显式注入优先，否则读环境变量。"""
        if self._api_key:
            return self._api_key
        key = getenv_secret(self.config.api_key_env)
        if not key:
            raise MissingCredentialError(self.config.api_key_env)
        self._api_key = key
        return key

    def has_credentials(self) -> bool:
        """只检查是否可解析，不抛异常；用于 --dry-run。"""
        try:
            self.resolve_api_key()
            return True
        except MissingCredentialError:
            return False

    @property
    def endpoint(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.resolve_api_key()}",
            "Content-Type": "application/json",
        }
        headers.update(self.config.extra_headers)
        return headers

    # -- 生命周期 -----------------------------------------------------------

    async def __aenter__(self) -> "DeepSeekClient":
        if self._transport is None:
            transport = HttpxTransport(max_connections=self.config.max_connections)
            await transport.__aenter__()
            self._transport = transport
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_transport and self._transport is not None:
            closer = getattr(self._transport, "aclose", None)
            if closer is not None:
                await closer()
        if self._owns_transport:
            self._transport = None

    # -- 调用 ---------------------------------------------------------------

    def build_payload(
        self,
        messages: Sequence[ChatMessage],
        *,
        force_json: bool | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        want_json = self.config.response_format_json if force_json is None else force_json
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [message.to_dict() for message in messages],
            "temperature": (
                self.config.temperature if temperature is None else temperature
            ),
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        if extra:
            payload.update(extra)
        return payload

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        force_json: bool | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> CompletionResult:
        """发送一次 chat completion，带指数退避重试。

        Raises:
            MissingCredentialError: 无 API key。
            GeneratorError: 所有重试都失败。
        """
        if self._transport is None:
            raise GeneratorError(
                "未初始化传输层。请用 `async with DeepSeekClient(...) as client:` "
                "或在构造时注入 transport。"
            )

        payload = self.build_payload(
            messages,
            force_json=force_json,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=extra,
        )

        last_error: str = "未知错误"
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                status, body = await self._transport.post_json(
                    self.endpoint,
                    headers=self._headers(),
                    payload=payload,
                    timeout_s=self.config.request_timeout_s,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"网络错误：{type(exc).__name__}"
            else:
                if status == 200:
                    return self._parse_success(body, attempt)
                last_error = f"HTTP {status}：{_short_error(body)}"
                if status not in self.retry.retry_status_codes:
                    raise GeneratorError(
                        f"不可重试的服务端错误。{redact(last_error)}"
                    )

            if attempt < self.retry.max_attempts:
                await asyncio.sleep(self._backoff(attempt))

        raise GeneratorError(
            f"重试 {self.retry.max_attempts} 次后仍失败：{redact(last_error)}"
        )

    def _backoff(self, attempt: int) -> float:
        base = self.retry.initial_backoff_s * (
            self.retry.backoff_multiplier ** (attempt - 1)
        )
        jitter = self._rng.uniform(0.0, self.retry.jitter_s)
        return min(base + jitter, self.retry.max_backoff_s)

    @staticmethod
    def _parse_success(body: Mapping[str, Any], attempt: int) -> CompletionResult:
        choices = body.get("choices") or []
        if not choices:
            raise GeneratorError(f"服务端返回没有 choices：{redact(str(body))[:200]}")

        first = choices[0] or {}
        message = first.get("message") or {}
        content = message.get("content")
        if content is None:
            # 某些部署把结构化输出放在 reasoning_content / text
            content = message.get("reasoning_content") or first.get("text") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        return CompletionResult(
            text=content,
            usage=Usage.from_payload(body.get("usage")),
            model=str(body.get("model") or ""),
            finish_reason=str(first.get("finish_reason") or ""),
            request_id=str(body.get("id") or ""),
            attempts=attempt,
        )


def _short_error(body: Mapping[str, Any]) -> str:
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping):
        return str(error.get("message") or error)[:300]
    if error:
        return str(error)[:300]
    return str(body)[:300]
