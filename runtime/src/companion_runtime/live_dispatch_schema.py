"""PostgreSQL v18 witness tying production attempts/outbox rows to live authority."""

from __future__ import annotations

from typing import Final


LIVE_DISPATCH_SCHEMA_V18_STATEMENTS: Final[tuple[str, ...]] = (
    """
    ALTER TABLE action_attempts
        ADD COLUMN IF NOT EXISTS dispatch_scope_key TEXT,
        ADD COLUMN IF NOT EXISTS dispatch_claim_id TEXT
    """,
    """
    ALTER TABLE outbox
        ADD COLUMN IF NOT EXISTS dispatch_scope_key TEXT,
        ADD COLUMN IF NOT EXISTS dispatch_claim_id TEXT
    """,
    """
    ALTER TABLE action_attempts
        ADD CONSTRAINT uq_action_attempts_dispatch_witness
        UNIQUE (dispatch_scope_key, attempt_id, dispatch_claim_id)
    """,
    """
    ALTER TABLE outbox
        ADD CONSTRAINT uq_outbox_dispatch_witness
        UNIQUE (dispatch_scope_key, outbox_id, dispatch_claim_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS live_dispatch_claims (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        claim_id TEXT NOT NULL CHECK (btrim(claim_id) <> ''),
        authority_id TEXT NOT NULL,
        authority_revision BIGINT NOT NULL CHECK (authority_revision > 0),
        engine_key TEXT NOT NULL CHECK (engine_key IN ('runtime_v2', 'langchao')),
        may_dispatch BOOLEAN NOT NULL DEFAULT TRUE CHECK (may_dispatch),
        round_id TEXT NOT NULL CHECK (btrim(round_id) <> ''),
        candidate_id TEXT NOT NULL CHECK (btrim(candidate_id) <> ''),
        candidate_version TEXT NOT NULL CHECK (btrim(candidate_version) <> ''),
        attempt_id TEXT NOT NULL CHECK (btrim(attempt_id) <> ''),
        render_outbox_id TEXT NOT NULL CHECK (btrim(render_outbox_id) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        claim_sha256 TEXT NOT NULL CHECK (claim_sha256 ~ '^[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (scope_key, claim_id),
        CONSTRAINT uq_live_dispatch_claim_id UNIQUE (claim_id),
        CONSTRAINT uq_live_dispatch_claim_attempt UNIQUE (attempt_id),
        CONSTRAINT uq_live_dispatch_claim_render_outbox UNIQUE (render_outbox_id),
        CONSTRAINT uq_live_dispatch_claim_round UNIQUE (scope_key, engine_key, round_id),
        CONSTRAINT uq_live_dispatch_claim_idempotency UNIQUE (scope_key, idempotency_key),
        CONSTRAINT fk_live_dispatch_claim_authority
            FOREIGN KEY (scope_key, authority_id, authority_revision, engine_key, may_dispatch)
            REFERENCES langchao_authority_revisions
                (scope_key, authority_id, revision, engine_key, may_dispatch)
            ON DELETE RESTRICT,
        CONSTRAINT fk_live_dispatch_claim_attempt
            FOREIGN KEY (scope_key, attempt_id, claim_id)
            REFERENCES action_attempts
                (dispatch_scope_key, attempt_id, dispatch_claim_id)
            DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT fk_live_dispatch_claim_render_outbox
            FOREIGN KEY (scope_key, render_outbox_id, claim_id)
            REFERENCES outbox
                (dispatch_scope_key, outbox_id, dispatch_claim_id)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE OR REPLACE FUNCTION validate_live_dispatch_claim()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
              FROM langchao_authority_active AS a
              JOIN langchao_authority_revisions AS r
                ON r.scope_key = a.scope_key
               AND r.authority_id = a.authority_id
               AND r.revision = a.revision
             WHERE a.scope_key = NEW.scope_key
               AND r.authority_id = NEW.authority_id
               AND r.revision = NEW.authority_revision
               AND r.engine_key = NEW.engine_key
               AND r.mode = 'live'
               AND r.may_dispatch
        ) THEN
            RAISE EXCEPTION 'dispatch claim is not the active live authority for scope %', NEW.scope_key
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER live_dispatch_claim_validate_active
    BEFORE INSERT ON live_dispatch_claims
    FOR EACH ROW EXECUTE FUNCTION validate_live_dispatch_claim()
    """,
    """
    CREATE OR REPLACE FUNCTION witness_action_attempt_dispatch()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE claim live_dispatch_claims%ROWTYPE;
    BEGIN
        SELECT * INTO claim FROM live_dispatch_claims WHERE attempt_id = NEW.attempt_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'action attempt % has no live dispatch claim', NEW.attempt_id
                USING ERRCODE = '23503';
        END IF;
        IF claim.candidate_id IS DISTINCT FROM NEW.candidate_id THEN
            RAISE EXCEPTION 'action attempt % candidate differs from dispatch claim', NEW.attempt_id
                USING ERRCODE = '23514';
        END IF;
        NEW.dispatch_scope_key := claim.scope_key;
        NEW.dispatch_claim_id := claim.claim_id;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER action_attempts_require_live_dispatch_claim
    BEFORE INSERT ON action_attempts
    FOR EACH ROW EXECUTE FUNCTION witness_action_attempt_dispatch()
    """,
    """
    CREATE OR REPLACE FUNCTION witness_outbox_dispatch()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE attempt_identity TEXT;
    DECLARE round_identity TEXT;
    DECLARE claim live_dispatch_claims%ROWTYPE;
    BEGIN
        IF NEW.kind NOT IN ('render', 'send') THEN
            RETURN NEW;
        END IF;
        attempt_identity := NEW.payload_json ->> 'attempt_id';
        IF attempt_identity IS NULL OR btrim(attempt_identity) = '' THEN
            RAISE EXCEPTION '% outbox % has no attempt identity', NEW.kind, NEW.outbox_id
                USING ERRCODE = '23514';
        END IF;
        SELECT * INTO claim FROM live_dispatch_claims WHERE attempt_id = attempt_identity;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'outbox % has no live dispatch claim', NEW.outbox_id
                USING ERRCODE = '23503';
        END IF;
        IF NEW.kind = 'render' AND claim.render_outbox_id <> NEW.outbox_id THEN
            RAISE EXCEPTION 'render outbox % differs from dispatch claim', NEW.outbox_id
                USING ERRCODE = '23514';
        END IF;
        round_identity := NEW.payload_json ->> 'decision_id';
        IF round_identity IS NOT NULL AND round_identity <> claim.round_id THEN
            RAISE EXCEPTION 'outbox % round differs from dispatch claim', NEW.outbox_id
                USING ERRCODE = '23514';
        END IF;
        NEW.dispatch_scope_key := claim.scope_key;
        NEW.dispatch_claim_id := claim.claim_id;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER outbox_require_live_dispatch_claim
    BEFORE INSERT ON outbox
    FOR EACH ROW EXECUTE FUNCTION witness_outbox_dispatch()
    """,
    """
    CREATE OR REPLACE FUNCTION reject_live_dispatch_claim_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'live dispatch claims are immutable' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER live_dispatch_claims_immutable
    BEFORE UPDATE OR DELETE ON live_dispatch_claims
    FOR EACH ROW EXECUTE FUNCTION reject_live_dispatch_claim_mutation()
    """,
)


__all__ = ["LIVE_DISPATCH_SCHEMA_V18_STATEMENTS"]
