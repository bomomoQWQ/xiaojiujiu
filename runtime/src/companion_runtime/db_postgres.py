"""PostgreSQL storage layer: the same schema and the same transaction semantics.

The SQLite backend (:mod:`companion_runtime.db`) is the reference. This module
re-uses its :data:`~companion_runtime.db.SCHEMA_STATEMENTS`, its added-column list
and its :data:`~companion_runtime.db.SCHEMA_VERSION` unchanged, and supplies only
the primitives that genuinely differ: how a transaction starts, how "one writer at
a time" is enforced, and how a statement reaches the server. The transaction
template, the three hook kinds and the access helpers stay in
:class:`~companion_runtime.db_base.DatabaseBase`, so a nested transaction, a
released savepoint and a discarded one behave *identically* on both backends.

Three choices are deliberate and worth stating up front:

* **The column types are the shared schema's** (``TEXT``/``REAL``/``INTEGER``),
  not PostgreSQL's richer ones. The Runtime stores every instant as ISO-8601 text
  - which still sorts chronologically as text - and every JSON blob as text it
  parses in Python, so ``timestamptz``/``jsonb``/``double precision`` would buy
  nothing and would make the two backends differ in ways the rest of the code
  cannot see.
* **Placeholders stay ``?``.** More than a hundred statements in this codebase are
  written for SQLite. Instead of rewriting them, every statement - including the
  one the transaction template hands to a projection - passes through
  :func:`translate_placeholders`, a real lexer, which turns ``?`` into ``%s`` and
  escapes a literal ``%``.
* **"One writer at a time" is an advisory lock.** PostgreSQL has no
  ``BEGIN IMMEDIATE``, so :meth:`PostgresDatabase._begin` takes the
  transaction-scoped lock described at :data:`WRITER_LOCK_KEY`. It is the
  PostgreSQL spelling of the invariant SQLite gets from its write lock, and it is
  what keeps outbox claiming and the reducer's read-modify-write of the projection
  tables race-free.

Known differences this module cannot remove:

* PostgreSQL ``REAL`` is single precision and ``INTEGER`` is 32-bit, where SQLite
  stores both in 8 bytes. The shared DDL names those types and the Runtime's own
  values (probabilities, counters, versions) fit comfortably in both, so widening
  them to ``DOUBLE PRECISION``/``BIGINT`` here would silently fork the schema.
* Two more SQLite spellings are handled by :func:`translate_placeholders` instead
  of at the call sites, so the SQLite backend keeps running the exact text it
  always has: ``LIMIT -1`` (SQLite's "no limit", which PostgreSQL rejects) is
  dropped, and ``rowid`` (the implicit insertion-order key) becomes ``ctid``,
  PostgreSQL's physical row locator. ``raw_events`` is append-only and the Runtime
  admits one writer at a time, so ``ctid`` is the same ordering ``rowid`` gave.
* A constraint violation is translated into the backend-neutral
  :class:`~companion_runtime.db_base.ConflictError` by
  :meth:`TranslatingConnection.execute`, which is the one funnel both
  ``PostgresDatabase.execute()`` and a projection's own ``conn.execute()`` go
  through. The psycopg exception stays reachable as the error's ``__cause__``.
* The ``PRAGMA``/``-wal``/online-backup helpers in
  :mod:`companion_runtime.maintenance` are SQLite by construction and are out of
  scope for this backend: PostgreSQL is made durable by the server's own tooling
  (WAL archiving, ``pg_basebackup``, ``pg_dump``, replication), not by a
  ``VACUUM INTO`` of a single file. This backend therefore declares
  :attr:`PostgresDatabase.supports_durability_commands` as ``False``, and those
  commands refuse at their entrance with
  :class:`~companion_runtime.maintenance.DurabilityUnsupported` instead of failing
  halfway through with a syntax error. A PostgreSQL implementation would set that
  flag to ``True`` and live beside the SQLite one.
* The rest of the surface is portable as it stands. There is no ``INSERT OR
  IGNORE``/``INSERT OR REPLACE`` left anywhere in the Runtime - the seed insert in
  ``RuntimeProjection.ensure`` spells it ``ON CONFLICT ... DO NOTHING``, which both
  engines understand - so every statement this backend has to run is either shared
  as written or covered by the rewrites above.

One thing this module cannot paper over, and callers must know: PostgreSQL aborts
a transaction when a statement fails, so after a caught
:class:`~companion_runtime.db_base.ConflictError` the *next* statement in that
transaction fails with "current transaction is aborted" until it is rolled back
(SQLite, by contrast, keeps the transaction open). A recovery path that reads the
row which caused the conflict therefore has to run after a rollback on this
backend - see :attr:`ConflictError.dialect`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Sequence

from .db import Database as _SqliteDatabase
from .db import SCHEMA_STATEMENTS, SCHEMA_VERSION
from .db_base import ConflictError, DatabaseBase
from .utility import isoformat

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # the driver is an optional dependency of the package
    psycopg = None
    dict_row = None

LOGGER = logging.getLogger("companion_runtime.db")

#: Whether the psycopg driver could be imported. The placeholder lexer, the schema
#: portability checks and the conformance tests do not need it; opening a
#: connection does, and fails with a clear message instead of an ImportError.
PSYCOPG_AVAILABLE: bool = psycopg is not None

#: The psycopg exception classes a constraint violation arrives as, or an empty
#: tuple when the driver is missing. Resolved once, at import, so the translation
#: in :meth:`TranslatingConnection.execute` never evaluates ``psycopg.errors`` on a
#: machine that has no driver (an empty ``except`` tuple simply never matches).
#: ``IntegrityError`` is the parent of ``UniqueViolation``, ``ForeignKeyViolation``,
#: ``NotNullViolation``, ``CheckViolation`` and ``ExclusionViolation`` - exactly the
#: set of failures the Runtime treats as a conflict rather than a fault. Lock
#: timeouts and statement errors are deliberately *not* in it.
CONFLICT_ERRORS: tuple[type[BaseException], ...] = (
    () if psycopg is None else (psycopg.errors.IntegrityError,)
)

#: The transaction-scoped advisory lock key that makes the Runtime single-writer.
#:
#: ``0x52554E54494D45`` is ASCII ``RUNTIME``: a fixed, arbitrary-but-stable key
#: that every Runtime writer agrees on. What it protects is not one table but the
#: whole database, because the Runtime treats one cognitive round as one
#: transaction: a reducer's read-modify-write of ``runtime_state``, the projection
#: updates that follow it, and the outbox claim that must not go to two workers
#: all depend on no other writer being inside that transaction at the same time.
#: SQLite gets this from ``BEGIN IMMEDIATE``; PostgreSQL has no equivalent, so
#: :meth:`PostgresDatabase._begin` asks for this lock instead.
#:
#: Two properties matter:
#:
#: * It is *transaction*-scoped (``pg_advisory_xact_lock``), so the server releases
#:   it on ``COMMIT``, on ``ROLLBACK``, and on session death. There is no unlock
#:   call to forget and no lock to leak when a process is killed.
#: * It is taken in ``_begin``, which the transaction template only calls at the
#:   outermost level, *before* any savepoint exists. That placement is load
#:   bearing: PostgreSQL releases a transaction-level advisory lock taken after a
#:   savepoint when that savepoint is rolled back, so taking it inside a nested
#:   level would let ``ROLLBACK TO SAVEPOINT`` silently drop the writer lock in
#:   the middle of the transaction.
#:
#: Operators can see who holds it with::
#:
#:     SELECT * FROM pg_locks
#:     WHERE locktype = 'advisory' AND objsubid = 1
#:       AND classid::bigint = 5395790 AND objid::bigint = 1414090053;
WRITER_LOCK_KEY: int = 0x52554E54494D45

#: Matches the two SQLite-only spellings this backend rewrites. Both are matched
#: only in ordinary SQL - never inside a literal, an identifier or a comment.
#:
#: * ``LIMIT -1`` is SQLite's "no limit". PostgreSQL rejects a negative limit, and
#:   since a missing ``LIMIT`` already means "all rows" there, the clause is
#:   dropped. SQLite requires ``LIMIT`` before ``OFFSET``, so the portable
#:   spelling has to be a rewrite rather than an omitted clause in the source.
#: * ``rowid`` is SQLite's implicit insertion-order key. PostgreSQL's equivalent
#:   is ``ctid``, the physical row locator: it orders append-only tables by
#:   insertion, and the single-writer lock means no other writer can interleave
#:   tuples into the same page. (On a table whose rows are *rewritten* - ``outbox``
#:   leases, for instance - ``ctid`` moves with the update, which is why the
#:   outbox's own tiebreaker is its primary key instead.)
_DIALECT_REWRITES = re.compile(r"(?i)\b(?:limit\s+-1(?![0-9a-z_])|rowid\b)")

#: Matches ``password=...`` in a libpq-style connection string.
_PASSWORD_ASSIGNMENT = re.compile(r"(?i)(password\s*=\s*)\S+")

#: Matches the ``user:password@`` part of a connection URI.
_URI_PASSWORD = re.compile(r"(?i)(://[^:/@\s]+:)([^@/\s]+)(@)")


def redact(text: Any) -> str:
    """Mask anything password-shaped in a diagnostic string.

    libpq's own error messages do not echo the connection string (measured
    against psycopg 3.3 / PostgreSQL 18: a wrong password, a refused connection
    and an unknown database all report the failure without the password), but
    ``describe()`` is a health surface and a DSN is a secret, so the message is
    scrubbed before it can be returned or logged.

    Args:
        text: Raw message, usually ``str(exception)``.

    Returns:
        The message with any URI or ``password=`` credential replaced by ``***``.
    """
    scrubbed = _URI_PASSWORD.sub(r"\1***\3", str(text))
    return _PASSWORD_ASSIGNMENT.sub(r"\1***", scrubbed)


def _escape_percent(text: str, with_params: bool) -> str:
    """Double the ``%`` signs of a chunk that is passed through verbatim."""
    return text.replace("%", "%%") if with_params else text


def _scan_quoted(sql: str, start: int, quote: str) -> int:
    """Return the index just past the closing ``quote`` of a literal at ``start``.

    A doubled quote (``''`` inside a single-quoted string, ``""`` inside a quoted
    identifier) is an escaped quote, not the end of the token. An unterminated
    token swallows the rest of the statement, which is what the server would do
    with it too.
    """
    size = len(sql)
    index = start + 1
    while index < size:
        if sql[index] == quote:
            if index + 1 < size and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return size


def _scan_block_comment(sql: str, start: int) -> int:
    """Return the index just past the ``*/`` that closes a comment at ``start``.

    PostgreSQL block comments nest, so the depth is counted rather than searching
    for the first ``*/``.
    """
    size = len(sql)
    index = start
    depth = 0
    while index < size:
        if sql.startswith("/*", index):
            depth += 1
            index += 2
            continue
        if sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
            continue
        index += 1
    return size


def translate_placeholders(sql: str, *, with_params: bool = True) -> str:
    """Rewrite SQLite-flavoured SQL into what psycopg and PostgreSQL accept.

    This is a lexer, not a search-and-replace, because every substitution it makes
    is only correct when the surrounding token is known:

    * ``?`` outside a token becomes ``%s``. Inside a string literal, inside a
      quoted identifier, inside a ``--`` line comment or inside a (nestable)
      ``/* */`` block comment it is ordinary text and is left alone.
    * A literal ``%`` becomes ``%%`` whenever parameters are supplied. psycopg
      does not parse SQL: it scans the whole statement for ``%``-sequences, so a
      percent sign inside a *string literal* or a *comment* has to be escaped too,
      and an unescaped one is a hard error (``only '%s', '%b', '%t' are allowed as
      placeholders``). With no parameters psycopg sends the statement verbatim and
      no escaping is wanted.
    * The two SQLite-only spellings described at :data:`_DIALECT_REWRITES` are
      rewritten in ordinary SQL only: ``LIMIT -1`` is dropped and ``rowid``
      becomes ``ctid``.

    Dollar-quoted strings (``$tag$ ... $tag$``) are not understood: the Runtime's
    SQL never contains one. Teach this lexer about them before adding one, because
    a ``%`` inside such a body would be escaped. PostgreSQL's ``?`` operators
    (``jsonb`` existence) are not understood either - the schema deliberately keeps
    JSON in ``TEXT``, so no statement uses one.

    Args:
        sql: Statement written with ``?`` placeholders, as SQLite runs it.
        with_params: Whether the statement will be executed with parameters, which
            is exactly when a literal ``%`` must be escaped.

    Returns:
        The statement as psycopg should receive it.
    """
    if not sql:
        return sql
    if (
        _DIALECT_REWRITES.search(sql) is None
        and "?" not in sql
        and (not with_params or "%" not in sql)
    ):
        return sql
    parts: list[str] = []
    index = 0
    size = len(sql)
    while index < size:
        char = sql[index]
        if char in {"'", '"'}:
            end = _scan_quoted(sql, index, char)
            parts.append(_escape_percent(sql[index:end], with_params))
            index = end
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = size if end < 0 else end
            parts.append(_escape_percent(sql[index:end], with_params))
            index = end
        elif sql.startswith("/*", index):
            end = _scan_block_comment(sql, index)
            parts.append(_escape_percent(sql[index:end], with_params))
            index = end
        elif char == "?":
            parts.append("%s")
            index += 1
        elif char == "%":
            parts.append("%%" if with_params else "%")
            index += 1
        elif char in "lLrR" and (rewrite := _DIALECT_REWRITES.match(sql, index)) is not None:
            if char in "rR":
                parts.append("ctid")
            # ``LIMIT -1`` appends nothing: on PostgreSQL its absence *is* the
            # same statement, "return every row".
            index = rewrite.end()
        else:
            parts.append(char)
            index += 1
    return "".join(parts)


def quote_identifier(name: str) -> str:
    """Quote ``name`` as a PostgreSQL identifier (savepoint names, mostly).

    The transaction template generates savepoint names itself (``sp_0``,
    ``sp_1``, ...), so this is not a defence against a caller's SQL; it keeps the
    names from depending on PostgreSQL's case folding, which would otherwise let
    ``SAVEPOINT SP_0`` and ``ROLLBACK TO SAVEPOINT sp_0`` refer to different
    levels.
    """
    return '"' + str(name).replace('"', '""') + '"'


class TranslatingConnection:
    """The connection the transaction template hands to a projection.

    ``DatabaseBase.transaction()`` and :meth:`DatabaseBase.read` yield the raw
    connection, and every projection, reducer and API call site writes SQLite SQL
    against it (``conn.execute("... WHERE id = ?", (...))``). This adapter is
    therefore the single funnel where that SQL is translated, so the same
    statement text runs on both backends:

    * ``execute`` accepts ``?`` placeholders, like ``sqlite3.Connection.execute``.
    * Statement failures that mean *the same thing* on both engines are translated:
      a constraint violation (``psycopg.errors.IntegrityError`` and its subclasses)
      becomes :class:`~companion_runtime.db_base.ConflictError`, the neutral type a
      caller can catch without knowing which backend is underneath. Everything
      else - a lock timeout, a syntax error, a missing table - is re-raised as
      psycopg raised it, because the right response to those differs per backend
      and translating them would hide that.
    * Everything else (``cursor()``, ``close()``, ``info``, ...) is delegated to
      psycopg unchanged, so the native ``%s`` API stays reachable for SQL that is
      deliberately PostgreSQL-only. Such a call bypasses the translation.
    """

    def __init__(self, raw: Any) -> None:
        """Wrap an open psycopg connection."""
        self._raw = raw

    @property
    def raw(self) -> Any:
        """Return the underlying psycopg connection."""
        return self._raw

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        """Execute one SQLite-flavoured statement and return its cursor.

        Args:
            sql: Statement written with ``?`` placeholders.
            params: Positional (or named) parameters. Empty means "no
                parameters", which is not the same as an empty tuple to psycopg:
                with no parameters psycopg sends the text verbatim and never
                interprets ``%``, which is what lets DDL and ``LIKE '%x%'``
                literals through untouched.

        Returns:
            The psycopg cursor, whose rows are dicts (``row_factory=dict_row``).

        Raises:
            companion_runtime.db_base.ConflictError: If the statement violated a
                constraint. The psycopg error is kept as ``__cause__``. Note that
                PostgreSQL has already aborted the surrounding transaction at that
                point, so a caller that wants to continue must roll back (or use a
                savepoint) first.
        """
        statement = translate_placeholders(sql, with_params=bool(params))
        try:
            return self._raw.execute(statement, params or None)
        except CONFLICT_ERRORS as error:
            raise ConflictError(
                str(error), dialect="postgres", native=error
            ) from error

    def close(self) -> None:
        """Close the underlying connection."""
        self._raw.close()

    def __getattr__(self, name: str) -> Any:
        """Delegate anything else to the psycopg connection."""
        return getattr(self._raw, name)


class PostgresUnavailable(RuntimeError):
    """Raised when the PostgreSQL backend cannot be reached or configured."""


class PostgresDatabase(DatabaseBase):
    """A thread-safe PostgreSQL backend with the Runtime's transaction contract.

    Access is serialised through the base class's re-entrant lock, exactly like
    the SQLite backend, and the connection is opened lazily on first use so that a
    store can be constructed (and :meth:`describe` called) while the server is
    down. Cross-process serialisation is the advisory lock at
    :data:`WRITER_LOCK_KEY`; SQLite's per-statement ``busy_timeout`` becomes
    PostgreSQL's ``lock_timeout``, which - measured on PostgreSQL 18 - also bounds
    a blocked ``pg_advisory_xact_lock`` wait.

    Rows are dicts (``row_factory=dict_row``), the same shape of access
    (``row["column"]``) the rest of the Runtime uses on ``sqlite3.Row``, so
    :func:`companion_runtime.db.row_to_dict` and every projection work unchanged.
    """

    #: Backend name used in logs and health output.
    dialect = "postgres"

    #: This backend implements none of the SQLite durability commands
    #: (:func:`~companion_runtime.maintenance.checkpoint`, ``verify``, ``backup``,
    #: ``restore``): there is no ``-wal`` file to fold back, no ``PRAGMA
    #: integrity_check`` and no single database file to copy a snapshot over. They
    #: therefore refuse up front with
    #: :class:`~companion_runtime.maintenance.DurabilityUnsupported`, which is the
    #: honest answer - the alternative would be an operator discovering it from a
    #: backup that never happened. PostgreSQL durability is the server's own
    #: (WAL archiving, ``pg_basebackup``, ``pg_dump``, replication). A
    #: ``pg_dump``-based implementation would flip this flag to ``True`` and add the
    #: commands beside the SQLite ones in :mod:`companion_runtime.maintenance`.
    supports_durability_commands = False

    #: Re-used from the SQLite backend rather than restated, so the two schemas
    #: cannot drift apart.
    ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = _SqliteDatabase.ADDED_COLUMNS

    def __init__(
        self,
        dsn: str,
        busy_timeout_ms: int = 5000,
        *,
        connect_timeout_s: int = 10,
        statement_timeout_ms: int | None = None,
        application_name: str = "companion_runtime",
    ) -> None:
        """Prepare the store; the connection itself is opened on first use.

        Args:
            dsn: A libpq connection string (``postgresql://user:pw@host:port/db``).
                It carries a password, so it is never returned by
                :meth:`describe` and never logged.
            busy_timeout_ms: How long a blocked writer waits before giving up.
                Applied as the session's ``lock_timeout``.
            connect_timeout_s: TCP/login timeout for opening the connection, so a
                dead server fails a health check instead of hanging it.
            statement_timeout_ms: Optional cap on a single statement. Left unset
                by default on purpose: SQLite's ``busy_timeout`` only ever bounded
                *lock* waits, and reusing it here would abort legitimately slow
                statements (a migration, a wide scan) that the SQLite backend
                happily runs.
            application_name: Reported in ``pg_stat_activity``, which is how an
                operator finds the Runtime's sessions on a shared server.
        """
        super().__init__(busy_timeout_ms=busy_timeout_ms)
        if not dsn or not str(dsn).strip():
            # Without this, libpq would silently fall back to the PG* environment
            # variables and connect to whatever host they name.
            raise ValueError("PostgresDatabase requires a non-empty PostgreSQL DSN")
        self.dsn = str(dsn)
        self.connect_timeout_s = int(connect_timeout_s)
        self.statement_timeout_ms = (
            None if statement_timeout_ms is None else int(statement_timeout_ms)
        )
        self.application_name = str(application_name)
        self._closed = False

    # ------------------------------------------------------------ connection

    def _connection(self) -> TranslatingConnection:
        """Return the live connection, opening it on first use.

        Raises:
            PostgresUnavailable: If the driver is missing or the server cannot be
                reached. The message never contains the DSN.
            RuntimeError: If the store has been closed.
        """
        with self._lock:
            if self._conn is not None:
                return self._conn
            return self._connect()

    def _connect(self) -> TranslatingConnection:
        """Open and configure the connection (idempotent; the lock is re-entrant)."""
        with self._lock:
            if self._conn is not None:
                return self._conn
            if self._closed:
                raise RuntimeError("PostgresDatabase is closed")
            if psycopg is None:
                raise PostgresUnavailable(
                    "the psycopg driver is not installed; install 'psycopg[binary]'"
                )
            raw = None
            try:
                # autocommit: the transaction template issues its own BEGIN/COMMIT,
                # so psycopg must not wrap or commit anything behind its back.
                raw = psycopg.connect(
                    self.dsn,
                    autocommit=True,
                    row_factory=dict_row,
                    connect_timeout=self.connect_timeout_s,
                    application_name=self.application_name,
                )
                self._apply_session_settings(raw)
            except Exception as error:
                # `from None` keeps a libpq message from being echoed in a chained
                # traceback that this module has not scrubbed, and the half-open
                # socket from a failed configuration is closed rather than leaked.
                if raw is not None:
                    try:
                        raw.close()
                    except Exception:
                        LOGGER.debug("Closing a half-open connection failed", exc_info=True)
                raise PostgresUnavailable(redact(f"{type(error).__name__}: {error}")) from None
            self._conn = TranslatingConnection(raw)
            LOGGER.info(
                "Connected to PostgreSQL (busy_timeout_ms=%d, statement_timeout_ms=%s)",
                self.busy_timeout_ms,
                self.statement_timeout_ms,
            )
            return self._conn

    def _apply_session_settings(self, raw: Any) -> None:
        """Apply the connection-wide settings, once per connection.

        These are session settings rather than ``SET LOCAL`` inside each
        transaction because SQLite's ``PRAGMA busy_timeout`` is likewise a
        connection-wide budget that also covers the autocommit statements issued
        outside any transaction.
        """
        settings: list[tuple[str, str]] = [
            # Every instant the Runtime stores is ISO-8601 text produced in Python
            # with an explicit offset, so the server's zone cannot change a stored
            # value. Pinning UTC keeps ad-hoc SQL and log timestamps consistent
            # with the values next to them.
            ("TimeZone", "UTC"),
            # The busy-timeout equivalent: how long a statement may wait for a lock
            # before failing. This is what makes a second writer fail loudly
            # instead of blocking forever, and it bounds the advisory-lock wait in
            # _begin too.
            ("lock_timeout", f"{self.busy_timeout_ms}ms"),
        ]
        if self.statement_timeout_ms is not None:
            settings.append(("statement_timeout", f"{self.statement_timeout_ms}ms"))
        for name, value in settings:
            # set_config, not SET: it takes parameters, so no value is ever
            # interpolated into SQL text.
            raw.execute("SELECT set_config(%s, %s, false)", (name, value))

    def close(self) -> None:
        """Close the connection, if one was ever opened."""
        with self._lock:
            self._closed = True
            connection, self._conn = self._conn, None
            if connection is None:
                return
            try:
                connection.close()
            except Exception:
                LOGGER.warning("Closing the PostgreSQL connection failed", exc_info=True)

    def describe(self) -> dict[str, Any]:
        """Return operator-facing facts about the store.

        Never raises: a health endpoint has to be able to report "the server is
        down" as a value. Never returns the DSN and never a password - only the
        server version and the database name, plus the writer-lock key an operator
        needs in order to see who currently holds the write lock.

        Returns:
            A dict with ``dialect``, ``server_version``, ``database`` and
            ``writer_lock_key``; ``server_version`` and ``database`` are ``None``
            and an ``error`` entry is added when the server cannot be reached.
        """
        info: dict[str, Any] = {
            "dialect": self.dialect,
            "server_version": None,
            "database": None,
            "writer_lock_key": WRITER_LOCK_KEY,
            # A static fact, so it is reported even when the server is down: an
            # operator asking "why did my backup refuse?" reads it from here.
            "durability_commands": self.supports_durability_commands,
        }
        try:
            with self._lock:
                row = self._connection().execute(
                    "SELECT current_setting('server_version') AS server_version, "
                    "current_database() AS database"
                ).fetchone()
            info["server_version"] = str(row["server_version"])
            info["database"] = str(row["database"])
        except Exception as error:
            info["error"] = redact(f"{type(error).__name__}: {error}")
        return info

    # ------------------------------------------------------- backend primitives
    #
    # Everything that differs from SQLite is confined to these methods, exactly as
    # in companion_runtime.db: how a transaction starts, how savepoints are named
    # and how one statement runs. The transaction template, the three hook kinds and
    # the access helpers live in DatabaseBase.

    def _begin(self, *, immediate: bool) -> None:
        """Start the outermost transaction, taking the writer lock when asked.

        ``BEGIN`` is PostgreSQL's deferred transaction, the counterpart of
        SQLite's plain ``BEGIN``. ``immediate=True`` adds the transaction-scoped
        advisory lock, PostgreSQL's counterpart of ``BEGIN IMMEDIATE``.

        Args:
            immediate: Take the writer lock up front. Must not be set for a
                read-mostly transaction, so that readers never serialise.

        Raises:
            PostgresUnavailable: If the server cannot be reached.
            psycopg.errors.LockNotAvailable: If the writer lock is held elsewhere
                for longer than ``busy_timeout_ms``.
        """
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            if immediate:
                connection.execute("SELECT pg_advisory_xact_lock(?)", (WRITER_LOCK_KEY,))
        except BaseException:
            # A failed BEGIN or a lock timeout leaves an aborted transaction on a
            # connection that is shared by every later call, which would then fail
            # with "current transaction is aborted" for reasons that have nothing
            # to do with the caller. Roll it back before re-raising.
            self._rollback_quietly(connection)
            raise

    @staticmethod
    def _rollback_quietly(connection: TranslatingConnection) -> None:
        """Best-effort ROLLBACK, used when the transaction never really started."""
        try:
            connection.execute("ROLLBACK")
        except Exception:
            LOGGER.debug("ROLLBACK after a failed BEGIN was refused", exc_info=True)

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    def _savepoint(self, name: str) -> None:
        self._conn.execute(f"SAVEPOINT {quote_identifier(name)}")

    def _release(self, name: str) -> None:
        # The template calls this both when a nested level ends well and, after a
        # ROLLBACK TO SAVEPOINT, to drop the level it just rewound. PostgreSQL keeps
        # the savepoint alive across ROLLBACK TO, so both calls are valid - which is
        # what makes the nesting behaviour identical to SQLite's.
        self._conn.execute(f"RELEASE SAVEPOINT {quote_identifier(name)}")

    def _rollback_to(self, name: str) -> None:
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {quote_identifier(name)}")

    def _execute(self, sql: str, params: Sequence[Any] | dict[str, Any]) -> Any:
        return self._connection().execute(sql, params)

    def column_names(self, table: str) -> set[str]:
        """Return the column names of ``table`` (PostgreSQL introspection).

        Args:
            table: Table name, resolved through the search path.

        Returns:
            The set of column names, empty if the table does not exist. If the
            search path exposes two tables of the same name, the columns of both
            are returned; the Runtime never runs with such a path.
        """
        with self._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = ? AND table_schema = ANY(current_schemas(false))",
                    (table,),
                )
                .fetchall()
            )
        return {row["column_name"] for row in rows}

    # -------------------------------------------------------------- migration

    def migrate(self) -> int:
        """Create every table, apply column additions and record the version.

        The statements are the SQLite backend's, executed verbatim on PostgreSQL:
        the shared DDL avoids every SQLite-only construct (no ``AUTOINCREMENT``,
        no ``WITHOUT ROWID``, ``CREATE INDEX IF NOT EXISTS`` since PostgreSQL 9.5)
        and carries no placeholders, so psycopg passes it through untouched.

        Returns:
            The current :data:`~companion_runtime.db.SCHEMA_VERSION`.
        """
        with self.transaction() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            for table, column, column_type in self.ADDED_COLUMNS:
                # PostgreSQL can test for the column itself, so unlike the SQLite
                # path this needs no column_names() round trip; the statement is
                # also a no-op when the column is already there.
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}"
                )
            connection.execute(
                "INSERT INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (str(SCHEMA_VERSION), isoformat(datetime.now().astimezone())),
            )
        return SCHEMA_VERSION
