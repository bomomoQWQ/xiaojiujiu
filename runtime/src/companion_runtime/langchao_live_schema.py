"""PostgreSQL v19 recovery placeholders for committed 「浪潮」 live sends."""

from __future__ import annotations

from typing import Final

LANGCHAO_LIVE_SCHEMA_V19_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_live_commits (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        round_id TEXT NOT NULL CHECK (btrim(round_id) <> ''),
        langchao_candidate_id TEXT NOT NULL CHECK (btrim(langchao_candidate_id) <> ''),
        candidate_revision BIGINT NOT NULL CHECK (candidate_revision > 0),
        source_candidate_id TEXT NOT NULL CHECK (btrim(source_candidate_id) <> ''),
        reward_contract_id TEXT NOT NULL CHECK (btrim(reward_contract_id) <> ''),
        reward_revision BIGINT NOT NULL CHECK (reward_revision > 0),
        expected_token_ids JSONB NOT NULL CHECK (jsonb_typeof(expected_token_ids) = 'array'),
        snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
        attempt_id TEXT NOT NULL CHECK (btrim(attempt_id) <> ''),
        render_outbox_id TEXT NOT NULL CHECK (btrim(render_outbox_id) <> ''),
        committed_at TIMESTAMPTZ NOT NULL,
        terminal_ack_id TEXT,
        terminal_ack_kind TEXT CHECK (terminal_ack_kind IN ('sent', 'failed')),
        terminal_acknowledged_at TIMESTAMPTZ,
        PRIMARY KEY (scope_key, round_id),
        CONSTRAINT uq_langchao_live_commit_attempt UNIQUE (scope_key, attempt_id),
        CONSTRAINT ck_langchao_live_terminal_pair CHECK (
            (terminal_ack_kind IS NULL AND terminal_ack_id IS NULL AND terminal_acknowledged_at IS NULL)
            OR (terminal_ack_kind IS NOT NULL AND terminal_ack_id IS NOT NULL
                AND btrim(terminal_ack_id) <> '' AND terminal_acknowledged_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_langchao_live_commits_pending
    ON langchao_live_commits (scope_key, committed_at)
    WHERE terminal_ack_kind IS NULL
    """,
)

__all__ = ["LANGCHAO_LIVE_SCHEMA_V19_STATEMENTS"]
