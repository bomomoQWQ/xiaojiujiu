"""PostgreSQL v12 persistence schema for the isolated 「浪潮」 contracts."""

from __future__ import annotations

from typing import Final


LANGCHAO_SCHEMA_V12_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_goal_identities (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        goal_id TEXT NOT NULL CHECK (btrim(goal_id) <> ''),
        semantic_key TEXT NOT NULL CHECK (btrim(semantic_key) <> ''),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, goal_id),
        CONSTRAINT uq_langchao_goal_identity_semantic UNIQUE (scope_key, semantic_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_reward_identities (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        reward_contract_id TEXT NOT NULL CHECK (btrim(reward_contract_id) <> ''),
        semantic_key TEXT NOT NULL CHECK (btrim(semantic_key) <> ''),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, reward_contract_id),
        CONSTRAINT uq_langchao_reward_identity_semantic UNIQUE (scope_key, semantic_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_candidate_identities (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        candidate_id TEXT NOT NULL CHECK (btrim(candidate_id) <> ''),
        semantic_key TEXT NOT NULL CHECK (btrim(semantic_key) <> ''),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, candidate_id),
        CONSTRAINT uq_langchao_candidate_identity_semantic UNIQUE (scope_key, semantic_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_goal_revisions (
        scope_key TEXT NOT NULL,
        goal_id TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        kind TEXT NOT NULL CHECK (kind IN ('continuous_need', 'finite', 'open_activity')),
        ownership TEXT NOT NULL CHECK (ownership IN ('user_request', 'self_commitment', 'shared_arrangement', 'self_wish', 'self_interest')),
        status TEXT NOT NULL CHECK (status IN ('adopted', 'actionable', 'waiting', 'paused', 'completed', 'dropped', 'invalidated')),
        parent_goal_id TEXT,
        reward_contract_id TEXT,
        contract_created_at TIMESTAMPTZ NOT NULL,
        contract_updated_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, goal_id, revision),
        CONSTRAINT uq_langchao_goal_revision_hash UNIQUE (scope_key, goal_id, revision, payload_sha256),
        CONSTRAINT fk_langchao_goal_revision_identity FOREIGN KEY (scope_key, goal_id)
            REFERENCES langchao_goal_identities (scope_key, goal_id) ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_goal_revision_parent FOREIGN KEY (scope_key, parent_goal_id)
            REFERENCES langchao_goal_identities (scope_key, goal_id) ON DELETE RESTRICT,
        -- Goal revision 1 may leave reward_contract_id NULL to break the natural
        -- goal/reward creation cycle; a later immutable goal revision may bind it.
        CONSTRAINT fk_langchao_goal_revision_reward FOREIGN KEY (scope_key, reward_contract_id)
            REFERENCES langchao_reward_identities (scope_key, reward_contract_id) ON DELETE RESTRICT,
        CONSTRAINT ck_langchao_goal_revision_time CHECK (contract_updated_at >= contract_created_at),
        CONSTRAINT ck_langchao_goal_revision_parent_not_self CHECK (parent_goal_id IS NULL OR parent_goal_id <> goal_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_reward_revisions (
        scope_key TEXT NOT NULL,
        reward_contract_id TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        goal_id TEXT NOT NULL,
        total_cap DOUBLE PRECISION NOT NULL CHECK (
            total_cap = total_cap
            AND total_cap <> 'Infinity'::double precision
            AND total_cap <> '-Infinity'::double precision
            AND total_cap >= 0.0
        ),
        contract_created_at TIMESTAMPTZ NOT NULL,
        contract_updated_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, reward_contract_id, revision),
        CONSTRAINT uq_langchao_reward_revision_hash UNIQUE (scope_key, reward_contract_id, revision, payload_sha256),
        CONSTRAINT fk_langchao_reward_revision_identity FOREIGN KEY (scope_key, reward_contract_id)
            REFERENCES langchao_reward_identities (scope_key, reward_contract_id) ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_reward_revision_goal FOREIGN KEY (scope_key, goal_id)
            REFERENCES langchao_goal_identities (scope_key, goal_id) ON DELETE RESTRICT,
        CONSTRAINT ck_langchao_reward_revision_time CHECK (contract_updated_at >= contract_created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_candidate_revisions (
        scope_key TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        kind TEXT NOT NULL CHECK (kind IN ('external_message', 'internal_process', 'defer_or_rest')),
        state TEXT NOT NULL CHECK (state IN ('proposed', 'validated', 'competitive', 'dormant', 'reserved', 'retired')),
        retirement_reason TEXT CHECK (retirement_reason IN ('completed', 'invalidated', 'superseded', 'dropped', 'window_closed')),
        reward_contract_id TEXT NOT NULL,
        reward_revision BIGINT NOT NULL CHECK (reward_revision > 0),
        resource_budget DOUBLE PRECISION NOT NULL CHECK (
            resource_budget = resource_budget
            AND resource_budget <> 'Infinity'::double precision
            AND resource_budget <> '-Infinity'::double precision
            AND resource_budget >= 0.0
        ),
        available_from TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ,
        contract_created_at TIMESTAMPTZ NOT NULL,
        contract_updated_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, candidate_id, revision),
        CONSTRAINT uq_langchao_candidate_revision_hash UNIQUE (scope_key, candidate_id, revision, payload_sha256),
        CONSTRAINT fk_langchao_candidate_revision_identity FOREIGN KEY (scope_key, candidate_id)
            REFERENCES langchao_candidate_identities (scope_key, candidate_id) ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_candidate_revision_reward FOREIGN KEY
            (scope_key, reward_contract_id, reward_revision)
            REFERENCES langchao_reward_revisions (scope_key, reward_contract_id, revision)
            ON DELETE RESTRICT,
        CONSTRAINT ck_langchao_candidate_revision_window CHECK (expires_at IS NULL OR expires_at > available_from),
        CONSTRAINT ck_langchao_candidate_revision_time CHECK (contract_updated_at >= contract_created_at),
        CONSTRAINT ck_langchao_candidate_revision_retirement CHECK (
            (state = 'retired' AND retirement_reason IS NOT NULL)
            OR (state <> 'retired' AND retirement_reason IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_goal_active (
        scope_key TEXT NOT NULL,
        goal_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, goal_id),
        CONSTRAINT fk_langchao_goal_active_revision FOREIGN KEY (scope_key, goal_id, revision)
            REFERENCES langchao_goal_revisions (scope_key, goal_id, revision) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_reward_active (
        scope_key TEXT NOT NULL,
        reward_contract_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, reward_contract_id),
        CONSTRAINT fk_langchao_reward_active_revision FOREIGN KEY (scope_key, reward_contract_id, revision)
            REFERENCES langchao_reward_revisions (scope_key, reward_contract_id, revision) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_candidate_active (
        scope_key TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, candidate_id),
        CONSTRAINT fk_langchao_candidate_active_revision FOREIGN KEY (scope_key, candidate_id, revision)
            REFERENCES langchao_candidate_revisions (scope_key, candidate_id, revision) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_candidate_goal_refs (
        scope_key TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        candidate_revision BIGINT NOT NULL,
        goal_id TEXT NOT NULL,
        goal_revision BIGINT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        PRIMARY KEY (scope_key, candidate_id, candidate_revision, goal_id),
        CONSTRAINT uq_langchao_candidate_goal_ref_ordinal UNIQUE (scope_key, candidate_id, candidate_revision, ordinal),
        CONSTRAINT fk_langchao_candidate_goal_ref_candidate FOREIGN KEY (scope_key, candidate_id, candidate_revision)
            REFERENCES langchao_candidate_revisions (scope_key, candidate_id, revision) ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_candidate_goal_ref_goal FOREIGN KEY (scope_key, goal_id, goal_revision)
            REFERENCES langchao_goal_revisions (scope_key, goal_id, revision) ON DELETE RESTRICT
    )
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_revision_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '浪潮 revision rows are immutable' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_goal_revisions_immutable
    BEFORE UPDATE OR DELETE ON langchao_goal_revisions
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_revision_mutation()
    """,
    """
    CREATE TRIGGER langchao_reward_revisions_immutable
    BEFORE UPDATE OR DELETE ON langchao_reward_revisions
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_revision_mutation()
    """,
    """
    CREATE TRIGGER langchao_candidate_revisions_immutable
    BEFORE UPDATE OR DELETE ON langchao_candidate_revisions
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_revision_mutation()
    """,
)


__all__ = ["LANGCHAO_SCHEMA_V12_STATEMENTS"]
