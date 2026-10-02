"""PostgreSQL-native schema for the experimental user-model v2 store.

This module is deliberately declarative.  It is not imported by either active
storage backend and therefore does not change the Runtime's migration path.  A
future PostgreSQL migration runner can consume :data:`MIGRATIONS` or the
flattened result of :func:`schema_statements`.
"""

from __future__ import annotations

from typing import Final

from .capability_witness_schema import CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS
from .langchao_authority_schema import LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS
from .langchao_outcome_schema import LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS
from .langchao_schema import LANGCHAO_SCHEMA_V12_STATEMENTS
from .langchao_shadow_schema import LANGCHAO_SHADOW_SCHEMA_V16_STATEMENTS
from .langchao_social_schema import LANGCHAO_SOCIAL_SCHEMA_STATEMENTS
from .langchao_live_schema import LANGCHAO_LIVE_SCHEMA_V19_STATEMENTS
from .langchao_live_fk_schema import LANGCHAO_LIVE_FK_SCHEMA_V20_STATEMENTS
from .langchao_no_send_schema import LANGCHAO_NO_SEND_SCHEMA_V24_STATEMENTS
from .langchao_state_schema import LANGCHAO_STATE_SCHEMA_V14_STATEMENTS
from .langchao_user_outcome_schema import LANGCHAO_USER_OUTCOME_SCHEMA_V23_STATEMENTS
from .live_dispatch_legacy_schema import LIVE_DISPATCH_LEGACY_SCHEMA_V25_STATEMENTS
from .live_dispatch_schema import LIVE_DISPATCH_SCHEMA_V18_STATEMENTS
from .runtime_committed_decision_v2_schema import COMMITTED_DECISION_SCHEMA_STATEMENTS
from .runtime_core_v2_schema import CORE_SCHEMA_STATEMENTS, CORE_SCHEMA_VERSION
from .privacy_deletion_schema import PRIVACY_DELETION_SCHEMA_V21_STATEMENTS

USER_MODEL_SCHEMA_VERSION: Final[int] = 25

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
    ADD CONSTRAINT uq_interaction_target_labels_v2_active_identity
        UNIQUE (scope_key, target_label_id, exposure_id, target_name)
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
            FOREIGN KEY (scope_key, target_label_id, exposure_id, target_name)
            REFERENCES interaction_target_labels_v2
                (scope_key, target_label_id, exposure_id, target_name)
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
_SERVICE_ADAPTER_SCHEMA: tuple[str, ...] = (
    """
    ALTER TABLE interaction_exposures_v2
    ADD COLUMN exposure_payload JSONB
        CHECK (exposure_payload IS NULL OR jsonb_typeof(exposure_payload) = 'object')
    """,
    """
    ALTER TABLE interaction_exposures_v2
    ADD COLUMN feature_snapshot JSONB
        CHECK (feature_snapshot IS NULL OR jsonb_typeof(feature_snapshot) = 'object')
    """,
)


