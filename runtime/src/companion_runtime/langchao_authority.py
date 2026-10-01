"""Domain contracts for scoped 「浪潮」 engine authority and dispatch claims.

Authority is deliberately separate from engine execution and message sending.  An
immutable authority revision says which engine *may* dispatch; an independently
CAS-versioned pointer publishes one revision for a scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class AuthorityEngine(str, Enum):
    RUNTIME_V2 = "runtime_v2"
    LANGCHAO = "langchao"
    NONE = "none"


class AuthorityMode(str, Enum):
    LIVE = "live"
    SHADOW = "shadow"
    DISABLED = "disabled"


def authority_may_dispatch(engine_key: AuthorityEngine, mode: AuthorityMode) -> bool:
    """Return the sole dispatch predicate persisted by the v15 schema."""

    return mode is AuthorityMode.LIVE and engine_key is not AuthorityEngine.NONE


@dataclass(frozen=True, slots=True)
class AuthorityRevision:
    scope_key: str
    authority_id: str
    revision: int
    engine_key: AuthorityEngine
    mode: AuthorityMode
    may_dispatch: bool
    reason: str
    payload: Mapping[str, Any]
    payload_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActiveAuthority:
    revision: AuthorityRevision
    pointer_version: int


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    scope_key: str
    dispatch_id: str
    authority_id: str
    authority_revision: int
    engine_key: AuthorityEngine
    may_dispatch: bool
    candidate_id: str
    candidate_revision: int
    attempt_id: str
    idempotency_key: str
    claim_sha256: str
    created_at: datetime


__all__ = [
    "ActiveAuthority",
    "AuthorityEngine",
    "AuthorityMode",
    "AuthorityRevision",
    "DispatchClaim",
    "authority_may_dispatch",
]
