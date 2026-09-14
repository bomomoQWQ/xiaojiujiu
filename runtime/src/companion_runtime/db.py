"""SQLite storage layer: schema, migrations and low-level helpers.

The database holds two kinds of data, kept conceptually separate:

* **append-only history** - ``raw_events``, ``interpretation_versions``,
  ``interaction_observations``, ``attempt_events``, ``reappraisals``,
  ``background_tasks``, ``outbox``. Rows are inserted, never rewritten
  (except for delivery bookkeeping such as leases).
* **current projection** - ``runtime_state``, ``active_emotion_events``,
  ``boundaries``, ``unfinished_matters``, ``candidate_intents``,
  ``action_attempts``, ``activated_memories``, ``user_model_*``,
  ``working_situation_items``. In principle rebuildable from history.

Only :mod:`companion_runtime.storage` writes here, and inside it only the
reducer is allowed to touch the projection tables.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from .utility import ensure_aware, isoformat, parse_datetime

LOGGER = logging.getLogger("companion_runtime.db")

SCHEMA_VERSION = 2

SCHEMA_STATEMENTS: tuple[str, ...] = (
    # ------------------------------------------------------------------ version
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ------------------------------------------------------- current projection
    """
    CREATE TABLE IF NOT EXISTS runtime_state (
        runtime_id            TEXT PRIMARY KEY,
        version               INTEGER NOT NULL,
        updated_at            TEXT NOT NULL,
        epoch_at              TEXT,
        last_tick_at          TEXT,
        last_user_message_at  TEXT,
        last_contact_at       TEXT,
        last_exchange_at      TEXT,
        cooldown_until        TEXT,
        foreground_pause_until TEXT,
        contact_count_today   INTEGER NOT NULL DEFAULT 0,
        contact_day           TEXT,
        allow_proactive       INTEGER NOT NULL DEFAULT 1,
        mood_valence          REAL NOT NULL DEFAULT 0,
        mood_arousal          REAL NOT NULL DEFAULT 0,
        mood_stability        REAL NOT NULL DEFAULT 0.7,
        approach_impulse      REAL NOT NULL DEFAULT 0.05,
        restraint             REAL NOT NULL DEFAULT 0.5,
        pressure              REAL NOT NULL DEFAULT 0,
        values_json           TEXT NOT NULL DEFAULT '{}',
        meta_json             TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # ------------------------------------------------------- immutable history
    """
    CREATE TABLE IF NOT EXISTS raw_events (
        event_id          TEXT PRIMARY KEY,
        seq               INTEGER,
        event_type        TEXT NOT NULL,
        timestamp         TEXT NOT NULL,
        actor             TEXT NOT NULL,
        conversation_id   TEXT,
        content           TEXT,
        metadata_json     TEXT NOT NULL DEFAULT '{}',
        source_event_ids  TEXT NOT NULL DEFAULT '[]',
        runtime_version   INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_events_ts ON raw_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_raw_events_type ON raw_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_raw_events_conv ON raw_events(conversation_id, timestamp)",
    # --------------------------------------------------- semantic settlement
    # Architecture patch v0.2 separates the acting layer from persistent
    # cognition, which means an event no longer has to be interpreted the moment
    # it arrives. This table is the *derived* reading of a raw event: it is
    # rebuildable, and a missing row means "not looked at yet". ``raw_events``
    # stays untouched, so an unresolved event is never lost.
    """
    CREATE TABLE IF NOT EXISTS event_semantics (
        event_id            TEXT PRIMARY KEY,
        semantic_status     TEXT NOT NULL DEFAULT 'unresolved',
        direction           TEXT,
        intensity_band      TEXT,
        confidence          REAL,
        settlement_source   TEXT,
        evidence            TEXT,
        potential_relevance TEXT NOT NULL DEFAULT 'low',
        unresolved_reason   TEXT,
        settled_at          TEXT,
        deep_refresh_id     TEXT,
        version             INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_event_semantics_status "
    "ON event_semantics(semantic_status, potential_relevance)",
    """
    CREATE TABLE IF NOT EXISTS interpretation_versions (
        interpretation_id TEXT PRIMARY KEY,
        target_kind       TEXT NOT NULL,
        target_id         TEXT NOT NULL,
        interpretation_version INTEGER NOT NULL,
        supersedes_id     TEXT,
        content           TEXT NOT NULL,
        confidence        REAL NOT NULL DEFAULT 0.5,
        source_version    INTEGER NOT NULL DEFAULT 0,
        source_event_ids  TEXT NOT NULL DEFAULT '[]',
        created_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_interp_target ON interpretation_versions(target_kind, target_id)",
    """
    CREATE TABLE IF NOT EXISTS reappraisals (
        reappraisal_id    TEXT PRIMARY KEY,
        source_event_ids  TEXT NOT NULL DEFAULT '[]',
        previous_interpretation TEXT,
        new_interpretation      TEXT NOT NULL,
        delta_summary     TEXT,
        created_at        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS interaction_observations (
        observation_id  TEXT PRIMARY KEY,
        created_at      TEXT NOT NULL,
        attempt_id      TEXT,
        action_json     TEXT NOT NULL DEFAULT '{}',
        context_json    TEXT NOT NULL DEFAULT '{}',
        outcome_json    TEXT NOT NULL DEFAULT '{}',
        source_event_ids TEXT NOT NULL DEFAULT '[]',
        attribution_confidence REAL NOT NULL DEFAULT 0.5,
        source_weight   REAL NOT NULL DEFAULT 0.5,
        semantic_confidence REAL NOT NULL DEFAULT 0.5,
        weight          REAL NOT NULL DEFAULT 0,
        applied         INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS background_tasks (
        task_id           TEXT PRIMARY KEY,
        task_type         TEXT NOT NULL,
        priority          TEXT NOT NULL,
        based_on_version  INTEGER NOT NULL,
        source_event_ids  TEXT NOT NULL DEFAULT '[]',
        status            TEXT NOT NULL DEFAULT 'in_flight',
        created_at        TEXT NOT NULL,
        settled_at        TEXT,
        outcome           TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox (
        outbox_id         TEXT PRIMARY KEY,
        kind              TEXT NOT NULL,
        payload_json      TEXT NOT NULL DEFAULT '{}',
        status            TEXT NOT NULL DEFAULT 'pending',
        priority          INTEGER NOT NULL DEFAULT 100,
        available_at      TEXT,
        created_at        TEXT NOT NULL,
        lease_owner       TEXT,
        lease_expires_at  TEXT,
        attempts          INTEGER NOT NULL DEFAULT 0,
        max_attempts      INTEGER NOT NULL DEFAULT 3,
        acked_at          TEXT,
        last_error        TEXT,
        conversation_id   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(status, available_at, priority)",
    # ----------------------------------------------------------- projections
    """
    CREATE TABLE IF NOT EXISTS active_emotion_events (
        emotion_event_id  TEXT PRIMARY KEY,
        source_event_id   TEXT NOT NULL,
        direction         TEXT NOT NULL,
        intensity         REAL NOT NULL,
        activation        REAL NOT NULL,
        target            TEXT NOT NULL DEFAULT 'user',
        semantic_label    TEXT,
        created_at        TEXT NOT NULL,
        decay_rate        REAL NOT NULL DEFAULT 0.08,
        status            TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS emotion_explanations (
        explanation_id    TEXT PRIMARY KEY,
        cache_key         TEXT NOT NULL,
        payload_json      TEXT NOT NULL DEFAULT '{}',
        source            TEXT NOT NULL DEFAULT 'template',
        created_at        TEXT NOT NULL,
        last_used_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_emotion_explanation_key ON emotion_explanations(cache_key)",
    """
    CREATE TABLE IF NOT EXISTS boundaries (
        boundary_id       TEXT PRIMARY KEY,
        type              TEXT NOT NULL,
        scope             TEXT NOT NULL DEFAULT 'all_topics',
        allow_reply       INTEGER NOT NULL DEFAULT 1,
        allow_proactive   INTEGER NOT NULL DEFAULT 0,
        starts_at         TEXT,
        expires_at        TEXT,
        revocable_by      TEXT NOT NULL DEFAULT 'explicit_user_revoke',
        source_event_id   TEXT,
        revoked_at        TEXT,
        note              TEXT,
        created_at        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unfinished_matters (
        unfinished_id     TEXT PRIMARY KEY,
        title             TEXT NOT NULL,
        source_event_ids  TEXT NOT NULL DEFAULT '[]',
        status            TEXT NOT NULL,
        waiting_until     TEXT,
        priority          REAL NOT NULL DEFAULT 0.5,
        mute_until        TEXT,
        expire_at         TEXT,
        resolution_conditions TEXT NOT NULL DEFAULT '[]',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        resolution_note   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_unfinished_status ON unfinished_matters(status, waiting_until)",
    """
    CREATE TABLE IF NOT EXISTS working_situation_items (
        item_id           TEXT PRIMARY KEY,
        kind              TEXT NOT NULL,
        content           TEXT NOT NULL,
        confidence        REAL NOT NULL DEFAULT 0.5,
        salience          REAL NOT NULL DEFAULT 0.5,
        source_kind       TEXT NOT NULL DEFAULT 'event',
        source_id         TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        expires_at        TEXT,
        status            TEXT NOT NULL DEFAULT 'active'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wsi_status ON working_situation_items(status, salience)",
    """
    CREATE TABLE IF NOT EXISTS memory_candidates (
        candidate_id      TEXT PRIMARY KEY,
        summary           TEXT NOT NULL,
        kind              TEXT NOT NULL DEFAULT 'episodic',
        source_event_ids  TEXT NOT NULL DEFAULT '[]',
        value             REAL NOT NULL DEFAULT 0,
        status            TEXT NOT NULL DEFAULT 'pending',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        consolidated_memory_id TEXT,
        topics_json       TEXT NOT NULL DEFAULT '[]',
        confidence        REAL NOT NULL DEFAULT 0.5
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        memory_id         TEXT PRIMARY KEY,
        kind              TEXT NOT NULL,
        summary           TEXT NOT NULL,
        structured_json   TEXT NOT NULL DEFAULT '{}',
        topics_json       TEXT NOT NULL DEFAULT '[]',
        importance        REAL NOT NULL DEFAULT 0.5,
        confidence        REAL NOT NULL DEFAULT 0.5,
        status            TEXT NOT NULL DEFAULT 'active',
        source_event_ids  TEXT NOT NULL DEFAULT '[]',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        archived_at       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status, importance)",
    """
    CREATE TABLE IF NOT EXISTS activated_memories (
        memory_id         TEXT PRIMARY KEY,
        activation        REAL NOT NULL DEFAULT 0,
        last_recalled_at  TEXT,
        recall_count      INTEGER NOT NULL DEFAULT 0,
        reason            TEXT,
        updated_at        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_model_params (
        scope             TEXT PRIMARY KEY,
        params_json       TEXT NOT NULL DEFAULT '{}',
        precision_json    TEXT NOT NULL DEFAULT '{}',
        observations      INTEGER NOT NULL DEFAULT 0,
        effective_count   REAL NOT NULL DEFAULT 0,
        last_updated_at   TEXT,
        last_summary_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_intents (
        candidate_id      TEXT PRIMARY KEY,
        type              TEXT NOT NULL,
        intent            TEXT NOT NULL,
        goal              TEXT NOT NULL DEFAULT '',
        target            TEXT NOT NULL DEFAULT '',
        sources_json      TEXT NOT NULL DEFAULT '[]',
        constraints_json  TEXT NOT NULL DEFAULT '[]',
        preconditions_json TEXT NOT NULL DEFAULT '[]',
        invalidate_json   TEXT NOT NULL DEFAULT '[]',
        confidence        REAL NOT NULL DEFAULT 0.5,
        status            TEXT NOT NULL,
        internal_need     REAL NOT NULL DEFAULT 0.5,
        unfinished_relevance REAL NOT NULL DEFAULT 0,
        emotion_relevance REAL NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        expires_at        TEXT,
        retired_reason    TEXT,
        proposed_by       TEXT NOT NULL DEFAULT 'rule'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate_intents(status, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS action_attempts (
        attempt_id        TEXT PRIMARY KEY,
        candidate_id      TEXT,
        state             TEXT NOT NULL,
        intent            TEXT NOT NULL,
        goal              TEXT NOT NULL DEFAULT '',
        based_on_version  INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        committed_at      TEXT,
        rendered_text     TEXT,
        failure_reason    TEXT,
        reconcile_action  TEXT,
        superseded_json   TEXT NOT NULL DEFAULT '[]',
        outbox_id         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempt_state ON action_attempts(state, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS attempt_events (
        attempt_event_id  TEXT PRIMARY KEY,
        attempt_id        TEXT NOT NULL,
        from_state        TEXT,
        to_state          TEXT NOT NULL,
        reason            TEXT,
        runtime_version   INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempt_events ON attempt_events(attempt_id, created_at)",
)

#: JSON columns that hold arrays; every other JSON column holds an object.
JSON_LIST_COLUMNS: frozenset[str] = frozenset(
    {
        "source_event_ids",
        "topics_json",
        "sources_json",
        "constraints_json",
        "preconditions_json",
        "invalidate_json",
        "resolution_conditions",
        "superseded_json",
    }
)

JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "raw_events": ("metadata_json", "source_event_ids"),
    "memories": ("structured_json", "topics_json", "source_event_ids"),
    "memory_candidates": ("source_event_ids", "topics_json"),
    "candidate_intents": (
        "sources_json",
        "constraints_json",
        "preconditions_json",
        "invalidate_json",
    ),
    "unfinished_matters": ("source_event_ids", "resolution_conditions"),
    "interaction_observations": ("action_json", "context_json", "outcome_json", "source_event_ids"),
    "runtime_state": ("values_json", "meta_json"),
    "outbox": ("payload_json",),
    "user_model_params": ("params_json", "precision_json", "last_summary_json"),
    "action_attempts": ("superseded_json",),
    "emotion_explanations": ("payload_json",),
    "background_tasks": ("source_event_ids",),
    "interpretation_versions": ("source_event_ids",),
    "reappraisals": ("source_event_ids",),
}


def dumps(value: Any) -> str:
    """Serialise a value to compact JSON for a TEXT column."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Any, default: Any = None) -> Any:
    """Deserialise a TEXT column, tolerating ``None`` and malformed values."""
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    """A thin, thread-safe SQLite wrapper with WAL and explicit transactions.

    The Runtime is a single-writer system, but FastAPI may serve requests from
    several threads, so access is serialised through a re-entrant lock while
    SQLite itself runs in autocommit-explicit transaction mode.
    """

    def __init__(self, path: str | Path, busy_timeout_ms: int = 5000, wal: bool = True) -> None:
        """Open (and create) the database at ``path``.

        Args:
            path: Filesystem path, or ``":memory:"`` for an ephemeral database.
            busy_timeout_ms: SQLite busy timeout.
            wal: Enable write-ahead logging for file-backed databases.
        """
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if wal and self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._depth = 0

    # ------------------------------------------------------------------ context

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a block inside a transaction, nesting via savepoints.

        Args:
            immediate: Acquire a write lock up front (``BEGIN IMMEDIATE``),
                which is what makes outbox claiming race-free.

        Yields:
            The underlying connection.
        """
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                self._conn.execute(f"SAVEPOINT sp_{self._depth}")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO sp_{self._depth}")
                    self._conn.execute(f"RELEASE sp_{self._depth}")
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(f"RELEASE sp_{self._depth}")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Run read-only statements under the connection lock."""
        with self._lock:
            yield self._conn

    # ------------------------------------------------------------------- access

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Execute a statement and return the cursor."""
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        """Execute a query and materialise all rows."""
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        """Execute a query and return the first row, or ``None``."""
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------- migration

    #: Columns added after the first released schema. Each entry is applied with
    #: ``ALTER TABLE`` when missing, so an existing database upgrades in place.
    ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("runtime_state", "epoch_at", "TEXT"),
        ("runtime_state", "last_exchange_at", "TEXT"),
    )

    def migrate(self) -> int:
        """Create every table, apply column additions and record the version.

        Returns:
            The current :data:`SCHEMA_VERSION`.
        """
        with self.transaction() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            for table, column, column_type in self.ADDED_COLUMNS:
                existing = {
                    row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in existing:
                    LOGGER.info("Adding column %s.%s", table, column)
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            conn.execute(
                "INSERT INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(SCHEMA_VERSION), isoformat(datetime.now().astimezone())),
            )
        return SCHEMA_VERSION


def row_to_dict(row: sqlite3.Row | None, table: str | None = None) -> dict[str, Any] | None:
    """Convert a SQLite row into a plain dict with JSON columns decoded.

    Args:
        row: Row to convert.
        table: Table name used to look up :data:`JSON_COLUMNS`.

    Returns:
        A dictionary, or ``None`` when ``row`` is ``None``.
    """
    if row is None:
        return None
    data = dict(row)
    for column in JSON_COLUMNS.get(table or "", ()):
        if column in data:
            default: Any = [] if column in JSON_LIST_COLUMNS else {}
            data[column] = loads(data[column], default)
    return data


def row_timestamp(data: dict[str, Any], key: str, default: datetime | None = None) -> datetime | None:
    """Read an ISO text column as an aware datetime."""
    return parse_datetime(data.get(key)) or default


def row_bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    """Read an INTEGER column as a bool."""
    value = data.get(key, None)
    if value is None:
        return default
    return bool(value)


def row_time_columns(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Normalise the named columns of ``data`` from text to aware datetimes."""
    out = dict(data)
    for key in keys:
        if key in out:
            out[key] = ensure_aware(out[key]) if not isinstance(out[key], datetime) else out[key]
    return out
