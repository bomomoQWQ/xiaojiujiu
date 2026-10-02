"""Pre-render, immutable feature contract shared by prediction and training.

``render_plan_v1`` describes properties of a message that are known before any text is
rendered.  It is the only render-level input consumed by the user-model feature encoder.
The post-render actual-action witness may verify this plan, but must never replace it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

RENDER_PLAN_SCHEMA_VERSION = "render_plan_v1"
PRESSURE_TIERS = ("none", "low", "medium", "high", "urgent")
LENGTH_BUCKETS = ("short", "medium", "long")


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _boolean(source: Mapping[str, Any], name: str, *, default: bool = False) -> bool:
    value = source.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"render_plan_v1.{name} must be a boolean")
    return value


def _enum(source: Mapping[str, Any], name: str, choices: tuple[str, ...], *, default: str) -> str:
    value = source.get(name, default)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"render_plan_v1.{name} must be one of {choices!r}")
    return value


def _version(source: Mapping[str, Any], name: str, *, default: str = "unknown") -> str:
    value = source.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"render_plan_v1.{name} must be a non-empty string")
    return value.strip()


def build_render_plan_v1(
    *,
    asks_reply: bool = False,
    pressure_tier: str = "low",
    completion_claim_intent: bool = False,
    commitment: bool = False,
    length_bucket: str = "medium",
    template_version: str = "unknown",
    style_version: str = "unknown",
) -> dict[str, Any]:
    """Validate and freeze one pre-registered render design.

    The revision is content-derived.  A caller cannot retain a revision while changing a
    design field, which makes candidate, render and exposure snapshots comparable.
    """

    raw = {
        "schema_version": RENDER_PLAN_SCHEMA_VERSION,
        "asks_reply": asks_reply,
        "pressure_tier": pressure_tier,
        "completion_claim_intent": completion_claim_intent,
        "commitment": commitment,
        "length_bucket": length_bucket,
        "template_version": template_version,
        "style_version": style_version,
    }
    body = {
        "schema_version": RENDER_PLAN_SCHEMA_VERSION,
        "asks_reply": _boolean(raw, "asks_reply"),
        "pressure_tier": _enum(raw, "pressure_tier", PRESSURE_TIERS, default="low"),
        "completion_claim_intent": _boolean(raw, "completion_claim_intent"),
        "commitment": _boolean(raw, "commitment"),
        "length_bucket": _enum(raw, "length_bucket", LENGTH_BUCKETS, default="medium"),
        "template_version": _version(raw, "template_version"),
        "style_version": _version(raw, "style_version"),
    }
    body["revision"] = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    return body


def normalize_render_plan_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical plan and reject a stale or forged supplied revision."""

    if not isinstance(value, Mapping):
        raise TypeError("render_plan_v1 must be a mapping")
    schema = value.get("schema_version", RENDER_PLAN_SCHEMA_VERSION)
    if schema != RENDER_PLAN_SCHEMA_VERSION:
        raise ValueError(f"unsupported render plan schema: {schema!r}")
    plan = build_render_plan_v1(
        asks_reply=_boolean(value, "asks_reply"),
        pressure_tier=_enum(value, "pressure_tier", PRESSURE_TIERS, default="low"),
        completion_claim_intent=_boolean(value, "completion_claim_intent"),
        commitment=_boolean(value, "commitment"),
        length_bucket=_enum(value, "length_bucket", LENGTH_BUCKETS, default="medium"),
        template_version=_version(value, "template_version"),
        style_version=_version(value, "style_version"),
    )
    supplied = value.get("revision")
    if supplied is not None and supplied != plan["revision"]:
        raise ValueError("render_plan_v1 revision does not match its design fields")
    return plan


def plan_from_action(action: Mapping[str, Any]) -> dict[str, Any]:
    """Read an explicit plan or create the compatibility plan before candidate scoring.

    Compatibility derives only from already-planned action metadata, never rendered text.
    New producers should always provide ``render_plan_v1`` explicitly.
    """

    value = action.get(RENDER_PLAN_SCHEMA_VERSION)
    if isinstance(value, Mapping):
        return normalize_render_plan_v1(value)
    pressure = action.get("pressure_tier", action.get("pressure", "low"))
    if not isinstance(pressure, str) or pressure not in PRESSURE_TIERS:
        pressure = "low"
    return build_render_plan_v1(
        asks_reply=bool(action.get("asks_reply", action.get("question", False))),
        pressure_tier=pressure,
        completion_claim_intent=bool(
            action.get("completion_claim_intent", action.get("claims_task_completion", False))
        ),
        commitment=bool(action.get("commitment", action.get("makes_commitment", False))),
        length_bucket=str(action.get("length_bucket") or "medium"),
        template_version=str(action.get("template_version") or "unknown"),
        style_version=str(action.get("style_version") or "unknown"),
    )


def freeze_action_render_plan(action: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an action and bind its canonical pre-render plan."""

    result = dict(action)
    result[RENDER_PLAN_SCHEMA_VERSION] = plan_from_action(action)
    return result


def stable_version_code(value: str) -> float:
    """Deterministically encode an opaque registered version into an exact 52-bit value."""

    digest = hashlib.sha256(value.encode("utf-8")).digest()
    integer = int.from_bytes(digest[:8], "big") >> 12
    return integer / float(1 << 52)


__all__ = [
    "LENGTH_BUCKETS",
    "PRESSURE_TIERS",
    "RENDER_PLAN_SCHEMA_VERSION",
    "build_render_plan_v1",
    "freeze_action_render_plan",
    "normalize_render_plan_v1",
    "plan_from_action",
    "stable_version_code",
]
