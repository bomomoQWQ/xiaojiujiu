"""PostgreSQL v26 semantic dispatch witness and recoverable live settlement."""
from __future__ import annotations

from typing import Final

LANGCHAO_LIVE_SETTLEMENT_SCHEMA_V26_STATEMENTS: Final[tuple[str, ...]] = (
    """
    ALTER TABLE langchao_dispatch_claims
        ADD CONSTRAINT uq_langchao_dispatch_claim_exact_live_commit
        UNIQUE (scope_key, dispatch_id, candidate_id, candidate_revision,
                attempt_id, authority_revision)
    """,
    """
    ALTER TABLE langchao_live_commits
        ADD COLUMN semantic_dispatch_id TEXT,
        ADD COLUMN authority_revision BIGINT,
        ADD COLUMN settlement_status TEXT NOT NULL DEFAULT 'pending',
        ADD COLUMN settlement_completed_at TIMESTAMPTZ,
        ADD COLUMN exposure_recovery_required BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN exposure_recovered_at TIMESTAMPTZ,
        ADD CONSTRAINT ck_langchao_live_semantic_witness_pair CHECK (
            (semantic_dispatch_id IS NULL AND authority_revision IS NULL)
            OR (semantic_dispatch_id IS NOT NULL AND btrim(semantic_dispatch_id) <> ''
                AND authority_revision IS NOT NULL AND authority_revision > 0)
        ),
        ADD CONSTRAINT ck_langchao_live_settlement_status CHECK (
            settlement_status IN ('pending', 'completed')
        ),
        ADD CONSTRAINT ck_langchao_live_settlement_completed CHECK (
            (settlement_status = 'pending' AND settlement_completed_at IS NULL)
            OR (settlement_status = 'completed' AND settlement_completed_at IS NOT NULL)
        ),
        ADD CONSTRAINT ck_langchao_live_exposure_recovery CHECK (
            (NOT exposure_recovery_required AND exposure_recovered_at IS NULL)
            OR exposure_recovery_required
        )
    """,
    """
    UPDATE langchao_live_commits AS c
       SET semantic_dispatch_id = d.dispatch_id,
           authority_revision = d.authority_revision
      FROM langchao_dispatch_claims AS d
     WHERE c.semantic_dispatch_id IS NULL
       AND d.scope_key = c.scope_key
       AND d.candidate_id = c.langchao_candidate_id
       AND d.candidate_revision = c.candidate_revision
       AND d.attempt_id = c.attempt_id
    """,
    """
    ALTER TABLE langchao_live_commits
        ADD CONSTRAINT fk_langchao_live_commit_semantic_dispatch
        FOREIGN KEY (scope_key, semantic_dispatch_id, langchao_candidate_id,
                     candidate_revision, attempt_id, authority_revision)
        REFERENCES langchao_dispatch_claims
            (scope_key, dispatch_id, candidate_id, candidate_revision,
             attempt_id, authority_revision)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
        NOT VALID
    """,
    """
    ALTER TABLE langchao_live_commits
        VALIDATE CONSTRAINT fk_langchao_live_commit_semantic_dispatch
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_langchao_live_settlement_recovery
    ON langchao_live_commits (scope_key, committed_at, round_id)
    WHERE settlement_status = 'pending' OR exposure_recovery_required
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_commit_no_send_split()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        -- A round must not carry two contradictory accounts of itself. The one
        -- exception is the delivery-stage record: a round that committed may fail its
        -- acknowledgement, and censoring that delivery is the settlement of the commit
        -- rather than a competing story. This mirrors
        -- LangchaoNoSendRepository.save, which keys the same distinction off the
        -- shared DELIVERY_STAGE constant.
        IF TG_TABLE_NAME = 'langchao_live_commits' THEN
            IF EXISTS (
                SELECT 1 FROM langchao_no_send_results AS n
                WHERE n.scope_key = NEW.scope_key AND n.round_id = NEW.round_id
                  AND n.stage <> 'delivery'
            ) THEN
                RAISE EXCEPTION 'round already has durable no-send result'
                    USING ERRCODE = '23505';
            END IF;
        ELSE
            IF NEW.stage <> 'delivery' AND EXISTS (
                SELECT 1 FROM langchao_live_commits AS c
                WHERE c.scope_key = NEW.scope_key AND c.round_id = NEW.round_id
            ) THEN
                RAISE EXCEPTION 'round already has durable live commit'
                    USING ERRCODE = '23505';
            END IF;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_live_commits_exclude_no_send
    BEFORE INSERT ON langchao_live_commits
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_commit_no_send_split()
    """,
    """
    CREATE TRIGGER langchao_no_send_exclude_live_commit
    BEFORE INSERT ON langchao_no_send_results
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_commit_no_send_split()
    """,
)

__all__ = ["LANGCHAO_LIVE_SETTLEMENT_SCHEMA_V26_STATEMENTS"]
