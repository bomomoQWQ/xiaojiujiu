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
    :attr:`dialect`.
    """

    #: Short backend name used in logs and health output.
    dialect: str = "unknown"

    def __init__(self, *, busy_timeout_ms: int = 5000) -> None:
        """Prepare the shared state.

        Args:
            busy_timeout_ms: How long a blocked writer waits before giving up.
        """
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()
        self._depth = 0
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
        return {"dialect": self.dialect}

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
            self._txn_frames.append([])
            self._rollback_frames.append([])
            self._release_frames.append([])
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
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

    def transaction_depth(self) -> int:
        """Return the current nesting depth: ``0`` outside any transaction."""
        with self._lock:
            return self._depth
