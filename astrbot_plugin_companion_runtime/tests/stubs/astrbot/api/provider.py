"""Stub of ``astrbot.api.provider.ProviderRequest`` and ``LLMResponse``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderRequest:
    """Mirrors the fields of the real request object this plugin touches."""

    prompt: str | None = None
    session_id: str | None = ""
    contexts: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    extra_user_content_parts: list[Any] = field(default_factory=list)


class LLMResponse:
    """Mirrors ``LLMResponse.completion_text``."""

    def __init__(self, completion_text: str = "") -> None:
        self.completion_text = completion_text
