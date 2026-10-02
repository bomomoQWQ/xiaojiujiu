"""PostgreSQL v21 privacy deletion coordination contract.

The contract deliberately preserves accounting and provenance foreign keys.  Source rows
remain addressable, while plaintext is made unavailable through a tombstone/redaction
overlay or destruction of a per-source encryption key.  Work items are durable so a
coordinator can resume after a process crash.
"""

from __future__ import annotations

from typing import Final

PRIVACY_DELETION_SCHEMA_VERSION: Final[int] = 1
PRIVACY_DELETION_CONTRACT_VERSION: Final[str] = "privacy.deletion.v1"

PRIVACY_DELETION_SCHEMA_V21_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS privacy_deletion_requests_v1 (
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        request_id TEXT NOT NULL CHECK (btrim(request_id) <> ''),
        requested_by TEXT NOT NULL CHECK (requested_by IN ('user', 'authorized_operator')),
        selector_kind TEXT NOT NULL CHECK (selector_kind IN ('source', 'conversation', 'scope_exit')),
        selector_digest TEXT NOT NULL CHECK (selector_digest ~ '^[0-9a-f]{64}$'),
        selector_json JSONB NOT NULL CHECK (jsonb_typeof(selector_json) = 'object'),
        strategy TEXT NOT NULL CHECK (strategy IN ('tombstone', 'crypto_erasure', 'payload_redaction')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
        contract_version TEXT NOT NULL CHECK (contract_version = 'privacy.deletion.v1'),
        requested_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        last_error_code TEXT,
        PRIMARY KEY (scope_key, request_id),
        CONSTRAINT ck_privacy_deletion_completion CHECK (
            (status = 'completed' AND completed_at IS NOT NULL)
            OR (status <> 'completed' AND completed_at IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_deletion_work_v1 (
        scope_key TEXT NOT NULL,
        request_id TEXT NOT NULL,
        work_kind TEXT NOT NULL CHECK (work_kind IN (
            'source_protection', 'social_invalidation', 'goal_candidate_invalidation',
            'memory_interpretation_invalidation', 'live_outbox_stop',
            'learning_artifact_invalidation', 'outcome_evidence_minimization'
        )),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        result_counts JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result_counts) = 'object'),
        last_error_code TEXT,
        PRIMARY KEY (scope_key, request_id, work_kind),
        CONSTRAINT fk_privacy_deletion_work_request FOREIGN KEY (scope_key, request_id)
            REFERENCES privacy_deletion_requests_v1 (scope_key, request_id) ON DELETE RESTRICT,
        CONSTRAINT ck_privacy_deletion_work_lease CHECK (
            (status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR (status <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_source_tombstones_v1 (
        scope_key TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK (btrim(source_kind) <> ''),
        source_id_digest TEXT NOT NULL CHECK (source_id_digest ~ '^[0-9a-f]{64}$'),
        source_revision BIGINT NOT NULL CHECK (source_revision >= 0),
        strategy TEXT NOT NULL CHECK (strategy IN ('tombstone', 'crypto_erasure', 'payload_redaction')),
        key_destroyed BOOLEAN NOT NULL DEFAULT FALSE,
        request_id TEXT NOT NULL,
        protected_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (scope_key, source_kind, source_id_digest, source_revision),
        CONSTRAINT fk_privacy_source_tombstone_request FOREIGN KEY (scope_key, request_id)
            REFERENCES privacy_deletion_requests_v1 (scope_key, request_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_reference_invalidations_v1 (
        scope_key TEXT NOT NULL,
        request_id TEXT NOT NULL,
        artifact_kind TEXT NOT NULL CHECK (btrim(artifact_kind) <> ''),
        artifact_id_digest TEXT NOT NULL CHECK (artifact_id_digest ~ '^[0-9a-f]{64}$'),
        reason_code TEXT NOT NULL CHECK (reason_code = 'privacy_deletion'),
        invalidated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (scope_key, request_id, artifact_kind, artifact_id_digest),
        CONSTRAINT fk_privacy_reference_request FOREIGN KEY (scope_key, request_id)
            REFERENCES privacy_deletion_requests_v1 (scope_key, request_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_deletion_audit_v1 (
        scope_key TEXT NOT NULL,
        request_id TEXT NOT NULL,
        event_seq BIGINT NOT NULL CHECK (event_seq > 0),
        event_type TEXT NOT NULL CHECK (event_type IN (
            'requested', 'work_completed', 'work_failed', 'completed'
        )),
        work_kind TEXT,
        occurred_at TIMESTAMPTZ NOT NULL,
        counts JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(counts) = 'object'),
        error_code TEXT,
        PRIMARY KEY (scope_key, request_id, event_seq),
        CONSTRAINT fk_privacy_deletion_audit_request FOREIGN KEY (scope_key, request_id)
            REFERENCES privacy_deletion_requests_v1 (scope_key, request_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_privacy_deletion_work_claim
    ON privacy_deletion_work_v1 (status, lease_expires_at, ordinal)
    """,
)

__all__ = [
    "PRIVACY_DELETION_CONTRACT_VERSION",
    "PRIVACY_DELETION_SCHEMA_VERSION",
    "PRIVACY_DELETION_SCHEMA_V21_STATEMENTS",
]
