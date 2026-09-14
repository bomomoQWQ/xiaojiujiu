"""Append-only raw event log.

This is the historical root of the Runtime. Nothing in this module ever issues an
``UPDATE`` or ``DELETE``: raw events are the one thing the whole system treats as
ground truth. Semantic layers above it may be re-estimated, but the bytes written
here stay untouched.

An optional JSONL mirror is written for cheap external inspection and for
disaster recovery if the SQLite projection is ever rebuilt.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .db import Database, dumps, row_to_dict
from .typing import Actor, EventType, RawEvent, new_id
from .utility import ensure_aware, isoformat, parse_datetime, utcnow

LOGGER = logging.getLogger("companion_runtime.eventlog")


@dataclass(slots=True)
class EventQuery:
    """Filter for :meth:`EventLog.read`."""

    conversation_id: str | None = None
    event_types: Sequence[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 100
    newest_first: bool = False


class EventLog:
    """Append-only access to ``raw_events``.

    Args:
        db: Open database handle.
        mirror_path: Optional JSONL mirror path; ``None`` disables the mirror.
    """

    def __init__(self, db: Database, mirror_path: str | Path | None = None) -> None:
        self._db = db
        self._mirror_path = Path(mirror_path) if mirror_path else None
        self._mirror_lock = threading.Lock()
        if self._mirror_path is not None:
            self._mirror_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ writing

    def append(
        self,
        event_type: str | EventType,
        *,
        actor: str | Actor,
        content: str | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        source_event_ids: Iterable[str] | None = None,
        timestamp: datetime | None = None,
        runtime_version: int = 0,
        event_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> RawEvent:
        """Append one event to history.

        Args:
            event_type: Event kind, see :class:`~companion_runtime.typing.EventType`.
            actor: Who caused the event.
            content: Verbatim content (user text, assistant text, tool output).
            conversation_id: Conversation scope.
            metadata: Free-form structured payload.
            source_event_ids: Events this one is derived from.
            timestamp: Event time; defaults to now.
            runtime_version: Runtime state version at append time.
            event_id: Explicit identifier, mainly for tests and replays.
            connection: Optional connection to reuse for a transaction.

        Returns:
            The persisted :class:`RawEvent`.
        """
        event = RawEvent(
            event_id=event_id or new_id("event"),
            event_type=event_type.value if isinstance(event_type, EventType) else str(event_type),
            timestamp=ensure_aware(timestamp) or utcnow(),
            actor=actor.value if isinstance(actor, Actor) else str(actor),
            conversation_id=conversation_id,
            content=content,
            metadata=dict(metadata or {}),
            source_event_ids=list(source_event_ids or []),
            runtime_version=int(runtime_version),
            created_at=utcnow(),
        )
        sql = (
            "INSERT INTO raw_events(event_id, event_type, timestamp, actor, conversation_id, "
            "content, metadata_json, source_event_ids, runtime_version, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (
            event.event_id,
            event.event_type,
            isoformat(event.timestamp),
            event.actor,
            event.conversation_id,
            event.content,
            dumps(event.metadata),
            dumps(event.source_event_ids),
            event.runtime_version,
            isoformat(event.created_at),
        )
        if connection is not None:
            connection.execute(sql, params)
        else:
            with self._db.transaction() as conn:
                conn.execute(sql, params)
        self._mirror(event)
        return event

    def _mirror(self, event: RawEvent) -> None:
        """Append the event to the JSONL mirror when enabled."""
        if self._mirror_path is None:
            return
        line = json.dumps(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": isoformat(event.timestamp),
                "actor": event.actor,
                "conversation_id": event.conversation_id,
                "content": event.content,
                "metadata": event.metadata,
                "source_event_ids": event.source_event_ids,
                "runtime_version": event.runtime_version,
            },
            ensure_ascii=False,
        )
        with self._mirror_lock:
            with self._mirror_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")

    def append_many(self, events: Iterable[dict[str, Any]]) -> list[RawEvent]:
        """Append several events inside one transaction.

        Args:
            events: Keyword-argument mappings accepted by :meth:`append`.

        Returns:
            The persisted events in input order.
        """
        written: list[RawEvent] = []
        with self._db.transaction() as conn:
            for spec in events:
                written.append(self.append(connection=conn, **spec))
        return written

    # ------------------------------------------------------------------ reading

    def get(self, event_id: str) -> RawEvent | None:
        """Return one event by identifier, or ``None``."""
        row = self._db.query_one("SELECT * FROM raw_events WHERE event_id = ?", (event_id,))
        return self._to_event(row)

    def get_many(self, event_ids: Sequence[str]) -> list[RawEvent]:
        """Return the events for ``event_ids`` that exist, in input order."""
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        rows = self._db.query(
            f"SELECT * FROM raw_events WHERE event_id IN ({placeholders})", tuple(event_ids)
        )
        index = {row["event_id"]: row for row in rows}
        return [self._to_event(index[eid]) for eid in event_ids if eid in index]

    def exists(self, event_id: str) -> bool:
        """Return whether an event with this identifier exists."""
        row = self._db.query_one("SELECT 1 AS present FROM raw_events WHERE event_id = ?", (event_id,))
        return row is not None

    def read(self, query: EventQuery | None = None) -> list[RawEvent]:
        """Read events matching ``query``.

        Args:
            query: Filter and paging options.

        Returns:
            Matching events ordered by time.
        """
        spec = query or EventQuery()
        clauses: list[str] = []
        params: list[Any] = []
        if spec.conversation_id:
            clauses.append("conversation_id = ?")
            params.append(spec.conversation_id)
        if spec.event_types:
            placeholders = ",".join("?" for _ in spec.event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(spec.event_types)
        if spec.since is not None:
            clauses.append("timestamp >= ?")
            params.append(isoformat(spec.since))
        if spec.until is not None:
            clauses.append("timestamp < ?")
            params.append(isoformat(spec.until))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if spec.newest_first else "ASC"
        sql = f"SELECT * FROM raw_events {where} ORDER BY timestamp {order}, rowid {order} LIMIT ?"
        params.append(max(1, int(spec.limit)))
        rows = self._db.query(sql, tuple(params))
        return [event for event in (self._to_event(row) for row in rows) if event is not None]

    def recent(self, limit: int = 20, conversation_id: str | None = None) -> list[RawEvent]:
        """Return the most recent events in chronological order."""
        events = self.read(
            EventQuery(conversation_id=conversation_id, limit=limit, newest_first=True)
        )
        return list(reversed(events))

    def last_of_types(self, event_types: Sequence[str], conversation_id: str | None = None) -> RawEvent | None:
        """Return the newest event among ``event_types``, or ``None``."""
        events = self.read(
            EventQuery(
                conversation_id=conversation_id,
                event_types=list(event_types),
                limit=1,
                newest_first=True,
            )
        )
        return events[0] if events else None

    def last_user_message(self, conversation_id: str | None = None) -> RawEvent | None:
        """Return the newest user message, or ``None``."""
        return self.last_of_types([EventType.USER_MESSAGE.value], conversation_id)

    def count(self, event_type: str | None = None) -> int:
        """Count events, optionally restricted to one type."""
        if event_type:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM raw_events WHERE event_type = ?", (event_type,)
            )
        else:
            row = self._db.query_one("SELECT COUNT(*) AS n FROM raw_events")
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _to_event(row: sqlite3.Row | None) -> RawEvent | None:
        """Convert a database row into a :class:`RawEvent`."""
        data = row_to_dict(row, "raw_events")
        if data is None:
            return None
        return RawEvent(
            event_id=data["event_id"],
            event_type=data["event_type"],
            timestamp=parse_datetime(data["timestamp"]) or utcnow(),
            actor=data["actor"],
            conversation_id=data.get("conversation_id"),
            content=data.get("content"),
            metadata=data.get("metadata_json") or {},
            source_event_ids=data.get("source_event_ids") or [],
            runtime_version=int(data.get("runtime_version") or 0),
            created_at=parse_datetime(data.get("created_at")),
        )
