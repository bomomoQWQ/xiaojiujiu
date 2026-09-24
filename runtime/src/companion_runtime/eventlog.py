"""Append-only raw event log.

This is the historical root of the Runtime. Nothing in this module ever issues an
``UPDATE`` or ``DELETE``: raw events are the one thing the whole system treats as
ground truth. Semantic layers above it may be re-estimated, but the bytes written
here stay untouched.

An optional JSONL mirror is written for cheap external inspection and for
disaster recovery if the SQLite projection is ever rebuilt. The mirror is
deliberately *post-commit*: a line appears only after the transaction that holds
the row has committed, and a mirror IO failure is logged rather than allowed to
invalidate a commit that already succeeded.
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


@dataclass(slots=True)
class _MirrorFrame:
    """Mirror state for one open transaction level.

    A savepoint that releases cleanly hands its frame to its parent
    (:meth:`EventLog._release_frame`) instead of writing, so the mirror is only
    ever written once, at the outermost ``COMMIT``, when every line in it is
    known to have survived. ``events`` is ordered by global append sequence, which
    is what keeps that single write in append order even though a batch may queue
    at several levels.
    """

    events: dict[int, RawEvent]


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
        #: Queued mirror lines, keyed by the transaction frame that owns them. A
        #: frame is resolved by the outermost commit (written), by a rollback (its
        #: own lines dropped) or by a released savepoint (handed to its parent).
        self._mirror_frames: dict[int, _MirrorFrame] = {}
        #: Monotonic append counter; mirror order follows it across levels.
        self._mirror_seq = 0
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
            The persisted :class:`RawEvent`. When a mirror is configured the
            event also reaches it, but only after the transaction that carries
            the row has committed.
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
        # The mirror is a side effect outside SQLite: it is recorded only once the
        # transaction that owns the row has committed, and a mirror failure is
        # logged instead of propagated, so it can never undo a valid commit.
        self._mirror_after_commit(event)
        return event

    def _mirror(self, events: Iterable[RawEvent]) -> None:
        """Append ``events`` to the JSONL mirror, one line per event, in order."""
        if self._mirror_path is None:
            return
        lines = "".join(f"{self._encode_event(event)}\n" for event in events)
        if not lines:
            return
        with self._mirror_lock:
            with self._mirror_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(lines)

    @staticmethod
    def _encode_event(event: RawEvent) -> str:
        """Render one raw event as a single JSONL line (no trailing newline)."""
        return json.dumps(
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

    def _mirror_after_commit(self, event: RawEvent) -> None:
        """Queue ``event`` for the mirror and flush it once its commit landed.

        Outside a transaction the row is already committed when this runs, so the
        mirror is written straight away and an append costs no added latency.
        Inside one the line waits for the outermost ``COMMIT``: a rollback - of
        the transaction or of a single savepoint - drops exactly the lines queued
        under the level that was discarded, and a savepoint that releases cleanly
        moves its lines into its parent instead of writing them early.
        """
        if self._mirror_path is None:
            return
        depth = self._db.transaction_depth()
        if depth == 0:
            # Already committed by autocommit, so it is mirrored right away; the
            # write still cannot fail the append.
            self._prune_frames()
            self._write_mirror_or_warn([event], "1 event")
            return
        with self._mirror_lock:
            frame = self._mirror_frames.get(depth)
            if frame is None:
                frame = _MirrorFrame(events={})
                self._mirror_frames[depth] = frame
            # One flush is queued per frame on its first event and picks up every
            # later event of the batch, which is what keeps a batch to a single
            # mirror write.
            already_scheduled = bool(frame.events)
            frame.events[self._next_mirror_seq()] = event
        if already_scheduled:
            return
        # Buffered state and hooks are registered together; every hook closes over
        # its own frame, so a level resolves exactly the lines it owns: the flush
        # writes them at the outermost commit, the rollback hook drops them if the
        # rows go away, and the release hook hands them up to the parent savepoint.
        self._db.post_commit(lambda: self._flush_mirror(frame))
        self._db.on_rollback(lambda: self._discard_frame(frame))
        self._db.on_release(lambda: self._release_frame(frame))

    def _flush_mirror(self, frame: _MirrorFrame) -> None:
        """Write everything still queued once the outermost commit has landed.

        Events are written in append order and only once, which is what keeps the
        mirror a faithful prefix of the event log. A frame can own more than one
        entry when savepoints handed their lines up to it.
        """
        with self._mirror_lock:
            entries = self._collect_frame(frame)
        if entries:
            self._write_mirror_or_warn(
                [event for _seq, event in entries], f"{len(entries)} event(s)"
            )

    def _release_frame(self, frame: _MirrorFrame) -> None:
        """Hand a released savepoint's lines to its parent level.

        A released savepoint is neither a commit nor a rollback: its rows now
        belong to the enclosing transaction, so its lines must commit or roll back
        with the parent instead of being written early or dropped.

        The parent level is found by walking one depth up, and that lookup used to be
        the whole story - which lost every line in this shape: a savepoint appends the
        *only* event of its transaction, so the enclosing level has never created a
        frame to adopt it, ``_parent_frame`` answers ``None`` and the lines were dropped.
        It is not a corner case: a user message reaches the log two levels deep, so the
        mirror was missing every user message, every proactive send and every assistant
        message while the database had them (the simulation counted 320 mirrored events
        against 393 stored ones). An enclosing level with no frame yet now gets one, so
        the lines wait for the commit that will actually make them durable. Only a
        release with no enclosing transaction at all - which the database layer does not
        produce, since releases are savepoint events - writes straight away, and it
        writes rather than drops.
        """
        with self._mirror_lock:
            # Resolve the parent *before* unregistering this frame: the parent is
            # found by looking at the depths still holding this one.
            parent = self._parent_frame(frame)
            entries = self._collect_frame(frame)
            if not entries:
                return
            if parent is None:
                depth = self._db.transaction_depth()
                if depth > 0:
                    # The enclosing transaction has not logged an event of its own yet,
                    # so nothing is registered at its depth. Create its frame now: the
                    # lines belong to it and it is the level that will commit.
                    parent = _MirrorFrame(events={})
                    self._mirror_frames[depth] = parent
        if parent is None:
            self._write_mirror_or_warn(
                [event for _seq, event in entries], f"{len(entries)} event(s)"
            )
            return
        for event_seq, event in entries:
            parent.events[event_seq] = event
        # The parent needs a flush of its own for the adopted lines: its earlier
        # one has already run or carries only its own events.
        self._db.post_commit(lambda: self._flush_mirror(parent))

    def _discard_frame(self, frame: _MirrorFrame) -> None:
        """Drop a level's lines after its rows were rolled back.

        Everything queued at that level or deeper goes with it: those savepoints
        were inside the rolled-back transaction, so none of their lines survive,
        and an orphaned frame from an earlier failure must not be resurrected
        either.
        """
        with self._mirror_lock:
            depths = self._frame_depths(frame)
            if not depths:
                return
            for depth in [d for d in self._mirror_frames if d >= depths[0]]:
                del self._mirror_frames[depth]

    def _collect_frame(self, frame: _MirrorFrame) -> list[tuple[int, RawEvent]]:
        """Remove every entry belonging to ``frame`` and return them in order."""
        collected: list[tuple[int, RawEvent]] = []
        for depth in self._frame_depths(frame):
            collected.extend(self._mirror_frames.pop(depth).events.items())
        collected.sort()
        return collected

    def _frame_depths(self, frame: _MirrorFrame) -> list[int]:
        """Return the registered depths holding ``frame``, shallowest first."""
        return sorted(
            depth for depth, candidate in self._mirror_frames.items() if candidate is frame
        )

    def _next_mirror_seq(self) -> int:
        """Return the next global append sequence number."""
        self._mirror_seq += 1
        return self._mirror_seq

    def _parent_frame(self, frame: _MirrorFrame) -> _MirrorFrame | None:
        """Return the frame of the immediately enclosing transaction level."""
        depths = self._frame_depths(frame)
        if not depths or depths[0] <= 1:
            return None
        return self._mirror_frames.get(depths[0] - 1)

    def _prune_frames(self) -> None:
        """Drop queued lines whose transaction can no longer resolve them.

        A transaction abandoned without commit or rollback (a crash path, or a
        connection closed mid-round) leaves its frames behind; the next append
        starts from a clean depth map instead of resurrecting them.
        """
        with self._mirror_lock:
            if self._db.transaction_depth() == 0 and self._mirror_frames:
                LOGGER.warning(
                    "Discarding %d stale JSONL mirror frame(s) from an unresolvable "
                    "transaction",
                    len(self._mirror_frames),
                )
                self._mirror_frames.clear()

    def _write_mirror_or_warn(self, events: Sequence[RawEvent], count_label: str) -> None:
        """Write ``events`` to the mirror, downgrading IO failures to a warning.

        A mirror failure must never invalidate database state: by the time a
        buffered batch is written, the transaction that owns those rows has
        committed, so the failure is reported and dropped rather than raised.
        """
        try:
            self._mirror(events)
        except OSError:
            LOGGER.warning(
                "Could not write %s to the JSONL mirror %s; the database commit "
                "stands and no mirror line was recorded",
                count_label,
                self._mirror_path,
                exc_info=True,
            )

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
