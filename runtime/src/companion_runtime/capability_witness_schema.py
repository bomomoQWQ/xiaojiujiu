"""PostgreSQL v22 exact capability/task/artifact witness ledger."""

from __future__ import annotations

from typing import Final


CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS capability_artifact_witnesses (
        witness_id UUID PRIMARY KEY,
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        capability TEXT NOT NULL CHECK (btrim(capability) <> ''),
        operation TEXT NOT NULL CHECK (btrim(operation) <> ''),
        task_run_id TEXT NOT NULL CHECK (btrim(task_run_id) <> ''),
        task_status TEXT NOT NULL
            CHECK (task_status IN ('not_started', 'running', 'succeeded', 'failed', 'cancelled')),
        artifact_sha256 TEXT NOT NULL
            CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
        artifact_type TEXT NOT NULL CHECK (btrim(artifact_type) <> ''),
        artifact_status TEXT NOT NULL
            CHECK (artifact_status IN ('active', 'tombstoned', 'invalid')),
        created_at TIMESTAMPTZ NOT NULL,
        source_refs JSONB NOT NULL CHECK (
            jsonb_typeof(source_refs) = 'array' AND jsonb_array_length(source_refs) > 0
        ),
        tombstoned_at TIMESTAMPTZ,
        tombstone_reason TEXT,
        CONSTRAINT uq_capability_artifact_witness_exact
            UNIQUE (scope_key, task_run_id, artifact_sha256),
        CONSTRAINT ck_capability_artifact_witness_tombstone CHECK (
            (artifact_status = 'tombstoned' AND tombstoned_at IS NOT NULL
                AND btrim(tombstone_reason) <> '')
            OR (artifact_status <> 'tombstoned' AND tombstoned_at IS NULL
                AND tombstone_reason IS NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_capability_artifact_witness_lookup
    ON capability_artifact_witnesses
        (scope_key, task_run_id, artifact_sha256, task_status, artifact_status)
    """,
)


__all__ = ["CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS"]
