"""AstrBot-facing execution of leased Runtime actions.

This module lives next to ``main.py`` (not inside ``companion_runtime/``) on
purpose: the ``companion_runtime`` package is kept strictly free of AstrBot
imports so its protocol, queue, bridge, and outbox logic stay unit-testable
without a running AstrBot instance. Together with ``main.py``, this is the only
place that touches AstrBot.

Only public, documented AstrBot APIs are used:

* ``Context.get_current_chat_provider_id(umo=...)`` -- the session's current
  chat provider, honouring per-session model preferences.
* ``Context.llm_generate(chat_provider_id=..., prompt=..., system_prompt=...)``
  -- the documented SDK entry point for one-shot generation.
* ``Context.send_message(session, MessageChain)`` -- proactive delivery.
"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .companion_runtime.coerce import as_int, as_str
from .companion_runtime.protocol import LeasedAction, truncate_error
from .companion_runtime.retry_queue import NULL_LOG


class ActionExecutionError(RuntimeError):
    """Raised when a leased action cannot be executed on the host side."""


class AstrBotActionExecutor:
    """Executes ``render`` and ``send`` actions through public AstrBot APIs."""

    def __init__(self, *, context: Any, log: Any = NULL_LOG) -> None:
        """Create the executor.

        Args:
            context: The AstrBot ``Context`` handed to the plugin.
            log: Logger-like object.
        """
        self._context = context
        self._log = log

    async def render(self, action: LeasedAction) -> dict[str, Any]:
        """Render a prompt with the session's current AstrBot chat provider.

        Args:
            action: The leased ``render`` action. ``payload['prompt']`` holds the
                fully composed prompt (the Runtime owns prompt composition);
                ``payload['system_prompt']`` is optional, and
                ``payload['max_chars']`` optionally caps the returned text.

        Returns:
            A result mapping with the rendered ``text`` plus the provider id, so
            the Runtime can record which model actually spoke.

        Raises:
            ActionExecutionError: If the payload has no prompt, the session has no
                chat provider, or generation fails.
        """
        prompt = as_str(action.payload.get("prompt")).strip()
        if not prompt:
            raise ActionExecutionError("render action payload has no prompt")

        provider_id = await self._current_provider_id(action.session)
        kwargs: dict[str, Any] = {"chat_provider_id": provider_id, "prompt": prompt}
        system_prompt = as_str(action.payload.get("system_prompt")).strip()
        if system_prompt:
            kwargs["system_prompt"] = system_prompt

        try:
            response = await self._context.llm_generate(**kwargs)
        except Exception as exc:
            raise ActionExecutionError(f"llm_generate failed: {truncate_error(exc)}") from exc

        text = as_str(getattr(response, "completion_text", "")).strip()
        result: dict[str, Any] = {
            "text": text,
            "provider_id": provider_id,
            "chars": len(text),
        }
        max_chars = as_int(action.payload.get("max_chars"), 0)
        if max_chars > 0 and len(text) > max_chars:
            result["text"] = text[:max_chars]
            result["truncated"] = True
        return result

    async def send(self, action: LeasedAction, text: str) -> dict[str, Any]:
        """Deliver a proactive message to the action's session.

        Args:
            action: The leased ``send`` action; ``action.session`` is the
                ``unified_msg_origin`` the message goes to.
            text: The exact text to deliver, already authorized by the Runtime.

        Returns:
            ``{"sent": bool, "chars": int, ...}``. ``sent`` is ``False`` when no
            platform matched the session, which the Runtime records as a failure
            rather than as a delivered message.

        Raises:
            ActionExecutionError: If the text is empty or AstrBot rejects the
                session.
        """
        body = text.strip()
        if not body:
            raise ActionExecutionError("send action has no text")
        chain = MessageChain([Plain(body)])
        try:
            delivered = await self._context.send_message(action.session, chain)
        except Exception as exc:
            raise ActionExecutionError(f"send_message failed: {truncate_error(exc)}") from exc
        return {"sent": bool(delivered), "chars": len(body)}

    async def _current_provider_id(self, session: str) -> str:
        """Resolve the current chat provider id for one session."""
        try:
            provider_id = await self._context.get_current_chat_provider_id(umo=session)
        except Exception as exc:
            raise ActionExecutionError(
                f"no chat provider for session: {truncate_error(exc)}",
            ) from exc
        resolved = as_str(provider_id).strip()
        if not resolved:
            raise ActionExecutionError("session has no chat provider")
        return resolved
