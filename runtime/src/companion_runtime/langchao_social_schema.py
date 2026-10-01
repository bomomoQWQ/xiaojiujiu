"""PostgreSQL schema foundation for isolated 「浪潮」 social memory v1.

This declaration is registered as user-model migration v17.  It remains
independent of Runtime/context wiring and preserves the social-memory boundary.
"""

from __future__ import annotations

from typing import Final

LANGCHAO_SOCIAL_SCHEMA_VERSION: Final[int] = 1

LANGCHAO_SOCIAL_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS langchao_social_sources (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        source_kind TEXT NOT NULL CHECK (source_kind IN (
            'event', 'memory', 'memory_revision', 'user_statement',
            'assistant_action', 'boundary', 'arrangement', 'runtime_fact', 'internal_state'
        )),
        source_id TEXT NOT NULL CHECK (btrim(source_id) <> ''),
        source_revision BIGINT NOT NULL CHECK (source_revision > 0),
        source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
        observed_at TIMESTAMPTZ NOT NULL,
        locator TEXT,
        source_status TEXT NOT NULL DEFAULT 'active'
            CHECK (source_status IN ('active', 'tombstoned', 'invalidated')),
        tombstoned_at TIMESTAMPTZ,
        invalidated_at TIMESTAMPTZ,
        invalidation_reason TEXT,
        contract_version TEXT NOT NULL CHECK (contract_version = 'langchao.social.v1'),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, source_kind, source_id, source_revision),
        CONSTRAINT uq_langchao_social_source_hash UNIQUE
            (scope_key, source_kind, source_id, source_revision, source_sha256),
        CONSTRAINT ck_langchao_social_source_lifecycle CHECK (
            (source_status = 'active' AND tombstoned_at IS NULL AND invalidated_at IS NULL
                AND invalidation_reason IS NULL)
            OR (source_status = 'tombstoned' AND tombstoned_at IS NOT NULL
                AND invalidated_at IS NULL AND invalidation_reason IS NOT NULL)
            OR (source_status = 'invalidated' AND invalidated_at IS NOT NULL
                AND invalidation_reason IS NOT NULL)
        ),
        CONSTRAINT ck_langchao_social_source_locator
            CHECK (locator IS NULL OR btrim(locator) <> '')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_build_runs (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        build_run_id TEXT NOT NULL CHECK (btrim(build_run_id) <> ''),
        revision BIGINT NOT NULL CHECK (revision > 0),
        builder_version TEXT NOT NULL CHECK (btrim(builder_version) <> ''),
        input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
        output_sha256 TEXT NOT NULL CHECK (output_sha256 ~ '^[0-9a-f]{64}$'),
        status TEXT NOT NULL CHECK (status IN ('completed', 'degraded', 'failed')),
        degradation_reason TEXT,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, build_run_id, revision),
        CONSTRAINT uq_langchao_social_build_run_input
            UNIQUE (scope_key, builder_version, input_sha256),
        CONSTRAINT ck_langchao_social_build_run_time CHECK (completed_at >= started_at),
        CONSTRAINT ck_langchao_social_build_run_degradation CHECK (
            (status = 'completed' AND degradation_reason IS NULL)
            OR (status <> 'completed' AND degradation_reason IS NOT NULL
                AND btrim(degradation_reason) <> '')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_proposals (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        proposal_id TEXT NOT NULL CHECK (btrim(proposal_id) <> ''),
        revision BIGINT NOT NULL CHECK (revision > 0),
        build_run_id TEXT NOT NULL,
        build_run_revision BIGINT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'proposed', 'accepted', 'rejected', 'superseded', 'invalidated'
        )),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, proposal_id, revision),
        CONSTRAINT uq_langchao_social_proposal_hash
            UNIQUE (scope_key, proposal_id, revision, payload_sha256),
        CONSTRAINT fk_langchao_social_proposal_build FOREIGN KEY
            (scope_key, build_run_id, build_run_revision)
            REFERENCES langchao_social_build_runs (scope_key, build_run_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_items (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        item_key TEXT NOT NULL CHECK (btrim(item_key) <> ''),
        revision BIGINT NOT NULL CHECK (revision > 0),
        proposal_id TEXT NOT NULL,
        proposal_revision BIGINT NOT NULL,
        item_type TEXT NOT NULL CHECK (item_type IN (
            'shared_matter', 'participation_fact', 'confirmed_arrangement',
            'boundary_reference', 'interpretive_basis', 'uncertainty',
            'counterevidence', 'goal_draft'
        )),
        role TEXT NOT NULL CHECK (role IN (
            'user', 'companion', 'shared', 'system', 'third_party', 'unknown'
        )),
        status TEXT NOT NULL CHECK (status IN (
            'proposed', 'active', 'superseded', 'tombstoned', 'invalidated'
        )),
        summary TEXT NOT NULL CHECK (btrim(summary) <> ''),
        attributes JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(attributes) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        supersedes_item_key TEXT,
        supersedes_revision BIGINT,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, item_key, revision),
        CONSTRAINT uq_langchao_social_item_hash
            UNIQUE (scope_key, item_key, revision, payload_sha256),
        CONSTRAINT fk_langchao_social_item_proposal FOREIGN KEY
            (scope_key, proposal_id, proposal_revision)
            REFERENCES langchao_social_proposals (scope_key, proposal_id, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_social_item_supersedes FOREIGN KEY
            (scope_key, supersedes_item_key, supersedes_revision)
            REFERENCES langchao_social_items (scope_key, item_key, revision)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT ck_langchao_social_item_supersedes_pair CHECK (
            (supersedes_item_key IS NULL) = (supersedes_revision IS NULL)
            AND (supersedes_item_key IS NULL OR supersedes_item_key <> item_key)
        ),
        -- Acceptance is performed by a separate authority/repository operation;
        -- model/builder output remains a draft and can never directly adopt a goal.
        CONSTRAINT ck_langchao_social_goal_draft_not_adopted CHECK (
            item_type <> 'goal_draft' OR status IN ('proposed', 'superseded', 'tombstoned', 'invalidated')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_links (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        link_key TEXT NOT NULL CHECK (btrim(link_key) <> ''),
        revision BIGINT NOT NULL CHECK (revision > 0),
        proposal_id TEXT NOT NULL,
        proposal_revision BIGINT NOT NULL,
        relation TEXT NOT NULL CHECK (relation IN (
            'supports', 'contradicts', 'supersedes', 'derived_from',
            'refers_to', 'bounds', 'concerns'
        )),
        from_item_key TEXT NOT NULL,
        from_item_revision BIGINT NOT NULL,
        to_item_key TEXT NOT NULL,
        to_item_revision BIGINT NOT NULL,
        attributes JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(attributes) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, link_key, revision),
        CONSTRAINT fk_langchao_social_link_proposal FOREIGN KEY
            (scope_key, proposal_id, proposal_revision)
            REFERENCES langchao_social_proposals (scope_key, proposal_id, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_social_link_from FOREIGN KEY
            (scope_key, from_item_key, from_item_revision)
            REFERENCES langchao_social_items (scope_key, item_key, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_social_link_to FOREIGN KEY
            (scope_key, to_item_key, to_item_revision)
            REFERENCES langchao_social_items (scope_key, item_key, revision)
            ON DELETE RESTRICT,
        CONSTRAINT ck_langchao_social_link_not_self CHECK (
            (from_item_key, from_item_revision) <> (to_item_key, to_item_revision)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_state_events (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        state_event_id TEXT NOT NULL CHECK (btrim(state_event_id) <> ''),
        revision BIGINT NOT NULL CHECK (revision > 0),
        event_type TEXT NOT NULL CHECK (event_type IN (
            'proposal_accepted', 'proposal_rejected', 'item_superseded',
            'source_tombstoned', 'source_invalidated', 'dependency_invalidated',
            'projection_advanced'
        )),
        target_kind TEXT NOT NULL CHECK (target_kind IN (
            'proposal', 'item', 'source', 'link', 'projection'
        )),
        target_key TEXT NOT NULL CHECK (btrim(target_key) <> ''),
        expected_revision BIGINT CHECK (expected_revision IS NULL OR expected_revision >= 0),
        result_revision BIGINT NOT NULL CHECK (result_revision > 0),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        occurred_at TIMESTAMPTZ NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, state_event_id, revision),
        CONSTRAINT uq_langchao_social_state_event_hash
            UNIQUE (scope_key, state_event_id, revision, payload_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_projection_state (
        scope_key TEXT PRIMARY KEY CHECK (btrim(scope_key) <> ''),
        pointer_version BIGINT NOT NULL DEFAULT 0 CHECK (pointer_version >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_projection_heads (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        projection_key TEXT NOT NULL CHECK (btrim(projection_key) <> ''),
        item_key TEXT NOT NULL,
        item_revision BIGINT NOT NULL,
        pointer_version BIGINT NOT NULL CHECK (pointer_version > 0),
        based_on_state_event_id TEXT NOT NULL,
        based_on_state_event_revision BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (scope_key, projection_key),
        CONSTRAINT fk_langchao_social_projection_item FOREIGN KEY
            (scope_key, item_key, item_revision)
            REFERENCES langchao_social_items (scope_key, item_key, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_social_projection_event FOREIGN KEY
            (scope_key, based_on_state_event_id, based_on_state_event_revision)
            REFERENCES langchao_social_state_events (scope_key, state_event_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_item_sources (
        scope_key TEXT NOT NULL,
        item_key TEXT NOT NULL,
        item_revision BIGINT NOT NULL,
        source_kind TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_revision BIGINT NOT NULL,
        source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        PRIMARY KEY (scope_key, item_key, item_revision, source_kind, source_id, source_revision),
        CONSTRAINT uq_langchao_social_item_source_ordinal
            UNIQUE (scope_key, item_key, item_revision, ordinal),
        CONSTRAINT fk_langchao_social_item_source_item FOREIGN KEY
            (scope_key, item_key, item_revision)
            REFERENCES langchao_social_items (scope_key, item_key, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_social_item_source_source FOREIGN KEY
            (scope_key, source_kind, source_id, source_revision, source_sha256)
            REFERENCES langchao_social_sources
                (scope_key, source_kind, source_id, source_revision, source_sha256)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS langchao_social_link_sources (
        scope_key TEXT NOT NULL,
        link_key TEXT NOT NULL,
        link_revision BIGINT NOT NULL,
        source_kind TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_revision BIGINT NOT NULL,
        source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        PRIMARY KEY (scope_key, link_key, link_revision, source_kind, source_id, source_revision),
        CONSTRAINT uq_langchao_social_link_source_ordinal
            UNIQUE (scope_key, link_key, link_revision, ordinal),
        CONSTRAINT fk_langchao_social_link_source_link FOREIGN KEY
            (scope_key, link_key, link_revision)
            REFERENCES langchao_social_links (scope_key, link_key, revision)
            ON DELETE RESTRICT,
        CONSTRAINT fk_langchao_social_link_source_source FOREIGN KEY
            (scope_key, source_kind, source_id, source_revision, source_sha256)
            REFERENCES langchao_social_sources
                (scope_key, source_kind, source_id, source_revision, source_sha256)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE OR REPLACE FUNCTION langchao_reject_social_history_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '浪潮 social history rows are append-only' USING ERRCODE = '55000';
    END
    $$
    """,
    """
    CREATE TRIGGER langchao_social_proposals_immutable
    BEFORE UPDATE OR DELETE ON langchao_social_proposals
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_social_history_mutation()
    """,
    """
    CREATE TRIGGER langchao_social_items_immutable
    BEFORE UPDATE OR DELETE ON langchao_social_items
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_social_history_mutation()
    """,
    """
    CREATE TRIGGER langchao_social_links_immutable
    BEFORE UPDATE OR DELETE ON langchao_social_links
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_social_history_mutation()
    """,
    """
    CREATE TRIGGER langchao_social_state_events_immutable
    BEFORE UPDATE OR DELETE ON langchao_social_state_events
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_social_history_mutation()
    """,
    """
    CREATE TRIGGER langchao_social_build_runs_immutable
    BEFORE UPDATE OR DELETE ON langchao_social_build_runs
    FOR EACH ROW EXECUTE FUNCTION langchao_reject_social_history_mutation()
    """,
)


def schema_statements() -> tuple[str, ...]:
    return tuple(LANGCHAO_SOCIAL_SCHEMA_STATEMENTS)


__all__ = [
    "LANGCHAO_SOCIAL_SCHEMA_STATEMENTS", "LANGCHAO_SOCIAL_SCHEMA_VERSION", "schema_statements"
]
