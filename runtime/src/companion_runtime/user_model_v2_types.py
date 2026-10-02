"""Immutable, JSON-safe contracts for the second-generation user model.

This module is intentionally not imported by any current runtime path.  It supplies the
records on which future v2 producers and consumers can agree without changing the legacy
model, motivation logic, or database.

Missing evidence is never represented as ``False``: every target owns an independent label
state, and only observed states may carry a value.  Label horizons are explicit record data
because reply, continuation and negative-reaction windows may differ, while acceptance can
be settled by explicit feedback rather than a universal timeout.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, TypeAlias

USER_MODEL_V2_CONTRACT_VERSION = "2"
USER_MODEL_V2_FEATURE_VERSION = "user-model-v2.1-render-plan-v1"
USER_MODEL_V2_TARGET_CONTRACT_VERSION = "1"

#: Every feature-definition version whose rows still exist in storage.
#:
#: Reading history must not require the *latest* definition: a repository that refused to
#: deserialize yesterday's label would lose the ability to reconcile or retire it, and the
#: failure surfaces far away from the version bump (a scheduler round, a late ACK).  Rows
#: keep the version they were written with; consumers that actually learn or predict
#: compare against :data:`USER_MODEL_V2_FEATURE_VERSION` themselves and skip stale rows.
USER_MODEL_V2_FEATURE_VERSIONS: tuple[str, ...] = (
    "user-model-v2.0",
    "user-model-v2.1-render-plan-v1",
)

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenAttributes: TypeAlias = tuple[tuple[str, JsonScalar], ...]


class Target(str, Enum):
    """Independently labelled outcomes of one interaction exposure."""

    REPLY = "reply"
    ACCEPTANCE = "acceptance"
    CONTINUE = "continue"
    NEGATIVE = "negative"


class LabelStatus(str, Enum):
    """Why a target label does or does not currently have a value."""

    PENDING = "pending"
    OBSERVED_POSITIVE = "observed_positive"
    OBSERVED_NEGATIVE = "observed_negative"
    CENSORED = "censored"
    UNKNOWN = "unknown"
    UNATTRIBUTABLE = "unattributable"
    INVALIDATED = "invalidated"


class SupportStatus(str, Enum):
    """Quality of evidence supporting a target prediction."""

    PRIOR_ONLY = "prior_only"
    SPARSE = "sparse"
    INFORMATIVE = "informative"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class DeliveryBasis(str, Enum):
    """Strongest delivery fact on which an exposure is based."""

    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    READ = "read"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_timestamp(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_sources(source_event_ids: tuple[str, ...]) -> None:
    if not isinstance(source_event_ids, tuple):
        raise TypeError("source_event_ids must be a tuple to keep the contract immutable")
    if any(not isinstance(item, str) or not item.strip() for item in source_event_ids):
        raise ValueError("source_event_ids must contain only non-empty strings")
    if len(set(source_event_ids)) != len(source_event_ids):
        raise ValueError("source_event_ids must not contain duplicates")


def _require_attributes(attributes: FrozenAttributes) -> None:
    if not isinstance(attributes, tuple):
        raise TypeError("attributes must be a tuple of key/value pairs")
    keys: list[str] = []
    for item in attributes:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("each attribute must be a (key, JSON scalar) tuple")
        key, value = item
        _require_text("attribute key", key)
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise TypeError("attribute values must be JSON scalars")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("attribute float values must be finite")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("attribute keys must not contain duplicates")


def _validate_base(
    *,
    scope_key: str,
    contract_version: str,
    feature_version: str,
    target_contract_version: str,
    created_at: datetime,
    updated_at: datetime,
    source_event_ids: tuple[str, ...],
) -> None:
    _require_text("scope_key", scope_key)
    if contract_version != USER_MODEL_V2_CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {USER_MODEL_V2_CONTRACT_VERSION!r}")
    if feature_version not in USER_MODEL_V2_FEATURE_VERSIONS:
        raise ValueError(
            f"feature_version must be one of {USER_MODEL_V2_FEATURE_VERSIONS!r}"
        )
    if target_contract_version != USER_MODEL_V2_TARGET_CONTRACT_VERSION:
        raise ValueError(
            "target_contract_version must be "
            f"{USER_MODEL_V2_TARGET_CONTRACT_VERSION!r}"
        )
    _require_timestamp("created_at", created_at)
    _require_timestamp("updated_at", updated_at)
    if updated_at < created_at:
        raise ValueError("updated_at must not precede created_at")
    _require_sources(source_event_ids)


def _validate_window(start: datetime, end: datetime, horizon_seconds: int) -> None:
    _require_timestamp("window_started_at", start)
    _require_timestamp("window_ends_at", end)
    if not isinstance(horizon_seconds, int) or isinstance(horizon_seconds, bool):
        raise TypeError("horizon_seconds must be an integer")
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    if end <= start:
        raise ValueError("window_ends_at must be after window_started_at")
    if end - start != timedelta(seconds=horizon_seconds):
        raise ValueError("window duration must equal horizon_seconds")


def _base_dict(record: Any) -> dict[str, Any]:
    return {
        "scope_key": record.scope_key,
        "contract_version": record.contract_version,
        "feature_version": record.feature_version,
        "target_contract_version": record.target_contract_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "source_event_ids": list(record.source_event_ids),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionExposureV2:
    """One immutable opportunity for the user to react to an assistant action."""

    exposure_id: str
    scope_key: str
    occurred_at: datetime
    window_started_at: datetime
    window_ends_at: datetime
    horizon_seconds: int
    delivery_basis: DeliveryBasis
    created_at: datetime
    updated_at: datetime
    source_event_ids: tuple[str, ...] = ()
    attributes: FrozenAttributes = ()
    contract_version: str = USER_MODEL_V2_CONTRACT_VERSION
    feature_version: str = USER_MODEL_V2_FEATURE_VERSION
    target_contract_version: str = USER_MODEL_V2_TARGET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_text("exposure_id", self.exposure_id)
        _validate_base(**_base_args(self))
        _require_timestamp("occurred_at", self.occurred_at)
        if self.window_started_at != self.occurred_at:
            raise ValueError("window_started_at must equal occurred_at")
        _validate_window(self.window_started_at, self.window_ends_at, self.horizon_seconds)
        if not isinstance(self.delivery_basis, DeliveryBasis):
            raise TypeError("delivery_basis must be a DeliveryBasis")
        _require_attributes(self.attributes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exposure_id": self.exposure_id,
            **_base_dict(self),
            "occurred_at": self.occurred_at.isoformat(),
            "window_started_at": self.window_started_at.isoformat(),
            "window_ends_at": self.window_ends_at.isoformat(),
            "horizon_seconds": self.horizon_seconds,
            "delivery_basis": self.delivery_basis.value,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetLabelV2:
    """Lifecycle and optional observation for exactly one target."""

    label_id: str
    exposure_id: str
    scope_key: str
    target: Target
    status: LabelStatus
    window_started_at: datetime
    window_ends_at: datetime
    horizon_seconds: int
    created_at: datetime
    updated_at: datetime
    value: bool | float | None = None
    observed_at: datetime | None = None
    source_event_ids: tuple[str, ...] = ()
    contract_version: str = USER_MODEL_V2_CONTRACT_VERSION
    feature_version: str = USER_MODEL_V2_FEATURE_VERSION
    target_contract_version: str = USER_MODEL_V2_TARGET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_text("label_id", self.label_id)
        _require_text("exposure_id", self.exposure_id)
        _validate_base(**_base_args(self))
        if not isinstance(self.target, Target):
            raise TypeError("target must be a Target")
        if not isinstance(self.status, LabelStatus):
            raise TypeError("status must be a LabelStatus")
        _validate_window(self.window_started_at, self.window_ends_at, self.horizon_seconds)
        observed = self.status in {
            LabelStatus.OBSERVED_POSITIVE,
            LabelStatus.OBSERVED_NEGATIVE,
        }
        if observed:
            if self.value is None or (
                isinstance(self.value, int) and not isinstance(self.value, bool)
            ) or not isinstance(self.value, (bool, float)):
                raise ValueError("observed labels require a bool or float value in [0, 1]")
            numeric = float(self.value)
            if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
                raise ValueError("observed label value must be finite and in [0, 1]")
            if self.status is LabelStatus.OBSERVED_POSITIVE and numeric <= 0.0:
                raise ValueError("observed_positive requires a value greater than zero")
            if self.status is LabelStatus.OBSERVED_NEGATIVE and numeric > 0.0:
                raise ValueError("observed_negative requires a false/zero value")
            if self.observed_at is None:
                raise ValueError("observed labels require observed_at")
            _require_timestamp("observed_at", self.observed_at)
            if not self.window_started_at <= self.observed_at <= self.window_ends_at:
                raise ValueError("observed_at must fall inside the target label window")
        else:
            if self.value is not None:
                raise ValueError(f"{self.status.value} labels must not carry a value")
            if self.observed_at is not None:
                raise ValueError(f"{self.status.value} labels must not carry observed_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "label_id": self.label_id,
            "exposure_id": self.exposure_id,
            **_base_dict(self),
            "target": self.target.value,
            "status": self.status.value,
            "value": self.value,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "window_started_at": self.window_started_at.isoformat(),
            "window_ends_at": self.window_ends_at.isoformat(),
            "horizon_seconds": self.horizon_seconds,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetPredictionV2:
    """Point and interval prediction plus target-specific evidence support."""

    prediction_id: str
    scope_key: str
    target: Target
    point: float | None
    lower: float | None
    upper: float | None
    interval_level: float | None
    interval_kind: str | None
    support: SupportStatus
    predicted_at: datetime
    created_at: datetime
    updated_at: datetime
    source_event_ids: tuple[str, ...] = ()
    contract_version: str = USER_MODEL_V2_CONTRACT_VERSION
    feature_version: str = USER_MODEL_V2_FEATURE_VERSION
    target_contract_version: str = USER_MODEL_V2_TARGET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_text("prediction_id", self.prediction_id)
        _validate_base(**_base_args(self))
        if not isinstance(self.target, Target):
            raise TypeError("target must be a Target")
        if not isinstance(self.support, SupportStatus):
            raise TypeError("support must be a SupportStatus")
        _require_timestamp("predicted_at", self.predicted_at)
        numeric_fields = (self.point, self.lower, self.upper, self.interval_level)
        if self.support is SupportStatus.UNAVAILABLE:
            if any(value is not None for value in numeric_fields) or self.interval_kind is not None:
                raise ValueError("unavailable support cannot carry a prediction")
            return
        _require_text("interval_kind", self.interval_kind)  # type: ignore[arg-type]
        for name, value in (
            ("point", self.point),
            ("lower", self.lower),
            ("upper", self.upper),
            ("interval_level", self.interval_level),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(float(value)) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        assert self.point is not None and self.lower is not None and self.upper is not None
        assert self.interval_level is not None
        if not self.lower <= self.point <= self.upper:
            raise ValueError("prediction interval must satisfy lower <= point <= upper")
        if self.interval_level <= 0.0:
            raise ValueError("interval_level must be greater than zero")

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            **_base_dict(self),
            "target": self.target.value,
            "point": None if self.point is None else float(self.point),
            "lower": None if self.lower is None else float(self.lower),
            "upper": None if self.upper is None else float(self.upper),
            "interval_level": None if self.interval_level is None else float(self.interval_level),
            "interval_kind": self.interval_kind,
            "support": self.support.value,
            "predicted_at": self.predicted_at.isoformat(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionEnvelopeV2:
    """Atomic prediction snapshot containing exactly one row for every target."""

    envelope_id: str
    scope_key: str
    predictions: tuple[TargetPredictionV2, ...]
    predicted_at: datetime
    based_on_state_version: int
    created_at: datetime
    updated_at: datetime
    parameter_snapshot_ids: tuple[tuple[Target, str | None], ...] = ()
    source_event_ids: tuple[str, ...] = ()
    contract_version: str = USER_MODEL_V2_CONTRACT_VERSION
    feature_version: str = USER_MODEL_V2_FEATURE_VERSION
    target_contract_version: str = USER_MODEL_V2_TARGET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_text("envelope_id", self.envelope_id)
        _validate_base(**_base_args(self))
        _require_timestamp("predicted_at", self.predicted_at)
        if not isinstance(self.based_on_state_version, int) or isinstance(
            self.based_on_state_version, bool
        ) or self.based_on_state_version < 0:
            raise ValueError("based_on_state_version must be a non-negative integer")
        if not isinstance(self.predictions, tuple):
            raise TypeError("predictions must be a tuple")
        targets = tuple(item.target for item in self.predictions)
        if len(self.predictions) != len(Target) or set(targets) != set(Target):
            raise ValueError("prediction envelope must contain exactly one prediction per target")
        if not isinstance(self.parameter_snapshot_ids, tuple):
            raise TypeError("parameter_snapshot_ids must be a tuple")
        parameter_targets: list[Target] = []
        for item in self.parameter_snapshot_ids:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("parameter_snapshot_ids entries must be (Target, id-or-None) tuples")
            target, snapshot_id = item
            if not isinstance(target, Target):
                raise TypeError("parameter_snapshot_ids keys must be Target values")
            if snapshot_id is not None:
                _require_text("parameter snapshot id", snapshot_id)
            parameter_targets.append(target)
        if self.parameter_snapshot_ids and (
            len(parameter_targets) != len(Target) or set(parameter_targets) != set(Target)
        ):
            raise ValueError("parameter_snapshot_ids must contain exactly one entry per target")
        for item in self.predictions:
            if item.scope_key != self.scope_key:
                raise ValueError("all predictions must use the envelope scope_key")
            if item.contract_version != self.contract_version:
                raise ValueError("all predictions must use the envelope contract_version")
            if item.feature_version != self.feature_version:
                raise ValueError("all predictions must use the envelope feature_version")
            if item.target_contract_version != self.target_contract_version:
                raise ValueError("all predictions must use the envelope target_contract_version")
            if item.predicted_at != self.predicted_at:
                raise ValueError("all predictions must use the envelope predicted_at")

    def to_dict(self) -> dict[str, Any]:
        order = {target: index for index, target in enumerate(Target)}
        return {
            "envelope_id": self.envelope_id,
            **_base_dict(self),
            "predicted_at": self.predicted_at.isoformat(),
            "based_on_state_version": self.based_on_state_version,
            "parameter_snapshot_ids": {
                target.value: snapshot_id for target, snapshot_id in self.parameter_snapshot_ids
            },
            "predictions": [
                item.to_dict()
                for item in sorted(self.predictions, key=lambda item: order[item.target])
            ],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectationV2:
    """Prediction fixed at exposure time for later target-by-target comparison."""

    expectation_id: str
    exposure_id: str
    envelope: PredictionEnvelopeV2
    scope_key: str
    fixed_at: datetime
    window_started_at: datetime
    window_ends_at: datetime
    horizon_seconds: int
    created_at: datetime
    updated_at: datetime
    source_event_ids: tuple[str, ...] = ()
    contract_version: str = USER_MODEL_V2_CONTRACT_VERSION
    feature_version: str = USER_MODEL_V2_FEATURE_VERSION
    target_contract_version: str = USER_MODEL_V2_TARGET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _require_text("expectation_id", self.expectation_id)
        _require_text("exposure_id", self.exposure_id)
        _validate_base(**_base_args(self))
        _require_timestamp("fixed_at", self.fixed_at)
        if self.fixed_at != self.window_started_at:
            raise ValueError("fixed_at must equal window_started_at")
        _validate_window(self.window_started_at, self.window_ends_at, self.horizon_seconds)
        if self.envelope.scope_key != self.scope_key:
            raise ValueError("envelope and expectation must use the same scope_key")
        if self.envelope.contract_version != self.contract_version:
            raise ValueError("envelope and expectation must use the same contract_version")
        if self.envelope.feature_version != self.feature_version:
            raise ValueError("envelope and expectation must use the same feature_version")
        if self.envelope.target_contract_version != self.target_contract_version:
            raise ValueError("envelope and expectation must use the same target_contract_version")
        if self.envelope.predicted_at > self.fixed_at:
            raise ValueError("an expectation cannot use a prediction made after it was fixed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "expectation_id": self.expectation_id,
            "exposure_id": self.exposure_id,
            **_base_dict(self),
            "fixed_at": self.fixed_at.isoformat(),
            "window_started_at": self.window_started_at.isoformat(),
            "window_ends_at": self.window_ends_at.isoformat(),
            "horizon_seconds": self.horizon_seconds,
            "envelope": self.envelope.to_dict(),
        }


def _base_args(record: Any) -> dict[str, Any]:
    return {
        "scope_key": record.scope_key,
        "contract_version": record.contract_version,
        "feature_version": record.feature_version,
        "target_contract_version": record.target_contract_version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "source_event_ids": record.source_event_ids,
    }


__all__ = [
    "DeliveryBasis",
    "ExpectationV2",
    "FrozenAttributes",
    "InteractionExposureV2",
    "LabelStatus",
    "PredictionEnvelopeV2",
    "SupportStatus",
    "Target",
    "TargetLabelV2",
    "TargetPredictionV2",
    "USER_MODEL_V2_CONTRACT_VERSION",
    "USER_MODEL_V2_FEATURE_VERSION",
    "USER_MODEL_V2_FEATURE_VERSIONS",
    "USER_MODEL_V2_TARGET_CONTRACT_VERSION",
]
