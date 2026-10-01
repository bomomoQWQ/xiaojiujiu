"""PostgreSQL v20 exact witness for persisted 「浪潮」 live commits."""

from __future__ import annotations

from typing import Final


LANGCHAO_LIVE_FK_SCHEMA_V20_STATEMENTS: Final[tuple[str, ...]] = (
    """
    ALTER TABLE live_dispatch_claims
        ADD CONSTRAINT uq_live_dispatch_claim_exact_witness
        UNIQUE (scope_key, attempt_id, render_outbox_id, claim_id)
    """,
    """
    ALTER TABLE langchao_live_commits
        ADD COLUMN claim_id TEXT NOT NULL CHECK (btrim(claim_id) <> '')
    """,
    """
    ALTER TABLE langchao_live_commits
        ADD CONSTRAINT fk_langchao_live_commit_exact_claim
        FOREIGN KEY (scope_key, attempt_id, render_outbox_id, claim_id)
        REFERENCES live_dispatch_claims
            (scope_key, attempt_id, render_outbox_id, claim_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
    """,
)


__all__ = ["LANGCHAO_LIVE_FK_SCHEMA_V20_STATEMENTS"]
