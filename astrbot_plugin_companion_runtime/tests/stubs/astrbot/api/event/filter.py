"""Stub of ``astrbot.api.event.filter``.

Mirrors the real registration decorators closely enough to record what a plugin
registers, including ``register_custom_filter`` instantiating the given
``CustomFilter`` class with ``raise_error`` positionally.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrbot.api.event.astr_message_event import AstrMessageEvent


class CustomFilter:
    """Stand-in for ``astrbot.api.event.filter.CustomFilter``."""

    def __init__(self, raise_error: bool = True, **kwargs: Any) -> None:
        del kwargs
        self.raise_error = raise_error

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        """Return whether the event passes this filter."""
        raise NotImplementedError


class Registration:
    """One recorded registration."""

    def __init__(
        self,
        kind: str,
        name: str | None,
        handler: Callable[..., Any],
        filter_instance: CustomFilter | None = None,
    ) -> None:
        self.kind = kind
        self.name = name
        self.handler = handler
        self.filter_instance = filter_instance


REGISTRATIONS: list[Registration] = []


def _record(
    kind: str,
    name: str | None,
    handler: Callable[..., Any],
    filter_instance: CustomFilter | None = None,
) -> None:
    REGISTRATIONS.append(Registration(kind, name, handler, filter_instance))


def handler_by_name(name: str) -> Callable[..., Any]:
    """Return the handler registered under ``name``."""
    for registration in REGISTRATIONS:
        if registration.name == name:
            return registration.handler
    raise AssertionError(f"no handler named {name!r} was registered")


def registration_for(name: str) -> Registration:
    """Return the full registration record for ``name``."""
    for registration in REGISTRATIONS:
        if registration.name == name:
            return registration
    raise AssertionError(f"no registration named {name!r}")


def command(name: str | None = None, **kwargs: Any):
    """Stub of ``@filter.command``."""

    def decorator(handler):
        _record("command", name or handler.__name__, handler)
        return handler

    del kwargs
    return decorator


def on_llm_request(**kwargs: Any):
    """Stub of ``@filter.on_llm_request``."""

    def decorator(handler):
        _record("on_llm_request", handler.__name__, handler)
        return handler

    del kwargs
    return decorator


def after_message_sent(**kwargs: Any):
    """Stub of ``@filter.after_message_sent``."""

    def decorator(handler):
        _record("after_message_sent", handler.__name__, handler)
        return handler

    del kwargs
    return decorator


def custom_filter(custom_type_filter, *args: Any, **kwargs: Any):
    """Stub of ``@filter.custom_filter``."""
    del kwargs
    raise_error = args[0] if args else True
    filter_instance = custom_type_filter(raise_error)
    if not isinstance(filter_instance, CustomFilter):
        raise AssertionError("custom_filter must produce a CustomFilter instance")

    def decorator(handler):
        _record("message", handler.__name__, handler, filter_instance)
        return handler

    return decorator
