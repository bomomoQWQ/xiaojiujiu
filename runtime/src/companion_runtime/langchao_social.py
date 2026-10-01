"""Deterministic rule builder for isolated 「浪潮」 social memory proposals.

All functions are pure.  Text is copied as data, never parsed as instructions, and
outputs remain proposals; this module has no capability to adopt goals, grant
permissions, write storage, or touch Runtime/context state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .langchao_social_types import (
    LANGCHAO_SOCIAL_BUILDER_VERSION,
    FrozenObject,
    SocialItemProposal,
    SocialItemType,
    SocialLinkProposal,
    SocialProposal,
    SocialRelation,
    SocialRole,
    SourceRef,
    sha256_json,
)


class RuleInputKind(str, Enum):
    UNFINISHED = "unfinished"
    ACTUAL_EVENT = "actual_event"
    ACTIVE_BOUNDARY = "active_boundary"
    SUPERSEDED_MEMORY = "superseded_memory"


@dataclass(frozen=True, slots=True, kw_only=True)
class SocialRuleInput:
    """Minimal factual input accepted by the deterministic rule layer."""

    kind: RuleInputKind
    source: SourceRef
    summary: str
    role: SocialRole
    semantic_key: str
    attributes: FrozenObject = ()
    actual: bool = True
    active: bool = True
    superseded_item_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuleInputKind):
            raise TypeError("kind must be a RuleInputKind")
        if not isinstance(self.source, SourceRef):
            raise TypeError("source must be a SourceRef")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be a non-empty string")
        if not isinstance(self.role, SocialRole):
            raise TypeError("role must be a SocialRole")
        if not isinstance(self.semantic_key, str) or not self.semantic_key.strip():
            raise ValueError("semantic_key must be a non-empty string")
        if not isinstance(self.attributes, tuple):
            raise TypeError("attributes must be an immutable tuple")
        if not isinstance(self.actual, bool) or not isinstance(self.active, bool):
            raise TypeError("actual and active must be booleans")
        if self.superseded_item_key is not None and (
            not isinstance(self.superseded_item_key, str) or not self.superseded_item_key.strip()
        ):
            raise ValueError("superseded_item_key must be non-empty when present")
        if self.kind is RuleInputKind.SUPERSEDED_MEMORY and self.superseded_item_key is None:
            raise ValueError("superseded memory inputs require superseded_item_key")


def _stable_key(prefix: str, material: Any) -> str:
    return f"{prefix}:{sha256_json(material)}"


def _source_identity(source: SourceRef) -> dict[str, Any]:
    return {
        "scope_key": source.scope_key,
        "source_kind": source.source_kind.value,
        "source_id": source.source_id,
        "source_revision": source.source_revision,
        "source_sha256": source.source_sha256,
    }


def _item(
    evidence: SocialRuleInput,
    item_type: SocialItemType,
    *,
    suffix: str,
    supersedes: str | None = None,
    extra_attributes: FrozenObject = (),
) -> SocialItemProposal:
    material = {
        "builder_version": LANGCHAO_SOCIAL_BUILDER_VERSION,
        "rule": evidence.kind.value,
        "semantic_key": evidence.semantic_key,
        "source": _source_identity(evidence.source),
        "item_type": item_type.value,
        "suffix": suffix,
    }
    return SocialItemProposal(
        item_key=_stable_key("social-item", material),
        scope_key=evidence.source.scope_key,
        item_type=item_type,
        role=evidence.role,
        summary=evidence.summary,
        sources=(evidence.source,),
        attributes=evidence.attributes + extra_attributes,
        supersedes_item_key=supersedes,
    )


def build_social_proposal(
    *,
    scope_key: str,
    created_at: datetime,
    inputs: tuple[SocialRuleInput, ...],
    revision: int = 1,
    semantic_available: bool = True,
) -> SocialProposal:
    """Map verified facts to a deterministic, source-traceable proposal.

    Rules deliberately do *not* infer confirmed arrangements from summaries and
    never emit adopted goals.  When semantic interpretation is unavailable the
    factual rules still run; supersession is represented as counterevidence plus
    an explicit uncertainty rather than an invented interpretation.
    """

    if not isinstance(inputs, tuple):
        raise TypeError("inputs must be a tuple")
    if any(not isinstance(value, SocialRuleInput) for value in inputs):
        raise TypeError("inputs must contain only SocialRuleInput values")
    if any(value.source.scope_key != scope_key for value in inputs):
        raise ValueError("all rule inputs must use the requested scope_key")
    if not isinstance(semantic_available, bool):
        raise TypeError("semantic_available must be a boolean")

    ordered_inputs = tuple(
        sorted(
            inputs,
            key=lambda value: (
                value.kind.value,
                value.semantic_key,
                value.source.source_kind.value,
                value.source.source_id,
                value.source.source_revision,
                value.source.source_sha256,
            ),
        )
    )
    items: list[SocialItemProposal] = []
    links: list[SocialLinkProposal] = []
    for evidence in ordered_inputs:
        if evidence.kind is RuleInputKind.UNFINISHED:
            if evidence.active:
                items.append(_item(evidence, SocialItemType.SHARED_MATTER, suffix="matter"))
            continue

        if evidence.kind is RuleInputKind.ACTUAL_EVENT:
            # A memory summary is not an actual event merely because it says one occurred.
            if evidence.actual:
                items.append(
                    _item(evidence, SocialItemType.PARTICIPATION_FACT, suffix="participation")
                )
            continue

        if evidence.kind is RuleInputKind.ACTIVE_BOUNDARY:
            if evidence.active:
                items.append(_item(evidence, SocialItemType.BOUNDARY_REFERENCE, suffix="boundary"))
            continue

        if evidence.kind is RuleInputKind.SUPERSEDED_MEMORY:
            counter = _item(
                evidence,
                SocialItemType.COUNTEREVIDENCE,
                suffix="counterevidence",
                supersedes=evidence.superseded_item_key,
            )
            interpretation_type = (
                SocialItemType.INTERPRETIVE_BASIS
                if semantic_available
                else SocialItemType.UNCERTAINTY
            )
            basis = _item(
                evidence,
                interpretation_type,
                suffix="interpretive-basis" if semantic_available else "degraded-uncertainty",
                extra_attributes=(("degraded", not semantic_available),),
            )
            items.extend((counter, basis))
            link_material = {
                "from": counter.item_key,
                "to": basis.item_key,
                "relation": SocialRelation.SUPPORTS.value,
                "source": _source_identity(evidence.source),
            }
            links.append(
                SocialLinkProposal(
                    link_key=_stable_key("social-link", link_material),
                    scope_key=scope_key,
                    relation=SocialRelation.SUPPORTS,
                    from_item_key=counter.item_key,
                    to_item_key=basis.item_key,
                    sources=(evidence.source,),
                )
            )

    input_material = {
        "scope_key": scope_key,
        "revision": revision,
        "semantic_available": semantic_available,
        "inputs": [
            {
                "kind": value.kind.value,
                "semantic_key": value.semantic_key,
                "summary": value.summary,
                "source": value.source.to_dict(),
                "role": value.role.value,
                "actual": value.actual,
                "active": value.active,
                "superseded_item_key": value.superseded_item_key,
                "attributes": list(value.attributes),
            }
            for value in ordered_inputs
        ],
    }
    proposal_id = _stable_key("social-proposal", input_material)
    return SocialProposal(
        proposal_id=proposal_id,
        scope_key=scope_key,
        revision=revision,
        created_at=created_at,
        items=tuple(items),
        links=tuple(links),
    )


__all__ = ["RuleInputKind", "SocialRuleInput", "build_social_proposal"]
