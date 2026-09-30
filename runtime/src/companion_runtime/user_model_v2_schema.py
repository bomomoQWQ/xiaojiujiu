"""PostgreSQL-native schema for the experimental user-model v2 store.

This module is deliberately declarative.  It is not imported by either active
storage backend and therefore does not change the Runtime's migration path.  A
future PostgreSQL migration runner can consume :data:`MIGRATIONS` or the
flattened result of :func:`schema_statements`.
"""

from __future__ import annotations

from typing import Final

USER_MODEL_SCHEMA_VERSION: Final[int] = 2

_INITIAL_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS interaction_exposures_v2 (
        exposure_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        occurred_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        action JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(action) = 'object'),
        context JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(context) = 'object'),
        exposure_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0
            CHECK (exposure_weight >= 0.0 AND exposure_weight <= 1.0),
        propensity DOUBLE PRECISION NOT NULL
            CHECK (propensity > 0.0 AND propensity <= 1.0),
        schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version = 2),
        row_version BIGINT NOT NULL DEFAULT 1 CHECK (row_version > 0),
        supersedes_exposure_id UUID,
        CONSTRAINT uq_interaction_exposures_v2_idempotency
            UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_interaction_exposures_v2_scope_id
            UNIQUE (scope_key, exposure_id),
        CONSTRAINT fk_interaction_exposures_v2_supersedes
            FOREIGN KEY (scope_key, supersedes_exposure_id)
            REFERENCES interaction_exposures_v2 (scope_key, exposure_id)
            DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT ck_interaction_exposures_v2_not_self
            CHECK (supersedes_exposure_id IS NULL OR supersedes_exposure_id <> exposure_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS interaction_target_labels_v2 (
        target_label_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        exposure_id UUID NOT NULL,
        labelled_at TIMESTAMPTZ NOT NULL,
        target_name TEXT NOT NULL CHECK (btrim(target_name) <> ''),
        target_value JSONB NOT NULL CHECK (jsonb_typeof(target_value) IS NOT NULL),
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
        confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0
            CHECK (confidence >= 0.0 AND confidence <= 1.0),
        schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version = 2),
        label_version BIGINT NOT NULL DEFAULT 1 CHECK (label_version > 0),
        CONSTRAINT uq_interaction_target_labels_v2_idempotency
            UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_interaction_target_labels_v2_natural
            UNIQUE (scope_key, exposure_id, target_name, label_version),
        CONSTRAINT fk_interaction_target_labels_v2_exposure
            FOREIGN KEY (scope_key, exposure_id)
            REFERENCES interaction_exposures_v2 (scope_key, exposure_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_model_parameter_snapshots_v2 (
        parameter_snapshot_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        effective_at TIMESTAMPTZ NOT NULL,
        parameters JSONB NOT NULL CHECK (jsonb_typeof(parameters) = 'object'),
        provenance JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provenance) = 'object'),
        training_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0
            CHECK (training_weight >= 0.0),
        schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version = 2),
        parameter_version BIGINT NOT NULL CHECK (parameter_version > 0),
        parent_snapshot_id UUID,
        CONSTRAINT uq_user_model_parameter_snapshots_v2_idempotency
            UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_user_model_parameter_snapshots_v2_version
            UNIQUE (scope_key, parameter_version),
        CONSTRAINT uq_user_model_parameter_snapshots_v2_scope_id
            UNIQUE (scope_key, parameter_snapshot_id),
        CONSTRAINT fk_user_model_parameter_snapshots_v2_parent
            FOREIGN KEY (scope_key, parent_snapshot_id)
            REFERENCES user_model_parameter_snapshots_v2 (scope_key, parameter_snapshot_id)
            DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT ck_user_model_parameter_snapshots_v2_not_self
            CHECK (parent_snapshot_id IS NULL OR parent_snapshot_id <> parameter_snapshot_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_model_active_parameters_v2 (
        active_parameters_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        parameter_snapshot_id UUID NOT NULL,
        activated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        deactivated_at TIMESTAMPTZ,
        activation_context JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(activation_context) = 'object'),
        rollout_fraction DOUBLE PRECISION NOT NULL DEFAULT 1.0
            CHECK (rollout_fraction > 0.0 AND rollout_fraction <= 1.0),
        schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version = 2),
        activation_version BIGINT NOT NULL CHECK (activation_version > 0),
        CONSTRAINT uq_user_model_active_parameters_v2_idempotency
            UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_user_model_active_parameters_v2_version
            UNIQUE (scope_key, activation_version),
        CONSTRAINT fk_user_model_active_parameters_v2_snapshot
            FOREIGN KEY (scope_key, parameter_snapshot_id)
            REFERENCES user_model_parameter_snapshots_v2 (scope_key, parameter_snapshot_id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_user_model_active_parameters_v2_interval
            CHECK (deactivated_at IS NULL OR deactivated_at >= activated_at)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_user_model_active_parameters_v2_current
    ON user_model_active_parameters_v2 (scope_key)
    WHERE deactivated_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS prediction_snapshots_v2 (
        prediction_snapshot_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        parameter_snapshot_id UUID NOT NULL,
        predicted_at TIMESTAMPTZ NOT NULL,
        valid_until TIMESTAMPTZ,
        features JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(features) = 'object'),
        predictions JSONB NOT NULL CHECK (jsonb_typeof(predictions) = 'object'),
        confidence DOUBLE PRECISION NOT NULL
            CHECK (confidence >= 0.0 AND confidence <= 1.0),
        schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version = 2),
        prediction_version BIGINT NOT NULL CHECK (prediction_version > 0),
        CONSTRAINT uq_prediction_snapshots_v2_idempotency
            UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_prediction_snapshots_v2_version
            UNIQUE (scope_key, prediction_version),
        CONSTRAINT uq_prediction_snapshots_v2_scope_id
            UNIQUE (scope_key, prediction_snapshot_id),
        CONSTRAINT fk_prediction_snapshots_v2_parameters
            FOREIGN KEY (scope_key, parameter_snapshot_id)
            REFERENCES user_model_parameter_snapshots_v2 (scope_key, parameter_snapshot_id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_prediction_snapshots_v2_interval
            CHECK (valid_until IS NULL OR valid_until >= predicted_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expectations_v2 (
        expectation_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        prediction_snapshot_id UUID NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        due_at TIMESTAMPTZ,
        resolved_at TIMESTAMPTZ,
        expectation JSONB NOT NULL CHECK (jsonb_typeof(expectation) = 'object'),
        resolution JSONB CHECK (resolution IS NULL OR jsonb_typeof(resolution) = 'object'),
        expected_value DOUBLE PRECISION NOT NULL,
        tolerance DOUBLE PRECISION NOT NULL DEFAULT 0.0 CHECK (tolerance >= 0.0),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'met', 'missed', 'cancelled')),
        schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version = 2),
        expectation_version BIGINT NOT NULL CHECK (expectation_version > 0),
        CONSTRAINT uq_expectations_v2_idempotency UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_expectations_v2_version UNIQUE (scope_key, expectation_version),
        CONSTRAINT uq_expectations_v2_scope_id UNIQUE (scope_key, expectation_id),
        CONSTRAINT fk_expectations_v2_prediction
            FOREIGN KEY (scope_key, prediction_snapshot_id)
            REFERENCES prediction_snapshots_v2 (scope_key, prediction_snapshot_id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_expectations_v2_resolution
            CHECK ((status = 'pending' AND resolved_at IS NULL)
                OR (status <> 'pending' AND resolved_at IS NOT NULL)),
        CONSTRAINT ck_expectations_v2_due
            CHECK (due_at IS NULL OR due_at >= created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wait_processes_v2 (
        wait_process_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        expectation_id UUID NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        wake_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        policy JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(policy) = 'object'),
        result JSONB CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
        urgency DOUBLE PRECISION NOT NULL DEFAULT 0.0
            CHECK (urgency >= 0.0 AND urgency <= 1.0),
        status TEXT NOT NULL DEFAULT 'waiting'
            CHECK (status IN ('waiting', 'ready', 'completed', 'cancelled')),
        schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version = 2),
        wait_version BIGINT NOT NULL CHECK (wait_version > 0),
        CONSTRAINT uq_wait_processes_v2_idempotency UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_wait_processes_v2_version UNIQUE (scope_key, wait_version),
        CONSTRAINT fk_wait_processes_v2_expectation
            FOREIGN KEY (scope_key, expectation_id)
            REFERENCES expectations_v2 (scope_key, expectation_id)
            ON DELETE CASCADE,
        CONSTRAINT ck_wait_processes_v2_wake
            CHECK (wake_at IS NULL OR wake_at >= started_at),
        CONSTRAINT ck_wait_processes_v2_completion
            CHECK ((status IN ('waiting', 'ready') AND completed_at IS NULL)
                OR (status IN ('completed', 'cancelled') AND completed_at IS NOT NULL))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_interaction_exposures_v2_scope_time
    ON interaction_exposures_v2 (scope_key, occurred_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_interaction_target_labels_v2_exposure
    ON interaction_target_labels_v2 (scope_key, exposure_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_snapshots_v2_scope_time
    ON prediction_snapshots_v2 (scope_key, predicted_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_expectations_v2_pending
    ON expectations_v2 (scope_key, due_at)
    WHERE status = 'pending'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wait_processes_v2_wake
    ON wait_processes_v2 (scope_key, wake_at)
    WHERE status IN ('waiting', 'ready')
    """,
)

# Target labels are immutable revisions.  This separate pointer table is the
# schema contract required to choose one active revision without mutating the
# revision row (and without confusing "largest version" with "active").
_ACTIVE_LABEL_SCHEMA: tuple[str, ...] = (
    """
    ALTER TABLE interaction_target_labels_v2
    ADD CONSTRAINT uq_interaction_target_labels_v2_scope_id
        UNIQUE (scope_key, target_label_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS user_model_active_labels_v2 (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        exposure_id UUID NOT NULL,
        target_name TEXT NOT NULL CHECK (btrim(target_name) <> ''),
        target_label_id UUID NOT NULL,
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, exposure_id, target_name),
        CONSTRAINT uq_user_model_active_labels_v2_idempotency
            UNIQUE (scope_key, idempotency_key),
        CONSTRAINT fk_user_model_active_labels_v2_label
            FOREIGN KEY (scope_key, target_label_id)
            REFERENCES interaction_target_labels_v2 (scope_key, target_label_id)
            ON DELETE RESTRICT,
        CONSTRAINT fk_user_model_active_labels_v2_exposure
            FOREIGN KEY (scope_key, exposure_id)
            REFERENCES interaction_exposures_v2 (scope_key, exposure_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_model_active_labels_v2_target
    ON user_model_active_labels_v2 (scope_key, target_name)
    """,
)

# Migration identifiers are append-only and intentionally independent of the
# active Runtime schema version in db.py.  Never renumber an existing entry.
MIGRATIONS: Final[tuple[tuple[int, tuple[str, ...]], ...]] = (
    (1, _INITIAL_SCHEMA),
    (2, _ACTIVE_LABEL_SCHEMA),
)


def schema_statements() -> tuple[str, ...]:
    """Return all v2 DDL statements in stable migration order.

    A new tuple is assembled from immutable module constants on every call.  The
    function has no database, environment, clock, or v2-type dependency.
    """

    return tuple(statement for _version, statements in MIGRATIONS for statement in statements)


__all__ = [
    "MIGRATIONS",
    "USER_MODEL_SCHEMA_VERSION",
    "schema_statements",
]
