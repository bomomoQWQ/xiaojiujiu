"""v21 render-to-send actual-action witness and scope-drift gate.

The witness freezes mechanically reproducible facts about the exact text handed to the
transport.  Semantic facts are accepted only from an explicitly approved review; absent
review stays ``unknown`` and is never guessed from the text.  Text-derived facts are audit
metadata, not user-model prediction features.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .render_plan_v1 import plan_from_action

ACTUAL_ACTION_WITNESS_SCHEMA_VERSION = "21"
DEFAULT_RENDER_VERSION = "unknown"
DEFAULT_TEMPLATE_VERSION = "unknown"
DEFAULT_ENCODER_VERSION = "actual-action-witness-v21"


class SemanticReviewStatus(str, Enum):
    APPROVED = "approved"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class ActualActionAttribution(str, Enum):
    ATTRIBUTABLE = "ATTRIBUTABLE"
    UNATTRIBUTABLE = "UNATTRIBUTABLE"


class ScopeDriftError(ValueError):
    """The approved final-text semantics exceed the authorised plan."""


_SEMANTIC_FIELDS = (
    "asks_reply",
    "pressure",
    "makes_commitment",
    "claims_task_completion",
)


def _normalized_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("rendered text must not be empty")
    return unicodedata.normalize("NFC", text.strip())


def _structure(text: str) -> dict[str, int]:
    # Mechanical only: no inference that punctuation means a request or pressure.
    paragraphs = [part for part in re.split(r"\n\s*\n", text) if part.strip()]
    lines = text.splitlines() or [text]
    return {
        "paragraph_count": len(paragraphs) or 1,
        "line_count": len(lines),
        "question_mark_count": text.count("?") + text.count("？"),
        "exclamation_mark_count": text.count("!") + text.count("！"),
    }


def _semantic_review(review: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(review, Mapping):
        return {
            "status": SemanticReviewStatus.UNKNOWN.value,
            **{field: "unknown" for field in _SEMANTIC_FIELDS},
            "reviewer": None,
            "review_version": None,
        }
    status = str(review.get("status") or SemanticReviewStatus.UNKNOWN.value).lower()
    if status not in {item.value for item in SemanticReviewStatus}:
        raise ValueError("semantic review status must be approved, rejected, or unknown")
    result: dict[str, Any] = {
        "status": status,
        "reviewer": str(review.get("reviewer") or "").strip() or None,
        "review_version": str(review.get("review_version") or "").strip() or None,
    }
    for field in _SEMANTIC_FIELDS:
        value = review.get(field, "unknown")
        if field == "pressure":
            if value != "unknown" and not isinstance(value, str):
                raise TypeError("semantic pressure must be a reviewed band string or unknown")
        elif value != "unknown" and not isinstance(value, bool):
            raise TypeError(f"semantic {field} must be boolean or unknown")
        result[field] = value
    if status == SemanticReviewStatus.APPROVED.value:
        if not result["reviewer"] or not result["review_version"]:
            raise ValueError("approved semantic review requires reviewer and review_version")
        if any(result[field] == "unknown" for field in _SEMANTIC_FIELDS):
            raise ValueError("approved semantic review must decide every semantic field")
    return result


def build_actual_action_witness(
    *,
    text: str,
    attempt_id: str,
    render_outbox_id: str | None,
    send_outbox_id: str,
    render_metadata: Mapping[str, Any] | None = None,
    semantic_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the exact rendered action, bound to attempt and both outbox stages."""

    normalized = _normalized_text(text)
    metadata = render_metadata if isinstance(render_metadata, Mapping) else {}
    review = _semantic_review(semantic_review)
    revision_material = "\0".join(
        (
            attempt_id,
            render_outbox_id or "direct",
            send_outbox_id,
            normalized,
            str(metadata.get("render_version") or DEFAULT_RENDER_VERSION),
            str(metadata.get("template_version") or DEFAULT_TEMPLATE_VERSION),
            str(metadata.get("style_version") or DEFAULT_TEMPLATE_VERSION),
            str(metadata.get("render_plan_revision") or "unbound"),
            str(metadata.get("encoder_version") or DEFAULT_ENCODER_VERSION),
        )
    )
    revision = hashlib.sha256(revision_material.encode("utf-8")).hexdigest()
    return {
        "schema_version": ACTUAL_ACTION_WITNESS_SCHEMA_VERSION,
        "actual_action_revision": revision,
        "attempt_id": attempt_id,
        "render_outbox_id": render_outbox_id,
        "send_outbox_id": send_outbox_id,
        "rendered_text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "normalized": {
            "form": "NFC",
            "length_codepoints": len(normalized),
            "length_utf8_bytes": len(normalized.encode("utf-8")),
            "structure": _structure(normalized),
        },
        "render_version": str(metadata.get("render_version") or DEFAULT_RENDER_VERSION),
        "template_version": str(metadata.get("template_version") or DEFAULT_TEMPLATE_VERSION),
        "style_version": str(metadata.get("style_version") or DEFAULT_TEMPLATE_VERSION),
        "render_plan_revision": str(metadata.get("render_plan_revision") or "") or None,
        "encoder_version": str(metadata.get("encoder_version") or DEFAULT_ENCODER_VERSION),
        "semantic_review": review,
        "attribution": (
            ActualActionAttribution.ATTRIBUTABLE.value
            if review["status"] == SemanticReviewStatus.APPROVED.value
            else ActualActionAttribution.UNATTRIBUTABLE.value
        ),
    }


