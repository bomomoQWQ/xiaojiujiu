"""Dependency-free value coercion helpers.

The adapter reads values from three loosely typed sources: the AstrBot plugin
config (the WebUI may hand values over as strings), HTTP responses from the
Runtime, and the Runtime's own action payloads. Everything crossing those
boundaries goes through the helpers below so a malformed value can never raise.
"""

from __future__ import annotations

from typing import Any

_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "on", "t"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "off", "f", ""})


def as_str(value: Any, default: str = "") -> str:
    """Coerce ``value`` to ``str``, falling back to ``default``."""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return default


def as_bool(value: Any, default: bool = False) -> bool:
    """Coerce ``value`` to ``bool``, falling back to ``default``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return default


def as_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to ``int``, falling back to ``default``."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        token = value.strip()
        if token:
            try:
                return int(float(token))
            except ValueError:
                return default
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to ``float``, falling back to ``default``."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        token = value.strip()
        if token:
            try:
                return float(token)
            except ValueError:
                return default
    return default


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into the inclusive ``[low, high]`` range."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def as_mapping(value: Any) -> dict[str, Any]:
    """Return a shallow ``dict`` copy of a mapping-like value, else ``{}``."""
    if isinstance(value, dict):
        return dict(value)
    return {}
