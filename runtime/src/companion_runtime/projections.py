"""Read/write access to the *current projection* tables.

Every write helper here is called exclusively by the reducer
(:mod:`companion_runtime.reducer`), which is the single writer of Runtime state.
Read helpers are safe for any module or HTTP handler.

Time columns are stored as ISO-8601 text in UTC so that lexical ordering equals
chronological ordering, which keeps the SQL simple and portable.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Sequence

from .db import Database, dumps, row_to_dict
from .typing import (
    ActionAttempt,
    ActivatedMemory,
    AttemptState,
    Boundary,
    CandidateIntent,
    CandidateStatus,
    EmotionEvent,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    OutboxItem,
    OutboxStatus,
    RawEvent,
    RuntimeState,
    UnfinishedMatter,
    UnfinishedStatus,
    ValueProfile,
    new_id,
)
from .utility import ensure_aware, isoformat, parse_datetime, utcnow
LOGGER = logging.getLogger("companion_runtime.projections")

#: Writable columns of ``runtime_state``, in INSERT order. ``runtime_id`` is the
#: primary key and is prepended by :meth:`RuntimeProjection.write`.
RUNTIME_STATE_COLUMNS = (
    "runtime_id",
    "version",
    "updated_at",
    "epoch_at",
    "last_tick_at",
    "last_user_message_at",
    "last_contact_at",
    "last_exchange_at",
    "cooldown_until",
    "foreground_pause_until",
    "contact_count_today",
    "contact_day",
    "allow_proactive",
    "mood_valence",
    "mood_arousal",
    "mood_stability",
    "approach_impulse",
    "restraint",
    "pressure",
    "values_json",
    "meta_json",
)


class VersionConflict(RuntimeError):
    """Raised when an optimistic-concurrency check fails."""

    def __init__(self, expected: int, actual: int) -> None:
        """Record the expected and observed versions."""
        super().__init__(f"runtime_state version conflict: expected {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


# --------------------------------------------------------------------------------------
# runtime_state
# --------------------------------------------------------------------------------------


class RuntimeProjection:
    """The single-row ``runtime_state`` projection."""

    def __init__(self, db: Database, runtime_id: str = "companion") -> None:
        """Bind the projection to a database and runtime identifier."""
        self._db = db
        self.runtime_id = runtime_id

    def ensure(self, now: datetime | None = None) -> RuntimeState:
        """Create the runtime row if missing and return the current state.

        Args:
            now: Creation timestamp for a brand-new row. Callers that drive a
                simulated or replayed clock must pass their own reference time so
                the creation epoch does not jump to wall-clock time.

        Returns:
            The current runtime state.
        """
        row = self._db.query_one(
            "SELECT * FROM runtime_state WHERE runtime_id = ?", (self.runtime_id,)
        )
        if row is None:
            # The row is created with a NULL epoch: the epoch is set by the first
            # tick, using that caller's clock. Seeding it with wall-clock time
            # here would break any simulated or replayed timeline.
            wall_clock = isoformat(utcnow())
            epoch = isoformat(ensure_aware(now)) if now is not None else None
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_state("
                    "runtime_id, version, updated_at, epoch_at, last_tick_at, allow_proactive, values_json"
                    ") VALUES(?, 0, ?, ?, ?, 1, ?)",
                    (
                        self.runtime_id,
                        wall_clock,
                        epoch,
                        epoch,
                        dumps(ValueProfile().to_dict()),
                    ),
                )
        return self.read()

    def read(self) -> RuntimeState:
        """Return the current runtime state (defaults if the row is absent)."""
        row = self._db.query_one(
            "SELECT * FROM runtime_state WHERE runtime_id = ?", (self.runtime_id,)
        )
        data = row_to_dict(row, "runtime_state")
        if data is None:
            now = utcnow()
            return RuntimeState(updated_at=now, epoch_at=now, last_tick_at=now)
        return RuntimeState(
            version=int(data.get("version") or 0),
            updated_at=parse_datetime(data.get("updated_at")),
            epoch_at=parse_datetime(data.get("epoch_at")) or parse_datetime(data.get("updated_at")),
            last_tick_at=parse_datetime(data.get("last_tick_at")),
            mood_valence=float(data.get("mood_valence") or 0.0),
            mood_arousal=float(data.get("mood_arousal") or 0.0),
            mood_stability=float(data.get("mood_stability") or 0.7),
            approach_impulse=float(data.get("approach_impulse") or 0.0),
            restraint=float(data.get("restraint") or 0.5),
            pressure=float(data.get("pressure") or 0.0),
            cooldown_until=parse_datetime(data.get("cooldown_until")),
            contact_count_today=int(data.get("contact_count_today") or 0),
            last_contact_at=parse_datetime(data.get("last_contact_at")),
            last_user_message_at=parse_datetime(data.get("last_user_message_at")),
            last_exchange_at=parse_datetime(data.get("last_exchange_at")),
            allow_proactive=bool(data.get("allow_proactive", 1)),
            foreground_pause_until=parse_datetime(data.get("foreground_pause_until")),
            values=ValueProfile.from_mapping(data.get("values_json") or {}),
            meta=dict(data.get("meta_json") or {}),
        )

    def write(
        self,
        state: RuntimeState,
        connection: sqlite3.Connection,
        *,
        expect_version: int | None = None,
    ) -> int:
        """Persist ``state`` and return the new version.

        Args:
            state: State to persist; ``version`` is incremented here.
            connection: Connection of the enclosing reducer transaction.
            expect_version: When given, the stored version must match it.

        Returns:
            The new runtime version.

        Raises:
            VersionConflict: If ``expect_version`` does not match the stored version.
        """
        current = connection.execute(
            "SELECT version FROM runtime_state WHERE runtime_id = ?", (self.runtime_id,)
        ).fetchone()
        stored_version = int(current["version"]) if current else 0
        if expect_version is not None and stored_version != expect_version:
            raise VersionConflict(expect_version, stored_version)

        new_version = max(stored_version, int(state.version)) + 1
        state.version = new_version
        state.updated_at = utcnow()
        state.epoch_at = state.epoch_at or state.updated_at
        payload = (
            self.runtime_id,
            new_version,
            isoformat(state.updated_at),
            isoformat(state.epoch_at),
            isoformat(state.last_tick_at),
            isoformat(state.last_user_message_at),
            isoformat(state.last_contact_at),
            isoformat(state.last_exchange_at),
            isoformat(state.cooldown_until),
            isoformat(state.foreground_pause_until),
            int(state.contact_count_today),
            state.meta.get("contact_day"),
            int(bool(state.allow_proactive)),
            float(state.mood_valence),
            float(state.mood_arousal),
            float(state.mood_stability),
            float(state.approach_impulse),
            float(state.restraint),
            float(state.pressure),
            dumps(state.values.to_dict()),
            dumps(state.meta),
        )
        connection.execute(
            "INSERT INTO runtime_state(" + ", ".join(RUNTIME_STATE_COLUMNS) + ") "
            "VALUES(" + ",".join("?" for _ in RUNTIME_STATE_COLUMNS) + ") "
            "ON CONFLICT(runtime_id) DO UPDATE SET "
            + ", ".join(f"{col}=excluded.{col}" for col in RUNTIME_STATE_COLUMNS if col != "version")
            + ", version=excluded.version",
            payload,
        )
        return new_version

    def bump_version(self, reason: str, connection: sqlite3.Connection) -> int:
        """Increment the version without changing any other field.

        Args:
            reason: Recorded in ``meta['last_bump_reason']`` for auditing.
            connection: Connection of the enclosing transaction.

        Returns:
            The new version.
        """
        state = self.read()
        state.meta = dict(state.meta) | {"last_bump_reason": reason}
        return self.write(state, connection)


# --------------------------------------------------------------------------------------
# working situation
# --------------------------------------------------------------------------------------


class SituationProjection:
    """Bounded working-set items projected from raw events."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def upsert(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        content: str,
        salience: float = 0.5,
        confidence: float = 0.5,
        source_kind: str = "event",
        source_id: str | None = None,
        expires_at: datetime | None = None,
        item_id: str | None = None,
    ) -> str:
        """Insert or replace one working-situation item.

        When ``item_id`` is omitted the item is keyed by its content so that
        repeated projections of the same fact refresh a single row instead of
        piling up duplicates in the bounded working set.

        Returns:
            The item identifier.
        """
        identifier = item_id
        if identifier is None:
            existing = self._db.query_one(
                "SELECT item_id FROM working_situation_items WHERE content = ? AND kind = ? "
                "AND status = 'active' LIMIT 1",
                (content, kind),
            )
            identifier = str(existing["item_id"]) if existing is not None else new_id("memory")
        now = isoformat(utcnow())
        connection.execute(
            "INSERT INTO working_situation_items(item_id, kind, content, confidence, salience, "
            "source_kind, source_id, created_at, updated_at, expires_at, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active') "
            "ON CONFLICT(item_id) DO UPDATE SET content=excluded.content, salience=excluded.salience, "
            "confidence=excluded.confidence, updated_at=excluded.updated_at, expires_at=excluded.expires_at, "
            "status='active'",
            (
                identifier,
                kind,
                content,
                float(confidence),
                float(salience),
                source_kind,
                source_id,
                now,
                now,
                isoformat(expires_at),
            ),
        )
        return identifier

    def list_active(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return active working-situation items ordered by salience."""
        rows = self._db.query(
            "SELECT * FROM working_situation_items WHERE status = 'active' "
            "ORDER BY salience DESC, updated_at DESC LIMIT ?",
            (int(limit),),
        )
        return [row_to_dict(row, "working_situation_items") or {} for row in rows]

    def expire(self, connection: sqlite3.Connection, now: datetime) -> int:
        """Mark expired items and prune the tail of the working set.

        Returns:
            Number of items expired.
        """
        cursor = connection.execute(
            "UPDATE working_situation_items SET status = 'expired' "
            "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (isoformat(now),),
        )
        return int(cursor.rowcount or 0)

    def prune(self, connection: sqlite3.Connection, keep: int = 24) -> int:
        """Deactivate the least salient items beyond ``keep``.

        Returns:
            Number of items deactivated.
        """
        cursor = connection.execute(
            "UPDATE working_situation_items SET status = 'evicted' WHERE item_id IN ("
            "SELECT item_id FROM working_situation_items WHERE status = 'active' "
            "ORDER BY salience DESC, updated_at DESC LIMIT -1 OFFSET ?)",
            (int(keep),),
        )
        return int(cursor.rowcount or 0)

    def clear(self, connection: sqlite3.Connection) -> None:
        """Remove every item; used by tests and by full rebuilds."""
        connection.execute("DELETE FROM working_situation_items")


# --------------------------------------------------------------------------------------
# emotion
# --------------------------------------------------------------------------------------


class EmotionProjection:
    """Active emotion impact events and cached explanations."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def list_active(self) -> list[EmotionEvent]:
        """Return the active emotion events, strongest first."""
        rows = self._db.query(
            "SELECT * FROM active_emotion_events WHERE status = 'active' "
            "ORDER BY intensity DESC, created_at DESC"
        )
        return [self._to_event(row) for row in rows]

    def upsert(self, connection: sqlite3.Connection, event: EmotionEvent) -> None:
        """Insert or replace one emotion event."""
        connection.execute(
            "INSERT INTO active_emotion_events(emotion_event_id, source_event_id, direction, "
            "intensity, activation, target, semantic_label, created_at, decay_rate, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active') "
            "ON CONFLICT(emotion_event_id) DO UPDATE SET intensity=excluded.intensity, "
            "activation=excluded.activation, direction=excluded.direction, "
            "semantic_label=excluded.semantic_label, status='active'",
            (
                event.emotion_event_id,
                event.source_event_id,
                event.direction,
                float(event.intensity),
                float(event.activation),
                event.target,
                event.semantic_label,
                isoformat(event.created_at or utcnow()),
                float(event.decay_rate),
            ),
        )

    def update_intensity(
        self, connection: sqlite3.Connection, emotion_event_id: str, intensity: float, activation: float
    ) -> None:
        """Persist new intensity/activation for one emotion event."""
        connection.execute(
            "UPDATE active_emotion_events SET intensity = ?, activation = ? WHERE emotion_event_id = ?",
            (float(intensity), float(activation), emotion_event_id),
        )

    def deactivate(self, connection: sqlite3.Connection, emotion_event_ids: Sequence[str]) -> None:
        """Mark emotion events as decayed."""
        for identifier in emotion_event_ids:
            connection.execute(
                "UPDATE active_emotion_events SET status = 'decayed' WHERE emotion_event_id = ?",
                (identifier,),
            )

    def clear(self, connection: sqlite3.Connection) -> None:
        """Delete every emotion event row."""
        connection.execute("DELETE FROM active_emotion_events")

    def cached_explanation(self, cache_key: str, now: datetime, ttl_seconds: float) -> dict[str, Any] | None:
        """Return a cached emotion explanation when it is still fresh."""
        row = self._db.query_one(
            "SELECT * FROM emotion_explanations WHERE cache_key = ? ORDER BY last_used_at DESC LIMIT 1",
            (cache_key,),
        )
        data = row_to_dict(row, "emotion_explanations")
        if data is None:
            return None
        last_used = parse_datetime(data.get("last_used_at"))
        if last_used is None or (ensure_aware(now) - last_used).total_seconds() > ttl_seconds:
            return None
        return data.get("payload_json") or None

    def store_explanation(
        self,
        connection: sqlite3.Connection,
        *,
        cache_key: str,
        payload: dict[str, Any],
        source: str = "template",
        now: datetime | None = None,
    ) -> str:
        """Store (or refresh) an emotion explanation in the cache.

        Returns:
            The explanation identifier.
        """
        stamp = isoformat(now or utcnow())
        existing = connection.execute(
            "SELECT explanation_id FROM emotion_explanations WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if existing is not None:
            connection.execute(
                "UPDATE emotion_explanations SET payload_json = ?, source = ?, last_used_at = ? "
                "WHERE explanation_id = ?",
                (dumps(payload), source, stamp, existing["explanation_id"]),
            )
            return str(existing["explanation_id"])
        identifier = new_id("memory")
        connection.execute(
            "INSERT INTO emotion_explanations(explanation_id, cache_key, payload_json, source, "
            "created_at, last_used_at) VALUES(?, ?, ?, ?, ?, ?)",
            (identifier, cache_key, dumps(payload), source, stamp, stamp),
        )
        return identifier

    @staticmethod
    def _to_event(row: sqlite3.Row) -> EmotionEvent:
        """Convert a database row into an :class:`EmotionEvent`."""
        data = row_to_dict(row, "active_emotion_events") or {}
        return EmotionEvent(
            emotion_event_id=data["emotion_event_id"],
            source_event_id=data["source_event_id"],
            direction=data["direction"],
            intensity=float(data["intensity"]),
            activation=float(data["activation"]),
            target=data.get("target") or "user",
            semantic_label=data.get("semantic_label"),
            created_at=parse_datetime(data.get("created_at")),
            decay_rate=float(data.get("decay_rate") or 0.08),
        )


# --------------------------------------------------------------------------------------
# boundaries
# --------------------------------------------------------------------------------------


class BoundaryProjection:
    """The boundary state machine's storage."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def list_all(self, include_revoked: bool = False) -> list[Boundary]:
        """Return every boundary, optionally including revoked ones."""
        sql = "SELECT * FROM boundaries"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        rows = self._db.query(sql + " ORDER BY created_at DESC")
        return [self._to_boundary(row) for row in rows]

    def active(self, now: datetime) -> list[Boundary]:
        """Return the boundaries currently in force."""
        return [boundary for boundary in self.list_all() if boundary.is_active(now)]

    def get(self, boundary_id: str) -> Boundary | None:
        """Return one boundary by identifier."""
        row = self._db.query_one("SELECT * FROM boundaries WHERE boundary_id = ?", (boundary_id,))
        return self._to_boundary(row) if row is not None else None

    def upsert(self, connection: sqlite3.Connection, boundary: Boundary) -> str:
        """Insert or replace a boundary row."""
        connection.execute(
            "INSERT INTO boundaries(boundary_id, type, scope, allow_reply, allow_proactive, starts_at, "
            "expires_at, revocable_by, source_event_id, revoked_at, note, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(boundary_id) DO UPDATE SET type=excluded.type, scope=excluded.scope, "
            "allow_reply=excluded.allow_reply, allow_proactive=excluded.allow_proactive, "
            "starts_at=excluded.starts_at, expires_at=excluded.expires_at, revoked_at=excluded.revoked_at, "
            "note=excluded.note",
            (
                boundary.boundary_id,
                boundary.type,
                boundary.scope,
                int(boundary.allow_reply),
                int(boundary.allow_proactive),
                isoformat(boundary.starts_at),
                isoformat(boundary.expires_at),
                boundary.revocable_by,
                boundary.source_event_id,
                isoformat(boundary.revoked_at),
                boundary.note,
                isoformat(utcnow()),
            ),
        )
        return boundary.boundary_id

    @staticmethod
    def _to_boundary(row: sqlite3.Row) -> Boundary:
        """Convert a database row into a :class:`Boundary`."""
        data = row_to_dict(row, "boundaries") or {}
        return Boundary(
            boundary_id=data["boundary_id"],
            type=data["type"],
            scope=data.get("scope") or "all_topics",
            allow_reply=bool(data.get("allow_reply", 1)),
            allow_proactive=bool(data.get("allow_proactive", 0)),
            starts_at=parse_datetime(data.get("starts_at")),
            expires_at=parse_datetime(data.get("expires_at")),
            revocable_by=data.get("revocable_by") or "explicit_user_revoke",
            source_event_id=data.get("source_event_id"),
            revoked_at=parse_datetime(data.get("revoked_at")),
            note=data.get("note"),
        )


# --------------------------------------------------------------------------------------
# unfinished matters
# --------------------------------------------------------------------------------------


class UnfinishedProjection:
    """The unfinished-matter state machine's storage."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def list_open(self) -> list[UnfinishedMatter]:
        """Return matters that are still live (open, waiting or due)."""
        rows = self._db.query(
            "SELECT * FROM unfinished_matters WHERE status IN ('open', 'waiting', 'due') "
            "ORDER BY priority DESC, COALESCE(waiting_until, created_at) ASC"
        )
        return [self._to_matter(row) for row in rows]

    def list_all(self, limit: int = 200) -> list[UnfinishedMatter]:
        """Return matters of every status, newest first."""
        rows = self._db.query(
            "SELECT * FROM unfinished_matters ORDER BY updated_at DESC LIMIT ?", (int(limit),)
        )
        return [self._to_matter(row) for row in rows]

    def get(self, unfinished_id: str) -> UnfinishedMatter | None:
        """Return one matter by identifier."""
        row = self._db.query_one(
            "SELECT * FROM unfinished_matters WHERE unfinished_id = ?", (unfinished_id,)
        )
        return self._to_matter(row) if row is not None else None

    def upsert(self, connection: sqlite3.Connection, matter: UnfinishedMatter) -> str:
        """Insert or replace a matter row."""
        now = isoformat(utcnow())
        matter.updated_at = utcnow()
        connection.execute(
            "INSERT INTO unfinished_matters(unfinished_id, title, source_event_ids, status, waiting_until, "
            "priority, mute_until, expire_at, resolution_conditions, created_at, updated_at, resolution_note) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(unfinished_id) DO UPDATE SET title=excluded.title, "
            "source_event_ids=excluded.source_event_ids, status=excluded.status, "
            "waiting_until=excluded.waiting_until, priority=excluded.priority, mute_until=excluded.mute_until, "
            "expire_at=excluded.expire_at, resolution_conditions=excluded.resolution_conditions, "
            "updated_at=excluded.updated_at, resolution_note=excluded.resolution_note",
            (
                matter.unfinished_id,
                matter.title,
                dumps(matter.source_event_ids),
                matter.status,
                isoformat(matter.waiting_until),
                float(matter.priority),
                isoformat(matter.mute_until),
                isoformat(matter.expire_at),
                dumps(matter.resolution_conditions),
                isoformat(matter.created_at or utcnow()),
                now,
                matter.resolution_note,
            ),
        )
        return matter.unfinished_id

    def set_status(
        self,
        connection: sqlite3.Connection,
        unfinished_id: str,
        status: str,
        *,
        note: str | None = None,
    ) -> None:
        """Change the status of one matter."""
        connection.execute(
            "UPDATE unfinished_matters SET status = ?, updated_at = ?, "
            "resolution_note = COALESCE(?, resolution_note) WHERE unfinished_id = ?",
            (status, isoformat(utcnow()), note, unfinished_id),
        )

    @staticmethod
    def _to_matter(row: sqlite3.Row) -> UnfinishedMatter:
        """Convert a database row into an :class:`UnfinishedMatter`."""
        data = row_to_dict(row, "unfinished_matters") or {}
        return UnfinishedMatter(
            unfinished_id=data["unfinished_id"],
            title=data["title"],
            source_event_ids=data.get("source_event_ids") or [],
            status=data["status"],
            waiting_until=parse_datetime(data.get("waiting_until")),
            priority=float(data.get("priority") or 0.5),
            mute_until=parse_datetime(data.get("mute_until")),
            expire_at=parse_datetime(data.get("expire_at")),
            resolution_conditions=data.get("resolution_conditions") or [],
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            resolution_note=data.get("resolution_note"),
        )


# --------------------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------------------


class MemoryProjection:
    """Memory candidates, consolidated memories and the activation pool."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    # -------------------------------------------------------------- candidates

    def pending_candidates(self, limit: int = 100) -> list[MemoryCandidate]:
        """Return candidates awaiting consolidation, most valuable first."""
        rows = self._db.query(
            "SELECT * FROM memory_candidates WHERE status = 'pending' ORDER BY value DESC LIMIT ?",
            (int(limit),),
        )
        return [self._to_candidate(row) for row in rows]

    def list_candidates(self, status: str | None = None, limit: int = 100) -> list[MemoryCandidate]:
        """Return memory candidates, optionally filtered by status."""
        if status:
            rows = self._db.query(
                "SELECT * FROM memory_candidates WHERE status = ? ORDER BY value DESC LIMIT ?",
                (status, int(limit)),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM memory_candidates ORDER BY value DESC LIMIT ?", (int(limit),)
            )
        return [self._to_candidate(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        """Return one memory candidate by identifier."""
        row = self._db.query_one(
            "SELECT * FROM memory_candidates WHERE candidate_id = ?", (candidate_id,)
        )
        return self._to_candidate(row) if row is not None else None

    def upsert_candidate(self, connection: sqlite3.Connection, candidate: MemoryCandidate) -> str:
        """Insert or replace a memory candidate row."""
        now = isoformat(utcnow())
        connection.execute(
            "INSERT INTO memory_candidates(candidate_id, summary, kind, source_event_ids, value, status, "
            "created_at, updated_at, consolidated_memory_id, topics_json, confidence) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET summary=excluded.summary, kind=excluded.kind, "
            "source_event_ids=excluded.source_event_ids, value=excluded.value, status=excluded.status, "
            "updated_at=excluded.updated_at, consolidated_memory_id=excluded.consolidated_memory_id, "
            "topics_json=excluded.topics_json, confidence=excluded.confidence",
            (
                candidate.candidate_id,
                candidate.summary,
                candidate.kind,
                dumps(candidate.source_event_ids),
                float(candidate.value),
                candidate.status,
                isoformat(candidate.created_at or utcnow()),
                now,
                candidate.consolidated_memory_id,
                dumps(candidate.topics),
                float(candidate.confidence),
            ),
        )
        return candidate.candidate_id

    def set_candidate_status(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        status: str,
        *,
        consolidated_memory_id: str | None = None,
    ) -> None:
        """Update the status of one memory candidate."""
        connection.execute(
            "UPDATE memory_candidates SET status = ?, updated_at = ?, "
            "consolidated_memory_id = COALESCE(?, consolidated_memory_id) WHERE candidate_id = ?",
            (status, isoformat(utcnow()), consolidated_memory_id, candidate_id),
        )

    # ----------------------------------------------------------------- memories

    def list_memories(
        self, status: str | None = MemoryStatus.ACTIVE.value, limit: int = 100
    ) -> list[Memory]:
        """Return long-term memories, optionally filtered by retention status."""
        if status is None:
            rows = self._db.query(
                "SELECT * FROM memories ORDER BY importance DESC LIMIT ?", (int(limit),)
            )
        elif isinstance(status, (list, tuple, set)):
            placeholders = ",".join("?" for _ in status)
            rows = self._db.query(
                f"SELECT * FROM memories WHERE status IN ({placeholders}) "
                "ORDER BY importance DESC LIMIT ?",
                (*status, int(limit)),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM memories WHERE status = ? ORDER BY importance DESC LIMIT ?",
                (status, int(limit)),
            )
        return [self._to_memory(row) for row in rows]

    def get_memory(self, memory_id: str) -> Memory | None:
        """Return one long-term memory by identifier."""
        row = self._db.query_one("SELECT * FROM memories WHERE memory_id = ?", (memory_id,))
        return self._to_memory(row) if row is not None else None

    def get_memories(self, memory_ids: Sequence[str]) -> dict[str, Memory]:
        """Return several memories keyed by identifier."""
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        rows = self._db.query(f"SELECT * FROM memories WHERE memory_id IN ({placeholders})", tuple(memory_ids))
        return {row["memory_id"]: self._to_memory(row) for row in rows}

    def upsert_memory(self, connection: sqlite3.Connection, memory: Memory) -> str:
        """Insert or replace a long-term memory row."""
        now = isoformat(utcnow())
        connection.execute(
            "INSERT INTO memories(memory_id, kind, summary, structured_json, topics_json, importance, "
            "confidence, status, source_event_ids, created_at, updated_at, archived_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET kind=excluded.kind, summary=excluded.summary, "
            "structured_json=excluded.structured_json, topics_json=excluded.topics_json, "
            "importance=excluded.importance, confidence=excluded.confidence, status=excluded.status, "
            "source_event_ids=excluded.source_event_ids, updated_at=excluded.updated_at, "
            "archived_at=excluded.archived_at",
            (
                memory.memory_id,
                memory.kind,
                memory.summary,
                dumps(memory.structured),
                dumps(memory.topics),
                float(memory.importance),
                float(memory.confidence),
                memory.status,
                dumps(memory.source_event_ids),
                isoformat(memory.created_at or utcnow()),
                now,
                isoformat(memory.archived_at),
            ),
        )
        return memory.memory_id

    def set_memory_status(self, connection: sqlite3.Connection, memory_id: str, status: str) -> None:
        """Change the retention status of a memory."""
        connection.execute(
            "UPDATE memories SET status = ?, updated_at = ?, archived_at = ? WHERE memory_id = ?",
            (
                status,
                isoformat(utcnow()),
                isoformat(utcnow()) if status == MemoryStatus.ARCHIVED.value else None,
                memory_id,
            ),
        )

    # --------------------------------------------------------------- activation

    def list_activated(self, limit: int = 20) -> list[ActivatedMemory]:
        """Return the activation pool, most activated first."""
        rows = self._db.query(
            "SELECT * FROM activated_memories WHERE activation > 0 "
            "ORDER BY activation DESC LIMIT ?",
            (int(limit),),
        )
        return [self._to_activation(row) for row in rows]

    def upsert_activation(self, connection: sqlite3.Connection, activated: ActivatedMemory) -> None:
        """Insert or replace one activation row."""
        connection.execute(
            "INSERT INTO activated_memories(memory_id, activation, last_recalled_at, recall_count, "
            "reason, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET activation=excluded.activation, "
            "last_recalled_at=excluded.last_recalled_at, recall_count=excluded.recall_count, "
            "reason=excluded.reason, updated_at=excluded.updated_at",
            (
                activated.memory_id,
                float(activated.activation),
                isoformat(activated.last_recalled_at),
                int(activated.recall_count),
                activated.reason,
                isoformat(utcnow()),
            ),
        )

    def delete_activation(self, connection: sqlite3.Connection, memory_id: str) -> None:
        """Drop one activation row."""
        connection.execute("DELETE FROM activated_memories WHERE memory_id = ?", (memory_id,))

    @staticmethod
    def _to_candidate(row: sqlite3.Row) -> MemoryCandidate:
        """Convert a database row into a :class:`MemoryCandidate`."""
        data = row_to_dict(row, "memory_candidates") or {}
        return MemoryCandidate(
            candidate_id=data["candidate_id"],
            summary=data["summary"],
            kind=data.get("kind") or "episodic",
            source_event_ids=data.get("source_event_ids") or [],
            value=float(data.get("value") or 0.0),
            status=data.get("status") or "pending",
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            consolidated_memory_id=data.get("consolidated_memory_id"),
            topics=data.get("topics_json") or [],
            confidence=float(data.get("confidence") or 0.5),
        )

    @staticmethod
    def _to_memory(row: sqlite3.Row) -> Memory:
        """Convert a database row into a :class:`Memory`."""
        data = row_to_dict(row, "memories") or {}
        return Memory(
            memory_id=data["memory_id"],
            kind=data["kind"],
            summary=data["summary"],
            structured=data.get("structured_json") or {},
            topics=data.get("topics_json") or [],
            importance=float(data.get("importance") or 0.5),
            confidence=float(data.get("confidence") or 0.5),
            status=data.get("status") or MemoryStatus.ACTIVE.value,
            source_event_ids=data.get("source_event_ids") or [],
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            archived_at=parse_datetime(data.get("archived_at")),
        )

    @staticmethod
    def _to_activation(row: sqlite3.Row) -> ActivatedMemory:
        """Convert a database row into an :class:`ActivatedMemory`."""
        data = row_to_dict(row, "activated_memories") or {}
        return ActivatedMemory(
            memory_id=data["memory_id"],
            activation=float(data.get("activation") or 0.0),
            last_recalled_at=parse_datetime(data.get("last_recalled_at")),
            recall_count=int(data.get("recall_count") or 0),
            reason=data.get("reason"),
        )


# --------------------------------------------------------------------------------------
# candidate intents
# --------------------------------------------------------------------------------------


class CandidateProjection:
    """The candidate intent pool."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def list_by_status(self, statuses: Sequence[str], limit: int = 50) -> list[CandidateIntent]:
        """Return candidates whose status is in ``statuses``."""
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self._db.query(
            f"SELECT * FROM candidate_intents WHERE status IN ({placeholders}) "
            "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (*statuses, int(limit)),
        )
        return [self._to_candidate(row) for row in rows]

    def list_active(self, limit: int = 50) -> list[CandidateIntent]:
        """Return candidates eligible for the motivational game."""
        return self.list_by_status(
            [CandidateStatus.NEW.value, CandidateStatus.ACTIVE.value, CandidateStatus.DORMANT.value],
            limit=limit,
        )

    def get(self, candidate_id: str) -> CandidateIntent | None:
        """Return one candidate by identifier."""
        row = self._db.query_one(
            "SELECT * FROM candidate_intents WHERE candidate_id = ?", (candidate_id,)
        )
        return self._to_candidate(row) if row is not None else None

    def upsert(self, connection: sqlite3.Connection, candidate: CandidateIntent) -> str:
        """Insert or replace a candidate row."""
        now = isoformat(utcnow())
        candidate.updated_at = utcnow()
        connection.execute(
            "INSERT INTO candidate_intents(candidate_id, type, intent, goal, target, sources_json, "
            "constraints_json, preconditions_json, invalidate_json, confidence, status, internal_need, "
            "unfinished_relevance, emotion_relevance, created_at, updated_at, expires_at, retired_reason, "
            "proposed_by) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET type=excluded.type, intent=excluded.intent, "
            "goal=excluded.goal, target=excluded.target, sources_json=excluded.sources_json, "
            "constraints_json=excluded.constraints_json, preconditions_json=excluded.preconditions_json, "
            "invalidate_json=excluded.invalidate_json, confidence=excluded.confidence, status=excluded.status, "
            "internal_need=excluded.internal_need, unfinished_relevance=excluded.unfinished_relevance, "
            "emotion_relevance=excluded.emotion_relevance, updated_at=excluded.updated_at, "
            "expires_at=excluded.expires_at, retired_reason=excluded.retired_reason",
            (
                candidate.candidate_id,
                candidate.type,
                candidate.intent,
                candidate.goal,
                candidate.target,
                dumps(candidate.sources),
                dumps(candidate.constraints),
                dumps(candidate.preconditions),
                dumps(candidate.invalidate_when),
                float(candidate.confidence),
                candidate.status,
                float(candidate.internal_need),
                float(candidate.unfinished_relevance),
                float(candidate.emotion_relevance),
                isoformat(candidate.created_at or utcnow()),
                now,
                isoformat(candidate.expires_at),
                candidate.retired_reason,
                candidate.proposed_by,
            ),
        )
        return candidate.candidate_id

    def set_status(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        status: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Change a candidate's lifecycle status."""
        connection.execute(
            "UPDATE candidate_intents SET status = ?, updated_at = ?, "
            "retired_reason = COALESCE(?, retired_reason) WHERE candidate_id = ?",
            (status, isoformat(utcnow()), reason, candidate_id),
        )

    @staticmethod
    def _to_candidate(row: sqlite3.Row) -> CandidateIntent:
        """Convert a database row into a :class:`CandidateIntent`."""
        data = row_to_dict(row, "candidate_intents") or {}
        return CandidateIntent(
            candidate_id=data["candidate_id"],
            type=data["type"],
            intent=data["intent"],
            goal=data.get("goal") or "",
            target=data.get("target") or "",
            sources=data.get("sources_json") or [],
            constraints=data.get("constraints_json") or [],
            preconditions=data.get("preconditions_json") or [],
            invalidate_when=data.get("invalidate_json") or [],
            confidence=float(data.get("confidence") or 0.5),
            status=data["status"],
            internal_need=float(data.get("internal_need") or 0.5),
            unfinished_relevance=float(data.get("unfinished_relevance") or 0.0),
            emotion_relevance=float(data.get("emotion_relevance") or 0.0),
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            expires_at=parse_datetime(data.get("expires_at")),
            retired_reason=data.get("retired_reason"),
            proposed_by=data.get("proposed_by") or "rule",
        )


# --------------------------------------------------------------------------------------
# action attempts
# --------------------------------------------------------------------------------------


class AttemptProjection:
    """Action attempts and their append-only transition log."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def get(self, attempt_id: str) -> ActionAttempt | None:
        """Return one attempt by identifier."""
        row = self._db.query_one("SELECT * FROM action_attempts WHERE attempt_id = ?", (attempt_id,))
        return self._to_attempt(row) if row is not None else None

    def list_by_state(self, states: Sequence[str], limit: int = 50) -> list[ActionAttempt]:
        """Return attempts in the given states, oldest first."""
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        rows = self._db.query(
            f"SELECT * FROM action_attempts WHERE state IN ({placeholders}) "
            "ORDER BY created_at ASC LIMIT ?",
            (*states, int(limit)),
        )
        return [self._to_attempt(row) for row in rows]

    def list_all(self, limit: int = 50) -> list[ActionAttempt]:
        """Return the newest attempts."""
        rows = self._db.query(
            "SELECT * FROM action_attempts ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )
        return [self._to_attempt(row) for row in rows]

    def count_in_flight(self) -> int:
        """Count attempts that have not reached a terminal state."""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM action_attempts WHERE state NOT IN "
            "('resolved', 'aborted', 'expired', 'failed')"
        )
        return int(row["n"]) if row else 0

    def upsert(self, connection: sqlite3.Connection, attempt: ActionAttempt) -> str:
        """Insert or replace an attempt row."""
        attempt.updated_at = attempt.updated_at or utcnow()
        connection.execute(
            "INSERT INTO action_attempts(attempt_id, candidate_id, state, intent, goal, based_on_version, "
            "created_at, updated_at, committed_at, rendered_text, failure_reason, reconcile_action, "
            "superseded_json, outbox_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(attempt_id) DO UPDATE SET candidate_id=excluded.candidate_id, state=excluded.state, "
            "intent=excluded.intent, goal=excluded.goal, based_on_version=excluded.based_on_version, "
            "updated_at=excluded.updated_at, committed_at=excluded.committed_at, "
            "rendered_text=excluded.rendered_text, failure_reason=excluded.failure_reason, "
            "reconcile_action=excluded.reconcile_action, superseded_json=excluded.superseded_json, "
            "outbox_id=excluded.outbox_id",
            (
                attempt.attempt_id,
                attempt.candidate_id,
                attempt.state,
                attempt.intent,
                attempt.goal,
                int(attempt.based_on_version),
                isoformat(attempt.created_at or utcnow()),
                isoformat(attempt.updated_at),
                isoformat(attempt.committed_at),
                attempt.rendered_text,
                attempt.failure_reason,
                attempt.reconcile_action,
                dumps(attempt.superseded_by_event_ids),
                attempt.outbox_id,
            ),
        )
        return attempt.attempt_id

    def record_transition(
        self,
        connection: sqlite3.Connection,
        *,
        attempt_id: str,
        from_state: str | None,
        to_state: str,
        reason: str | None = None,
        runtime_version: int = 0,
    ) -> str:
        """Append a transition to the attempt's immutable log.

        Returns:
            The transition log identifier.
        """
        identifier = new_id("memory")
        connection.execute(
            "INSERT INTO attempt_events(attempt_event_id, attempt_id, from_state, to_state, reason, "
            "runtime_version, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                attempt_id,
                from_state,
                to_state,
                reason,
                int(runtime_version),
                isoformat(utcnow()),
            ),
        )
        return identifier

    def transitions(self, attempt_id: str) -> list[dict[str, Any]]:
        """Return the transition history of one attempt."""
        rows = self._db.query(
            "SELECT * FROM attempt_events WHERE attempt_id = ? ORDER BY created_at ASC", (attempt_id,)
        )
        return [row_to_dict(row, "attempt_events") or {} for row in rows]

    @staticmethod
    def _to_attempt(row: sqlite3.Row) -> ActionAttempt:
        """Convert a database row into an :class:`ActionAttempt`."""
        data = row_to_dict(row, "action_attempts") or {}
        return ActionAttempt(
            attempt_id=data["attempt_id"],
            candidate_id=data.get("candidate_id"),
            state=data["state"],
            intent=data["intent"],
            goal=data.get("goal") or "",
            based_on_version=int(data.get("based_on_version") or 0),
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            committed_at=parse_datetime(data.get("committed_at")),
            rendered_text=data.get("rendered_text"),
            failure_reason=data.get("failure_reason"),
            reconcile_action=data.get("reconcile_action"),
            superseded_by_event_ids=data.get("superseded_json") or [],
            outbox_id=data.get("outbox_id"),
        )


# --------------------------------------------------------------------------------------
# outbox
# --------------------------------------------------------------------------------------


class OutboxProjection:
    """The asynchronous delivery queue with claim/lease/ack semantics."""

    def __init__(self, db: Database, runtime_id: str = "companion") -> None:
        """Bind the projection to a database and runtime identifier."""
        self._db = db
        self.runtime_id = runtime_id

    def enqueue(self, connection: sqlite3.Connection, item: OutboxItem) -> str:
        """Insert an outbox row."""
        item.created_at = item.created_at or utcnow()
        connection.execute(
            "INSERT INTO outbox(outbox_id, kind, payload_json, status, priority, available_at, created_at, "
            "lease_owner, lease_expires_at, attempts, max_attempts, acked_at, last_error, conversation_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(outbox_id) DO UPDATE SET payload_json=excluded.payload_json, status=excluded.status, "
            "priority=excluded.priority, available_at=excluded.available_at",
            (
                item.outbox_id,
                item.kind,
                dumps(item.payload),
                item.status,
                int(item.priority),
                isoformat(item.available_at),
                isoformat(item.created_at),
                item.lease_owner,
                isoformat(item.lease_expires_at),
                int(item.attempts),
                int(item.max_attempts),
                isoformat(item.acked_at),
                item.last_error,
                item.conversation_id,
            ),
        )
        return item.outbox_id

    def get(self, outbox_id: str) -> OutboxItem | None:
        """Return one outbox row by identifier."""
        row = self._db.query_one("SELECT * FROM outbox WHERE outbox_id = ?", (outbox_id,))
        return self._to_item(row) if row is not None else None

    def list_items(
        self, status: str | None = None, limit: int = 50, conversation_id: str | None = None
    ) -> list[OutboxItem]:
        """Return outbox rows, newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self._db.query(
            f"SELECT * FROM outbox {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
        )
        return [self._to_item(row) for row in rows]

    def reclaim_expired(self, connection: sqlite3.Connection, now: datetime) -> int:
        """Return expired leases to the pending pool or fail them.

        Rows whose attempt budget is exhausted are marked ``failed``.

        Returns:
            Number of rows transitioned.
        """
        stamp = isoformat(now)
        cursor = connection.execute(
            "UPDATE outbox SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL, "
            "last_error = COALESCE(last_error, 'lease expired') "
            "WHERE status = 'leased' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ? "
            "AND attempts >= max_attempts",
            (stamp,),
        )
        failed = int(cursor.rowcount or 0)
        cursor = connection.execute(
            "UPDATE outbox SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL, "
            "available_at = ?, last_error = COALESCE(last_error, 'lease expired') "
            "WHERE status = 'leased' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ? "
            "AND attempts < max_attempts",
            (stamp, stamp),
        )
        requeued = int(cursor.rowcount or 0)
        return failed + requeued

    def claim(
        self,
        connection: sqlite3.Connection,
        *,
        owner: str,
        now: datetime,
        lease_seconds: float,
        limit: int = 1,
        kinds: Sequence[str] | None = None,
    ) -> list[OutboxItem]:
        """Atomically lease up to ``limit`` ready rows for ``owner``.

        The caller must already hold a write transaction; ``BEGIN IMMEDIATE``
        makes the select-then-update sequence race-free across processes.

        Args:
            connection: Write connection.
            owner: Lease owner identifier (worker id).
            now: Reference time.
            lease_seconds: Lease duration.
            limit: Maximum number of rows to lease.
            kinds: Optional restriction to specific outbox kinds.

        Returns:
            The leased items, highest priority first.
        """
        stamp = isoformat(now)
        lease_until = isoformat(now + timedelta(seconds=lease_seconds))
        clauses = ["status = 'pending'", "(available_at IS NULL OR available_at <= ?)"]
        params: list[Any] = [stamp]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        params.append(max(1, int(limit)))
        rows = connection.execute(
            "SELECT outbox_id FROM outbox WHERE " + " AND ".join(clauses) +
            " ORDER BY priority ASC, created_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        claimed: list[OutboxItem] = []
        for row in rows:
            identifier = row["outbox_id"]
            connection.execute(
                "UPDATE outbox SET status = 'leased', lease_owner = ?, lease_expires_at = ?, "
                "attempts = attempts + 1 WHERE outbox_id = ? AND status = 'pending'",
                (owner, lease_until, identifier),
            )
            item = self.get(identifier)
            if item is not None:
                claimed.append(item)
        return claimed

    def ack(self, connection: sqlite3.Connection, outbox_id: str, now: datetime | None = None) -> bool:
        """Mark a leased row as delivered.

        Returns:
            ``True`` when a leased row was transitioned.
        """
        cursor = connection.execute(
            "UPDATE outbox SET status = 'delivered', acked_at = ?, lease_owner = NULL, "
            "lease_expires_at = NULL WHERE outbox_id = ? AND status = 'leased'",
            (isoformat(now or utcnow()), outbox_id),
        )
        return bool(cursor.rowcount)

    def nack(
        self,
        connection: sqlite3.Connection,
        outbox_id: str,
        *,
        error: str,
        retry_at: datetime | None = None,
        terminal: bool = False,
    ) -> bool:
        """Return a leased row to the queue, or fail it terminally.

        Returns:
            ``True`` when the row was updated.
        """
        row = connection.execute(
            "SELECT attempts, max_attempts, status FROM outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        if row is None or row["status"] != OutboxStatus.LEASED.value:
            return False
        exhausted = bool(row["attempts"] >= row["max_attempts"])
        if terminal or exhausted:
            connection.execute(
                "UPDATE outbox SET status = 'failed', last_error = ?, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE outbox_id = ?",
                (error, outbox_id),
            )
        else:
            connection.execute(
                "UPDATE outbox SET status = 'pending', last_error = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, available_at = ? WHERE outbox_id = ?",
                (error, isoformat(retry_at), outbox_id),
            )
        return True

    def cancel(self, connection: sqlite3.Connection, outbox_id: str, reason: str | None = None) -> bool:
        """Cancel a row that has not been delivered yet.

        Returns:
            ``True`` when the row was cancelled.
        """
        cursor = connection.execute(
            "UPDATE outbox SET status = 'cancelled', last_error = COALESCE(?, last_error), "
            "lease_owner = NULL, lease_expires_at = NULL "
            "WHERE outbox_id = ? AND status IN ('pending', 'leased')",
            (reason, outbox_id),
        )
        return bool(cursor.rowcount)

    def cancel_for_attempt(
        self, connection: sqlite3.Connection, attempt_id: str, reason: str | None = None
    ) -> int:
        """Cancel every pending outbox row that references ``attempt_id``.

        Returns:
            Number of cancelled rows.
        """
        cursor = connection.execute(
            "UPDATE outbox SET status = 'cancelled', last_error = COALESCE(?, last_error) "
            "WHERE status IN ('pending', 'leased') AND payload_json LIKE ?",
            (reason, f'%"{attempt_id}"%'),
        )
        return int(cursor.rowcount or 0)

    def stats(self) -> dict[str, int]:
        """Return a count per outbox status."""
        rows = self._db.query("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        return {str(row["status"]): int(row["n"]) for row in rows}

    @staticmethod
    def _to_item(row: sqlite3.Row) -> OutboxItem:
        """Convert a database row into an :class:`OutboxItem`."""
        data = row_to_dict(row, "outbox") or {}
        return OutboxItem(
            outbox_id=data["outbox_id"],
            kind=data["kind"],
            payload=data.get("payload_json") or {},
            status=data["status"],
            priority=int(data.get("priority") or 100),
            available_at=parse_datetime(data.get("available_at")),
            created_at=parse_datetime(data.get("created_at")),
            lease_owner=data.get("lease_owner"),
            lease_expires_at=parse_datetime(data.get("lease_expires_at")),
            attempts=int(data.get("attempts") or 0),
            max_attempts=int(data.get("max_attempts") or 3),
            acked_at=parse_datetime(data.get("acked_at")),
            last_error=data.get("last_error"),
            conversation_id=data.get("conversation_id"),
        )


# --------------------------------------------------------------------------------------
# background tasks
# --------------------------------------------------------------------------------------


class TaskProjection:
    """Snapshots of in-flight background tasks used by the protocol layer."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def register(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        task_type: str,
        based_on_version: int,
        source_event_ids: Sequence[str],
        priority: str,
    ) -> str:
        """Record a task snapshot at dispatch time."""
        connection.execute(
            "INSERT INTO background_tasks(task_id, task_type, priority, based_on_version, "
            "source_event_ids, status, created_at) VALUES(?, ?, ?, ?, ?, 'in_flight', ?) "
            "ON CONFLICT(task_id) DO UPDATE SET task_type=excluded.task_type, priority=excluded.priority, "
            "based_on_version=excluded.based_on_version, source_event_ids=excluded.source_event_ids, "
            "status='in_flight'",
            (
                task_id,
                task_type,
                priority,
                int(based_on_version),
                dumps(list(source_event_ids)),
                isoformat(utcnow()),
            ),
        )
        return task_id

    def settle(
        self, connection: sqlite3.Connection, task_id: str, outcome: str, status: str = "settled"
    ) -> None:
        """Mark a task as settled with an outcome label."""
        connection.execute(
            "UPDATE background_tasks SET status = ?, settled_at = ?, outcome = ? WHERE task_id = ?",
            (status, isoformat(utcnow()), outcome, task_id),
        )

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Return one task snapshot."""
        return row_to_dict(
            self._db.query_one("SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)),
            "background_tasks",
        )

    def list_in_flight(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return in-flight task snapshots, oldest first."""
        rows = self._db.query(
            "SELECT * FROM background_tasks WHERE status = 'in_flight' ORDER BY created_at ASC LIMIT ?",
            (int(limit),),
        )
        return [row_to_dict(row, "background_tasks") or {} for row in rows]


# --------------------------------------------------------------------------------------
# interaction observations / user model
# --------------------------------------------------------------------------------------


class UserModelProjection:
    """Observation history plus the distilled preference parameters."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    GLOBAL_SCOPE = "global"

    def record_observation(self, connection: sqlite3.Connection, observation: Any) -> str:
        """Append one interaction observation.

        Args:
            connection: Write connection.
            observation: Either an
                :class:`~companion_runtime.typing.InteractionObservation` or an
                equivalent mapping. A dataclass is normalised here so callers do
                not have to serialise datetimes themselves.

        Returns:
            The observation identifier.
        """
        if dataclasses.is_dataclass(observation) and not isinstance(observation, type):
            data: dict[str, Any] = dataclasses.asdict(observation)
        else:
            data = dict(observation)
        identifier = data.get("observation_id") or new_id("observation")
        created_at = data.get("created_at")
        connection.execute(
            "INSERT INTO interaction_observations(observation_id, created_at, attempt_id, action_json, "
            "context_json, outcome_json, source_event_ids, attribution_confidence, source_weight, "
            "semantic_confidence, weight, applied) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                isoformat(created_at if isinstance(created_at, datetime) else parse_datetime(created_at) or utcnow()),
                data.get("attempt_id"),
                dumps(data.get("action") or {}),
                dumps(data.get("context") or {}),
                dumps(data.get("outcome") or {}),
                dumps(list(data.get("source_event_ids") or [])),
                float(data.get("attribution_confidence", 0.5)),
                float(data.get("source_weight", 0.5)),
                float(data.get("semantic_confidence", 0.5)),
                float(data.get("weight", 0.0)),
                int(bool(data.get("applied", False))),
            ),
        )
        return identifier

    def mark_observation_applied(self, connection: sqlite3.Connection, observation_id: str) -> None:
        """Flag an observation as folded into the user model."""
        connection.execute(
            "UPDATE interaction_observations SET applied = 1 WHERE observation_id = ?",
            (observation_id,),
        )

    def list_observations(self, limit: int = 100, applied: bool | None = None) -> list[dict[str, Any]]:
        """Return recorded observations, newest first."""
        if applied is None:
            rows = self._db.query(
                "SELECT * FROM interaction_observations ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM interaction_observations WHERE applied = ? ORDER BY created_at DESC LIMIT ?",
                (int(bool(applied)), int(limit)),
            )
        return [row_to_dict(row, "interaction_observations") or {} for row in rows]

    def get_params(self, scope: str = GLOBAL_SCOPE) -> dict[str, Any] | None:
        """Return the stored parameter block for ``scope``."""
        row = self._db.query_one("SELECT * FROM user_model_params WHERE scope = ?", (scope,))
        return row_to_dict(row, "user_model_params")

    def list_params(self) -> list[dict[str, Any]]:
        """Return every stored parameter block."""
        rows = self._db.query("SELECT * FROM user_model_params ORDER BY scope ASC")
        return [row_to_dict(row, "user_model_params") or {} for row in rows]

    def upsert_params(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str = GLOBAL_SCOPE,
        params: dict[str, Any],
        precision: dict[str, Any],
        observations: int,
        effective_count: float,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """Persist a parameter block (global or contextual)."""
        now = isoformat(utcnow())
        connection.execute(
            "INSERT INTO user_model_params(scope, params_json, precision_json, observations, "
            "effective_count, last_updated_at, last_summary_json) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET params_json=excluded.params_json, "
            "precision_json=excluded.precision_json, observations=excluded.observations, "
            "effective_count=excluded.effective_count, last_updated_at=excluded.last_updated_at, "
            "last_summary_json=COALESCE(excluded.last_summary_json, user_model_params.last_summary_json)",
            (
                scope,
                dumps(params),
                dumps(precision),
                int(observations),
                float(effective_count),
                now,
                dumps(summary) if summary is not None else None,
            ),
        )

    def set_summary(
        self, connection: sqlite3.Connection, summary: dict[str, Any], scope: str = GLOBAL_SCOPE
    ) -> None:
        """Replace the natural-language summary of a parameter block.

        The row is created with the model's seed parameters when it does not exist
        yet: a prose summary about the user must never be silently dropped just
        because the numeric block has not been persisted first.
        """
        from .user_model import default_parameter_block

        now = isoformat(utcnow())
        payload = dumps(summary)
        cursor = connection.execute(
            "UPDATE user_model_params SET last_summary_json = ? WHERE scope = ?", (payload, scope)
        )
        if cursor.rowcount:
            return
        params, precision = default_parameter_block()
        connection.execute(
            "INSERT INTO user_model_params(scope, params_json, precision_json, observations, "
            "effective_count, last_updated_at, last_summary_json) VALUES(?, ?, ?, 0, 0, ?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET last_summary_json = excluded.last_summary_json",
            (scope, dumps(params), dumps(precision), now, payload),
        )


# --------------------------------------------------------------------------------------
# interpretations / reappraisals
# --------------------------------------------------------------------------------------


class InterpretationProjection:
    """Append-only interpretation versions and reappraisal events."""

    def __init__(self, db: Database) -> None:
        """Bind the projection to a database."""
        self._db = db

    def add_version(
        self,
        connection: sqlite3.Connection,
        *,
        target_kind: str,
        target_id: str,
        content: str,
        confidence: float,
        source_version: int,
        source_event_ids: Sequence[str],
        supersedes_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a new interpretation version for a target.

        Returns:
            The inserted interpretation record.
        """
        row = connection.execute(
            "SELECT MAX(interpretation_version) AS v FROM interpretation_versions "
            "WHERE target_kind = ? AND target_id = ?",
            (target_kind, target_id),
        ).fetchone()
        next_version = int(row["v"] or 0) + 1
        identifier = new_id("interpretation")
        connection.execute(
            "INSERT INTO interpretation_versions(interpretation_id, target_kind, target_id, "
            "interpretation_version, supersedes_id, content, confidence, source_version, source_event_ids, "
            "created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                target_kind,
                target_id,
                next_version,
                supersedes_id,
                content,
                float(confidence),
                int(source_version),
                dumps(list(source_event_ids)),
                isoformat(utcnow()),
            ),
        )
        return {
            "interpretation_id": identifier,
            "target_kind": target_kind,
            "target_id": target_id,
            "interpretation_version": next_version,
            "supersedes_id": supersedes_id,
            "content": content,
            "confidence": confidence,
        }

    def latest(self, target_kind: str, target_id: str) -> dict[str, Any] | None:
        """Return the newest interpretation for a target."""
        row = self._db.query_one(
            "SELECT * FROM interpretation_versions WHERE target_kind = ? AND target_id = ? "
            "ORDER BY interpretation_version DESC LIMIT 1",
            (target_kind, target_id),
        )
        return row_to_dict(row, "interpretation_versions")

    def list_for_target(self, target_kind: str, target_id: str) -> list[dict[str, Any]]:
        """Return every interpretation version for a target, oldest first."""
        rows = self._db.query(
            "SELECT * FROM interpretation_versions WHERE target_kind = ? AND target_id = ? "
            "ORDER BY interpretation_version ASC",
            (target_kind, target_id),
        )
        return [row_to_dict(row, "interpretation_versions") or {} for row in rows]

    def add_reappraisal(
        self,
        connection: sqlite3.Connection,
        *,
        source_event_ids: Sequence[str],
        new_interpretation: str,
        previous_interpretation: str | None = None,
        delta_summary: str | None = None,
    ) -> str:
        """Append a reappraisal event (history is never rolled back).

        Returns:
            The reappraisal identifier.
        """
        identifier = new_id("memory")
        connection.execute(
            "INSERT INTO reappraisals(reappraisal_id, source_event_ids, previous_interpretation, "
            "new_interpretation, delta_summary, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (
                identifier,
                dumps(list(source_event_ids)),
                previous_interpretation,
                new_interpretation,
                delta_summary,
                isoformat(utcnow()),
            ),
        )
        return identifier

    def list_reappraisals(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent reappraisal events, newest first."""
        rows = self._db.query(
            "SELECT * FROM reappraisals ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )
        return [row_to_dict(row, "reappraisals") or {} for row in rows]


# --------------------------------------------------------------------------------------
# aggregator
# --------------------------------------------------------------------------------------


class Projections:
    """Convenience bundle of every projection object."""

    def __init__(self, db: Database, runtime_id: str = "companion") -> None:
        """Construct all projections over one database."""
        self.db = db
        self.runtime = RuntimeProjection(db, runtime_id)
        self.situation = SituationProjection(db)
        self.emotion = EmotionProjection(db)
        self.boundaries = BoundaryProjection(db)
        self.unfinished = UnfinishedProjection(db)
        self.memory = MemoryProjection(db)
        self.candidates = CandidateProjection(db)
        self.attempts = AttemptProjection(db)
        self.outbox = OutboxProjection(db, runtime_id)
        self.tasks = TaskProjection(db)
        self.user_model = UserModelProjection(db)
        self.interpretations = InterpretationProjection(db)

    def ensure_defaults(self, now: datetime | None = None) -> RuntimeState:
        """Create the runtime row and return it."""
        return self.runtime.ensure(now)
