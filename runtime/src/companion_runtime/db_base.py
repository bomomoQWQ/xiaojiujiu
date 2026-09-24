"""Backend-agnostic transaction plumbing shared by every store.

The Runtime is a single-writer system: one cognitive round is one transaction, and
that transaction is the unit of durability. That invariant, the hook machinery that
lets side effects (the raw-event JSONL mirror) fire *after* a commit, and the access
helpers are identical whether the rows live in SQLite or in PostgreSQL, so they live
here and each backend only supplies the primitives it actually differs in: how a
transaction starts, how a savepoint is named, and how a statement is executed.

Two things are deliberately *not* abstracted away:

* ``BEGIN IMMEDIATE`` (SQLite) and an advisory lock (PostgreSQL) both exist to make
  "one writer at a time" true. A backend that cannot offer that must say so instead
  of pretending, so :meth:`DatabaseBase.begin` takes ``immediate`` and the backend
  decides how to honour it.
* Hook ordering. A released savepoint hands its post-commit hooks to its parent and
  runs its own release hooks; a rolled-back level discards exactly its own. The
  JSONL mirror's correctness depends on that, so it is spelled out once, here, and
  the backends never see it.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

LOGGER = logging.getLogger("companion_runtime.db")


class ConflictError(Exception):
    """A statement violated a constraint the schema declares: the neutral conflict.

    The Runtime has one place where a constraint violation is a normal outcome
    rather than a failure: the deduplicating raw-event append, where a second
    writer inserting an ``event_id`` that already exists means "this message was
    already ingested". That case used to be recognised by catching
    :class:`sqlite3.IntegrityError`, a type PostgreSQL does not raise, so the same
    code silently stopped working the moment ``storage.dsn`` selected the other
    backend. This class is the backend-neutral spelling of that event: each
    backend translates its own exception into it at the storage boundary, and a
    caller writes one ``except`` clause that means the same thing on both.

    What it means:
        A duplicate primary/unique key, a missing foreign-key target, a
        ``NOT NULL`` or ``CHECK`` failure. It is *not* a lock or busy condition
        (SQLite's "database is locked", PostgreSQL's ``lock_timeout``) and not a
        statement error: those stay the backend's own exception, because the
        right response to them is different - wait and retry, or fix the SQL.

    What it does not promise:
        That the transaction which raised it is still usable. SQLite leaves the
        transaction open after the failing statement, so a caller may read the
        row that caused the conflict; PostgreSQL aborts the transaction, so the
        next statement in it fails with "current transaction is aborted" until it
        is rolled back. A recovery path written for both backends has to assume
        the stricter one - see :attr:`dialect` for which backend refused.

    Attributes:
        dialect: The backend that raised it (``"sqlite"``, ``"postgres"``).
        native: The backend's own exception, also reachable as ``__cause__``.
    """

    def __init__(
        self,
        *args: Any,
        dialect: str = "unknown",
        native: BaseException | None = None,
    ) -> None:
        """Build the error.

        Args:
            *args: Message arguments, passed through to :class:`Exception`
                unchanged so that ``error.args`` still holds what the backend
                said.
            dialect: Backend name, see :attr:`DatabaseBase.dialect`.
            native: The backend's own exception object.
        """
        super().__init__(*args)
        self.dialect = dialect
        self.native = native


@runtime_checkable
class DbCursor(Protocol):
    """The cursor surface the Runtime actually uses."""

    rowcount: int

    def fetchall(self) -> list[Any]:
        """Return every remaining row."""

    def fetchone(self) -> Any | None:
        """Return the next row, or ``None``."""


@runtime_checkable
class DbConnection(Protocol):
    """The connection surface a projection receives inside a transaction."""

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        """Execute one statement."""


class DatabaseBase:
    """Transaction frames, hooks and access helpers shared by all backends.

    Subclasses provide :meth:`_begin`, :meth:`_commit`, :meth:`_rollback`,
    :meth:`_savepoint`, :meth:`_release`, :meth:`_rollback_to`, :meth:`_execute`,
    :meth:`column_names`, :meth:`close` and :meth:`migrate`, and set
    :attr:`dialect`. They also translate their own constraint violation into
    :class:`ConflictError` at the statement boundary (see
    :func:`~companion_runtime.db.Database`'s connection subclass and
    :class:`~companion_runtime.db_postgres.TranslatingConnection`).

    A backend that cannot run the durability commands in
    :mod:`companion_runtime.maintenance` leaves
    :attr:`supports_durability_commands` at its default, which is ``False``: the
    commands then refuse up front instead of failing halfway through.
    """

    #: Short backend name used in logs and health output.
    dialect: str = "unknown"

    #: Whether this backend implements the durability commands of
    #: :mod:`companion_runtime.maintenance` - ``checkpoint``, ``verify``,
    #: ``backup`` and ``restore`` - which are built on SQLite machinery
    #: (``PRAGMA``, the ``-wal``/``-shm`` sidecar files, ``VACUUM INTO`` and
    #: file-level copies).
    #:
    #: The default is ``False`` on purpose: a backend that has not said it can run
    #: them gets a clear refusal (``maintenance.DurabilityUnsupported``) rather
    #: than a confusing dialect error halfway through a backup, and never a silent
    #: no-op that would leave an operator believing a snapshot exists.
    #: :class:`~companion_runtime.db.Database` sets it to ``True``; a PostgreSQL
    #: implementation would set it to ``True`` here (or override the commands) and
    #: live beside :mod:`companion_runtime.maintenance`.
    supports_durability_commands: bool = False

    def __init__(self, *, busy_timeout_ms: int = 5000) -> None:
        """Prepare the shared state.

        Args:
            busy_timeout_ms: How long a blocked writer waits before giving up.
        """
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()
        self._depth = 0
        #: Per-thread transaction depth, readable *without* taking ``self._lock``.
        #:
        #: ``transaction`` holds ``self._lock`` for the whole block (one connection, one
        #: writer), so :meth:`in_transaction` blocks when another thread is inside one.
        #: A caller deciding whether it *may* take another lock must not block on the
        #: database to find out - that is how a lock-order inversion deadlocks - so the
        #: depth is mirrored here in thread-local storage for that decision only.
        self._txn_local = threading.local()
        self._conn: Any = None
        #: One callback frame per open :meth:`transaction` level, outermost first.
        #: Frames are consumed by COMMIT and thrown away by ROLLBACK, which is what
        #: keeps a post-commit hook from ever observing a row that was rolled back.
        self._txn_frames: list[list[Callable[[], None]]] = []
        #: Per-level :meth:`on_rollback` callbacks: they run when their level (or an
        #: enclosing one) rolls back, and are dropped unrun when it commits.
        self._rollback_frames: list[list[Callable[[], None]]] = []
        #: Per-level :meth:`on_release` callbacks: they run when their level ends
        #: well by handing its work to the parent (a savepoint that released),
        #: which is neither a commit of the whole transaction nor a rollback.
        self._release_frames: list[list[Callable[[], None]]] = []

    # ------------------------------------------------------- backend primitives

    def _begin(self, *, immediate: bool) -> None:
        """Start the outermost transaction, taking the write lock when asked."""
        raise NotImplementedError

    def _commit(self) -> None:
        """Commit the outermost transaction."""
        raise NotImplementedError

    def _rollback(self) -> None:
        """Roll back the outermost transaction."""
        raise NotImplementedError

    def _savepoint(self, name: str) -> None:
        """Open a nested level."""
        raise NotImplementedError

    def _release(self, name: str) -> None:
        """Close a nested level that ended well."""
        raise NotImplementedError

    def _rollback_to(self, name: str) -> None:
        """Discard a nested level."""
        raise NotImplementedError

    def _execute(self, sql: str, params: Sequence[Any] | dict[str, Any]) -> Any:
        """Run one statement on the shared connection."""
        raise NotImplementedError

    def column_names(self, table: str) -> set[str]:
        """Return the column names of ``table``."""
        raise NotImplementedError

    def migrate(self) -> int:
        """Create the schema and return its version."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the underlying connection."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """Return operator-facing facts about the store (never secrets)."""
        return {
            "dialect": self.dialect,
            "durability_commands": self.supports_durability_commands,
        }

    # ------------------------------------------------------------------ context

    @contextmanager
    def transaction(self, immediate: bool = True):
        """Run a block inside a transaction, nesting via savepoints.

        Hooks registered with :meth:`post_commit` inside the block run once, in
        registration order, after the outermost transaction has committed. Hooks
        registered with :meth:`on_rollback` run only when a transaction actually
        rolls back, and :meth:`on_release` hooks run when a savepoint releases
        into its parent. Each kind is buffered per level: a rollback - of the
        whole transaction or of one savepoint - discards the hooks registered
        inside the part that was rolled back, so a hook never observes a row that
        is no longer there, while a level that ended well keeps its hooks queued
        for the commit. Hook failures are logged, never raised: the commit itself
        has already happened and must not be undone.

        Args:
            immediate: Acquire the write lock up front (``BEGIN IMMEDIATE`` on
                SQLite, an advisory lock on PostgreSQL), which is what makes
                outbox claiming race-free.

        Yields:
            The connection a projection executes against.
        """
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._begin(immediate=immediate)
            else:
                self._savepoint(f"sp_{self._depth}")
            self._depth += 1
            # Mirror the depth for the lock-free query below. Only the thread holding
            # the lock can be inside, so its mirror is the whole truth; every other
            # thread reads zero without touching the lock.
            self._txn_local.depth = self._depth
            self._txn_frames.append([])
            self._rollback_frames.append([])
            self._release_frames.append([])
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                self._txn_local.depth = self._depth
                self._txn_frames.pop()
                rollbacks = self._rollback_frames.pop()
                self._release_frames.pop()
                if outermost:
                    self._rollback()
                else:
                    self._rollback_to(f"sp_{self._depth}")
                    self._release(f"sp_{self._depth}")
                self._run_hooks(rollbacks)
                raise
            else:
                self._depth -= 1
                self._txn_local.depth = self._depth
                hooks = self._txn_frames.pop()
                rollbacks = self._rollback_frames.pop()
                releases = self._release_frames.pop()
                try:
                    if outermost:
                        self._commit()
                    else:
                        self._release(f"sp_{self._depth}")
                except BaseException:
                    # The statements did not commit, so every hook registered
                    # under this transaction is dropped rather than run.
                    LOGGER.warning(
                        "Transaction commit failed; discarding %d post-commit hook(s)",
                        len(hooks) + sum(len(frame) for frame in self._txn_frames),
                        exc_info=True,
                    )
                    self._txn_frames.clear()
                    self._rollback_frames.clear()
                    self._release_frames.clear()
                    raise
                if outermost:
                    self._run_hooks(hooks)
                else:
                    # A released savepoint is part of its parent transaction:
                    # post-commit hooks wait for the outermost commit, and rollback
                    # hooks must still fire if an enclosing level rolls back. The
                    # release hooks are this level's own result and run now.
                    self._txn_frames[-1].extend(hooks)
                    self._rollback_frames[-1].extend(rollbacks)
                    self._run_hooks(releases)

    def post_commit(self, callback: Callable[[], None]) -> None:
        """Register ``callback`` to run after the current transaction commits.

        Outside a transaction the callback runs immediately, because the
        autocommit write it documents is already durable. Inside one it is
        buffered on the innermost level: it runs after the outermost ``COMMIT``
        and is dropped if that part of the transaction rolls back. This is what
        makes an external mirror (see
        :mod:`companion_runtime.eventlog`) unable to record an event that the
        database itself never kept.

        Args:
            callback: Zero-argument callable.
        """
        with self._lock:
            if self._depth:
                self._txn_frames[self._depth - 1].append(callback)
            else:
                self._run_hooks([callback])

    def on_rollback(self, callback: Callable[[], None]) -> None:
        """Register ``callback`` to run when its transaction level is discarded.

        Used by buffered side effects that must drop the state belonging to a
        level that rolled back. It runs after the rollback and, like a failing
        post-commit hook, cannot fail the caller. Outside a transaction there is
        nothing to discard, so the callback is not kept.

        Args:
            callback: Zero-argument callable.
        """
        with self._lock:
            if self._depth:
                self._rollback_frames[self._depth - 1].append(callback)

    def on_release(self, callback: Callable[[], None]) -> None:
        """Register ``callback`` to run when its savepoint releases into its parent.

        This is the third outcome a level can have - neither a commit nor a
        rollback - and buffered state needs it to distinguish "adopt this work"
        from "throw it away". Outside a transaction there is no level to release,
        so the callback is not kept.

        Args:
            callback: Zero-argument callable.
        """
        with self._lock:
            if self._depth:
                self._release_frames[self._depth - 1].append(callback)

    @staticmethod
    def _run_hooks(hooks: Sequence[Callable[[], None]]) -> None:
        """Run hooks, logging and swallowing any failure.

        The transaction has already committed by the time a post-commit hook runs,
        so a failing hook must not turn valid database state into a failed call.
        The same tolerance is applied to rollback and release hooks, which share
        this runner.
        """
        for hook in hooks:
            try:
                hook()
            except Exception:
                LOGGER.warning("Post-commit hook failed", exc_info=True)

    @contextmanager
    def read(self):
        """Run read-only statements under the connection lock."""
        with self._lock:
            yield self._conn

    # ------------------------------------------------------------------- access

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        """Execute a statement and return the cursor."""
        with self._lock:
            return self._execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[Any]:
        """Execute a query and materialise all rows."""
        with self._lock:
            return list(self._execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any | None:
        """Execute a query and return the first row, or ``None``."""
        with self._lock:
            return self._execute(sql, params).fetchone()

    def in_transaction(self) -> bool:
        """Return whether this thread is currently inside :meth:`transaction`."""
        with self._lock:
            return self._depth > 0

    def in_transaction_nowait(self) -> bool:
        """Return whether *this* thread is inside a transaction, without locking.

        :meth:`in_transaction` takes the connection lock, which ``transaction`` holds
        for its whole block - so asking it from a second thread blocks until the first
        thread's transaction ends. That is fine for inspection and fatal for a guard:
        a caller deciding whether it may take *another* lock must not wait on the
        database to find out, because the thread inside the transaction may be waiting
        for exactly that other lock. This query reads a thread-local mirror instead, so
        it never blocks and never lies about the calling thread.
        """
        return getattr(self._txn_local, "depth", 0) > 0

    def transaction_depth(self) -> int:
        """Return the current nesting depth: ``0`` outside any transaction."""
        with self._lock:
            return self._depth
