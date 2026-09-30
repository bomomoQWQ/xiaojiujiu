"""Bounded production maintenance for Runtime/User-model v2.

The loop is deliberately synchronous and callback-friendly: a scheduler may call
:meth:`V2Maintenance.run_due` without creating a resident worker.  One invocation
settles a bounded number of expired pending-label windows, emits expectation and
emotion-shadow records exactly once per label revision, and (at a separately
rate-limited cadence) rebuilds the four target heads from active labels.

There is no Jev dependency.  Silence uses the label contract itself: R=false,
N=false, while C/A remain unknown when no user response was observed.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Mapping, Protocol, Sequence
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from .emotion_v2_interface import EmotionInputV2, expectation_emotion_input
from .expectations_v2 import ExpectationSettlementV2, settle_expectation_target
from .user_model_v2_estimator import GaussianPrior
from .user_model_v2_features import DEFAULT_FEATURE_SPEC_V2
from .user_model_v2_labels import SettlementContextV2
from .user_model_v2_service import FittedTargetSnapshotV2, PreparedExposureV2, UserModelV2Service
from .user_model_v2_types import ExpectationV2, Target, TargetLabelV2


def _utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True, slots=True, kw_only=True)
class DueExpectationV2:
    """Expectation joined to the active label revision it must consume."""

    expectation: ExpectationV2
    label: TargetLabelV2
    label_revision: int
    previous: ExpectationSettlementV2 | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveParameterPointerV2:
    snapshot_id: str | None
    activation_version: int = 0


class V2MaintenanceRepository(Protocol):
    def list_due_pending_exposures(
        self, *, scope_key: str, as_of: datetime, limit: int
    ) -> Sequence[PreparedExposureV2]: ...

    def list_due_expectations(
        self, *, scope_key: str, as_of: datetime, limit: int
    ) -> Sequence[DueExpectationV2]: ...

    def write_expectation_revision_once(
        self,
        *,
        scope_key: str,
        settlement: ExpectationSettlementV2,
        emotion_shadow: EmotionInputV2,
        settled_at: datetime,
    ) -> bool: ...

    def get_active_parameter_pointer(self, *, scope_key: str) -> ActiveParameterPointerV2: ...

    def save_parameter_snapshot_and_activate(
        self,
        *,
        scope_key: str,
        snapshot_id: str,
        expected_snapshot_id: str | None,
        parameter_version: int,
        activation_version: int,
        effective_at: datetime,
        parameters: Mapping[str, object],
        provenance: Mapping[str, object],
        idempotency_key: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class V2MaintenanceConfig:
    minimum_interval_seconds: float = 30.0
    fit_interval_seconds: float = 3600.0
    max_due_exposures: int = 64
    max_due_expectations: int = 256
    max_fit_targets: int = 4
    half_life_seconds: float = 30.0 * 24.0 * 3600.0
    prior_precision: float = 1.0

    def __post_init__(self) -> None:
        for name in ("minimum_interval_seconds", "fit_interval_seconds", "half_life_seconds", "prior_precision"):
            value = float(getattr(self, name))
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("max_due_exposures", "max_due_expectations", "max_fit_targets"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_fit_targets > len(Target):
            raise ValueError("max_fit_targets cannot exceed the target count")


@dataclass(frozen=True, slots=True, kw_only=True)
class V2MaintenanceResult:
    ran: bool
    reason: str
    exposures_scanned: int = 0
    label_revisions_written: int = 0
    expectation_revisions_written: int = 0
    fit_attempted: bool = False
    fit_activated: bool = False
    fitted_targets: tuple[Target, ...] = ()


class V2Maintenance:
    """Non-overlapping, resource-bounded v2 maintenance callback."""

    def __init__(
        self,
        *,
        scope_key: str,
        user_model: UserModelV2Service,
        repository: V2MaintenanceRepository,
        config: V2MaintenanceConfig | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key is required")
        self.scope_key = scope_key
        self.user_model = user_model
        self.repository = repository
        self.config = config or V2MaintenanceConfig()
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._last_run: float | None = None
        self._last_fit: float | None = None

    def run_due(self, *, now: datetime, force: bool = False, fit: bool = True) -> V2MaintenanceResult:
        """Run one bounded pass; suitable for scheduler/maintenance callbacks."""

        _utc("now", now)
        tick = float(self.monotonic())
        if not self._lock.acquire(blocking=False):
            return V2MaintenanceResult(ran=False, reason="already_running")
        try:
            if not force and self._last_run is not None and tick - self._last_run < self.config.minimum_interval_seconds:
                return V2MaintenanceResult(ran=False, reason="interval")
            self._last_run = tick
            due = tuple(self.repository.list_due_pending_exposures(
                scope_key=self.scope_key, as_of=now, limit=self.config.max_due_exposures
            ))
            revisions = 0
            context = SettlementContextV2(as_of=now, observation_complete=True)
            for prepared in due:
                before = {
                    target: self.user_model.repository.get_active_label(
                        scope_key=self.scope_key,
                        exposure_id=prepared.exposure.exposure_id,
                        target=target,
                    )
                    for target in Target
                }
                settled = self.user_model.settle_observation(
                    prepared=prepared, observations=(), context=context
                )
                revisions += sum(
                    1
                    for label in settled
                    if before[label.target] is not None and label != before[label.target][0]
                )

            expectation_writes = 0
            for item in self.repository.list_due_expectations(
                scope_key=self.scope_key, as_of=now, limit=self.config.max_due_expectations
            ):
                settlement = settle_expectation_target(
                    item.expectation,
                    item.label,
                    label_revision=item.label_revision,
                    previous=item.previous,
                )
                shadow = expectation_emotion_input(settlement, shadow=True)
                if self.repository.write_expectation_revision_once(
                    scope_key=self.scope_key,
                    settlement=settlement,
                    emotion_shadow=shadow,
                    settled_at=now,
                ):
                    expectation_writes += 1

            fit_due = fit and (
                force or self._last_fit is None or tick - self._last_fit >= self.config.fit_interval_seconds
            )
            activated = False
            fitted: tuple[Target, ...] = ()
            if fit_due:
                # Mark attempt before expensive work so repeated failures cannot hot-loop.
                self._last_fit = tick
                activated, fitted = self._fit_and_activate(now)
            return V2MaintenanceResult(
                ran=True,
                reason="ok",
                exposures_scanned=len(due),
                label_revisions_written=revisions,
                expectation_revisions_written=expectation_writes,
                fit_attempted=fit_due,
                fit_activated=activated,
                fitted_targets=fitted,
            )
        finally:
            self._lock.release()

    def _fit_and_activate(self, now: datetime) -> tuple[bool, tuple[Target, ...]]:
        dimension = len(DEFAULT_FEATURE_SPEC_V2.names)
        prior = GaussianPrior(
            mean=np.zeros(dimension, dtype=np.float64),
            precision=np.eye(dimension, dtype=np.float64) * self.config.prior_precision,
        )
        targets = tuple(Target)[: self.config.max_fit_targets]
        fits: list[FittedTargetSnapshotV2] = []
        for target in targets:
            dataset = self.user_model.build_fit_dataset(scope_key=self.scope_key, target=target)
            fits.append(self.user_model.fit_target(
                dataset=dataset,
                prior=prior,
                fit_time=now,
                half_life_seconds=self.config.half_life_seconds,
            ))
        pointer = self.repository.get_active_parameter_pointer(scope_key=self.scope_key)
        parameter_version = pointer.activation_version + 1
        material = "\0".join((self.scope_key, now.isoformat(), *(target.value for target in targets)))
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        snapshot_id = str(uuid5(NAMESPACE_URL, f"runtime-v2-fit:{digest}"))
        parameters = {item.target.value: dict(item.payload) for item in fits}
        provenance = {
            "kind": "maintenance_v2_map_laplace",
            "fit_time": now.isoformat(),
            "targets": [target.value for target in targets],
            "active_labels_only": True,
        }
        activated = self.repository.save_parameter_snapshot_and_activate(
            scope_key=self.scope_key,
            snapshot_id=snapshot_id,
            expected_snapshot_id=pointer.snapshot_id,
            parameter_version=parameter_version,
            activation_version=parameter_version,
            effective_at=now,
            parameters=parameters,
            provenance=provenance,
            idempotency_key=f"maintenance-v2-fit:{digest}",
        )
        return activated, targets


__all__ = [
    "ActiveParameterPointerV2",
    "DueExpectationV2",
    "V2Maintenance",
    "V2MaintenanceConfig",
    "V2MaintenanceRepository",
    "V2MaintenanceResult",
]
