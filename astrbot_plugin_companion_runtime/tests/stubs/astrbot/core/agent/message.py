"""Stub of ``astrbot.core.agent.message`` (TextPart and friends)."""

from __future__ import annotations


class ContentPart:
    """Mirrors the real content part base class."""

    type: str = ""

    def __init__(self) -> None:
        self._no_save = False

    def mark_as_temp(self):
        """Mark this part as provider-facing only, not persisted."""
        self._no_save = True
        return self


class TextPart(ContentPart):
    """Mirrors ``TextPart(text=...)``."""

    type = "text"

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.text = text
