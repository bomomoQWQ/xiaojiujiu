"""Stub of ``astrbot.api.event``."""

from . import filter
from .astr_message_event import AstrMessageEvent
from .message_event_result import MessageChain, MessageEventResult

__all__ = ["AstrMessageEvent", "MessageChain", "MessageEventResult", "filter"]