def verify_render_plan_v1(
    *, planned_action: Mapping[str, Any], witness: Mapping[str, Any]
) -> tuple[str, tuple[str, ...]]:
    """Compare reviewed actual semantics with the frozen plan.

    The witness is verification evidence only.  Unknown review or any semantic/version
    mismatch is ``UNATTRIBUTABLE`` and never mutates prediction/training features.
    """

    plan = plan_from_action(planned_action)
    review = witness.get("semantic_review")
    if not isinstance(review, Mapping) or review.get("status") != SemanticReviewStatus.APPROVED.value:
        return ActualActionAttribution.UNATTRIBUTABLE.value, ("semantic_review_unknown",)
    drift: list[str] = []
    if bool(review.get("asks_reply")) != plan["asks_reply"]:
        drift.append("asks_reply")
    if review.get("pressure") != plan["pressure_tier"]:
        drift.append("pressure_tier")
    if bool(review.get("makes_commitment")) != plan["commitment"]:
        drift.append("commitment")
    if bool(review.get("claims_task_completion")) != plan["completion_claim_intent"]:
        drift.append("completion_claim_intent")
    if "render_plan_v1" in planned_action and witness.get("template_version") != plan["template_version"]:
        drift.append("template_version")
    return (
        ActualActionAttribution.UNATTRIBUTABLE.value if drift else ActualActionAttribution.ATTRIBUTABLE.value,
        tuple(drift),
    )


def enforce_plan_render_scope(
    *,
    planned_action: Mapping[str, Any],
    witness: Mapping[str, Any],
    reauthorized: bool = False,
) -> None:
    """Block approved semantic expansion unless a new authorisation covers it.

    Unknown review never becomes a positive semantic claim.  The approved reviewer, not
    punctuation or keywords, decides whether final text asks for a reply, adds pressure,
    makes a commitment, or claims task completion.
    """

    review = witness.get("semantic_review")
    if not isinstance(review, Mapping) or review.get("status") != SemanticReviewStatus.APPROVED.value:
        return
    attribution, drift = verify_render_plan_v1(planned_action=planned_action, witness=witness)
    if (
        attribution == ActualActionAttribution.UNATTRIBUTABLE.value
        and drift != ("semantic_review_unknown",)
        and not reauthorized
    ):
        plan = plan_from_action(planned_action)
        raise ScopeDriftError(
            "render_scope_drift:" + ",".join(drift) + f":new_render_plan_revision_required:{plan['revision']}"
        )


def actual_action_for_exposure(
    planned_action: Mapping[str, Any], witness: Mapping[str, Any]
) -> dict[str, Any]:
    """Return exposure action provenance without exposing text-derived values to prediction."""

    result = dict(planned_action)
    result["actual_action_witness"] = dict(witness)
    result["actual_action_revision"] = witness.get("actual_action_revision")
    result["actual_action_attribution"] = witness.get(
        "attribution", ActualActionAttribution.UNATTRIBUTABLE.value
    )
    return result


__all__ = [
    "ACTUAL_ACTION_WITNESS_SCHEMA_VERSION",
    "ActualActionAttribution",
    "ScopeDriftError",
    "SemanticReviewStatus",
    "actual_action_for_exposure",
    "build_actual_action_witness",
    "enforce_plan_render_scope",
    "verify_render_plan_v1",
]
