"""The durability dialect gate and the backend-neutral conflict exception.

Two portability gaps are closed here, and every test is phrased as the invariant an
operator or a caller depends on rather than as a description of the implementation:

* **The durability commands are SQLite-only** - ``checkpoint``, ``verify``,
  ``backup``, ``restore``, ``maintenance_tick`` and the two ``PRAGMA`` readers. On a
  PostgreSQL store they refuse up front with
  :class:`~companion_runtime.maintenance.DurabilityUnsupported`: before a statement
  runs, before a file is created, and never as a silent no-op that would leave a
  deployment believing it has a snapshot it does not have.
* **A constraint violation is a backend-neutral conflict.** Both backends translate
  their native error into :class:`~companion_runtime.db_base.ConflictError` at the
  statement boundary, and the SQLite translation *stays* a
  ``sqlite3.IntegrityError``, so the ``except`` clauses that already exist keep
  working while new ones can be written once for both backends.

The gate tests need no server, and deliberately so: "it refused before touching
anything" is only provable when touching anything would fail, which is what the
poisoned store below does. The tests that do need a live server skip unless
``CR_TEST_PG_DSN`` is set, following the convention in ``test_db_postgres.py``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import companion_runtime.db_postgres as db_postgres
from companion_runtime.config import StorageConfig
from companion_runtime.db import Database, open_database
from companion_runtime.db_base import ConflictError, DatabaseBase
from companion_runtime.db_postgres import (
    PSYCOPG_AVAILABLE,
    PostgresDatabase,
    TranslatingConnection,
)
from companion_runtime.eventlog import EventLog
from companion_runtime.maintenance import (
    DurabilityUnsupported,
    as_database,
    backup,
    checkpoint,
    journal_mode,
    maintenance_tick,
    require_durability,
    restore,
    sqlite_error_is_corruption,
    synchronous_mode,
    verify,
)
from companion_runtime.runtime import Runtime
from companion_runtime.typing import EventType
from companion_runtime.utility import isoformat, utcnow

from conftest import BASE_TIME, build_config

#: A DSN libpq rejects while parsing it, before any socket is opened - the same
#: trick ``test_db_postgres.py`` uses for its no-server tests. A command that tries
#: to contact the store fails here with ``PostgresUnavailable``; one that refuses
#: first cannot.
UNREACHABLE_DSN = "postgresql://postgres:hunter2@127.0.0.1:55432/runtime?bogus=1"

#: Every durability command, so the parametrised tests below cover the whole
#: surface: a command that forgets the gate is exactly the failure this file exists
#: to catch.
COMMANDS: tuple[str, ...] = (
    "journal_mode",
    "synchronous_mode",
    "checkpoint",
    "verify",
    "backup",
    "restore",
    "maintenance_tick",
)

_DSN = os.environ.get("CR_TEST_PG_DSN", "").strip()

requires_psycopg = pytest.mark.skipif(
    not PSYCOPG_AVAILABLE,
    reason="install psycopg[binary] to exercise the PostgreSQL translation",
)

requires_postgres = pytest.mark.skipif(
    not _DSN or not PSYCOPG_AVAILABLE,
    reason="set CR_TEST_PG_DSN and install psycopg[binary] to run the PostgreSQL tests",
)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def postgres_store() -> DatabaseBase:
    """A PostgreSQL-configured store, opened the way a deployment opens one.

    The DSN cannot be reached, which is the point: every test that uses this store
    asserts a behaviour which must happen *before* the database is contacted.
    """
    return open_database(StorageConfig(dsn=UNREACHABLE_DSN))


def run_command(command: str, store: Any, folder: Path) -> Any:
    """Invoke one durability command against ``store``, writing only inside ``folder``.

    The arguments only have to be plausible: every caller here expects the command to
    be refused before it uses any of them.
    """
    if command == "journal_mode":
        return journal_mode(store)
    if command == "synchronous_mode":
        return synchronous_mode(store)
    if command == "checkpoint":
        return checkpoint(store, mode="TRUNCATE")
    if command == "verify":
        return verify(store)
    if command == "backup":
        return backup(store, folder / "snapshot.sqlite3")
    if command == "restore":
        return restore(folder / "missing.sqlite3", folder / "database.sqlite3", db=store)
    if command == "maintenance_tick":
        return maintenance_tick(store, backup_dir=folder / "backups")
    raise AssertionError(f"unhandled durability command: {command}")


class PoisonedPostgres(PostgresDatabase):
    """A PostgreSQL store that fails the test the moment it is used.

    The gate must answer from the backend's *declared* capability, so nothing about
    it may reach the database. Every path that could - a statement, a query, the
    connection the transaction template hands out - raises ``AssertionError`` here,
    which turns "it failed somewhere in the middle" into a test failure instead of a
    confusing pass. The store never connects: the DSN is only there because the
    constructor requires one.
    """

    def _execute(self, sql: str, params: Any) -> Any:
        raise AssertionError(f"the durability gate executed SQL: {sql[:60]!r}")

    def read(self) -> Any:
        raise AssertionError("the durability gate opened the connection")

    def transaction(self, immediate: bool = True) -> Any:
        raise AssertionError("the durability gate opened a transaction")


def file_runtime(tmp_path: Path) -> tuple[Runtime, str]:
    """A Runtime over a real file, so durability is observable (as in test_durability)."""
    config = build_config()
    database_path = str(tmp_path / "data" / "runtime.sqlite3")
    config.storage.database_path = database_path
    config.storage.raw_log_path = str(tmp_path / "data" / "raw_events.jsonl")
    config.storage.mirror_raw_events = False
    config.storage.wal = True
    return Runtime(config, seed=11, created_at=BASE_TIME), database_path


# --------------------------------------------------------------------------------------
# the declared capability
# --------------------------------------------------------------------------------------


def test_the_capability_defaults_to_false_and_only_sqlite_declares_it() -> None:
    """Which backend can run the durability commands is a declared fact, not a guess.

    The base class fails closed: a backend that has not said it can checkpoint,
    verify, back up and restore gets the refusal, so a third backend cannot inherit
    "yes" by accident. SQLite declares it because that is what these commands were
    written for; PostgreSQL declares ``False`` and keeps that declaration visible in
    ``describe()``, which is where an operator looks after a refusal.
    """
    assert DatabaseBase.supports_durability_commands is False
    assert Database.supports_durability_commands is True
    assert PostgresDatabase.supports_durability_commands is False

    sqlite = Database(":memory:")
    store = postgres_store()
    try:
        assert sqlite.describe()["durability_commands"] is True
        assert store.describe()["durability_commands"] is False
    finally:
        sqlite.close()
        store.close()


def test_selecting_postgres_warns_once_unless_the_gap_is_acknowledged(caplog) -> None:
    """The switch states its own limit at startup instead of at 03:00.

    A warning, not a refusal: PostgreSQL is a supported backend and the Runtime reads
    and writes everything on it. What must not happen is an operator discovering the
    missing durability commands from a failed scheduled maintenance pass, so
    ``open_database`` names the gap once - and the configuration field records that
    it has been understood, which is all that field does.
    """
    with caplog.at_level(logging.WARNING, logger="companion_runtime.db"):
        store = open_database(StorageConfig(dsn=UNREACHABLE_DSN))
    try:
        messages = [record.getMessage() for record in caplog.records]
        assert any("DurabilityUnsupported" in message for message in messages), messages
    finally:
        store.close()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="companion_runtime.db"):
        store = open_database(StorageConfig(dsn=UNREACHABLE_DSN, durability_gap_acknowledged=True))
    try:
        assert caplog.records == [], "acknowledging the gap must silence the warning"
    finally:
        store.close()

    # The documented defaults: warn, and never carry a credential of our own.
    assert StorageConfig().durability_gap_acknowledged is False
    assert StorageConfig().dsn == ""
    assert StorageConfig().is_postgres is False


def test_as_database_normalises_and_require_durability_gates() -> None:
    """Accepting a store and being allowed to use it are two different questions.

    ``as_database`` normalises a store or a Runtime-like object; ``require_durability``
    then decides per backend. The decision is by *capability*, not by class, so a
    store that does not carry the flag at all is refused too - it cannot be handed a
    backup it is unable to take.
    """
    sqlite = Database(":memory:")
    store = postgres_store()
    runtime_like = type("RuntimeLike", (), {"db": sqlite})()
    try:
        assert as_database(sqlite) is sqlite
        assert as_database(store) is store
        assert as_database(runtime_like) is sqlite
        assert require_durability(sqlite, "checkpoint") is sqlite
        assert require_durability(runtime_like, "verify") is sqlite

        with pytest.raises(TypeError):
            as_database(object())

        with pytest.raises(DurabilityUnsupported) as caught:
            require_durability(DatabaseBase(), "checkpoint")
        assert caught.value.command == "checkpoint"
        assert caught.value.dialect == "unknown"  # fails closed, without inventing one

        with pytest.raises(DurabilityUnsupported):
            require_durability(store, "backup")
    finally:
        sqlite.close()
        store.close()


# --------------------------------------------------------------------------------------
# the gate: every command refuses on PostgreSQL
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_every_durability_command_refuses_on_a_postgres_store(
    command: str, tmp_path: Path
) -> None:
    """The refusal is typed, named and honest - never a half-finished command.

    The error carries the command and the backend as attributes, so a caller (the
    CLI, the HTTP layer) can report it; the message names the mechanism that is
    missing and the answer for a PostgreSQL deployment, because "unsupported" alone
    tells an operator nothing. It is also a ``RuntimeError``, so existing
    "the recovery drill failed" handling keeps reporting it instead of crashing.
    """
    store = postgres_store()
    try:
        with pytest.raises(DurabilityUnsupported) as caught:
            run_command(command, store, tmp_path)
    finally:
        store.close()

    error = caught.value
    assert error.command == command
    assert error.dialect == "postgres"
    assert isinstance(error, RuntimeError)
    message = str(error)
    assert command in message
    assert "SQLite-only" in message
    assert "postgres" in message


@pytest.mark.parametrize("command", COMMANDS)
def test_the_refusal_happens_before_the_database_is_touched(
    command: str, tmp_path: Path
) -> None:
    """The decision comes from the declared capability, not from trying the command.

    Every statement path of this store raises, so a single query - the ``PRAGMA``
    read, the ``integrity_check``, the ``VACUUM INTO`` - would surface as an
    ``AssertionError`` instead of the refusal. That is the difference between failing
    early and failing somewhere.
    """
    store = PoisonedPostgres(UNREACHABLE_DSN)
    try:
        with pytest.raises(DurabilityUnsupported):
            run_command(command, store, tmp_path)
    finally:
        store.close()


@pytest.mark.parametrize("command", COMMANDS)
def test_the_refusal_leaves_the_filesystem_untouched(command: str, tmp_path: Path) -> None:
    """Nothing is created, replaced, moved or deleted before the refusal.

    A refused ``maintenance_tick`` must not even create its backup directory, and a
    refused ``backup`` must not create or replace its target: a half-written snapshot,
    or an empty directory that looks like a configured backup location, is exactly the
    artefact a durability command must never leave behind. ``restore`` is included
    even though its source does not exist here - it must refuse for the *backend*
    reason, not report a missing file.
    """
    store = postgres_store()
    try:
        with pytest.raises(DurabilityUnsupported):
            run_command(command, store, tmp_path)
    finally:
        store.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == []


# --------------------------------------------------------------------------------------
# the same commands on SQLite are unchanged
# --------------------------------------------------------------------------------------


def test_sqlite_still_runs_every_durability_command(tmp_path: Path) -> None:
    """The gate takes nothing away from the backend these commands were written for.

    The whole surface is exercised on one file-backed Runtime - the two pragma
    readers, the checkpoint, the integrity check, a snapshot, the scheduled pass with
    its retention, and a restore - because "the dialect gate was added" must not mean
    "the SQLite path changed".
    """
    runtime, database_path = file_runtime(tmp_path)
    snapshot = tmp_path / "snapshots" / "snap.sqlite3"
    try:
        runtime.process_user_message(
            content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
        )
        assert journal_mode(runtime) == "wal"
        assert synchronous_mode(runtime) in {"1", "normal"}

        folded = checkpoint(runtime.db, mode="TRUNCATE")
        assert folded.mode == "TRUNCATE"
        assert folded.wal_bytes_after == 0

        report = verify(runtime)
        assert report.ok is True
        assert report.integrity == "ok"

        written = backup(runtime, snapshot)
        assert written.integrity == "ok"
        assert written.tables["raw_events"] == runtime.events.count()

        tick = maintenance_tick(runtime, backup_dir=tmp_path / "backups", keep=2)
        assert tick["verify"]["ok"] is True
        assert Path(tick["backup"]["path"]).exists()
        events = runtime.events.count()
    finally:
        runtime.close()

    # Restore is the file-level command: the closed store is passed for the dialect
    # gate only, exactly as a caller holding a Runtime would pass it.
    restored = restore(snapshot, database_path, db=runtime.db)
    assert restored.integrity == "ok"
    assert restored.tables["raw_events"] == events


def test_restore_without_a_store_keeps_its_historical_shape(tmp_path: Path) -> None:
    """``restore`` takes a path, so the gate can only run when a store is passed.

    This pins the limit instead of hiding it: a caller that hands over no store is
    vouching that the target is a SQLite deployment, which is what the CLI does today.
    Anything holding a store should pass it, and then a PostgreSQL target is refused
    before a single file is read.
    """
    runtime, database_path = file_runtime(tmp_path)
    snapshot = tmp_path / "snap.sqlite3"
    try:
        runtime.process_user_message(content="在吗", timestamp=BASE_TIME)
        backup(runtime.db, snapshot)
    finally:
        runtime.close()

    result = restore(snapshot, database_path)
    assert result.integrity == "ok"
    assert result.tables["raw_events"] >= 1


# --------------------------------------------------------------------------------------
# the SQLite half of the conflict seam
# --------------------------------------------------------------------------------------


def test_a_sqlite_constraint_violation_arrives_as_the_neutral_conflict_error() -> None:
    """One duplicate-key insert, one error type, whichever door the statement used.

    ``Database.execute()`` and a projection's ``conn.execute()`` inside a transaction
    are the two paths statements take, and both must report the same thing: the
    neutral conflict error, with the native SQLite exception kept as ``__cause__`` and
    as ``native`` so a traceback still shows what SQLite said.
    """
    db = Database(":memory:")
    db.migrate()
    insert = "INSERT INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?)"
    try:
        db.execute(insert, ("k", "1", "2026-03-01T09:00:00+00:00"))
        with pytest.raises(ConflictError) as direct:
            db.execute(insert, ("k", "2", "2026-03-01T09:00:00+00:00"))
        with pytest.raises(ConflictError) as in_transaction:
            with db.transaction() as conn:
                conn.execute(insert, ("k", "3", "2026-03-01T09:00:00+00:00"))
    finally:
        db.close()

    for error in (direct.value, in_transaction.value):
        assert error.dialect == "sqlite"
        assert "UNIQUE constraint failed" in str(error)
        assert isinstance(error.native, sqlite3.IntegrityError)
        assert isinstance(error.__cause__, sqlite3.IntegrityError)
        # The extended result code is carried over, so the neutral error is not less
        # informative than the native one.
        assert getattr(error, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_PRIMARYKEY"


def test_every_statement_method_of_the_sqlite_connection_translates() -> None:
    """A batch reports its violation exactly as a single statement does.

    ``executemany`` is the other way a constraint is violated in bulk; leaving it
    untranslated would make the neutral type depend on which method a caller happened
    to use.
    """
    db = Database(":memory:")
    db.migrate()
    try:
        with pytest.raises(ConflictError) as caught:
            with db.transaction() as conn:
                conn.executemany(
                    "INSERT INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?)",
                    [("a", "1", "now"), ("b", "2", "now"), ("a", "3", "now")],
                )
        assert caught.value.dialect == "sqlite"
    finally:
        db.close()


def test_the_neutral_error_is_still_a_sqlite_integrity_error() -> None:
    """Backwards compatibility: the ``except`` clauses that already exist keep working.

    Keeping ``sqlite3.IntegrityError`` in the hierarchy is what lets the neutral type
    be introduced without rewriting every call site at once, and what keeps tooling
    that type-checks a SQLite error behaving as before. A conflict is still not
    corruption, which is the distinction the startup path asks about.
    """
    db = Database(":memory:")
    db.migrate()
    try:
        db.execute("INSERT INTO schema_meta(key, value, updated_at) VALUES('k', '1', 'now')")
        with pytest.raises(sqlite3.IntegrityError) as caught:
            db.execute("INSERT INTO schema_meta(key, value, updated_at) VALUES('k', '2', 'now')")
    finally:
        db.close()

    assert isinstance(caught.value, ConflictError)
    assert sqlite_error_is_corruption(caught.value) is False


def test_an_ordinary_sqlite_error_is_not_translated() -> None:
    """Only constraint violations are conflicts; everything else stays as SQLite raised it.

    A missing table is a statement bug, not a write conflict, and a caller that
    retried it as one would loop forever - which is why the translation is scoped to
    ``sqlite3.IntegrityError`` and not to ``sqlite3.Error``.
    """
    db = Database(":memory:")
    db.migrate()
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            db.query_one("SELECT * FROM no_such_table")
    finally:
        db.close()
    assert not isinstance(caught.value, ConflictError)


def test_the_neutral_type_is_what_a_call_site_catches() -> None:
    """The exact shape ``runtime.py`` needs: catch the conflict, then read the row.

    This is the deduplicating append the Runtime uses for a redelivered message - the
    insert loses the race, the recorded event is the answer - written against the
    neutral type only, with no ``sqlite3`` import, because that is the point of the
    seam. It runs inside one transaction, which is where the Runtime does it, and
    reads the row through that same transaction: SQLite leaves it usable after the
    failed statement, while PostgreSQL would require a rollback first.
    """
    db = Database(":memory:")
    db.migrate()
    log = EventLog(db)
    try:
        log.append(
            EventType.USER_MESSAGE,
            actor="user",
            content="第一次",
            timestamp=BASE_TIME,
            event_id="evt_conflict",
        )

        with db.transaction() as conn:
            try:
                log.append(
                    EventType.USER_MESSAGE,
                    actor="user",
                    content="重投",
                    timestamp=BASE_TIME,
                    event_id="evt_conflict",
                    connection=conn,
                )
                duplicate = False
            except ConflictError:
                duplicate = True
            recorded = log.get("evt_conflict")

        assert duplicate is True
        assert recorded is not None
        assert recorded.content == "第一次"
        assert log.count() == 1
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# the PostgreSQL half of the conflict seam, without a server
# --------------------------------------------------------------------------------------


class _FailingConnection:
    """Stand-in for a psycopg connection whose every statement raises ``error``."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, params))
        raise self.error

    def close(self) -> None:
        """Parity with the real adapter."""


