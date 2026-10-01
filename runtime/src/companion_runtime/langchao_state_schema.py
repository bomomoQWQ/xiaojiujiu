"""PostgreSQL v14 persistence schema for isolated 「浪潮」 rounds and state."""

from __future__ import annotations

from typing import Final


LANGCHAO_STATE_SCHEMA_V14_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_rounds (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        round_id TEXT NOT NULL CHECK (btrim(round_id) <> ''),
        run_mode TEXT NOT NULL CHECK (run_mode IN ('live', 'shadow', 'replay')),
        status TEXT NOT NULL CHECK (status IN ('open', 'decided', 'deferred', 'aborted')),
        started_at TIMESTAMPTZ NOT NULL,
        ended_at TIMESTAMPTZ,
        event_cursor TEXT NOT NULL CHECK (btrim(event_cursor) <> ''),
        decision_candidate_id TEXT,
        decision_candidate_revision BIGINT CHECK (decision_candidate_revision > 0),
        decision_at TIMESTAMPTZ,
        defer_reason TEXT CHECK (defer_reason IS NULL OR btrim(defer_reason) <> ''),
        authority_revision BIGINT CHECK (authority_revision IS NULL OR authority_revision > 0),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, round_id),
        CONSTRAINT fk_langchao_round_decision_candidate FOREIGN KEY
            (scope_key, decision_candidate_id, decision_candidate_revision)
            REFERENCES langchao_candidate_revisions (scope_key, candidate_id, revision)
            ON DELETE RESTRICT,
        CONSTRAINT ck_langchao_round_terminal_time CHECK (
            (status = 'open' AND ended_at IS NULL)
            OR (status <> 'open' AND ended_at IS NOT NULL AND ended_at >= started_at)
        ),
        CONSTRAINT ck_langchao_round_outcome CHECK (
            (status = 'decided' AND decision_candidate_id IS NOT NULL
                AND decision_candidate_revision IS NOT NULL AND decision_at IS NOT NULL
                AND defer_reason IS NULL)
            OR (status = 'deferred' AND decision_candidate_id IS NULL
                AND decision_candidate_revision IS NULL AND decision_at IS NULL
                AND defer_reason IS NOT NULL)
            OR (status IN ('open', 'aborted') AND decision_candidate_id IS NULL
                AND decision_candidate_revision IS NULL AND decision_at IS NULL
                AND defer_reason IS NULL)
        ),
        CONSTRAINT ck_langchao_round_decision_time CHECK (
            decision_at IS NULL OR (decision_at >= started_at AND decision_at = ended_at)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_state_snapshots (
        scope_key TEXT NOT NULL,
        round_id TEXT NOT NULL,
        state_revision BIGINT NOT NULL CHECK (state_revision > 0),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        advanced_at TIMESTAMPTZ NOT NULL,
        event_cursor TEXT NOT NULL CHECK (btrim(event_cursor) <> ''),
        based_on_state_version BIGINT NOT NULL CHECK (based_on_state_version >= 0),
        goal_snapshot_version TEXT NOT NULL CHECK (btrim(goal_snapshot_version) <> ''),
        reward_snapshot_version TEXT NOT NULL CHECK (btrim(reward_snapshot_version) <> ''),
        candidate_snapshot_version TEXT NOT NULL CHECK (btrim(candidate_snapshot_version) <> ''),
        prediction_snapshot_version TEXT NOT NULL CHECK (btrim(prediction_snapshot_version) <> ''),
        value_profile_version TEXT NOT NULL CHECK (btrim(value_profile_version) <> ''),
        attention_version TEXT NOT NULL CHECK (btrim(attention_version) <> ''),
        parameter_version TEXT NOT NULL CHECK (btrim(parameter_version) <> ''),
        permission_version TEXT NOT NULL CHECK (btrim(permission_version) <> ''),
        state_version TEXT NOT NULL CHECK (btrim(state_version) <> ''),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, round_id, state_revision),
        CONSTRAINT uq_langchao_state_snapshot_hash UNIQUE
            (scope_key, round_id, state_revision, payload_sha256),
        CONSTRAINT fk_langchao_state_snapshot_round FOREIGN KEY (scope_key, round_id)
            REFERENCES langchao_rounds (scope_key, round_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_state_candidates (
        scope_key TEXT NOT NULL,
        round_id TEXT NOT NULL,
        state_revision BIGINT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        candidate_id TEXT NOT NULL,
        candidate_revision BIGINT NOT NULL CHECK (candidate_revision > 0),
        readiness DOUBLE PRECISION NOT NULL CHECK (
            readiness = readiness AND readiness <> 'Infinity'::double precision
            AND readiness <> '-Infinity'::double precision AND readiness >= 0.0 AND readiness <= 1.0
        ),
        attraction DOUBLE PRECISION NOT NULL CHECK (
            attraction = attraction AND attraction <> 'Infinity'::double precision
            AND attraction <> '-Infinity'::double precision
        ),
        PRIMARY KEY (scope_key, round_id, state_revision, candidate_id),
        CONSTRAINT uq_langchao_state_candidate_ordinal UNIQUE
            (scope_key, round_id, state_revision, ordinal),
        CONSTRAINT fk_langchao_state_candidate_snapshot FOREIGN KEY
            (scope_key, round_id, state_revision)
            REFERENCES langchao_state_snapshots (scope_key, round_id, state_revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_state_candidate_revision FOREIGN KEY
            (scope_key, candidate_id, candidate_revision)
            REFERENCES langchao_candidate_revisions (scope_key, candidate_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_active_state (
        scope_key TEXT PRIMARY KEY,
        round_id TEXT NOT NULL,
        state_revision BIGINT NOT NULL CHECK (state_revision > 0),
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_langchao_active_state_snapshot FOREIGN KEY
            (scope_key, round_id, state_revision)
            REFERENCES langchao_state_snapshots (scope_key, round_id, state_revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_integration_steps (
        scope_key TEXT NOT NULL,
        round_id TEXT NOT NULL,
        result_state_revision BIGINT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        started_at TIMESTAMPTZ NOT NULL,
        ended_at TIMESTAMPTZ NOT NULL,
        readiness_before JSONB NOT NULL CHECK (jsonb_typeof(readiness_before) = 'object'),
        readiness_after JSONB NOT NULL CHECK (jsonb_typeof(readiness_after) = 'object'),
        attraction JSONB NOT NULL CHECK (jsonb_typeof(attraction) = 'object'),
        first_crossing_candidates JSONB NOT NULL CHECK (jsonb_typeof(first_crossing_candidates) = 'array'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        step_version TEXT NOT NULL CHECK (btrim(step_version) <> ''),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, round_id, result_state_revision, ordinal),
        CONSTRAINT uq_langchao_integration_step_hash UNIQUE
            (scope_key, round_id, result_state_revision, ordinal, payload_sha256),
        CONSTRAINT fk_langchao_integration_step_state FOREIGN KEY
            (scope_key, round_id, result_state_revision)
            REFERENCES langchao_state_snapshots (scope_key, round_id, state_revision)
            ON DELETE RESTRICT,
        CONSTRAINT ck_langchao_integration_step_time CHECK (ended_at >= started_at)
    )
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_state_history_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '浪潮 state history rows are immutable' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_state_snapshots_immutable
    BEFORE UPDATE OR DELETE ON langchao_state_snapshots
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_state_history_mutation()
    """,
    """
    CREATE TRIGGER langchao_state_candidates_immutable
    BEFORE UPDATE OR DELETE ON langchao_state_candidates
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_state_history_mutation()
    """,
    """
    CREATE TRIGGER langchao_integration_steps_immutable
    BEFORE UPDATE OR DELETE ON langchao_integration_steps
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_state_history_mutation()
    """,
)


__all__ = ["LANGCHAO_STATE_SCHEMA_V14_STATEMENTS"]
