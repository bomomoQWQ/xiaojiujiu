"""PostgreSQL v24 audit ledger for terminal non-send round results."""
from __future__ import annotations

from typing import Final

LANGCHAO_NO_SEND_SCHEMA_V24_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_no_send_results (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        round_id TEXT NOT NULL CHECK (btrim(round_id) <> ''),
        reason TEXT NOT NULL CHECK (reason IN (
            'no_eligible_candidate', 'decision_budget_exhausted',
            'competition_stalemate', 'permission_denied', 'permission_revoked',
            'invalidated', 'dispatch_failed', 'delivery_unknown'
        )),
        stage TEXT NOT NULL CHECK (btrim(stage) <> ''),
        candidate_id TEXT,
        permission_version TEXT,
        details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, round_id),
        CHECK (candidate_id IS NULL OR btrim(candidate_id) <> ''),
        CHECK (permission_version IS NULL OR btrim(permission_version) <> '')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_langchao_no_send_results_scope_time
    ON langchao_no_send_results (scope_key, recorded_at DESC, round_id)
    """,
)

__all__ = ["LANGCHAO_NO_SEND_SCHEMA_V24_STATEMENTS"]
