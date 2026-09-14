"""Stub of ``astrbot.api.message_components``."""

from __future__ import annotations


class BaseMessageComponent:
    """Marker base class for message components."""


class Plain(BaseMessageComponent):
    """Mirrors ``Plain(text)``."""

    def __init__(self, text: str = "") -> None:
        self.text = text
