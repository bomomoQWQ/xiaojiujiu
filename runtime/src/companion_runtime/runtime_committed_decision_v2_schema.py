"""Deferred append-only schema fragment for Runtime-v2 committed decisions.

The workspace currently reserves user-model schema version 10 for the mechanical
application work.  This fragment is intentionally not inserted into ``MIGRATIONS`` yet:
once that migration lands, append these statements at the next free version (normally 11)
without renumbering either migration.
"""

from __future__ import annotations

from typing import Final

PREFERRED_COMMITTED_DECISION_SCHEMA_VERSION: Final[int] = 11

COMMITTED_DECISION_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS runtime_v2_committed_decisions (
        decision_id TEXT PRIMARY KEY CHECK (btrim(decision_id) <> ''),
        scope_key TEXT NOT NULL CHECK (btrim(scope_key) <> ''),
        snapshot_version INTEGER NOT NULL CHECK (snapshot_version > 0),
        candidate_snapshot JSONB NOT NULL
            CHECK (jsonb_typeof(candidate_snapshot) = 'object'),
        cold_start_exploration BOOLEAN NOT NULL DEFAULT FALSE,
        audit_snapshot JSONB NOT NULL CHECK (jsonb_typeof(audit_snapshot) = 'object'),
        audit_version TEXT NOT NULL CHECK (btrim(audit_version) <> ''),
        attempt_id TEXT NOT NULL CHECK (btrim(attempt_id) <> ''),
        render_outbox_id TEXT NOT NULL CHECK (btrim(render_outbox_id) <> ''),
        committed_at TIMESTAMPTZ NOT NULL,
        terminal_ack_id TEXT,
        terminal_status TEXT CHECK (terminal_status IN ('sent', 'failed')),
        terminal_acknowledged_at TIMESTAMPTZ,
        CHECK ((terminal_status IS NULL AND terminal_ack_id IS NULL
                AND terminal_acknowledged_at IS NULL)
            OR (terminal_status IS NOT NULL AND terminal_ack_id IS NOT NULL
                AND btrim(terminal_ack_id) <> ''
                AND terminal_acknowledged_at IS NOT NULL))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runtime_v2_committed_decisions_scope_pending
    ON runtime_v2_committed_decisions (scope_key, committed_at)
    WHERE terminal_status IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_v2_committed_decisions_attempt
    ON runtime_v2_committed_decisions (scope_key, attempt_id)
    """,
)

__all__ = [
    "COMMITTED_DECISION_SCHEMA_STATEMENTS",
    "PREFERRED_COMMITTED_DECISION_SCHEMA_VERSION",
]
