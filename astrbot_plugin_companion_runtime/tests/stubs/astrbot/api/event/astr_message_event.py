"""Stub of ``astrbot.api.event.AstrMessageEvent``."""

from __future__ import annotations


class AstrMessageEvent:
    """Marker base class; the tests use their own lightweight event object."""

    unified_msg_origin: str = ""
    message_str: str = ""
    message_obj: object | None = None
    is_at_or_wake_command: bool = False
