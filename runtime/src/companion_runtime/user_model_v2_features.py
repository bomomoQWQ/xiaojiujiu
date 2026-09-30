"""Pure feature contracts for user-model v2.

This module deliberately has no Runtime, database, configuration, or schema wiring.  It
freezes the exact action/context seen at an exposure and uses one encoder for both training
and prediction.  Missing input is represented by a parallel mask, never by pretending that
an imputed numeric zero was observed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeAlias, Union

from .user_model_v2_types import USER_MODEL_V2_FEATURE_VERSION

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = Union[JsonScalar, "FrozenJsonObject", tuple["FrozenJson", ...]]

QUESTION_TYPES = frozenset({"follow_up", "check_in", "question", "curious_question"})
EMOTIONAL_EXPRESSION_TYPES = frozenset({"share", "emotional_expression"})
TOPIC_SHIFT_TYPES = frozenset({"curious_question"})

# Changing any encoding semantics requires a new version.  Including this identifier in the
# fingerprint prevents an accidental rename/reorder-only interpretation of the digest.
_ENCODER_CONTRACT = "legacy-13-raw-time-and-count-v1"

V2_FEATURE_NAMES: tuple[str, ...] = (
    "bias",
    "proactive",
    "follow_up",
    "emotional_expression",
    "question",
    "topic_shift",
    "busy",
    "recent_contact_count",
    "hours_since_contact",
    "collision",
    "after_boundary",
    "novelty",
    "explicit_permission",
)


class FrozenJsonObject(Mapping[str, FrozenJson]):
    """An immutable, deterministically ordered JSON object.

    ``Mapping`` keeps the frozen value pleasant to inspect while :func:`thaw_json` and
    ``FeatureSnapshotV2.to_dict`` produce ordinary JSON containers for persistence.
    """

    __slots__ = ("_items", "_dict")

    def __init__(self, items: Sequence[tuple[str, FrozenJson]]) -> None:
        self._items = tuple(items)
        self._dict = dict(self._items)

    def __getitem__(self, key: str) -> FrozenJson:
        return self._dict[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenJsonObject({self._items!r})"


def _freeze_json(value: Any, *, path: str = "$") -> FrozenJson:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        items: list[tuple[str, FrozenJson]] = []
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            items.append((key, _freeze_json(child, path=f"{path}.{key}")))
        items.sort(key=lambda item: item[0])
        return FrozenJsonObject(items)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child, path=f"{path}[{index}]") for index, child in enumerate(value))
    raise TypeError(f"{path} contains a non-JSON value of type {type(value).__name__}")


def freeze_json_object(value: Mapping[str, Any], *, name: str = "value") -> FrozenJsonObject:
    """Validate and deeply freeze a JSON object, copying all mutable containers."""

    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    frozen = _freeze_json(value, path=name)
    assert isinstance(frozen, FrozenJsonObject)
    return frozen


def thaw_json(value: FrozenJson) -> Any:
    """Return ordinary JSON containers from a deeply frozen value."""

    if isinstance(value, FrozenJsonObject):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON text, rejecting non-JSON/non-finite input."""

    frozen = _freeze_json(value)
    return json.dumps(
        thaw_json(frozen),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_aware_datetime(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class FeatureSpecV2:
    """Versioned, ordered feature layout with a content-derived stable fingerprint."""

    names: tuple[str, ...] = V2_FEATURE_NAMES
    version: str = USER_MODEL_V2_FEATURE_VERSION
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.names, tuple):
            raise TypeError("names must be a tuple")
        if not self.names or any(not isinstance(name, str) or not name for name in self.names):
            raise ValueError("names must contain non-empty strings")
        if len(set(self.names)) != len(self.names):
            raise ValueError("names must be unique")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")
        payload = canonical_json(
            {"encoder_contract": _ENCODER_CONTRACT, "names": self.names, "version": self.version}
        )
        object.__setattr__(self, "fingerprint", hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return {
            "names": list(self.names),
            "version": self.version,
            "fingerprint": self.fingerprint,
        }


DEFAULT_FEATURE_SPEC_V2 = FeatureSpecV2()


@dataclass(frozen=True, slots=True)
class EncodedFeaturesV2:
    """Numeric vector and its parallel true-when-missing mask."""

    values: tuple[float, ...]
    missing_mask: tuple[bool, ...]

    def __post_init__(self) -> None:
        if len(self.values) != len(self.missing_mask):
            raise ValueError("values and missing_mask must have equal length")
        if any(not isinstance(value, float) or not math.isfinite(value) for value in self.values):
            raise ValueError("values must contain only finite floats")
        if any(not isinstance(item, bool) for item in self.missing_mask):
            raise TypeError("missing_mask must contain only booleans")


def _number(mapping: Mapping[str, Any], key: str) -> tuple[float, bool]:
    if key not in mapping or mapping[key] is None:
        return 0.0, True
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key} must be a number when present")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{key} must be finite")
    return result, False


