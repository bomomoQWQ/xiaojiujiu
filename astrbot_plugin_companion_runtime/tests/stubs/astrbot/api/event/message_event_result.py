"""Stub of ``astrbot.api.event.MessageChain`` / ``MessageEventResult``."""

from __future__ import annotations

from astrbot.api.message_components import BaseMessageComponent, Plain


class MessageChain:
    """Mirrors the real dataclass shape: ``MessageChain([Plain("hi")])``."""

    def __init__(self, chain: list[BaseMessageComponent] | None = None) -> None:
        self.chain: list[BaseMessageComponent] = list(chain or [])

    def message(self, message: str) -> MessageChain:
        """Append a plain text component."""
        self.chain.append(Plain(message))
        return self

    def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
        """Return the concatenated text of the chain."""
        del with_other_comps_mark
        return "".join(part.text for part in self.chain if isinstance(part, Plain))


class MessageEventResult(MessageChain):
    """Stub of the result wrapper returned by ``event.get_result()``."""

    def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
        """Return the concatenated text of the result."""
        del with_other_comps_mark
        return "".join(part.text for part in self.chain if isinstance(part, Plain))
