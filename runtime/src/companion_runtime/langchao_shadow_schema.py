"""PostgreSQL v16 audit schema for read-only 「浪潮」 shadow runs."""

from __future__ import annotations

from typing import Final


LANGCHAO_SHADOW_SCHEMA_VERSION: Final[int] = 16

LANGCHAO_SHADOW_SCHEMA_V16_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_shadow_runs (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        run_id TEXT NOT NULL CHECK (btrim(run_id) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        source_input_cursor TEXT NOT NULL CHECK (btrim(source_input_cursor) <> ''),
        source_input_version TEXT NOT NULL CHECK (btrim(source_input_version) <> ''),
        decision_round_id TEXT NOT NULL CHECK (btrim(decision_round_id) <> ''),
        mode TEXT NOT NULL DEFAULT 'shadow' CHECK (mode = 'shadow'),
        candidate_id TEXT,
        defer_reason TEXT,
        comparison JSONB NOT NULL CHECK (jsonb_typeof(comparison) = 'object'),
        audit JSONB NOT NULL CHECK (jsonb_typeof(audit) = 'object'),
        input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
        audit_sha256 TEXT NOT NULL CHECK (audit_sha256 ~ '^[0-9a-f]{64}$'),
        sent_count BIGINT NOT NULL DEFAULT 0 CHECK (sent_count = 0),
        reward_count BIGINT NOT NULL DEFAULT 0 CHECK (reward_count = 0),
        training_count BIGINT NOT NULL DEFAULT 0 CHECK (training_count = 0),
        quota_count BIGINT NOT NULL DEFAULT 0 CHECK (quota_count = 0),
        outbox_id TEXT CHECK (outbox_id IS NULL),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, run_id),
        CONSTRAINT uq_langchao_shadow_runs_idempotency UNIQUE (scope_key, idempotency_key),
        CONSTRAINT fk_langchao_shadow_runs_round FOREIGN KEY (scope_key, decision_round_id)
            REFERENCES langchao_rounds (scope_key, round_id) ON DELETE RESTRICT,
        CONSTRAINT ck_langchao_shadow_runs_outcome CHECK (
            (candidate_id IS NOT NULL AND defer_reason IS NULL)
            OR (candidate_id IS NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_langchao_shadow_runs_scope_round
    ON langchao_shadow_runs (scope_key, decision_round_id, recorded_at DESC)
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_shadow_run_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '浪潮 shadow run rows are immutable' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_shadow_runs_immutable
    BEFORE UPDATE OR DELETE ON langchao_shadow_runs
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_shadow_run_mutation()
    """,
)


__all__ = [
    "LANGCHAO_SHADOW_SCHEMA_VERSION",
    "LANGCHAO_SHADOW_SCHEMA_V16_STATEMENTS",
]
