"""Minimal stand-in for the AstrBot public API surface this plugin imports.

These modules exist *only* so ``tests/test_plugin_integration.py`` can exercise
``main.py`` wiring without a checkout of AstrBot. Shapes mirror AstrBot 4.28:

* ``astrbot.api.event.filter`` exposes ``command`` / ``on_llm_request`` /
  ``after_message_sent`` / ``custom_filter`` (and ``CustomFilter``), which is the
  documented plugin registration surface.
* ``astrbot.api.event.MessageChain`` and ``astrbot.api.message_components.Plain``
  are the documented outbound message types.
* ``astrbot.api.star.Star`` mirrors ``Star.__init__(context, config=None)`` and
  the per-plugin ``self.logger`` created by the real base class.
* ``astrbot.api.provider.ProviderRequest`` carries ``extra_user_content_parts``,
  the documented injection point for dynamic per-round context.
* ``astrbot.core.agent.message.TextPart`` is the documented content part with
  ``mark_as_temp()``.

Nothing here is shipped to AstrBot; the plugin never imports this package.
"""

from .api import logger

__all__ = ["logger"]
