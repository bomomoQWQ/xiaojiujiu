"""Stub of ``astrbot.api.star``."""

from __future__ import annotations

import logging
from typing import Any


class Context:
    """Stand-in for the plugin context; the integration test supplies its own."""


class Star:
    """Mirrors ``Star.__init__(context, config=None)`` plus ``self.logger``."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        del config
        self.context = context
        self.logger = logging.getLogger("astrbot.plugin.stub")

    async def initialize(self) -> None:
        """Called when the plugin is activated."""

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