# Offline v1 imports need durable reconciliation without allowing rejected legacy
# evidence into the active label tables.  These tables are append-only audit state;
# their uniqueness constraints make replay of the same plan harmless.
_IMPORT_AUDIT_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS user_model_migration_audits_v2 (
        migration_audit_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
        source_manifest_sha256 TEXT NOT NULL CHECK (length(source_manifest_sha256) = 64),
        dry_run BOOLEAN NOT NULL,
        reconciliation JSONB NOT NULL CHECK (jsonb_typeof(reconciliation) = 'object'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_user_model_migration_audits_v2_plan
            UNIQUE (scope_key, plan_sha256, source_manifest_sha256, dry_run)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_model_migration_quarantine_v2 (
        migration_quarantine_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
        source_table TEXT NOT NULL CHECK (btrim(source_table) <> ''),
        source_row TEXT NOT NULL CHECK (btrim(source_row) <> ''),
        classification TEXT NOT NULL CHECK (btrim(classification) <> ''),
        reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_user_model_migration_quarantine_v2_source
            UNIQUE (scope_key, plan_sha256, source_table, source_row, payload_sha256)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_model_migration_audits_v2_scope_created
    ON user_model_migration_audits_v2 (scope_key, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_model_migration_quarantine_v2_scope
    ON user_model_migration_quarantine_v2 (scope_key, plan_sha256)
    """,
)


# Runtime-v2-only facts which have no safe representation in the legacy projections.
# Exposures themselves stay in ``interaction_exposures_v2``; these append-only rows
# carry repeat identity, user-originated resets, and the complete decision audit.
_RUNTIME_ADAPTER_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runtime_v2_exposure_metadata (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        exposure_id UUID NOT NULL,
        concern_id TEXT,
        action_goal_id TEXT,
        acknowledged_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (scope_key, exposure_id),
        CONSTRAINT fk_runtime_v2_exposure_metadata_exposure
            FOREIGN KEY (scope_key, exposure_id)
            REFERENCES interaction_exposures_v2 (scope_key, exposure_id)
            ON DELETE CASCADE,
        CHECK (concern_id IS NULL OR btrim(concern_id) <> ''),
        CHECK (action_goal_id IS NULL OR btrim(action_goal_id) <> '')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runtime_v2_exposure_metadata_scope_time
    ON runtime_v2_exposure_metadata (scope_key, acknowledged_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_v2_user_matter_events (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        event_id TEXT NOT NULL CHECK (btrim(event_id) <> ''),
        occurred_at TIMESTAMPTZ NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('progress', 'reopen')),
        concern_id TEXT,
        action_goal_id TEXT,
        PRIMARY KEY (scope_key, event_id),
        CHECK (concern_id IS NOT NULL OR action_goal_id IS NOT NULL),
        CHECK (concern_id IS NULL OR btrim(concern_id) <> ''),
        CHECK (action_goal_id IS NULL OR btrim(action_goal_id) <> '')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runtime_v2_user_matter_events_scope_time
    ON runtime_v2_user_matter_events (scope_key, occurred_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_v2_decision_audits (
        decision_id TEXT PRIMARY KEY CHECK (btrim(decision_id) <> ''),
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        audit JSONB NOT NULL CHECK (jsonb_typeof(audit) = 'object'),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runtime_v2_decision_audits_scope_time
    ON runtime_v2_decision_audits (scope_key, updated_at DESC)
    """,
)


_MAINTENANCE_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runtime_v2_expectation_settlements (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        revision_key TEXT NOT NULL CHECK (btrim(revision_key) <> ''),
        settlement_key TEXT NOT NULL CHECK (btrim(settlement_key) <> ''),
        expectation_id UUID NOT NULL,
        exposure_id UUID NOT NULL,
        target_name TEXT NOT NULL,
        label_revision BIGINT NOT NULL CHECK (label_revision > 0),
        settlement JSONB NOT NULL CHECK (jsonb_typeof(settlement) = 'object'),
        emotion_shadow JSONB NOT NULL CHECK (jsonb_typeof(emotion_shadow) = 'object'),
        settled_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (scope_key, revision_key),
        CONSTRAINT fk_runtime_v2_expectation_settlement_expectation
            FOREIGN KEY (scope_key, expectation_id)
            REFERENCES expectations_v2 (scope_key, expectation_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runtime_v2_expectation_settlements_logical
    ON runtime_v2_expectation_settlements (scope_key, settlement_key, label_revision DESC)
    """,
)

# Cold-start safe exploration needs a *spend record*.  Without one, "she may
# explore when she knows nothing" would be an unbounded licence to poke people.
# The flag lives on the acknowledged-exposure row because that row already means
# "a proactive message actually reached the user" -- the only event that may
# consume exploration budget.
_COLD_START_EXPLORATION_SCHEMA: tuple[str, ...] = (
    """
    ALTER TABLE runtime_v2_exposure_metadata
        ADD COLUMN IF NOT EXISTS cold_start_exploration BOOLEAN NOT NULL DEFAULT FALSE
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runtime_v2_exposure_metadata_cold_start
    ON runtime_v2_exposure_metadata (scope_key, acknowledged_at DESC)
    WHERE cold_start_exploration
    """,
)


# Send acknowledgement creates the exposure and freezes its prediction atomically.
# The existing prediction table requires a parameter snapshot even for prior-only or
# unavailable heads, so the production envelope is stored directly on the expectation;
# this nullable FK is intentionally removed only for rows whose full v2 envelope is the
# durable prediction witness.
_EXPECTATION_PRODUCTION_SCHEMA: tuple[str, ...] = (
    """
    ALTER TABLE expectations_v2
        ALTER COLUMN prediction_snapshot_id DROP NOT NULL
    """,
    """
    ALTER TABLE expectations_v2
        DROP CONSTRAINT expectations_v2_status_check
    """,
    """
    ALTER TABLE expectations_v2
        ADD CONSTRAINT expectations_v2_status_check
        CHECK (status IN ('pending', 'met', 'missed', 'cancelled', 'settled'))
    """,
    """
    ALTER TABLE expectations_v2
        ADD COLUMN IF NOT EXISTS exposure_id UUID
    """,
    """
    ALTER TABLE expectations_v2
        ADD CONSTRAINT fk_expectations_v2_exposure
        FOREIGN KEY (scope_key, exposure_id)
        REFERENCES interaction_exposures_v2 (scope_key, exposure_id)
        ON DELETE CASCADE
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_expectations_v2_exposure
    ON expectations_v2 (scope_key, exposure_id)
    WHERE exposure_id IS NOT NULL
    """,
)


# Independent append-only evidence for the stopped SQLite mechanical-history import.
# It is deliberately separate from the user-model migration audit: the target core
# tables have no scope column, so scope is a run-level binding enforced by the applier.
_MECHANICAL_HISTORY_IMPORT_AUDIT_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS mechanical_history_import_audits_v1 (
        run_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        source_snapshot_sha256 TEXT NOT NULL CHECK (length(source_snapshot_sha256) = 64),
        plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
        importer_version TEXT NOT NULL CHECK (btrim(importer_version) <> ''),
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('applied', 'failed')),
        reconciliation JSONB NOT NULL CHECK (jsonb_typeof(reconciliation) = 'object'),
        CONSTRAINT ck_mechanical_history_import_audits_v1_interval
            CHECK (completed_at >= started_at),
        CONSTRAINT uq_mechanical_history_import_audits_v1_identity
            UNIQUE (scope_key, source_snapshot_sha256, plan_sha256, importer_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mechanical_history_import_quarantine_v1 (
        quarantine_id UUID PRIMARY KEY,
        run_id UUID NOT NULL,
        source_table TEXT NOT NULL CHECK (btrim(source_table) <> ''),
        source_pk TEXT,
        reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_mechanical_history_import_quarantine_v1_run
            FOREIGN KEY (run_id) REFERENCES mechanical_history_import_audits_v1 (run_id)
            ON DELETE RESTRICT,
        CONSTRAINT uq_mechanical_history_import_quarantine_v1_item
            UNIQUE (run_id, source_table, source_pk, reason, payload_sha256)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mechanical_history_import_audits_v1_scope_time
    ON mechanical_history_import_audits_v1 (scope_key, completed_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mechanical_history_import_quarantine_v1_run
    ON mechanical_history_import_quarantine_v1 (run_id)
    """,
)


MIGRATIONS: Final[tuple[tuple[int, tuple[str, ...]], ...]] = (
    (1, _INITIAL_SCHEMA),
    (2, _ACTIVE_LABEL_SCHEMA),
    (3, _SERVICE_ADAPTER_SCHEMA),
    # The active Runtime and api_v1 still use the stable projection table names.
    # Version 4 adds that complete core beside the user-model v2 tables, using
    # PostgreSQL-native types while preserving columns for projection reuse.
    (CORE_SCHEMA_VERSION, CORE_SCHEMA_STATEMENTS),
    (5, _IMPORT_AUDIT_SCHEMA),
    (6, _RUNTIME_ADAPTER_SCHEMA),
    (7, _MAINTENANCE_SCHEMA),
    (8, _COLD_START_EXPLORATION_SCHEMA),
    (9, _EXPECTATION_PRODUCTION_SCHEMA),
    (10, _MECHANICAL_HISTORY_IMPORT_AUDIT_SCHEMA),
    (11, COMMITTED_DECISION_SCHEMA_STATEMENTS),
    (12, LANGCHAO_SCHEMA_V12_STATEMENTS),
    (13, LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS),
    (14, LANGCHAO_STATE_SCHEMA_V14_STATEMENTS),
    (15, LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS),
    (16, LANGCHAO_SHADOW_SCHEMA_V16_STATEMENTS),
    (17, LANGCHAO_SOCIAL_SCHEMA_STATEMENTS),
    (18, LIVE_DISPATCH_SCHEMA_V18_STATEMENTS),
    (19, LANGCHAO_LIVE_SCHEMA_V19_STATEMENTS),
    (20, LANGCHAO_LIVE_FK_SCHEMA_V20_STATEMENTS),
    (21, PRIVACY_DELETION_SCHEMA_V21_STATEMENTS),
    (22, CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS),
    (23, LANGCHAO_USER_OUTCOME_SCHEMA_V23_STATEMENTS),
    (24, LANGCHAO_NO_SEND_SCHEMA_V24_STATEMENTS),
    (25, LIVE_DISPATCH_LEGACY_SCHEMA_V25_STATEMENTS),
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
