"""PostgreSQL adapter for :mod:`companion_runtime.user_model_v2_service`.

The adapter translates the immutable service/domain records to the existing native
PostgreSQL ``UserModelV2Repository``.  It remains deliberately outside Runtime/Jev
wiring: callers supply an already configured psycopg-like connection or repository.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5
from typing import Any, Iterable, Mapping

from .user_model_v2_features import DEFAULT_FEATURE_SPEC_V2, FeatureSnapshotV2
from .user_model_v2_repository import UserModelV2Repository, canonical_json
from .user_model_v2_service import ActiveTrainingRecordV2, PreparedExposureV2
from .user_model_v2_types import (
    DeliveryBasis,
    InteractionExposureV2,
    LabelStatus,
    Target,
    TargetLabelV2,
)


def _require_scope(scope_key: str) -> str:
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key is required and must be non-empty")
    return scope_key


def _json_object(value: Any, *, name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"stored {name} must be a JSON object")
    return value


def _aware_datetime(value: Any, *, name: str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"stored {name} must be a timezone-aware datetime")
    return value


def _row_value(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def _label_from_payload(value: Any) -> TargetLabelV2:
    payload = _json_object(value, name="target label payload")
    return TargetLabelV2(
        label_id=str(payload["label_id"]),
        exposure_id=str(payload["exposure_id"]),
        scope_key=str(payload["scope_key"]),
        target=Target(str(payload["target"])),
        status=LabelStatus(str(payload["status"])),
        value=payload.get("value"),
        observed_at=(
            None
            if payload.get("observed_at") is None
            else _aware_datetime(payload["observed_at"], name="observed_at")
        ),
        window_started_at=_aware_datetime(payload["window_started_at"], name="window_started_at"),
        window_ends_at=_aware_datetime(payload["window_ends_at"], name="window_ends_at"),
        horizon_seconds=int(payload["horizon_seconds"]),
        created_at=_aware_datetime(payload["created_at"], name="created_at"),
        updated_at=_aware_datetime(payload["updated_at"], name="updated_at"),
        source_event_ids=tuple(str(item) for item in payload.get("source_event_ids", ())),
        contract_version=str(payload["contract_version"]),
        feature_version=str(payload["feature_version"]),
        target_contract_version=str(payload["target_contract_version"]),
    )


def _exposure_from_payload(value: Any) -> InteractionExposureV2:
    payload = _json_object(value, name="exposure payload")
    attributes = payload.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise ValueError("stored exposure attributes must be a JSON object")
    return InteractionExposureV2(
        exposure_id=str(payload["exposure_id"]),
        scope_key=str(payload["scope_key"]),
        occurred_at=_aware_datetime(payload["occurred_at"], name="occurred_at"),
        window_started_at=_aware_datetime(payload["window_started_at"], name="window_started_at"),
        window_ends_at=_aware_datetime(payload["window_ends_at"], name="window_ends_at"),
        horizon_seconds=int(payload["horizon_seconds"]),
        delivery_basis=DeliveryBasis(str(payload["delivery_basis"])),
        created_at=_aware_datetime(payload["created_at"], name="created_at"),
        updated_at=_aware_datetime(payload["updated_at"], name="updated_at"),
        source_event_ids=tuple(str(item) for item in payload.get("source_event_ids", ())),
        attributes=tuple((str(key), child) for key, child in attributes.items()),
        contract_version=str(payload["contract_version"]),
        feature_version=str(payload["feature_version"]),
        target_contract_version=str(payload["target_contract_version"]),
    )


def _features_from_payload(
    value: Any,
    *,
    scope_key: str,
    exposure_id: str,
    action: Any,
    context: Any,
) -> FeatureSnapshotV2:
    payload = _json_object(value, name="feature snapshot")
    if str(payload.get("scope_key")) != scope_key or str(payload.get("exposure_id")) != exposure_id:
        raise ValueError("stored feature snapshot identity does not match its exposure")
    if payload.get("feature_version") != DEFAULT_FEATURE_SPEC_V2.version:
        raise ValueError("stored feature snapshot has an unsupported feature version")
    if payload.get("feature_fingerprint") != DEFAULT_FEATURE_SPEC_V2.fingerprint:
        raise ValueError("stored feature snapshot has an unsupported feature fingerprint")
    snapshot = FeatureSnapshotV2(
        scope_key=scope_key,
        exposure_id=exposure_id,
        action_json=_json_object(action, name="action"),
        context_json=_json_object(context, name="context"),
        context_cutoff_at=_aware_datetime(payload["context_cutoff_at"], name="context_cutoff_at"),
        created_at=_aware_datetime(payload["created_at"], name="feature created_at"),
    )
    # The persisted vector/mask are an integrity witness, not an alternate encoder.
    if tuple(payload.get("values", ())) != snapshot.values:
        raise ValueError("stored feature values do not match action/context encoding")
    if tuple(payload.get("missing_mask", ())) != snapshot.missing_mask:
        raise ValueError("stored feature missing mask does not match action/context encoding")
    return snapshot


class PostgresUserModelV2ServiceRepository:
    """Concrete service repository backed by ``UserModelV2Repository`` and PostgreSQL."""

    def __init__(
        self,
        connection: Any,
        repository: UserModelV2Repository | None = None,
    ) -> None:
        self.connection = connection
        self.repository = repository or UserModelV2Repository(connection)

    def _transaction(self) -> Any:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    def settlement_transaction(self) -> Any:
        """Own the complete label-and-outcome settlement transaction."""
        return self._transaction()

    def get_prepared_exposure(
        self, *, scope_key: str, idempotency_key: str
    ) -> PreparedExposureV2 | None:
        scope_key = _require_scope(scope_key)
        row = self.connection.execute(
            """SELECT exposure_id, action, context, exposure_payload, feature_snapshot
               FROM interaction_exposures_v2
               WHERE scope_key = %s AND idempotency_key = %s""",
            (scope_key, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        exposure_id = str(_row_value(row, "exposure_id", 0))
        exposure = _exposure_from_payload(_row_value(row, "exposure_payload", 3))
        features = _features_from_payload(
            _row_value(row, "feature_snapshot", 4),
            scope_key=scope_key,
            exposure_id=exposure_id,
            action=_row_value(row, "action", 1),
            context=_row_value(row, "context", 2),
        )
        label_rows = self.connection.execute(
            """SELECT l.target_value, a.pointer_version
               FROM user_model_active_labels_v2 AS a
               JOIN interaction_target_labels_v2 AS l
                 ON l.scope_key = a.scope_key
                AND l.target_label_id = a.target_label_id
                AND l.exposure_id = a.exposure_id
                AND l.target_name = a.target_name
               WHERE a.scope_key = %s AND a.exposure_id = %s
               ORDER BY CASE a.target_name
                   WHEN 'reply' THEN 1 WHEN 'acceptance' THEN 2
                   WHEN 'continue' THEN 3 WHEN 'negative' THEN 4 ELSE 5 END""",
            (scope_key, exposure_id),
        ).fetchall()
        labels = tuple(_label_from_payload(_row_value(item, "target_value", 0)) for item in label_rows)
        if len(labels) != len(Target) or tuple(label.target for label in labels) != tuple(Target):
            raise ValueError("prepared exposure must have exactly four active target labels")
        return PreparedExposureV2(exposure=exposure, features=features, labels=labels)

    def put_prepared_exposure(
        self, *, prepared: PreparedExposureV2, idempotency_key: str
    ) -> PreparedExposureV2:
        scope_key = _require_scope(prepared.exposure.scope_key)
        if prepared.features.scope_key != scope_key or any(
            label.scope_key != scope_key for label in prepared.labels
        ):
            raise ValueError("prepared exposure components must share one scope")
        legacy_exposure_id = prepared.exposure.exposure_id
        from .exposure_identity import canonical_exposure_id

        storage_exposure_id = canonical_exposure_id(scope_key, legacy_exposure_id)
        if storage_exposure_id != legacy_exposure_id:
            # Keep all domain components aligned with the storage identity; the legacy
            # attempt id remains present in feature/action provenance and the send-ack key.
            from dataclasses import replace

            exposure = replace(prepared.exposure, exposure_id=storage_exposure_id)
            features = replace(prepared.features, exposure_id=storage_exposure_id)
            labels = tuple(replace(label, exposure_id=storage_exposure_id) for label in prepared.labels)
            prepared = PreparedExposureV2(exposure=exposure, features=features, labels=labels)
        with self._transaction():
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"prepared-exposure:{scope_key}:{idempotency_key}",),
            )
            winner = self.get_prepared_exposure(
                scope_key=scope_key, idempotency_key=idempotency_key
            )
            if winner is not None:
                return winner
            self.repository.insert_exposure(
                scope_key=scope_key,
                exposure_id=prepared.exposure.exposure_id,
                occurred_at=prepared.exposure.occurred_at,
                action=prepared.features.to_dict()["action_json"],
                context=prepared.features.to_dict()["context_json"],
                propensity=1.0,
                idempotency_key=idempotency_key,
            )
            self.connection.execute(
                """UPDATE interaction_exposures_v2
                   SET exposure_payload = %s::jsonb, feature_snapshot = %s::jsonb
                   WHERE scope_key = %s AND exposure_id = %s AND idempotency_key = %s""",
                (
                    canonical_json(prepared.exposure.to_dict()),
                    canonical_json(prepared.features.to_dict()),
                    scope_key,
                    prepared.exposure.exposure_id,
                    idempotency_key,
                ),
            )
            for label in prepared.labels:
                activated = self.repository.insert_label_revision_and_activate(
                    scope_key=scope_key,
                    target_label_id=label.label_id,
                    exposure_id=label.exposure_id,
                    labelled_at=label.updated_at,
                    target_name=label.target.value,
                    target_value=label.to_dict(),
                    evidence={"status": label.status.value},
                    expected_pointer_version=None,
                    idempotency_key=f"{idempotency_key}:label:{label.target.value}:1",
                )
                if not activated:
                    raise RuntimeError("pending-label activation unexpectedly lost while locked")
            winner = self.get_prepared_exposure(
                scope_key=scope_key, idempotency_key=idempotency_key
            )
            if winner is None:
                raise RuntimeError("prepared exposure was not readable after insertion")
            return winner

    def get_active_label(
        self, *, scope_key: str, exposure_id: str, target: Target
    ) -> tuple[TargetLabelV2, int] | None:
        scope_key = _require_scope(scope_key)
        row = self.repository.get_active_label(
            scope_key=scope_key, exposure_id=exposure_id, target_name=target.value
        )
        if row is None:
            return None
        label = _label_from_payload(_row_value(row, "target_value", 6))
        revision = int(_row_value(row, "pointer_version", 11))
        if label.scope_key != scope_key or label.exposure_id != str(exposure_id) or label.target is not target:
            raise ValueError("active label payload does not match its scoped pointer")
        return label, revision

    def compare_and_swap_active_label(
        self,
        *,
        label: TargetLabelV2,
        revision: int,
        expected_revision: int,
        idempotency_key: str,
    ) -> bool:
        _require_scope(label.scope_key)
        if revision != expected_revision + 1:
            raise ValueError("revision must be exactly expected_revision + 1")
        activate = getattr(
            self.repository,
            "insert_label_revision_and_activate_in_transaction",
            self.repository.insert_label_revision_and_activate,
        )
        return activate(
            scope_key=label.scope_key,
            target_label_id=label.label_id,
            exposure_id=label.exposure_id,
            labelled_at=label.updated_at,
            target_name=label.target.value,
            target_value=label.to_dict(),
            evidence={"status": label.status.value, "source_event_ids": list(label.source_event_ids)},
            expected_pointer_version=expected_revision,
            idempotency_key=idempotency_key,
        )

    def list_active_training_records(
        self, *, scope_key: str, target: Target
    ) -> Iterable[ActiveTrainingRecordV2]:
        scope_key = _require_scope(scope_key)
        rows = self.connection.execute(
            """SELECT l.target_value, e.exposure_id, e.action, e.context,
                      e.feature_snapshot, e.exposure_weight
               FROM user_model_active_labels_v2 AS a
               JOIN interaction_target_labels_v2 AS l
                 ON l.scope_key = a.scope_key
                AND l.target_label_id = a.target_label_id
                AND l.exposure_id = a.exposure_id
                AND l.target_name = a.target_name
               JOIN interaction_exposures_v2 AS e
                 ON e.scope_key = a.scope_key AND e.exposure_id = a.exposure_id
               WHERE a.scope_key = %s AND a.target_name = %s
               ORDER BY l.labelled_at, l.exposure_id""",
            (scope_key, target.value),
        ).fetchall()
        records: list[ActiveTrainingRecordV2] = []
        for row in rows:
            exposure_id = str(_row_value(row, "exposure_id", 1))
            label = _label_from_payload(_row_value(row, "target_value", 0))
            if label.scope_key != scope_key or label.exposure_id != exposure_id or label.target is not target:
                raise ValueError("training label payload does not match its scoped join")
            features = _features_from_payload(
                _row_value(row, "feature_snapshot", 4),
                scope_key=scope_key,
                exposure_id=exposure_id,
                action=_row_value(row, "action", 2),
                context=_row_value(row, "context", 3),
            )
            records.append(
                ActiveTrainingRecordV2(
                    label=label,
                    features=features,
                    exposure_weight=float(_row_value(row, "exposure_weight", 5)),
                )
            )
        return tuple(records)


# A concise alias for composition roots that name the protocol rather than storage engine.
UserModelV2ServicePostgresRepository = PostgresUserModelV2ServiceRepository

__all__ = [
    "PostgresUserModelV2ServiceRepository",
    "UserModelV2ServicePostgresRepository",
]
