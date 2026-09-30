"""Application orchestration for the isolated user-model v2 pipeline.

This module deliberately does not import ``Runtime`` (or motivation/emotion/Jev).  Its
repository dependency is a small domain-facing protocol, making delivery, observation
settlement and fitting independently testable.  The concrete storage adapter may implement
this protocol directly or translate its existing SQL operations to these records.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from .user_model_v2_estimator import GaussianPrior, LaplaceFit, fit_map_laplace, half_life_weights
from .user_model_v2_features import DEFAULT_FEATURE_SPEC_V2, FeatureSnapshotV2
from .user_model_v2_labels import (
    SettlementContextV2,
    TargetObservationV2,
    next_label_revision,
    settle_target_label,
    settlement_key,
)
from .user_model_v2_types import (
    DeliveryBasis,
    InteractionExposureV2,
    LabelStatus,
    Target,
    TargetLabelV2,
)

ContextProvider = Callable[[], Mapping[str, Any]]


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedExposureV2:
    """The complete immutable result of a confirmed send."""

    exposure: InteractionExposureV2
    features: FeatureSnapshotV2
    labels: tuple[TargetLabelV2, ...]

    def __post_init__(self) -> None:
        if self.features.exposure_id != self.exposure.exposure_id:
            raise ValueError("feature snapshot and exposure must have the same exposure_id")
        if self.features.scope_key != self.exposure.scope_key:
            raise ValueError("feature snapshot and exposure must have the same scope_key")
        if tuple(label.target for label in self.labels) != tuple(Target):
            raise ValueError("labels must contain every target once in enum order")
        if any(label.exposure_id != self.exposure.exposure_id for label in self.labels):
            raise ValueError("all labels must belong to the exposure")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveTrainingRecordV2:
    """Repository DTO joining an active label to its frozen exposure features."""

    label: TargetLabelV2
    features: FeatureSnapshotV2
    exposure_weight: float = 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class FitDatasetV2:
    """Target-specific, estimator-ready observations."""

    target: Target
    exposure_ids: tuple[str, ...]
    design_rows: tuple[tuple[float, ...], ...]
    labels: tuple[float, ...]
    observed_at: tuple[datetime, ...]
    base_weights: tuple[float, ...]
    feature_version: str = DEFAULT_FEATURE_SPEC_V2.version
    feature_fingerprint: str = DEFAULT_FEATURE_SPEC_V2.fingerprint


@dataclass(frozen=True, slots=True, kw_only=True)
class FittedTargetSnapshotV2:
    """A fitted target and JSON-safe payload suitable for parameter persistence."""

    target: Target
    fit: LaplaceFit
    payload: Mapping[str, Any]


class UserModelV2ServiceRepository(Protocol):
    """Domain-facing storage boundary required by :class:`UserModelV2Service`."""

    def get_prepared_exposure(
        self, *, scope_key: str, idempotency_key: str
    ) -> PreparedExposureV2 | None: ...

    def put_prepared_exposure(
        self, *, prepared: PreparedExposureV2, idempotency_key: str
    ) -> PreparedExposureV2: ...

    def get_active_label(
        self, *, scope_key: str, exposure_id: str, target: Target
    ) -> tuple[TargetLabelV2, int] | None: ...

    def compare_and_swap_active_label(
        self,
        *,
        label: TargetLabelV2,
        revision: int,
        expected_revision: int,
        idempotency_key: str,
    ) -> bool: ...

    def list_active_training_records(
        self, *, scope_key: str, target: Target
    ) -> Iterable[ActiveTrainingRecordV2]: ...


class UserModelV2Service:
    """Pure-business application service over v2 feature/label/estimator primitives."""

    def __init__(self, repository: UserModelV2ServiceRepository) -> None:
        self.repository = repository

    def prepare_exposure(
        self,
        *,
        scope_key: str,
        exposure_id: str,
        idempotency_key: str,
        occurred_at: datetime,
        action: Mapping[str, Any],
        context_provider: ContextProvider,
        delivery_confirmed: bool,
        horizons: Mapping[Target, int],
        delivery_basis: DeliveryBasis = DeliveryBasis.DELIVERED,
        source_event_ids: tuple[str, ...] = (),
    ) -> PreparedExposureV2 | None:
        """Freeze and persist one exposure only after delivery confirmation.

        The idempotency lookup intentionally precedes context collection: replaying a confirmed
        send returns the persisted result and never recomputes exposure-time context.  A failed
        send performs no repository call and does not invoke ``context_provider``.
        """

        if not delivery_confirmed:
            return None
        existing = self.repository.get_prepared_exposure(
            scope_key=scope_key, idempotency_key=idempotency_key
        )
        if existing is not None:
            return existing
        if set(horizons) != set(Target):
            raise ValueError("horizons must configure exactly one positive horizon per target")
        for target, seconds in horizons.items():
            if not isinstance(target, Target):
                raise TypeError("horizon keys must be Target values")
            if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
                raise ValueError("every target horizon must be a positive integer")

        context = context_provider()
        snapshot = FeatureSnapshotV2(
            scope_key=scope_key,
            exposure_id=exposure_id,
            action_json=action,
            context_json=context,
            context_cutoff_at=occurred_at,
            created_at=occurred_at,
        )
        # The exposure's broad window is only a container.  Every label below owns its actual,
        # target-specific horizon; settlement reconstructs the corresponding exposure view.
        broad_horizon = max(horizons.values())
        exposure = InteractionExposureV2(
            exposure_id=exposure_id,
            scope_key=scope_key,
            occurred_at=occurred_at,
            window_started_at=occurred_at,
            window_ends_at=occurred_at + timedelta(seconds=broad_horizon),
            horizon_seconds=broad_horizon,
            delivery_basis=delivery_basis,
            created_at=occurred_at,
            updated_at=occurred_at,
            source_event_ids=source_event_ids,
        )
        labels = tuple(
            TargetLabelV2(
                label_id=_stable_label_id(scope_key, exposure_id, target, 1),
                exposure_id=exposure_id,
                scope_key=scope_key,
                target=target,
                status=LabelStatus.PENDING,
                window_started_at=occurred_at,
                window_ends_at=occurred_at + timedelta(seconds=horizons[target]),
                horizon_seconds=horizons[target],
                created_at=occurred_at,
                updated_at=occurred_at,
                source_event_ids=source_event_ids,
            )
            for target in Target
        )
        return self.repository.put_prepared_exposure(
            prepared=PreparedExposureV2(exposure=exposure, features=snapshot, labels=labels),
            idempotency_key=idempotency_key,
        )

    def settle_observation(
        self,
        *,
        prepared: PreparedExposureV2,
        observations: Iterable[TargetObservationV2],
        context: SettlementContextV2,
        target: Target | None = None,
    ) -> tuple[TargetLabelV2, ...]:
        """Generate label revisions with pure settlement and activate them by repository CAS."""

        materialized = tuple(observations)
        targets = (target,) if target is not None else tuple(Target)
        results: list[TargetLabelV2] = []
        pending_by_target = {label.target: label for label in prepared.labels}
        for current_target in targets:
            active = self.repository.get_active_label(
                scope_key=prepared.exposure.scope_key,
                exposure_id=prepared.exposure.exposure_id,
                target=current_target,
            )
            current, current_revision = (
                active if active is not None else (pending_by_target[current_target], 1)
            )
            target_exposure = replace(
                prepared.exposure,
                window_ends_at=current.window_ends_at,
                horizon_seconds=current.horizon_seconds,
            )
            candidate = settle_target_label(
                target_exposure,
                current_target,
                materialized,
                context,
                label_id=current.label_id,
            )
            revision = next_label_revision(
                current, candidate, current_revision=current_revision
            )
            if revision == current_revision:
                results.append(current)
                continue
            candidate = replace(
                candidate,
                label_id=_stable_label_id(
                    candidate.scope_key, candidate.exposure_id, candidate.target, revision
                ),
            )
            activated = self.repository.compare_and_swap_active_label(
                label=candidate,
                revision=revision,
                expected_revision=current_revision,
                idempotency_key=settlement_key(
                    candidate.scope_key, candidate.exposure_id, candidate.target, revision
                ),
            )
            if not activated:
                winner = self.repository.get_active_label(
                    scope_key=candidate.scope_key,
                    exposure_id=candidate.exposure_id,
                    target=candidate.target,
                )
                if winner is None or next_label_revision(
                    winner[0], candidate, current_revision=winner[1]
                ) != winner[1]:
                    raise RuntimeError("active-label CAS lost to a different revision")
                candidate = winner[0]
            results.append(candidate)
        return tuple(results)

    def build_fit_dataset(
        self,
        *,
        scope_key: str,
        target: Target,
        records: Iterable[ActiveTrainingRecordV2] | None = None,
    ) -> FitDatasetV2:
        """Build one target's dataset from active observed labels only."""

        source = records if records is not None else self.repository.list_active_training_records(
            scope_key=scope_key, target=target
        )
        valid: list[ActiveTrainingRecordV2] = []
        observed_statuses = {
            LabelStatus.OBSERVED_POSITIVE,
            LabelStatus.OBSERVED_NEGATIVE,
        }
        for record in source:
            label = record.label
            if label.scope_key != scope_key or label.target is not target:
                continue
            if label.status not in observed_statuses or label.value is None or label.observed_at is None:
                continue
            if record.features.scope_key != scope_key or record.features.exposure_id != label.exposure_id:
                continue
            if record.features.feature_version != DEFAULT_FEATURE_SPEC_V2.version:
                continue
            if record.features.feature_fingerprint != DEFAULT_FEATURE_SPEC_V2.fingerprint:
                continue
            if not 0.0 <= record.exposure_weight <= 1.0:
                continue
            valid.append(record)
        valid.sort(key=lambda item: (item.label.observed_at, item.label.exposure_id))  # type: ignore[arg-type]
        return FitDatasetV2(
            target=target,
            exposure_ids=tuple(item.label.exposure_id for item in valid),
            design_rows=tuple(item.features.values for item in valid),
            labels=tuple(float(item.label.value) for item in valid),  # type: ignore[arg-type]
            observed_at=tuple(item.label.observed_at for item in valid),  # type: ignore[misc]
            base_weights=tuple(float(item.exposure_weight) for item in valid),
        )

    def fit_target(
        self,
        *,
        dataset: FitDatasetV2,
        prior: GaussianPrior,
        fit_time: datetime,
        half_life_seconds: float,
    ) -> FittedTargetSnapshotV2:
        """Fit the estimator and produce a strict JSON-safe persistence payload."""

        if prior.mean.size != len(DEFAULT_FEATURE_SPEC_V2.names):
            raise ValueError("prior dimension must match the v2 feature specification")
        decay = half_life_weights(
            dataset.observed_at, fit_time=fit_time, half_life_seconds=half_life_seconds
        )
        weights = decay * np.asarray(dataset.base_weights, dtype=np.float64)
        design = np.asarray(dataset.design_rows, dtype=np.float64).reshape(
            len(dataset.design_rows), prior.mean.size
        )
        fit = fit_map_laplace(design, dataset.labels, weights, prior)
        payload: dict[str, Any] = {
            "target": dataset.target.value,
            "feature_version": dataset.feature_version,
            "feature_fingerprint": dataset.feature_fingerprint,
            "feature_names": list(DEFAULT_FEATURE_SPEC_V2.names),
            "fit_time": fit_time.isoformat(),
            "half_life_seconds": float(half_life_seconds),
            "map_parameters": fit.map_parameters.tolist(),
            "hessian": fit.hessian.tolist(),
            "covariance": fit.covariance.tolist(),
            "precision_cholesky": fit.precision_cholesky.tolist(),
            "covariance_cholesky": fit.covariance_cholesky.tolist(),
            "objective": float(fit.objective),
            "support": fit.support,
            "sample_count": fit.sample_count,
            "weight_sum": float(fit.weight_sum),
            "converged": fit.converged,
            "optimizer_message": fit.optimizer_message,
            "iterations": fit.iterations,
            "training_exposure_ids": list(dataset.exposure_ids),
        }
        return FittedTargetSnapshotV2(target=dataset.target, fit=fit, payload=payload)


def _stable_label_id(scope_key: str, exposure_id: str, target: Target, revision: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"user-model-v2:{scope_key}:{exposure_id}:{target.value}:{revision}"))


__all__ = [
    "ActiveTrainingRecordV2",
    "FitDatasetV2",
    "FittedTargetSnapshotV2",
    "PreparedExposureV2",
    "UserModelV2Service",
    "UserModelV2ServiceRepository",
]