@requires_psycopg
def test_the_postgres_adapter_translates_a_constraint_violation() -> None:
    """A psycopg integrity error becomes the same neutral conflict error.

    The adapter is the one funnel both ``PostgresDatabase.execute()`` and a
    projection's ``conn.execute()`` go through, so translating here is what makes a
    single ``except ConflictError`` cover both backends. The psycopg error is kept as
    ``__cause__`` (and as ``native``), because its diagnostic - which constraint, on
    which value - is what an operator needs.
    """
    native = db_postgres.psycopg.errors.UniqueViolation(
        'duplicate key value violates unique constraint "raw_events_pkey"'
    )
    raw = _FailingConnection(native)
    with pytest.raises(ConflictError) as caught:
        TranslatingConnection(raw).execute(
            "INSERT INTO raw_events(event_id) VALUES(?)", ("ev-1",)
        )

    error = caught.value
    assert error.dialect == "postgres"
    assert error.native is native
    assert error.__cause__ is native
    assert "duplicate key" in str(error)
    # The statement really was translated and sent before it failed.
    assert raw.calls == [("INSERT INTO raw_events(event_id) VALUES(%s)", ("ev-1",))]


@requires_psycopg
def test_the_postgres_adapter_leaves_other_failures_alone() -> None:
    """A missing table is not a conflict and must not be dressed as one.

    Translating it would tell a caller "the row already exists" when the truth is
    "your SQL is wrong" (or, for a lock timeout, "try again later"): three different
    responses behind one type.
    """
    native = db_postgres.psycopg.errors.UndefinedTable('relation "raw_events" does not exist')
    raw = _FailingConnection(native)
    with pytest.raises(db_postgres.psycopg.errors.UndefinedTable) as caught:
        TranslatingConnection(raw).execute("SELECT * FROM raw_events")
    assert caught.value is native


