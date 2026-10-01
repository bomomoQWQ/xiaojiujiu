"""Contract tests for the isolated PostgreSQL user-model v2 schema."""

from __future__ import annotations

import re

from companion_runtime.runtime_core_v2_schema import CORE_SCHEMA_STATEMENTS
from companion_runtime.user_model_v2_schema import (
    MIGRATIONS,
    USER_MODEL_SCHEMA_VERSION,
    schema_statements,
)

TABLES = (
    "interaction_exposures_v2",
    "interaction_target_labels_v2",
    "user_model_parameter_snapshots_v2",
    "user_model_active_parameters_v2",
    "prediction_snapshots_v2",
    "expectations_v2",
    "wait_processes_v2",
)

SQLITE_ONLY = (
    "AUTOINCREMENT",
    "WITHOUT ROWID",
    "PRAGMA",
    "INSERT OR REPLACE",
    "INSERT OR IGNORE",
    "sqlite_master",
    "strftime(",
)


def _normalise(sql: str) -> str:
    return " ".join(sql.split()).upper()


def _table_ddl(table: str) -> str:
    prefix = f"CREATE TABLE IF NOT EXISTS {table} (".upper()
    return next(statement for statement in schema_statements() if _normalise(statement).startswith(prefix))


def test_schema_uses_postgresql_native_types_and_no_placeholders() -> None:
    ddl = "\n".join(schema_statements())
    upper = ddl.upper()

    assert all(token.upper() not in upper for token in SQLITE_ONLY)
    assert "?" not in ddl
    assert "%S" not in upper
    assert "TIMESTAMPTZ" in upper
    assert "JSONB" in upper
    assert "DOUBLE PRECISION" in upper
    assert "::JSONB" in upper


def test_every_required_table_has_scope_native_types_and_versioning() -> None:
    for table in TABLES:
        ddl = _normalise(_table_ddl(table))
        assert "SCOPE_KEY TEXT NOT NULL" in ddl
        assert "TIMESTAMPTZ" in ddl
        assert "JSONB" in ddl
        assert "DOUBLE PRECISION" in ddl
        assert re.search(r"\b(?:SCHEMA_VERSION|[A-Z_]+_VERSION)\b", ddl)
        assert "FOREIGN KEY" in ddl
        assert "CHECK (" in ddl
        assert "UNIQUE (SCOPE_KEY," in ddl


def test_cross_table_foreign_keys_are_scope_safe() -> None:
    ddl = _normalise("\n".join(schema_statements()))
    expected_links = (
        "REFERENCES INTERACTION_EXPOSURES_V2 (SCOPE_KEY, EXPOSURE_ID)",
        "REFERENCES USER_MODEL_PARAMETER_SNAPSHOTS_V2 (SCOPE_KEY, PARAMETER_SNAPSHOT_ID)",
        "REFERENCES PREDICTION_SNAPSHOTS_V2 (SCOPE_KEY, PREDICTION_SNAPSHOT_ID)",
        "REFERENCES EXPECTATIONS_V2 (SCOPE_KEY, EXPECTATION_ID)",
    )
    assert all(link in ddl for link in expected_links)


def test_idempotency_and_single_active_parameter_constraints_are_explicit() -> None:
    ddl = _normalise("\n".join(schema_statements()))
    for table in TABLES:
        table_ddl = _normalise(_table_ddl(table))
        assert "IDEMPOTENCY_KEY TEXT NOT NULL" in table_ddl
        assert "UNIQUE (SCOPE_KEY, IDEMPOTENCY_KEY)" in table_ddl

    assert "CREATE UNIQUE INDEX IF NOT EXISTS UQ_USER_MODEL_ACTIVE_PARAMETERS_V2_CURRENT" in ddl
    assert "WHERE DEACTIVATED_AT IS NULL" in ddl