def _boolean(mapping: Mapping[str, Any], key: str) -> tuple[float, bool]:
    if key not in mapping or mapping[key] is None:
        return 0.0, True
    if not isinstance(mapping[key], bool):
        raise TypeError(f"{key} must be a boolean when present")
    return (1.0 if mapping[key] else 0.0), False


def _derived_action_boolean(
    action: Mapping[str, Any], key: str, positive_types: frozenset[str]
) -> tuple[float, bool]:
    if key in action and action[key] is not None:
        return _boolean(action, key)
    kind = action.get("type")
    if kind is None:
        return 0.0, True
    if not isinstance(kind, str):
        raise TypeError("type must be a string when present")
    return (1.0 if kind in positive_types else 0.0), False


def encode_features_v2(
    action: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    spec: FeatureSpecV2 = DEFAULT_FEATURE_SPEC_V2,
) -> EncodedFeaturesV2:
    """Encode both training and prediction inputs through the one v2 feature function.

    Missing values use numeric ``0.0`` solely as an imputation value; the corresponding
    mask bit remains true, so an observed zero is strictly distinguishable.  Unlike the
    legacy encoder, contact counts and elapsed hours remain raw and hours never saturate at
    24 hours.
    """

    if not isinstance(action, Mapping) or not isinstance(context, Mapping):
        raise TypeError("action and context must be mappings")
    if spec.names != V2_FEATURE_NAMES or spec.version != USER_MODEL_V2_FEATURE_VERSION:
        raise ValueError("encode_features_v2 only supports the declared default v2 feature spec")

    encoded: dict[str, tuple[float, bool]] = {
        "bias": (1.0, False),
        "proactive": _boolean(action, "proactive"),
        "follow_up": _derived_action_boolean(
            action, "follow_up", frozenset({"follow_up", "check_in"})
        ),
        "emotional_expression": _derived_action_boolean(
            action, "emotional_expression", EMOTIONAL_EXPRESSION_TYPES
        ),
        "question": _derived_action_boolean(action, "question", QUESTION_TYPES),
        "topic_shift": _derived_action_boolean(action, "topic_shift", TOPIC_SHIFT_TYPES),
        "busy": _number(context, "busy_probability"),
        "recent_contact_count": _number(context, "recent_contact_count"),
        "hours_since_contact": _number(context, "hours_since_contact"),
        "collision": _boolean(context, "user_active_now"),
        "after_boundary": _boolean(context, "ever_boundary"),
        "novelty": _number(context, "novelty"),
        "explicit_permission": _boolean(context, "explicit_permission"),
    }
    return EncodedFeaturesV2(
        values=tuple(encoded[name][0] for name in spec.names),
        missing_mask=tuple(encoded[name][1] for name in spec.names),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureSnapshotV2:
    """Immutable exposure-time action/context and the vector encoded from it."""

    scope_key: str
    exposure_id: str
    action_json: Mapping[str, Any]
    context_json: Mapping[str, Any]
    context_cutoff_at: datetime
    created_at: datetime
    spec: FeatureSpecV2 = DEFAULT_FEATURE_SPEC_V2
    values: tuple[float, ...] = field(init=False)
    missing_mask: tuple[bool, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scope_key, str) or not self.scope_key.strip():
            raise ValueError("scope_key must be a non-empty string")
        if not isinstance(self.exposure_id, str) or not self.exposure_id.strip():
            raise ValueError("exposure_id must be a non-empty string")
        _require_aware_datetime("context_cutoff_at", self.context_cutoff_at)
        _require_aware_datetime("created_at", self.created_at)
        if self.context_cutoff_at > self.created_at:
            raise ValueError("context_cutoff_at must not be after created_at")

        action = freeze_json_object(self.action_json, name="action_json")
        context = freeze_json_object(self.context_json, name="context_json")
        encoded = encode_features_v2(action, context, spec=self.spec)
        object.__setattr__(self, "action_json", action)
        object.__setattr__(self, "context_json", context)
        object.__setattr__(self, "values", encoded.values)
        object.__setattr__(self, "missing_mask", encoded.missing_mask)

    @property
    def feature_version(self) -> str:
        return self.spec.version

    @property
    def feature_fingerprint(self) -> str:
        return self.spec.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "exposure_id": self.exposure_id,
            "action_json": thaw_json(self.action_json),  # type: ignore[arg-type]
            "context_json": thaw_json(self.context_json),  # type: ignore[arg-type]
            "context_cutoff_at": self.context_cutoff_at.isoformat(),
            "feature_names": list(self.spec.names),
            "feature_version": self.spec.version,
            "feature_fingerprint": self.spec.fingerprint,
            "values": list(self.values),
            "missing_mask": list(self.missing_mask),
            "created_at": self.created_at.isoformat(),
        }