# --------------------------------------------------------------------------------------
# integration: a live PostgreSQL server
# --------------------------------------------------------------------------------------


@requires_postgres
def test_a_live_postgres_store_refuses_durability_and_reports_conflicts(
    tmp_path: Path,
) -> None:
    """Both halves of the seam against a real server.

    The gate is a declared capability, so it holds identically whether the server is
    up (here) or not (above) - and the translation is exercised on a real duplicate
    key, which is the case the Runtime hits when two processes ingest the same
    message. Statements run in autocommit between transactions, so the failed insert
    leaves the store immediately usable: the same property the Runtime's recovery
    path needs.
    """
    store = PostgresDatabase(_DSN, busy_timeout_ms=5000)
    event_id = f"evt_conflict_{os.urandom(6).hex()}"
    insert = (
        "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
        "VALUES(?, 'tool_result', ?, 'tool', ?)"
    )
    stamp = isoformat(utcnow())
    try:
        store.migrate()
        store.execute(insert, (event_id, stamp, stamp))
        with pytest.raises(ConflictError) as caught:
            store.execute(insert, (event_id, stamp, stamp))
        assert caught.value.dialect == "postgres"
        assert isinstance(caught.value.__cause__, db_postgres.psycopg.errors.IntegrityError)
        assert store.query_one("SELECT 1 AS ok")["ok"] == 1

        with pytest.raises(DurabilityUnsupported) as refused:
            checkpoint(store)
        assert refused.value.dialect == "postgres"
        with pytest.raises(DurabilityUnsupported):
            backup(store, tmp_path / "snapshot.sqlite3")
        assert sorted(path.name for path in tmp_path.iterdir()) == []
    finally:
        with contextlib.suppress(Exception):
            store.execute("DELETE FROM raw_events WHERE event_id = ?", (event_id,))
        store.close()
