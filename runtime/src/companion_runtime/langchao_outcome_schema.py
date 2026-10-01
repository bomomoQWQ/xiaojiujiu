"""PostgreSQL v13 result-token ledger for the isolated 「浪潮」 engine.

The ledger is append-only and deliberately has no Runtime or sending integration.
"""

from __future__ import annotations

from typing import Final


LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_outcome_identities (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        token_id TEXT NOT NULL CHECK (btrim(token_id) <> ''),
        reward_contract_id TEXT NOT NULL CHECK (btrim(reward_contract_id) <> ''),
        outcome_key TEXT NOT NULL CHECK (btrim(outcome_key) <> ''),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, token_id),
        CONSTRAINT fk_langchao_outcome_identity_reward
            FOREIGN KEY (scope_key, reward_contract_id)
            REFERENCES langchao_reward_identities (scope_key, reward_contract_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_outcome_revisions (
        scope_key TEXT NOT NULL,
        token_id TEXT NOT NULL,
        revision BIGINT NOT NULL CHECK (revision > 0),
        token_version TEXT NOT NULL CHECK (btrim(token_version) <> ''),
        reward_contract_id TEXT NOT NULL CHECK (btrim(reward_contract_id) <> ''),
        reward_contract_revision BIGINT NOT NULL CHECK (reward_contract_revision > 0),
        settlement_type TEXT NOT NULL
            CHECK (settlement_type IN ('expected', 'actual', 'correction')),
        status TEXT NOT NULL CHECK (status IN (
            'unexecuted', 'pending', 'confirmed', 'not_observed', 'censored',
            'unattributable', 'corrected'
        )),
        base_amount DOUBLE PRECISION NOT NULL CHECK (
            base_amount = base_amount
            AND base_amount <> 'Infinity'::double precision
            AND base_amount <> '-Infinity'::double precision
        ),
        direction_weights JSONB NOT NULL CHECK (
            jsonb_typeof(direction_weights) = 'object'
            AND direction_weights->'approach' IS NOT NULL
            AND direction_weights->'expression' IS NOT NULL
            AND direction_weights->'exploration' IS NOT NULL
            AND direction_weights->'care' IS NOT NULL
            AND direction_weights->'commitment' IS NOT NULL
            AND direction_weights->'repair' IS NOT NULL
            AND direction_weights->'autonomy' IS NOT NULL
            AND direction_weights->'rest' IS NOT NULL
            AND direction_weights - ARRAY[
                'approach', 'expression', 'exploration', 'care',
                'commitment', 'repair', 'autonomy', 'rest'
            ] = '{}'::jsonb
            AND jsonb_typeof(direction_weights->'approach') = 'number'
            AND jsonb_typeof(direction_weights->'expression') = 'number'
            AND jsonb_typeof(direction_weights->'exploration') = 'number'
            AND jsonb_typeof(direction_weights->'care') = 'number'
            AND jsonb_typeof(direction_weights->'commitment') = 'number'
            AND jsonb_typeof(direction_weights->'repair') = 'number'
            AND jsonb_typeof(direction_weights->'autonomy') = 'number'
            AND jsonb_typeof(direction_weights->'rest') = 'number'
            AND (direction_weights->>'approach')::double precision = (direction_weights->>'approach')::double precision
            AND (direction_weights->>'approach')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'approach')::double precision >= 0.0
            AND (direction_weights->>'expression')::double precision = (direction_weights->>'expression')::double precision
            AND (direction_weights->>'expression')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'expression')::double precision >= 0.0
            AND (direction_weights->>'exploration')::double precision = (direction_weights->>'exploration')::double precision
            AND (direction_weights->>'exploration')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'exploration')::double precision >= 0.0
            AND (direction_weights->>'care')::double precision = (direction_weights->>'care')::double precision
            AND (direction_weights->>'care')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'care')::double precision >= 0.0
            AND (direction_weights->>'commitment')::double precision = (direction_weights->>'commitment')::double precision
            AND (direction_weights->>'commitment')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'commitment')::double precision >= 0.0
            AND (direction_weights->>'repair')::double precision = (direction_weights->>'repair')::double precision
            AND (direction_weights->>'repair')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'repair')::double precision >= 0.0
            AND (direction_weights->>'autonomy')::double precision = (direction_weights->>'autonomy')::double precision
            AND (direction_weights->>'autonomy')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'autonomy')::double precision >= 0.0
            AND (direction_weights->>'rest')::double precision = (direction_weights->>'rest')::double precision
            AND (direction_weights->>'rest')::double precision NOT IN ('Infinity'::double precision, '-Infinity'::double precision)
            AND (direction_weights->>'rest')::double precision >= 0.0
        ),
        evidence_version TEXT NOT NULL CHECK (btrim(evidence_version) <> ''),
        idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
        milestone_id TEXT,
        observation_started_at TIMESTAMPTZ,
        observation_ends_at TIMESTAMPTZ,
        corrects_token_id TEXT,
        corrects_revision BIGINT,
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, token_id, revision),
        CONSTRAINT uq_langchao_outcome_idempotency UNIQUE (scope_key, idempotency_key),
        CONSTRAINT fk_langchao_outcome_revision_identity
            FOREIGN KEY (scope_key, token_id)
            REFERENCES langchao_outcome_identities (scope_key, token_id)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_outcome_revision_reward
            FOREIGN KEY (scope_key, reward_contract_id, reward_contract_revision)
            REFERENCES langchao_reward_revisions (scope_key, reward_contract_id, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_outcome_correction_exact
            FOREIGN KEY (scope_key, corrects_token_id, corrects_revision)
            REFERENCES langchao_outcome_revisions (scope_key, token_id, revision)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT ck_langchao_outcome_window_pair CHECK (
            (observation_started_at IS NULL) = (observation_ends_at IS NULL)
            AND (observation_started_at IS NULL OR observation_ends_at > observation_started_at)
        ),
        CONSTRAINT ck_langchao_outcome_pending_window CHECK (
            status <> 'pending' OR observation_started_at IS NOT NULL
        ),
        CONSTRAINT ck_langchao_outcome_settlement_status CHECK (
            (settlement_type = 'expected' AND status IN ('unexecuted', 'pending')
                AND corrects_token_id IS NULL AND corrects_revision IS NULL)
            OR (settlement_type = 'actual' AND status NOT IN ('unexecuted', 'corrected')
                AND corrects_token_id IS NULL AND corrects_revision IS NULL)
            OR (settlement_type = 'correction' AND status = 'corrected'
                AND corrects_token_id IS NOT NULL AND corrects_revision IS NOT NULL
                AND corrects_token_id <> token_id)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_outcome_active (
        scope_key TEXT NOT NULL,
        token_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, token_id),
        CONSTRAINT fk_langchao_outcome_active_revision
            FOREIGN KEY (scope_key, token_id, revision)
            REFERENCES langchao_outcome_revisions (scope_key, token_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_reward_outcomes (
        scope_key TEXT NOT NULL,
        reward_contract_id TEXT NOT NULL,
        reward_contract_revision BIGINT NOT NULL CHECK (reward_contract_revision > 0),
        token_id TEXT NOT NULL,
        outcome_revision BIGINT NOT NULL CHECK (outcome_revision > 0),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        PRIMARY KEY (scope_key, reward_contract_id, reward_contract_revision, token_id),
        CONSTRAINT uq_langchao_reward_outcome_ordinal UNIQUE
            (scope_key, reward_contract_id, reward_contract_revision, ordinal),
        CONSTRAINT fk_langchao_reward_outcome_reward
            FOREIGN KEY (scope_key, reward_contract_id, reward_contract_revision)
            REFERENCES langchao_reward_revisions (scope_key, reward_contract_id, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_reward_outcome_revision
            FOREIGN KEY (scope_key, token_id, outcome_revision)
            REFERENCES langchao_outcome_revisions (scope_key, token_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_outcome_revision_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '浪潮 outcome revision rows are immutable' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_outcome_revisions_immutable
    BEFORE UPDATE OR DELETE ON langchao_outcome_revisions
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_outcome_revision_mutation()
    """,
)


__all__ = ["LANGCHAO_OUTCOME_SCHEMA_V13_STATEMENTS"]
