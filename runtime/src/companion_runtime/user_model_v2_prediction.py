"""Pure active-parameter prediction service for user-model v2.

The service is deliberately detached from Runtime, Jev, database drivers, and side effects.
A repository supplies one active parameter payload per target; this module validates those
payloads, performs Laplace predictions, and returns the immutable v2 prediction contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from .user_model_v2_estimator import GaussianPrior, LaplaceFit, predict_laplace
from .user_model_v2_features import FeatureSnapshotV2
from .user_model_v2_types import (
    PredictionEnvelopeV2,
    SupportStatus,
    Target,
    TargetPredictionV2,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveParameterSnapshotV2:
    """Repository-neutral identity and JSON payload of one active target head."""

    parameter_snapshot_id: str
    target: Target
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.parameter_snapshot_id, str) or not self.parameter_snapshot_id.strip():
            raise ValueError("parameter_snapshot_id must be a non-empty string")
        if not isinstance(self.target, Target):
            raise TypeError("target must be a Target")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")


class UserModelV2PredictionRepository(Protocol):
    """Small read-only boundary needed by :class:`UserModelV2PredictionService`."""

    def get_active_parameter_snapshot(
        self, *, scope_key: str, target: Target
    ) -> ActiveParameterSnapshotV2 | None: ...


class UserModelV2PredictionService:
    """Build one four-head envelope from independently active target snapshots."""

    def __init__(
        self,
        repository: UserModelV2PredictionRepository,
        *,
        registered_priors: Mapping[Target, GaussianPrior] | None = None,
    ) -> None:
        self.repository = repository
        self.registered_priors = dict(registered_priors or {})
        if any(not isinstance(target, Target) for target in self.registered_priors):
            raise TypeError("registered_priors keys must be Target values")

    def predict(
        self,
        *,
        features: FeatureSnapshotV2,
        predicted_at: datetime,
        based_on_state_version: int,
        interval_level: float = 0.90,
        source_event_ids: tuple[str, ...] = (),
    ) -> PredictionEnvelopeV2:
        """Read all active heads and return their atomic prediction envelope.

        A missing feature is never interpreted as its encoder's numeric placeholder.  Until a
        missing-coefficient strategy is introduced, every head is explicitly unavailable.
        Missing active parameters use a registered prior when present, otherwise they too are
        unavailable.  Malformed active snapshots are rejected rather than silently falling back.
        """

        if not isinstance(features, FeatureSnapshotV2):
            raise TypeError("features must be a FeatureSnapshotV2")
        if not isinstance(predicted_at, datetime) or predicted_at.tzinfo is None:
            raise ValueError("predicted_at must be a timezone-aware datetime")
        if isinstance(based_on_state_version, bool) or not isinstance(based_on_state_version, int):
            raise TypeError("based_on_state_version must be an integer")
        if based_on_state_version < 0:
            raise ValueError("based_on_state_version must be non-negative")

        active = {
            target: self.repository.get_active_parameter_snapshot(
                scope_key=features.scope_key, target=target
            )
            for target in Target
        }
        has_missing = any(features.missing_mask)
        predictions: list[TargetPredictionV2] = []
        parameter_ids: list[tuple[Target, str | None]] = []
        for target in Target:
            snapshot = active[target]
            parameter_ids.append(
                (target, None if snapshot is None else snapshot.parameter_snapshot_id)
            )
            if snapshot is not None and snapshot.target is not target:
                raise ValueError(f"active snapshot target mismatch for {target.value}")

            if has_missing:
                predictions.append(
                    _unavailable_prediction(features.scope_key, target, predicted_at, source_event_ids)
                )
                continue

            if snapshot is None:
                prior = self.registered_priors.get(target)
                if prior is None:
                    predictions.append(
                        _unavailable_prediction(
                            features.scope_key, target, predicted_at, source_event_ids
                        )
                    )
                    continue
                fit = _fit_from_prior(prior)
                support = SupportStatus.PRIOR_ONLY
            else:
                fit, support = _validated_fit(snapshot.payload, features, target)

            result = predict_laplace(fit, features.values, interval_level=interval_level)
            predictions.append(
                TargetPredictionV2(
                    prediction_id=_stable_id(features.scope_key, predicted_at, target),
                    scope_key=features.scope_key,
                    target=target,
                    point=result.point_probability,
                    lower=result.probability_lower,
                    upper=result.probability_upper,
                    interval_level=result.interval_level,
                    interval_kind=result.interval_kind,
                    support=support,
                    predicted_at=predicted_at,
                    created_at=predicted_at,
                    updated_at=predicted_at,
                    source_event_ids=source_event_ids,
                )
            )

        return PredictionEnvelopeV2(
            envelope_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"user-model-v2-envelope:{features.scope_key}:{predicted_at.isoformat()}",
                )
            ),
            scope_key=features.scope_key,
            predictions=tuple(predictions),
            predicted_at=predicted_at,
            based_on_state_version=based_on_state_version,
            created_at=predicted_at,
            updated_at=predicted_at,
            parameter_snapshot_ids=tuple(parameter_ids),
            source_event_ids=source_event_ids,
        )


def _validated_fit(
    payload: Mapping[str, Any], features: FeatureSnapshotV2, expected_target: Target
) -> tuple[LaplaceFit, SupportStatus]:
    if payload.get("target") != expected_target.value:
        raise ValueError(f"parameter payload target mismatch for {expected_target.value}")
    if payload.get("feature_version") != features.feature_version:
        raise ValueError(f"feature version mismatch for {expected_target.value}")
    if payload.get("feature_fingerprint") != features.feature_fingerprint:
        raise ValueError(f"feature fingerprint mismatch for {expected_target.value}")
    names = payload.get("feature_names")
    if not isinstance(names, (list, tuple)) or tuple(names) != features.spec.names:
        raise ValueError(f"feature names mismatch for {expected_target.value}")

    dimension = len(features.values)
    beta = _vector(payload, "map_parameters", dimension)
    hessian = _matrix(payload, "hessian", dimension)
    covariance = _matrix(payload, "covariance", dimension)
    precision_cholesky = _matrix(payload, "precision_cholesky", dimension)
    covariance_cholesky = _matrix(payload, "covariance_cholesky", dimension)
    _validate_symmetric_positive_definite("hessian", hessian)
    _validate_symmetric_positive_definite("covariance", covariance)
    _validate_cholesky("precision_cholesky", precision_cholesky, hessian)
    _validate_cholesky("covariance_cholesky", covariance_cholesky, covariance)
    identity = hessian @ covariance
    if not np.allclose(identity, np.eye(dimension), rtol=1e-7, atol=1e-9):
        raise ValueError("hessian and covariance are not inverses")

    raw_support = payload.get("support")
    try:
        support = SupportStatus(raw_support)
    except (TypeError, ValueError) as exc:
        raise ValueError("parameter payload has invalid support") from exc
    if support is SupportStatus.UNAVAILABLE:
        raise ValueError("an active parameter payload cannot have unavailable support")

    sample_count = payload.get("sample_count", 0)
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0:
        raise ValueError("sample_count must be a non-negative integer")
    weight_sum = _finite_float(payload.get("weight_sum", 0.0), "weight_sum")
    if weight_sum < 0.0:
        raise ValueError("weight_sum must be non-negative")
    objective = _finite_float(payload.get("objective", 0.0), "objective")
    converged = payload.get("converged", True)
    if not isinstance(converged, bool):
        raise TypeError("converged must be a boolean")
    iterations = payload.get("iterations", 0)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise ValueError("iterations must be a non-negative integer")

    return (
        LaplaceFit(
            map_parameters=beta,
            hessian=hessian,
            covariance=covariance,
            precision_cholesky=precision_cholesky,
            covariance_cholesky=covariance_cholesky,
            objective=objective,
            support=support.value,
            sample_count=sample_count,
            weight_sum=weight_sum,
            converged=converged,
            optimizer_message=str(payload.get("optimizer_message", "loaded snapshot")),
            iterations=iterations,
        ),
        support,
    )


def _fit_from_prior(prior: GaussianPrior) -> LaplaceFit:
    covariance = np.linalg.inv(prior.precision)
    covariance = 0.5 * (covariance + covariance.T)
    return LaplaceFit(
        map_parameters=prior.mean.copy(),
        hessian=prior.precision.copy(),
        covariance=covariance,
        precision_cholesky=np.linalg.cholesky(prior.precision),
        covariance_cholesky=np.linalg.cholesky(covariance),
        objective=0.0,
        support=SupportStatus.PRIOR_ONLY.value,
        sample_count=0,
        weight_sum=0.0,
        converged=True,
        optimizer_message="no active parameters; returned registered prior",
        iterations=0,
    )


def _vector(payload: Mapping[str, Any], name: str, dimension: int) -> np.ndarray:
    try:
        value = np.asarray(payload[name], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a numeric vector") from exc
    if value.shape != (dimension,) or not np.all(np.isfinite(value)):
        raise ValueError(f"{name} must be a finite vector of dimension {dimension}")
    return value.copy()


def _matrix(payload: Mapping[str, Any], name: str, dimension: int) -> np.ndarray:
    try:
        value = np.asarray(payload[name], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a numeric matrix") from exc
    if value.shape != (dimension, dimension) or not np.all(np.isfinite(value)):
        raise ValueError(f"{name} must be a finite {dimension}x{dimension} matrix")
    return value.copy()


def _validate_symmetric_positive_definite(name: str, matrix: np.ndarray) -> None:
    if not np.allclose(matrix, matrix.T, rtol=1e-10, atol=1e-12):
        raise ValueError(f"{name} must be symmetric")
    try:
        np.linalg.cholesky(matrix)
    except np.linalg.LinAlgError as exc:
        raise ValueError(f"{name} must be positive definite") from exc


def _validate_cholesky(name: str, factor: np.ndarray, matrix: np.ndarray) -> None:
    if not np.allclose(factor, np.tril(factor), rtol=0.0, atol=1e-12):
        raise ValueError(f"{name} must be lower triangular")
    if np.any(np.diag(factor) <= 0.0):
        raise ValueError(f"{name} must have a positive diagonal")
    if not np.allclose(factor @ factor.T, matrix, rtol=1e-7, atol=1e-9):
        raise ValueError(f"{name} does not factor its matrix")


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _unavailable_prediction(
    scope_key: str,
    target: Target,
    predicted_at: datetime,
    source_event_ids: tuple[str, ...],
) -> TargetPredictionV2:
    return TargetPredictionV2(
        prediction_id=_stable_id(scope_key, predicted_at, target),
        scope_key=scope_key,
        target=target,
        point=None,
        lower=None,
        upper=None,
        interval_level=None,
        interval_kind=None,
        support=SupportStatus.UNAVAILABLE,
        predicted_at=predicted_at,
        created_at=predicted_at,
        updated_at=predicted_at,
        source_event_ids=source_event_ids,
    )


def _stable_id(scope_key: str, predicted_at: datetime, target: Target) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"user-model-v2-prediction:{scope_key}:{predicted_at.isoformat()}:{target.value}",
        )
    )


__all__ = [
    "ActiveParameterSnapshotV2",
    "UserModelV2PredictionRepository",
    "UserModelV2PredictionService",
]
