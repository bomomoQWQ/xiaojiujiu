"""Isolated contracts for 「浪潮」 social-emotional memory v1.

The records in this module are intentionally detached from Runtime and context
assembly.  They only carry proposals: a model or deterministic builder cannot
write an adopted goal, a permission, or an execution decision through them.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, TypeAlias

LANGCHAO_SOCIAL_CONTRACT_VERSION = "langchao.social.v1"
LANGCHAO_SOCIAL_BUILDER_VERSION = "langchao.social.rules.v1"

JsonScalar: TypeAlias = None | str | bool | int | float
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | tuple[tuple[str, "FrozenJson"], ...]
FrozenObject: TypeAlias = tuple[tuple[str, FrozenJson], ...]


class SocialItemType(str, Enum):
    SHARED_MATTER = "shared_matter"
    PARTICIPATION_FACT = "participation_fact"
    CONFIRMED_ARRANGEMENT = "confirmed_arrangement"
    BOUNDARY_REFERENCE = "boundary_reference"
    INTERPRETIVE_BASIS = "interpretive_basis"
    UNCERTAINTY = "uncertainty"
    COUNTEREVIDENCE = "counterevidence"
    GOAL_DRAFT = "goal_draft"


class SourceKind(str, Enum):
    EVENT = "event"
    MEMORY = "memory"
    MEMORY_REVISION = "memory_revision"
    USER_STATEMENT = "user_statement"
    ASSISTANT_ACTION = "assistant_action"
    BOUNDARY = "boundary"
    ARRANGEMENT = "arrangement"
    RUNTIME_FACT = "runtime_fact"
    INTERNAL_STATE = "internal_state"


class SocialRole(str, Enum):
    USER = "user"
    COMPANION = "companion"
    SHARED = "shared"
    SYSTEM = "system"
    THIRD_PARTY = "third_party"
    UNKNOWN = "unknown"


class SocialRelation(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    REFERS_TO = "refers_to"
    BOUNDS = "bounds"
    CONCERNS = "concerns"


class ProposalStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class SocialItemStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    TOMBSTONED = "tombstoned"
    INVALIDATED = "invalidated"


def _text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def _revision(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")


def _hash(name: str, value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _tuple(name: str, value: tuple[Any, ...]) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be a tuple")


def _json_value(value: FrozenJson, *, object_position: bool = False) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if not isinstance(value, tuple):
        raise TypeError("JSON values must be immutable tuples and JSON scalars")
    is_object = bool(value) and all(isinstance(x, tuple) and len(x) == 2 for x in value)
    if object_position or is_object:
        keys: list[str] = []
        for entry in value:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError("JSON objects must be tuples of (key, value) pairs")
            key, child = entry
            _text("JSON object key", key)
            keys.append(key)
            _json_value(child)
        if len(keys) != len(set(keys)):
            raise ValueError("JSON object keys must be unique")
        return
    for child in value:
        _json_value(child)


def thaw_json(value: FrozenJson) -> Any:
    """Return an ordinary JSON value without evaluating any contained text."""

    if isinstance(value, tuple):
        if value and all(isinstance(x, tuple) and len(x) == 2 for x in value):
            return {key: thaw_json(child) for key, child in value}
        return [thaw_json(child) for child in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRef:
    scope_key: str
    source_kind: SourceKind
    source_id: str
    source_revision: int
    source_sha256: str
    observed_at: datetime
    locator: str | None = None
    contract_version: str = LANGCHAO_SOCIAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _text("scope_key", self.scope_key)
        if not isinstance(self.source_kind, SourceKind):
            raise TypeError("source_kind must be a SourceKind")
        _text("source_id", self.source_id)
        _revision("source_revision", self.source_revision)
        _hash("source_sha256", self.source_sha256)
        _utc("observed_at", self.observed_at)
        if self.locator is not None:
            _text("locator", self.locator)
        if self.contract_version != LANGCHAO_SOCIAL_CONTRACT_VERSION:
            raise ValueError("unsupported social contract version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_sha256": self.source_sha256,
            "observed_at": self.observed_at.isoformat(),
            "locator": self.locator,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SocialItemProposal:
    item_key: str
    scope_key: str
    item_type: SocialItemType
    role: SocialRole
    summary: str
    sources: tuple[SourceRef, ...]
    attributes: FrozenObject = ()
    status: SocialItemStatus = SocialItemStatus.PROPOSED
    supersedes_item_key: str | None = None
    contract_version: str = LANGCHAO_SOCIAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _text("item_key", self.item_key)
        _text("scope_key", self.scope_key)
        if not isinstance(self.item_type, SocialItemType):
            raise TypeError("item_type must be a SocialItemType")
        if not isinstance(self.role, SocialRole):
            raise TypeError("role must be a SocialRole")
        if not isinstance(self.status, SocialItemStatus):
            raise TypeError("status must be a SocialItemStatus")
        if self.status is not SocialItemStatus.PROPOSED:
            raise ValueError("proposal DTOs may only contain proposed items")
        _text("summary", self.summary)
        _tuple("sources", self.sources)
        if not self.sources:
            raise ValueError("social items require at least one traceable source")
        if any(not isinstance(source, SourceRef) for source in self.sources):
            raise TypeError("sources must contain only SourceRef values")
        if any(source.scope_key != self.scope_key for source in self.sources):
            raise ValueError("item and all sources must share scope_key")
        if len({(s.source_kind, s.source_id, s.source_revision) for s in self.sources}) != len(self.sources):
            raise ValueError("sources must not contain duplicates")
        _json_value(self.attributes, object_position=True)
        if self.supersedes_item_key is not None:
            _text("supersedes_item_key", self.supersedes_item_key)
            if self.supersedes_item_key == self.item_key:
                raise ValueError("an item cannot supersede itself")
        if self.item_type is SocialItemType.GOAL_DRAFT and self.status is not SocialItemStatus.PROPOSED:
            raise ValueError("a social builder cannot adopt a goal")
        if self.contract_version != LANGCHAO_SOCIAL_CONTRACT_VERSION:
            raise ValueError("unsupported social contract version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "scope_key": self.scope_key,
            "item_type": self.item_type.value,
            "role": self.role.value,
            "summary": self.summary,
            "sources": [source.to_dict() for source in self.sources],
            "attributes": {key: thaw_json(value) for key, value in self.attributes},
            "status": self.status.value,
            "supersedes_item_key": self.supersedes_item_key,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SocialLinkProposal:
    link_key: str
    scope_key: str
    relation: SocialRelation
    from_item_key: str
    to_item_key: str
    sources: tuple[SourceRef, ...]
    attributes: FrozenObject = ()
    contract_version: str = LANGCHAO_SOCIAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("link_key", "scope_key", "from_item_key", "to_item_key"):
            _text(name, getattr(self, name))
        if self.from_item_key == self.to_item_key:
            raise ValueError("social links must connect two different items")
        if not isinstance(self.relation, SocialRelation):
            raise TypeError("relation must be a SocialRelation")
        _tuple("sources", self.sources)
        if not self.sources or any(not isinstance(source, SourceRef) for source in self.sources):
            raise ValueError("links require traceable SourceRef values")
        if any(source.scope_key != self.scope_key for source in self.sources):
            raise ValueError("link and all sources must share scope_key")
        _json_value(self.attributes, object_position=True)
        if self.contract_version != LANGCHAO_SOCIAL_CONTRACT_VERSION:
            raise ValueError("unsupported social contract version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_key": self.link_key,
            "scope_key": self.scope_key,
            "relation": self.relation.value,
            "from_item_key": self.from_item_key,
            "to_item_key": self.to_item_key,
            "sources": [source.to_dict() for source in self.sources],
            "attributes": {key: thaw_json(value) for key, value in self.attributes},
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SocialProposal:
    proposal_id: str
    scope_key: str
    revision: int
    created_at: datetime
    items: tuple[SocialItemProposal, ...]
    links: tuple[SocialLinkProposal, ...] = ()
    status: ProposalStatus = ProposalStatus.PROPOSED
    proposal_sha256: str = ""
    builder_version: str = LANGCHAO_SOCIAL_BUILDER_VERSION
    contract_version: str = LANGCHAO_SOCIAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _text("proposal_id", self.proposal_id)
        _text("scope_key", self.scope_key)
        _revision("revision", self.revision)
        _utc("created_at", self.created_at)
        _tuple("items", self.items)
        _tuple("links", self.links)
        if not isinstance(self.status, ProposalStatus):
            raise TypeError("status must be a ProposalStatus")
        if self.status is not ProposalStatus.PROPOSED:
            raise ValueError("builders may emit only proposed SocialProposal values")
        if any(item.scope_key != self.scope_key for item in self.items):
            raise ValueError("proposal and items must share scope_key")
        if any(link.scope_key != self.scope_key for link in self.links):
            raise ValueError("proposal and links must share scope_key")
        item_keys = [item.item_key for item in self.items]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("item_key values must be unique in a proposal")
        link_keys = [link.link_key for link in self.links]
        if len(link_keys) != len(set(link_keys)):
            raise ValueError("link_key values must be unique in a proposal")
        if any(link.from_item_key not in item_keys or link.to_item_key not in item_keys for link in self.links):
            raise ValueError("links may only reference items in the same proposal")
        _text("builder_version", self.builder_version)
        if self.contract_version != LANGCHAO_SOCIAL_CONTRACT_VERSION:
            raise ValueError("unsupported social contract version")
        expected = sha256_json(self.payload_dict())
        if self.proposal_sha256:
            _hash("proposal_sha256", self.proposal_sha256)
            if self.proposal_sha256 != expected:
                raise ValueError("proposal_sha256 does not match canonical proposal payload")
        else:
            object.__setattr__(self, "proposal_sha256", expected)

    def payload_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "scope_key": self.scope_key,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "items": [item.to_dict() for item in self.items],
            "links": [link.to_dict() for link in self.links],
            "status": self.status.value,
            "builder_version": self.builder_version,
            "contract_version": self.contract_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "proposal_sha256": self.proposal_sha256}


__all__ = [
    "FrozenJson", "FrozenObject", "LANGCHAO_SOCIAL_BUILDER_VERSION",
    "LANGCHAO_SOCIAL_CONTRACT_VERSION", "ProposalStatus", "SocialItemProposal",
    "SocialItemStatus", "SocialItemType", "SocialLinkProposal", "SocialProposal",
    "SocialRelation", "SocialRole", "SourceKind", "SourceRef", "canonical_json",
    "sha256_json", "thaw_json",
]
