"""Shared deterministic identity for acknowledged interaction exposures."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5


def canonical_exposure_id(scope_key: str, attempt_id: str) -> str:
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key must be non-empty")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("attempt_id must be non-empty")
    try:
        return str(UUID(attempt_id))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, f"prepared-exposure:{scope_key}:{attempt_id}"))


__all__ = ["canonical_exposure_id"]
