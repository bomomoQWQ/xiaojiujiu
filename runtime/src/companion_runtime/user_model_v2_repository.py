"""Pure PostgreSQL repository for the isolated user-model v2 schema.

The repository intentionally accepts a psycopg-like connection rather than the
Runtime database abstraction.  Its SQL is PostgreSQL-native (``%s``, JSONB and
``FOR UPDATE``), every read/write is explicitly scoped, and it is not wired into
Runtime or motivation.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID


class SchemaContractError(RuntimeError):
    """Raised when a required v2 schema contract is unavailable."""


def canonical_json(value: Any) -> str:
    """Return deterministic, strict JSON suitable for JSONB parameters/hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_idempotency_key(kind: str, scope_key: str, payload: Mapping[str, Any]) -> str:
    """Derive a stable key from an operation, scope, and canonical payload."""

    _require_scope(scope_key)
    material = canonical_json({"kind": kind, "scope_key": scope_key, "payload": payload})
    return f"v2:{kind}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _require_scope(scope_key: str) -> str:
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key is required and must be non-empty")
    return scope_key


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


class UserModelV2Repository:
    """Scoped PostgreSQL persistence operations for user-model v2."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def _transaction(self) -> Any:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    def insert_exposure(
        self,
        *,
        scope_key: str,
        exposure_id: UUID | str,
        occurred_at: datetime,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
        propensity: float,
        exposure_weight: float = 1.0,
        supersedes_exposure_id: UUID | str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        scope_key = _require_scope(scope_key)
        payload = {
            "exposure_id": str(exposure_id), "occurred_at": occurred_at.isoformat(),
            "action": action, "context": context, "propensity": propensity,
            "exposure_weight": exposure_weight,
            "supersedes_exposure_id": None if supersedes_exposure_id is None else str(supersedes_exposure_id),
        }
        key = idempotency_key or stable_idempotency_key("exposure", scope_key, payload)
        cursor = self.connection.execute(
            """INSERT INTO interaction_exposures_v2
               (exposure_id, scope_key, idempotency_key, occurred_at, action, context,
                exposure_weight, propensity, supersedes_exposure_id)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
               ON CONFLICT (scope_key, idempotency_key) DO UPDATE
               SET idempotency_key = EXCLUDED.idempotency_key
               RETURNING exposure_id""",
            (exposure_id, scope_key, key, occurred_at, canonical_json(action),
             canonical_json(context), exposure_weight, propensity, supersedes_exposure_id),
        )
        return _row_value(cursor.fetchone(), "exposure_id")

    def insert_label(
        self,
        *, scope_key: str, target_label_id: UUID | str, exposure_id: UUID | str,
        labelled_at: datetime, target_name: str, target_value: Any,
        evidence: Mapping[str, Any], confidence: float = 1.0, label_version: int = 1,
        idempotency_key: str | None = None,
    ) -> Any:
        scope_key = _require_scope(scope_key)
        payload = {"target_label_id": str(target_label_id), "exposure_id": str(exposure_id),
                   "labelled_at": labelled_at.isoformat(), "target_name": target_name,
                   "target_value": target_value, "evidence": evidence,
                   "confidence": confidence, "label_version": label_version}
        key = idempotency_key or stable_idempotency_key("label", scope_key, payload)
        cursor = self.connection.execute(
            """INSERT INTO interaction_target_labels_v2
               (target_label_id, scope_key, idempotency_key, exposure_id, labelled_at,
                target_name, target_value, evidence, confidence, label_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
               ON CONFLICT (scope_key, idempotency_key) DO UPDATE
               SET idempotency_key = EXCLUDED.idempotency_key
               RETURNING target_label_id""",
            (target_label_id, scope_key, key, exposure_id, labelled_at, target_name,
             canonical_json(target_value), canonical_json(evidence), confidence, label_version),
        )
        return _row_value(cursor.fetchone(), "target_label_id")

    def insert_label_revision_and_activate(
        self, *, scope_key: str, target_label_id: UUID | str, exposure_id: UUID | str,
        labelled_at: datetime, target_name: str, target_value: Any,
        evidence: Mapping[str, Any], confidence: float = 1.0,
        expected_pointer_version: int | None, idempotency_key: str | None = None,
    ) -> bool:
        """Atomically append the next label revision and CAS its active pointer."""
        with self._transaction():
            return self.insert_label_revision_and_activate_in_transaction(
                scope_key=scope_key, target_label_id=target_label_id, exposure_id=exposure_id,
                labelled_at=labelled_at, target_name=target_name, target_value=target_value,
                evidence=evidence, confidence=confidence,
                expected_pointer_version=expected_pointer_version,
                idempotency_key=idempotency_key,
            )

    def insert_label_revision_and_activate_in_transaction(
        self, *, scope_key: str, target_label_id: UUID | str, exposure_id: UUID | str,
        labelled_at: datetime, target_name: str, target_value: Any,
        evidence: Mapping[str, Any], confidence: float = 1.0,
        expected_pointer_version: int | None, idempotency_key: str | None = None,
    ) -> bool:
        """Transaction-neutral variant; the application service owns commit/rollback."""
        scope_key = _require_scope(scope_key)
        # Row locks cannot lock an absent pointer.  The scoped advisory lock serialises
        # first activation too and is held until the caller-owned transaction completes.
        self.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"label:{scope_key}:{exposure_id}:{target_name}",),
        )
        current = self.connection.execute(
            """SELECT target_label_id, pointer_version
               FROM user_model_active_labels_v2
               WHERE scope_key = %s AND exposure_id = %s AND target_name = %s
               FOR UPDATE""",
            (scope_key, exposure_id, target_name),
        ).fetchone()
        actual = None if current is None else int(_row_value(current, "pointer_version", 1))
        if actual != expected_pointer_version:
            return False
        label_version = 1 if actual is None else actual + 1
        self.insert_label(
            scope_key=scope_key, target_label_id=target_label_id, exposure_id=exposure_id,
            labelled_at=labelled_at, target_name=target_name, target_value=target_value,
            evidence=evidence, confidence=confidence, label_version=label_version,
            idempotency_key=idempotency_key,
        )
        pointer_key = stable_idempotency_key(
            "active-label", scope_key,
            {"exposure_id": str(exposure_id), "target_name": target_name,
             "target_label_id": str(target_label_id), "pointer_version": label_version},
        )
        cursor = self.connection.execute(
            """INSERT INTO user_model_active_labels_v2
               (scope_key, exposure_id, target_name, target_label_id, pointer_version, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (scope_key, exposure_id, target_name) DO UPDATE
               SET target_label_id = EXCLUDED.target_label_id,
                   pointer_version = EXCLUDED.pointer_version,
                   idempotency_key = EXCLUDED.idempotency_key,
                   updated_at = CURRENT_TIMESTAMP
               WHERE user_model_active_labels_v2.pointer_version = %s""",
            (scope_key, exposure_id, target_name, target_label_id, label_version,
             pointer_key, expected_pointer_version),
        )
        return getattr(cursor, "rowcount", 1) == 1

    def insert_parameter_snapshot(
        self, *, scope_key: str, parameter_snapshot_id: UUID | str,
        effective_at: datetime, parameters: Mapping[str, Any], provenance: Mapping[str, Any],
        parameter_version: int, training_weight: float = 1.0,
        parent_snapshot_id: UUID | str | None = None, idempotency_key: str | None = None,
    ) -> Any:
        scope_key = _require_scope(scope_key)
        payload = {"parameter_snapshot_id": str(parameter_snapshot_id),
                   "effective_at": effective_at.isoformat(), "parameters": parameters,
                   "provenance": provenance, "parameter_version": parameter_version,
                   "training_weight": training_weight,
                   "parent_snapshot_id": None if parent_snapshot_id is None else str(parent_snapshot_id)}
        key = idempotency_key or stable_idempotency_key("parameters", scope_key, payload)
        cursor = self.connection.execute(
            """INSERT INTO user_model_parameter_snapshots_v2
               (parameter_snapshot_id, scope_key, idempotency_key, effective_at, parameters,
                provenance, training_weight, parameter_version, parent_snapshot_id)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
               ON CONFLICT (scope_key, idempotency_key) DO UPDATE
               SET idempotency_key = EXCLUDED.idempotency_key
               RETURNING parameter_snapshot_id""",
            (parameter_snapshot_id, scope_key, key, effective_at, canonical_json(parameters),
             canonical_json(provenance), training_weight, parameter_version, parent_snapshot_id),
        )
        return _row_value(cursor.fetchone(), "parameter_snapshot_id")

    def compare_and_swap_active_parameters(
        self, *, scope_key: str, expected_snapshot_id: UUID | str | None,
        new_snapshot_id: UUID | str, activation_version: int,
        activation_context: Mapping[str, Any], rollout_fraction: float = 1.0,
        idempotency_key: str | None = None,
    ) -> bool:
        scope_key = _require_scope(scope_key)
        payload = {"new_snapshot_id": str(new_snapshot_id), "activation_version": activation_version,
                   "activation_context": activation_context, "rollout_fraction": rollout_fraction}
        key = idempotency_key or stable_idempotency_key("active-parameters", scope_key, payload)
        with self._transaction():
            # Serialize the empty-pointer case as well as updates to an existing row.
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"parameters:{scope_key}",),
            )
            current = self.connection.execute(
                """SELECT active_parameters_id, parameter_snapshot_id
                   FROM user_model_active_parameters_v2
                   WHERE scope_key = %s AND deactivated_at IS NULL FOR UPDATE""",
                (scope_key,),
            ).fetchone()
            actual = None if current is None else _row_value(current, "parameter_snapshot_id", 1)
            if (None if actual is None else str(actual)) != (
                None if expected_snapshot_id is None else str(expected_snapshot_id)
            ):
                return False
            if current is not None:
                self.connection.execute(
                    """UPDATE user_model_active_parameters_v2 SET deactivated_at = CURRENT_TIMESTAMP
                       WHERE scope_key = %s AND active_parameters_id = %s
                         AND deactivated_at IS NULL""",
                    (scope_key, _row_value(current, "active_parameters_id")),
                )
            cursor = self.connection.execute(
                """INSERT INTO user_model_active_parameters_v2
                   (active_parameters_id, scope_key, idempotency_key, parameter_snapshot_id,
                    activation_context, rollout_fraction, activation_version)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (scope_key, idempotency_key) DO NOTHING
                   RETURNING active_parameters_id""",
                (scope_key, key, new_snapshot_id, canonical_json(activation_context),
                 rollout_fraction, activation_version),
            )
            cursor.fetchone()
            return True

    def insert_prediction(
        self, *, scope_key: str, prediction_snapshot_id: UUID | str,
        parameter_snapshot_id: UUID | str, predicted_at: datetime,
        features: Mapping[str, Any], predictions: Mapping[str, Any], confidence: float,
        prediction_version: int, valid_until: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        scope_key = _require_scope(scope_key)
        payload = {"prediction_snapshot_id": str(prediction_snapshot_id),
                   "parameter_snapshot_id": str(parameter_snapshot_id),
                   "predicted_at": predicted_at.isoformat(), "features": features,
                   "predictions": predictions, "confidence": confidence,
                   "prediction_version": prediction_version,
                   "valid_until": None if valid_until is None else valid_until.isoformat()}
        key = idempotency_key or stable_idempotency_key("prediction", scope_key, payload)
        cursor = self.connection.execute(
            """INSERT INTO prediction_snapshots_v2
               (prediction_snapshot_id, scope_key, idempotency_key, parameter_snapshot_id,
                predicted_at, valid_until, features, predictions, confidence, prediction_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
               ON CONFLICT (scope_key, idempotency_key) DO UPDATE
               SET idempotency_key = EXCLUDED.idempotency_key
               RETURNING prediction_snapshot_id""",
            (prediction_snapshot_id, scope_key, key, parameter_snapshot_id, predicted_at,
             valid_until, canonical_json(features), canonical_json(predictions), confidence,
             prediction_version),
        )
        return _row_value(cursor.fetchone(), "prediction_snapshot_id")

    def insert_expectation(
        self, *, scope_key: str, expectation_id: UUID | str,
        prediction_snapshot_id: UUID | str, expectation: Mapping[str, Any],
        expected_value: float, expectation_version: int, tolerance: float = 0.0,
        due_at: datetime | None = None, idempotency_key: str | None = None,
    ) -> Any:
        scope_key = _require_scope(scope_key)
        payload = {"expectation_id": str(expectation_id),
                   "prediction_snapshot_id": str(prediction_snapshot_id),
                   "expectation": expectation, "expected_value": expected_value,
                   "expectation_version": expectation_version, "tolerance": tolerance,
                   "due_at": None if due_at is None else due_at.isoformat()}
        key = idempotency_key or stable_idempotency_key("expectation", scope_key, payload)
        cursor = self.connection.execute(
            """INSERT INTO expectations_v2
               (expectation_id, scope_key, idempotency_key, prediction_snapshot_id,
                due_at, expectation, expected_value, tolerance, expectation_version)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (scope_key, idempotency_key) DO UPDATE
               SET idempotency_key = EXCLUDED.idempotency_key
               RETURNING expectation_id""",
            (expectation_id, scope_key, key, prediction_snapshot_id, due_at,
             canonical_json(expectation), expected_value, tolerance, expectation_version),
        )
        return _row_value(cursor.fetchone(), "expectation_id")

    def get_active_parameters(self, *, scope_key: str) -> Any:
        scope_key = _require_scope(scope_key)
        return self.connection.execute(
            """SELECT a.*, s.parameters, s.provenance, s.parameter_version
               FROM user_model_active_parameters_v2 AS a
               JOIN user_model_parameter_snapshots_v2 AS s
                 ON s.scope_key = a.scope_key
                AND s.parameter_snapshot_id = a.parameter_snapshot_id
               WHERE a.scope_key = %s AND a.deactivated_at IS NULL""",
            (scope_key,),
        ).fetchone()

    def get_active_label(
        self, *, scope_key: str, exposure_id: UUID | str, target_name: str
    ) -> Any:
        scope_key = _require_scope(scope_key)
        return self.connection.execute(
            """SELECT l.*, a.pointer_version
               FROM user_model_active_labels_v2 AS a
               JOIN interaction_target_labels_v2 AS l
                 ON l.scope_key = a.scope_key AND l.target_label_id = a.target_label_id
               WHERE a.scope_key = %s AND a.exposure_id = %s AND a.target_name = %s""",
            (scope_key, exposure_id, target_name),
        ).fetchone()


__all__ = [
    "SchemaContractError", "UserModelV2Repository", "canonical_json",
    "stable_idempotency_key",
]
