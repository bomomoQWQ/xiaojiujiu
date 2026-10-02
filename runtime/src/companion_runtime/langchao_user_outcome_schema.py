"""v23 active pointer for settled Langchao user outcomes."""

from __future__ import annotations

from typing import Final

LANGCHAO_USER_OUTCOME_SCHEMA_V23_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_user_outcome_active (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        reward_contract_id TEXT NOT NULL CHECK (btrim(reward_contract_id) <> ''),
        episode_id TEXT NOT NULL CHECK (btrim(episode_id) <> ''),
        outcome_key TEXT NOT NULL CHECK (outcome_key IN ('reply', 'continuation', 'negative')),
        token_id TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        source_exposure_id UUID NOT NULL,
        source_label_revision BIGINT NOT NULL CHECK (source_label_revision > 0),
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, reward_contract_id, episode_id, outcome_key),
        CONSTRAINT fk_langchao_user_outcome_token
            FOREIGN KEY (scope_key, token_id, revision)
            REFERENCES langchao_outcome_revisions (scope_key, token_id, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_user_outcome_exposure
            FOREIGN KEY (scope_key, source_exposure_id)
            REFERENCES interaction_exposures_v2 (scope_key, exposure_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_langchao_user_outcome_history
    ON langchao_user_outcome_active (scope_key, episode_id, outcome_key)
    """,
)

__all__ = ["LANGCHAO_USER_OUTCOME_SCHEMA_V23_STATEMENTS"]
