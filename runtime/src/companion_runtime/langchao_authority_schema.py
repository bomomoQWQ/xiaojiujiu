"""PostgreSQL v15 schema for scoped engine authority and live dispatch claims."""

from __future__ import annotations

from typing import Final


LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_authority_revisions (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        authority_id TEXT NOT NULL CHECK (btrim(authority_id) <> ''),
        revision BIGINT NOT NULL CHECK (revision > 0),
        engine_key TEXT NOT NULL CHECK (engine_key IN ('runtime_v2', 'langchao', 'none')),
        mode TEXT NOT NULL CHECK (mode IN ('live', 'shadow', 'disabled')),
        may_dispatch BOOLEAN GENERATED ALWAYS AS
            (mode = 'live' AND engine_key <> 'none') STORED,
        reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, authority_id, revision),
        CONSTRAINT uq_langchao_authority_revision_hash
            UNIQUE (scope_key, authority_id, revision, payload_sha256),
        -- This redundant key is the SQL capability witness consumed by dispatch claims.
        CONSTRAINT uq_langchao_authority_dispatch_witness
            UNIQUE (scope_key, authority_id, revision, engine_key, may_dispatch)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_authority_active (
        scope_key TEXT PRIMARY KEY CHECK (btrim(scope_key) <> ''),
        authority_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_langchao_authority_active_revision
            FOREIGN KEY (scope_key, authority_id, revision)
            REFERENCES langchao_authority_revisions (scope_key, authority_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_dispatch_claims (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        dispatch_id TEXT NOT NULL CHECK (btrim(dispatch_id) <> ''),
        authority_id TEXT NOT NULL,
        authority_revision BIGINT NOT NULL CHECK (authority_revision > 0),
        engine_key TEXT NOT NULL CHECK (engine_key IN ('runtime_v2', 'langchao')),
        may_dispatch BOOLEAN NOT NULL DEFAULT TRUE CHECK (may_dispatch),
        candidate_id TEXT NOT NULL CHECK (btrim(candidate_id) <> ''),
        candidate_revision BIGINT NOT NULL CHECK (candidate_revision > 0),
        attempt_id TEXT NOT NULL CHECK (btrim(attempt_id) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        claim_sha256 TEXT NOT NULL CHECK (claim_sha256 ~ '^[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (scope_key, dispatch_id),
        CONSTRAINT uq_langchao_dispatch_claim_attempt UNIQUE (scope_key, attempt_id),
        CONSTRAINT uq_langchao_dispatch_claim_idempotency UNIQUE (scope_key, idempotency_key),
        CONSTRAINT uq_langchao_dispatch_claim_hash UNIQUE (scope_key, dispatch_id, claim_sha256),
        CONSTRAINT fk_langchao_dispatch_claim_live_authority
            FOREIGN KEY (scope_key, authority_id, authority_revision, engine_key, may_dispatch)
            REFERENCES langchao_authority_revisions
                (scope_key, authority_id, revision, engine_key, may_dispatch)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_dispatch_claim_candidate_revision
            FOREIGN KEY (scope_key, candidate_id, candidate_revision)
            REFERENCES langchao_candidate_revisions (scope_key, candidate_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_authority_revision_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '浪潮 authority revision rows are immutable' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_authority_revisions_immutable
    BEFORE UPDATE OR DELETE ON langchao_authority_revisions
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_authority_revision_mutation()
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_dispatch_claim_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '浪潮 dispatch claim rows are immutable' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_dispatch_claims_immutable
    BEFORE UPDATE OR DELETE ON langchao_dispatch_claims
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_dispatch_claim_mutation()
    """,
)


__all__ = ["LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS"]