def test_migration_versions_and_statement_order_are_stable() -> None:
    assert USER_MODEL_SCHEMA_VERSION == 14
    assert isinstance(MIGRATIONS, tuple)
    assert tuple(version for version, _statements in MIGRATIONS) == tuple(range(1, 15))
    assert all(isinstance(statements, tuple) for _version, statements in MIGRATIONS)
    assert schema_statements() == tuple(
        statement for _version, statements in MIGRATIONS for statement in statements
    )
    assert schema_statements() is not schema_statements()

    create_order = tuple(
        re.search(r"CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)", statement, re.IGNORECASE).group(1)
        for statement in schema_statements()
        if re.search(r"CREATE TABLE IF NOT EXISTS", statement, re.IGNORECASE)
    )
    core_create_order = tuple(
        re.search(r"CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)", statement, re.IGNORECASE).group(1)
        for statement in CORE_SCHEMA_STATEMENTS
        if re.search(r"CREATE TABLE IF NOT EXISTS", statement, re.IGNORECASE)
    )
    assert create_order == (
        TABLES
        + ("user_model_active_labels_v2",)
        + core_create_order
        + ("user_model_migration_audits_v2", "user_model_migration_quarantine_v2")
        + (
            "runtime_v2_exposure_metadata",
            "runtime_v2_user_matter_events",
            "runtime_v2_decision_audits",
            "runtime_v2_expectation_settlements",
            "mechanical_history_import_audits_v1",
            "mechanical_history_import_quarantine_v1",
            "runtime_v2_committed_decisions",
            "langchao_goal_identities",
            "langchao_reward_identities",
            "langchao_candidate_identities",
            "langchao_goal_revisions",
            "langchao_reward_revisions",
            "langchao_candidate_revisions",
            "langchao_goal_active",
            "langchao_reward_active",
            "langchao_candidate_active",
            "langchao_candidate_goal_refs",
            "langchao_outcome_identities",
            "langchao_outcome_revisions",
            "langchao_outcome_active",
            "langchao_reward_outcomes",
            "langchao_rounds",
            "langchao_state_snapshots",
            "langchao_state_candidates",
            "langchao_active_state",
            "langchao_integration_steps",
        )
    )


def test_v10_mechanical_history_audit_is_independent_and_privacy_minimal() -> None:
    version, statements = next(item for item in MIGRATIONS if item[0] == 10)
    ddl = _normalise("\n".join(statements))
    assert version == 10
    assert "MECHANICAL_HISTORY_IMPORT_AUDITS_V1" in ddl
    assert "RUN_ID UUID PRIMARY KEY" in ddl
    assert "STATUS IN ('APPLIED', 'FAILED')" in ddl
    assert "RECONCILIATION JSONB" in ddl
    assert "UNIQUE (SCOPE_KEY, SOURCE_SNAPSHOT_SHA256, PLAN_SHA256, IMPORTER_VERSION)" in ddl
    assert "MECHANICAL_HISTORY_IMPORT_QUARANTINE_V1" in ddl
    assert "SOURCE_TABLE TEXT" in ddl
    assert "SOURCE_PK TEXT" in ddl
    assert "PAYLOAD_SHA256 TEXT" in ddl
    assert "PAYLOAD JSONB" not in ddl
    assert "CONTENT TEXT" not in ddl


def test_v11_committed_decision_snapshot_has_terminal_placeholder() -> None:
    version, statements = next(item for item in MIGRATIONS if item[0] == 11)
    ddl = _normalise("\n".join(statements))
    assert version == 11
    assert "RUNTIME_V2_COMMITTED_DECISIONS" in ddl
    assert "DECISION_ID TEXT PRIMARY KEY" in ddl
    assert "CANDIDATE_SNAPSHOT JSONB NOT NULL" in ddl
    assert "AUDIT_SNAPSHOT JSONB NOT NULL" in ddl
    assert "ATTEMPT_ID TEXT NOT NULL" in ddl
    assert "RENDER_OUTBOX_ID TEXT NOT NULL" in ddl
    assert "TERMINAL_STATUS IN ('SENT', 'FAILED')" in ddl
    assert "WHERE TERMINAL_STATUS IS NULL" in ddl
