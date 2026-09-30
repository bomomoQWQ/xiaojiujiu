"""PostgreSQL-native schema for the legacy Runtime/API projection core.

The table and column names intentionally match :mod:`companion_runtime.db` so the
existing projections can run unchanged.  Types do not: PostgreSQL stores instants,
JSON, floating point values, counters and flags in their native representations.
"""

from __future__ import annotations

from typing import Final

CORE_SCHEMA_VERSION: Final[int] = 4

CORE_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runtime_state (
        runtime_id TEXT PRIMARY KEY, version BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        epoch_at TIMESTAMPTZ, last_tick_at TIMESTAMPTZ, last_user_message_at TIMESTAMPTZ,
        last_contact_at TIMESTAMPTZ, last_exchange_at TIMESTAMPTZ, cooldown_until TIMESTAMPTZ,
        foreground_pause_until TIMESTAMPTZ, contact_count_today BIGINT NOT NULL DEFAULT 0,
        contact_day DATE, allow_proactive BOOLEAN NOT NULL DEFAULT TRUE,
        mood_valence DOUBLE PRECISION NOT NULL DEFAULT 0, mood_arousal DOUBLE PRECISION NOT NULL DEFAULT 0,
        mood_stability DOUBLE PRECISION NOT NULL DEFAULT 0.7, approach_impulse DOUBLE PRECISION NOT NULL DEFAULT 0.05,
        restraint DOUBLE PRECISION NOT NULL DEFAULT 0.5, pressure DOUBLE PRECISION NOT NULL DEFAULT 0,
        values_json JSONB NOT NULL DEFAULT '{}'::jsonb, meta_json JSONB NOT NULL DEFAULT '{}'::jsonb
    )""",
    """CREATE TABLE IF NOT EXISTS raw_events (
        event_id TEXT PRIMARY KEY, seq BIGINT, event_type TEXT NOT NULL, timestamp TIMESTAMPTZ NOT NULL,
        actor TEXT NOT NULL, conversation_id TEXT, content TEXT,
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb, source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        runtime_version BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_raw_events_ts ON raw_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_raw_events_type ON raw_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_raw_events_conv ON raw_events(conversation_id, timestamp)",
    """CREATE TABLE IF NOT EXISTS event_semantics (
        event_id TEXT PRIMARY KEY, semantic_status TEXT NOT NULL DEFAULT 'unresolved', direction TEXT,
        intensity_band TEXT, confidence DOUBLE PRECISION, settlement_source TEXT, evidence TEXT,
        potential_relevance TEXT NOT NULL DEFAULT 'low', unresolved_reason TEXT, settled_at TIMESTAMPTZ,
        deep_refresh_id TEXT, version BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_event_semantics_status ON event_semantics(semantic_status, potential_relevance)",
    """CREATE TABLE IF NOT EXISTS interpretation_versions (
        interpretation_id TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
        interpretation_version BIGINT NOT NULL, supersedes_id TEXT, content TEXT NOT NULL,
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5, source_version BIGINT NOT NULL DEFAULT 0,
        source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb, created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_interp_target ON interpretation_versions(target_kind, target_id)",
    """CREATE TABLE IF NOT EXISTS reappraisals (
        reappraisal_id TEXT PRIMARY KEY, source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        previous_interpretation TEXT, new_interpretation TEXT NOT NULL, delta_summary TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS interaction_observations (
        observation_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, attempt_id TEXT,
        action_json JSONB NOT NULL DEFAULT '{}'::jsonb, context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        outcome_json JSONB NOT NULL DEFAULT '{}'::jsonb, source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        attribution_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        source_weight DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        semantic_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        weight DOUBLE PRECISION NOT NULL DEFAULT 0, applied BOOLEAN NOT NULL DEFAULT FALSE
    )""",
    """CREATE TABLE IF NOT EXISTS background_tasks (
        task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, priority TEXT NOT NULL,
        based_on_version BIGINT NOT NULL, source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        status TEXT NOT NULL DEFAULT 'in_flight', created_at TIMESTAMPTZ NOT NULL,
        settled_at TIMESTAMPTZ, outcome TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS outbox (
        outbox_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'pending', priority BIGINT NOT NULL DEFAULT 100,
        available_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL, lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ, attempts BIGINT NOT NULL DEFAULT 0,
        max_attempts BIGINT NOT NULL DEFAULT 3, acked_at TIMESTAMPTZ, last_error TEXT,
        conversation_id TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(status, available_at, priority)",
    """CREATE TABLE IF NOT EXISTS active_emotion_events (
        emotion_event_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL, direction TEXT NOT NULL,
        intensity DOUBLE PRECISION NOT NULL, activation DOUBLE PRECISION NOT NULL,
        target TEXT NOT NULL DEFAULT 'user', semantic_label TEXT, created_at TIMESTAMPTZ NOT NULL,
        decay_rate DOUBLE PRECISION NOT NULL DEFAULT 0.08, status TEXT NOT NULL DEFAULT 'active'
    )""",
    """CREATE TABLE IF NOT EXISTS emotion_explanations (
        explanation_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb, source TEXT NOT NULL DEFAULT 'template',
        created_at TIMESTAMPTZ NOT NULL, last_used_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_emotion_explanation_key ON emotion_explanations(cache_key)",
    """CREATE TABLE IF NOT EXISTS boundaries (
        boundary_id TEXT PRIMARY KEY, type TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'all_topics',
        allow_reply BOOLEAN NOT NULL DEFAULT TRUE, allow_proactive BOOLEAN NOT NULL DEFAULT FALSE,
        starts_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
        revocable_by TEXT NOT NULL DEFAULT 'explicit_user_revoke', source_event_id TEXT,
        revoked_at TIMESTAMPTZ, note TEXT, subject TEXT, created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS unfinished_matters (
        unfinished_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb, status TEXT NOT NULL,
        waiting_until TIMESTAMPTZ, priority DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        mute_until TIMESTAMPTZ, expire_at TIMESTAMPTZ,
        resolution_conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, resolution_note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_unfinished_status ON unfinished_matters(status, waiting_until)",
    """CREATE TABLE IF NOT EXISTS working_situation_items (
        item_id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5, salience DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        source_kind TEXT NOT NULL DEFAULT 'event', source_id TEXT,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ, status TEXT NOT NULL DEFAULT 'active'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wsi_status ON working_situation_items(status, salience)",
    """CREATE TABLE IF NOT EXISTS memory_candidates (
        candidate_id TEXT PRIMARY KEY, summary TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'episodic',
        source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb, value DOUBLE PRECISION NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL, consolidated_memory_id TEXT,
        topics_json JSONB NOT NULL DEFAULT '[]'::jsonb, confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        structured_json JSONB NOT NULL DEFAULT '{}'::jsonb
    )""",
    """CREATE TABLE IF NOT EXISTS memories (
        memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL,
        structured_json JSONB NOT NULL DEFAULT '{}'::jsonb, topics_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        importance DOUBLE PRECISION NOT NULL DEFAULT 0.5, confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        status TEXT NOT NULL DEFAULT 'active', source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, archived_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status, importance)",
    """CREATE TABLE IF NOT EXISTS activated_memories (
        memory_id TEXT PRIMARY KEY, activation DOUBLE PRECISION NOT NULL DEFAULT 0,
        last_recalled_at TIMESTAMPTZ, recall_count BIGINT NOT NULL DEFAULT 0,
        reason TEXT, updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS user_model_params (
        scope TEXT PRIMARY KEY, params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        precision_json JSONB NOT NULL DEFAULT '{}'::jsonb, observations BIGINT NOT NULL DEFAULT 0,
        effective_count DOUBLE PRECISION NOT NULL DEFAULT 0, last_updated_at TIMESTAMPTZ,
        last_summary_json JSONB
    )""",
    """CREATE TABLE IF NOT EXISTS candidate_intents (
        candidate_id TEXT PRIMARY KEY, type TEXT NOT NULL, intent TEXT NOT NULL,
        goal TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '',
        sources_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        constraints_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        preconditions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        invalidate_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5, status TEXT NOT NULL,
        internal_need DOUBLE PRECISION NOT NULL DEFAULT 0.5,
        unfinished_relevance DOUBLE PRECISION NOT NULL DEFAULT 0,
        emotion_relevance DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        wording_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, retired_reason TEXT,
        proposed_by TEXT NOT NULL DEFAULT 'rule'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate_intents(status, updated_at)",
    """CREATE TABLE IF NOT EXISTS action_attempts (
        attempt_id TEXT PRIMARY KEY, candidate_id TEXT, state TEXT NOT NULL, intent TEXT NOT NULL,
        goal TEXT NOT NULL DEFAULT '', based_on_version BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        committed_at TIMESTAMPTZ, rendered_text TEXT, failure_reason TEXT,
        reconcile_action TEXT, superseded_json JSONB NOT NULL DEFAULT '[]'::jsonb, outbox_id TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_attempt_state ON action_attempts(state, updated_at)",
    """CREATE TABLE IF NOT EXISTS attempt_events (
        attempt_event_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, from_state TEXT,
        to_state TEXT NOT NULL, reason TEXT, runtime_version BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_attempt_events ON attempt_events(attempt_id, created_at)",
    """CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT PRIMARY KEY, decided_at TIMESTAMPTZ NOT NULL,
        runtime_version BIGINT NOT NULL DEFAULT 0, conversation_id TEXT,
        trigger TEXT NOT NULL DEFAULT '', acted BOOLEAN NOT NULL, reason TEXT NOT NULL,
        chosen_candidate_id TEXT, hazard DOUBLE PRECISION NOT NULL DEFAULT 0,
        advantage DOUBLE PRECISION NOT NULL DEFAULT 0, silence_utility DOUBLE PRECISION NOT NULL DEFAULT 0,
        action_probability DOUBLE PRECISION NOT NULL DEFAULT 0, delta_t DOUBLE PRECISION NOT NULL DEFAULT 0,
        next_wake_at TIMESTAMPTZ, payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions(decided_at)",
    """CREATE TABLE IF NOT EXISTS state_samples (
        sample_id TEXT PRIMARY KEY, sampled_at TIMESTAMPTZ NOT NULL,
        runtime_version BIGINT NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
        mood_valence DOUBLE PRECISION NOT NULL DEFAULT 0, mood_arousal DOUBLE PRECISION NOT NULL DEFAULT 0,
        mood_stability DOUBLE PRECISION NOT NULL DEFAULT 0, approach_impulse DOUBLE PRECISION NOT NULL DEFAULT 0,
        restraint DOUBLE PRECISION NOT NULL DEFAULT 0, pressure DOUBLE PRECISION NOT NULL DEFAULT 0,
        allow_proactive BOOLEAN NOT NULL DEFAULT TRUE, payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_state_samples_at ON state_samples(sampled_at)",
    """CREATE TABLE IF NOT EXISTS refresh_runs (
        run_id TEXT PRIMARY KEY, ran_at TIMESTAMPTZ NOT NULL,
        runtime_version BIGINT NOT NULL DEFAULT 0, conversation_id TEXT,
        trigger TEXT NOT NULL DEFAULT '', ran BOOLEAN NOT NULL, reason TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '', degraded BOOLEAN NOT NULL,
        operations BIGINT NOT NULL DEFAULT 0, settled_events BIGINT NOT NULL DEFAULT 0,
        latency_ms BIGINT NOT NULL DEFAULT 0, payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_refresh_runs_at ON refresh_runs(ran_at)",
)

CORE_MIGRATIONS: Final[tuple[tuple[int, tuple[str, ...]], ...]] = (
    (CORE_SCHEMA_VERSION, CORE_SCHEMA_STATEMENTS),
)

__all__ = ["CORE_MIGRATIONS", "CORE_SCHEMA_STATEMENTS", "CORE_SCHEMA_VERSION"]
