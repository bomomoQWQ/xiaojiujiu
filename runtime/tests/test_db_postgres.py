"""Tests for the PostgreSQL backend (:mod:`companion_runtime.db_postgres`).

Two groups live here:

* **Always run** - the placeholder lexer, the portability of the schema the two
  backends share, the advisory-lock key, the connection adapter and the
  backend's conformance to the primitives :class:`DatabaseBase` expects. None of
  this needs a server, and none of it needs the psycopg driver to be installed.
* **Integration** - skipped unless ``CR_TEST_PG_DSN`` is set. They *drop the
  Runtime's tables* in the target database, so point that variable at a throwaway
  server and never at anything you care about::

      docker run -d --name pg-runtime-test -e POSTGRES_PASSWORD=runtime \\
          -e POSTGRES_DB=runtime -p 127.0.0.1:55432:5432 postgres:18-alpine
      set CR_TEST_PG_DSN=postgresql://postgres:runtime@127.0.0.1:55432/runtime
"""

from __future__ import annotations

import dis
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Iterator

import pytest

import companion_runtime.db_postgres as db_postgres
from companion_runtime.db import (
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    Database,
    dumps,
    loads,
    row_to_dict,
)
from companion_runtime.db_base import DatabaseBase
from companion_runtime.db_postgres import (
    PSYCOPG_AVAILABLE,
    WRITER_LOCK_KEY,
    PostgresDatabase,
    TranslatingConnection,
    redact,
    translate_placeholders,
)
from companion_runtime.eventlog import EventLog, EventQuery
from companion_runtime.projections import SituationProjection
from companion_runtime.utility import isoformat, utcnow

#: The primitives a backend must supply, as documented on DatabaseBase.
PRIMITIVES: tuple[str, ...] = (
    "_begin",
    "_commit",
    "_rollback",
    "_savepoint",
    "_release",
    "_rollback_to",
    "_execute",
    "column_names",
    "migrate",
    "close",
)

#: The constructs PostgreSQL would reject in the shared DDL, and the SQLite
#: catalogue table it does not have.
SQLITE_ONLY_CONSTRUCTS: tuple[str, ...] = (
    "AUTOINCREMENT",
    "WITHOUT ROWID",
    "PRAGMA",
    "INSERT OR REPLACE",
    "INSERT OR IGNORE",
    "strftime(",
    "sqlite_master",
)

# --------------------------------------------------------------------------------------
# the placeholder lexer
# --------------------------------------------------------------------------------------


def test_translate_converts_question_marks_outside_tokens() -> None:
    """A ``?`` in ordinary SQL becomes psycopg's ``%s``."""
    assert (
        translate_placeholders("SELECT * FROM raw_events WHERE actor = ? AND seq > ?")
        == "SELECT * FROM raw_events WHERE actor = %s AND seq > %s"
    )


def test_translate_skips_single_quoted_literals_including_doubled_quotes() -> None:
    """A ``?`` inside a literal is text, and ``''`` does not end the literal."""
    assert (
        translate_placeholders("SELECT 'is it ? yes' AS a, 'it''s ? still' AS b, ? AS c")
        == "SELECT 'is it ? yes' AS a, 'it''s ? still' AS b, %s AS c"
    )


def test_translate_skips_double_quoted_identifiers() -> None:
    """A ``?`` inside a quoted identifier is part of the name."""
    assert (
        translate_placeholders('SELECT "why? because" FROM t WHERE a = ?')
        == 'SELECT "why? because" FROM t WHERE a = %s'
    )


def test_translate_skips_line_comments() -> None:
    """A ``--`` comment hides its ``?`` until the end of the line."""
    assert (
        translate_placeholders("SELECT ? -- not this one ?\n, ? AS y")
        == "SELECT %s -- not this one ?\n, %s AS y"
    )


def test_translate_skips_nested_block_comments() -> None:
    """PostgreSQL block comments nest, so the depth is counted."""
    assert (
        translate_placeholders("SELECT ? /* outer /* inner ? */ still ? */ , ? AS y")
        == "SELECT %s /* outer /* inner ? */ still ? */ , %s AS y"
    )


def test_translate_escapes_percent_when_parameters_are_supplied() -> None:
    """Every literal ``%`` is doubled - in SQL, in a literal and in a comment.

    psycopg does not parse SQL: it scans the whole statement for ``%``-sequences
    whenever parameters are passed, so a percent sign inside a string literal
    would otherwise abort the statement with "only '%s', '%b', '%t' are allowed
    as placeholders".
    """
    assert (
        translate_placeholders(
            "SELECT '50% done' AS a, seq % 2 AS b FROM raw_events -- 100% ? here\nWHERE x = ?"
        )
        == "SELECT '50%% done' AS a, seq %% 2 AS b FROM raw_events -- 100%% ? here\nWHERE x = %s"
    )


def test_translate_leaves_percent_alone_without_parameters() -> None:
    """With no parameters psycopg sends the statement verbatim, so nothing is escaped."""
    sql = "SELECT '100%' AS a, seq % 2 AS b FROM t"
    assert translate_placeholders(sql, with_params=False) == sql
    # The placeholder rewrite still happens: it is the escaping that depends on it.
    assert (
        translate_placeholders("SELECT '100%' FROM t WHERE a = ?", with_params=False)
        == "SELECT '100%' FROM t WHERE a = %s"
    )


def test_translate_handles_a_literal_that_contains_every_special_case() -> None:
    """A literal holding ``?``, ``--``, ``/*``, ``%`` and a Chinese question mark.

    The full-width ``？`` is not a placeholder at all, and the ASCII punctuation
    inside the literal must neither open a comment nor become a parameter - only
    the ``%`` is escaped, because psycopg would otherwise read it as a format
    specifier.
    """
    sql = "SELECT '问号？ 半角? 注释-- 块/* 百分号%' AS literal, ? AS param -- trailing ? comment"
    assert translate_placeholders(sql) == (
        "SELECT '问号？ 半角? 注释-- 块/* 百分号%%' AS literal, %s AS param -- trailing ? comment"
    )


def test_translate_tolerates_unterminated_tokens() -> None:
    """An unterminated literal or comment swallows the rest of the statement."""
    assert translate_placeholders("SELECT 'abc ? %") == "SELECT 'abc ? %%"
    assert translate_placeholders("SELECT 1 /* ? %") == "SELECT 1 /* ? %%"


def test_translate_is_a_noop_when_nothing_needs_rewriting() -> None:
    """Returning early keeps the hot path cheap and the text untouched."""
    assert translate_placeholders("SELECT 1 AS ok") == "SELECT 1 AS ok"
    assert translate_placeholders("") == ""
    # A statement that merely contains a percent sign needs no escaping when it is
    # executed without parameters.
    assert (
        translate_placeholders("SELECT '100%' AS done", with_params=False)
        == "SELECT '100%' AS done"
    )


def test_translate_drops_sqlites_no_limit_spelling() -> None:
    """``LIMIT -1`` means "every row" on SQLite and is rejected by PostgreSQL."""
    rewritten = translate_placeholders(
        "UPDATE working_situation_items SET status = 'evicted' WHERE item_id IN ("
        "SELECT item_id FROM working_situation_items WHERE status = 'active' "
        "ORDER BY salience DESC, updated_at DESC LIMIT -1 OFFSET ?)"
    )
    assert "LIMIT" not in rewritten.upper()
    assert " ".join(rewritten.split()) == (
        "UPDATE working_situation_items SET status = 'evicted' WHERE item_id IN ("
        "SELECT item_id FROM working_situation_items WHERE status = 'active' "
        "ORDER BY salience DESC, updated_at DESC OFFSET %s)"
    )
    # Case and spacing are SQLite's business, not the caller's.
    assert translate_placeholders("SELECT 1 limit   -1").strip() == "SELECT 1"
    assert translate_placeholders("SELECT 1 LIMIT -1", with_params=False).strip() == "SELECT 1"


def test_translate_leaves_other_limits_alone() -> None:
    """Only the exact ``-1`` is "no limit"; a real limit is a real limit."""
    assert translate_placeholders("SELECT 1 LIMIT -12").strip() == "SELECT 1 LIMIT -12"
    assert translate_placeholders("SELECT 1 LIMIT ?").strip() == "SELECT 1 LIMIT %s"
    assert translate_placeholders("SELECT 1 LIMIT 0").strip() == "SELECT 1 LIMIT 0"


def test_translate_replaces_rowid_with_the_physical_row_locator() -> None:
    """``rowid`` is SQLite's implicit key; ``ctid`` plays that part on PostgreSQL."""
    assert translate_placeholders(
        "SELECT * FROM raw_events ORDER BY timestamp DESC, rowid DESC LIMIT ?"
    ) == "SELECT * FROM raw_events ORDER BY timestamp DESC, ctid DESC LIMIT %s"
    assert translate_placeholders("SELECT ROWID FROM t").strip() == "SELECT ctid FROM t"
    # Only a standalone identifier: a longer name that merely ends in "rowid" and a
    # qualified column both keep their meaning.
    assert translate_placeholders("SELECT my_rowid FROM t").strip() == "SELECT my_rowid FROM t"
    assert translate_placeholders("SELECT t.rowid FROM t").strip() == "SELECT t.ctid FROM t"


def test_translate_does_not_rewrite_inside_tokens() -> None:
    """Neither rewrite may touch text that merely mentions the spelling."""
    assert translate_placeholders("SELECT 'rowid LIMIT -1' AS x, ? AS y -- rowid LIMIT -1") == (
        "SELECT 'rowid LIMIT -1' AS x, %s AS y -- rowid LIMIT -1"
    )
    assert translate_placeholders('SELECT "rowid" FROM t') == 'SELECT "rowid" FROM t'
    assert translate_placeholders("SELECT 1 /* rowid LIMIT -1 */ , ?") == (
        "SELECT 1 /* rowid LIMIT -1 */ , %s"
    )


# --------------------------------------------------------------------------------------
# the shared schema, as seen from PostgreSQL
# --------------------------------------------------------------------------------------


def test_schema_statements_avoid_sqlite_only_constructs() -> None:
    """The shared DDL is portable: nothing in it is SQLite-only."""
    lowered = "\n".join(SCHEMA_STATEMENTS).lower()
    for construct in SQLITE_ONLY_CONSTRUCTS:
        assert construct.lower() not in lowered, (
            f"{construct!r} is SQLite-only and would not run on PostgreSQL"
        )


def test_schema_statements_carry_no_placeholders() -> None:
    """Migration executes the DDL verbatim, so it must not need parameters.

    psycopg only interprets ``%`` when parameters are supplied, which is why the
    DDL is executed without any; a ``?`` would reach the server as a syntax error.
    """
    for statement in SCHEMA_STATEMENTS:
        assert "?" not in statement, f"schema statement needs a parameter: {statement[:60]!r}"


def test_schema_reuses_the_sqlite_column_additions() -> None:
    """The post-release columns come from the SQLite backend, not a copy."""
    assert PostgresDatabase.ADDED_COLUMNS == Database.ADDED_COLUMNS


# --------------------------------------------------------------------------------------
# the writer lock
# --------------------------------------------------------------------------------------


def test_writer_lock_key_is_a_documented_module_constant() -> None:
    """The key is pinned, self-describing and a valid ``bigint`` for pg_advisory_xact_lock."""
    assert WRITER_LOCK_KEY == 0x52554E54494D45
    assert WRITER_LOCK_KEY == int.from_bytes(b"RUNTIME", "big")
    # pg_advisory_xact_lock(bigint) rejects anything that does not fit in a signed
    # 64-bit integer, so a careless constant would fail at run time rather than here.
    assert 0 < WRITER_LOCK_KEY < 2**63


def test_writer_lock_key_is_visible_in_pg_locks() -> None:
    """The key splits into the classid/objid pair an operator has to query."""
    high, low = WRITER_LOCK_KEY >> 32, WRITER_LOCK_KEY & 0xFFFFFFFF
    assert (high, low) == (5395790, 1414090053)


# --------------------------------------------------------------------------------------
# backend conformance
# --------------------------------------------------------------------------------------


def _raises_not_implemented(function: Any) -> bool:
    """Return whether ``function``'s bytecode loads ``NotImplementedError``.

    ``raise NotImplementedError`` compiles to a global load rather than a constant,
    so the instruction stream - not ``co_consts`` - is what shows it.
    """
    code = getattr(function, "__code__", None)
    if code is None:
        return False
    return any(
        instruction.argval == "NotImplementedError" for instruction in dis.get_instructions(code)
    )


def _abstract_primitives(cls: type) -> set[str]:
    """Return the methods of ``cls`` that exist only to raise NotImplementedError."""
    return {
        name
        for name, member in vars(cls).items()
        if not name.startswith("__") and callable(member) and _raises_not_implemented(member)
    }


def test_the_primitive_list_matches_the_base_class() -> None:
    """A rename or a new abstract method in the base class fails here, not at run time."""
    assert _abstract_primitives(DatabaseBase) == set(PRIMITIVES)


@pytest.mark.parametrize("name", PRIMITIVES)
def test_postgres_database_overrides_every_primitive(name: str) -> None:
    """No primitive is left inheriting the base class's NotImplementedError."""
    assert getattr(PostgresDatabase, name) is not getattr(DatabaseBase, name), (
        f"PostgresDatabase does not override {name}()"
    )


def test_postgres_database_declares_its_dialect() -> None:
    """The dialect is what health output and logs are keyed on."""
    assert PostgresDatabase.dialect == "postgres"
    assert issubclass(PostgresDatabase, DatabaseBase)
    # describe() is optional in the base class, but an operator needs the real one.
    assert PostgresDatabase.describe is not DatabaseBase.describe


def test_the_driver_is_needed_only_to_connect(monkeypatch) -> None:
    """The lexer, the schema checks and the class itself import without psycopg.

    The equality below restates the module's own definition
    (``PSYCOPG_AVAILABLE = psycopg is not None``) and, on a machine where the driver
    *is* installed, the rest of the old test never ran at all. The missing-driver
    case is therefore simulated instead of left to the host.
    """
    assert PSYCOPG_AVAILABLE == (db_postgres.psycopg is not None)
    monkeypatch.setattr(db_postgres, "psycopg", None)
    store = PostgresDatabase("postgresql://postgres:sekrit@127.0.0.1:55432/runtime")
    try:
        # Construction stays lazy without the driver...
        assert store.busy_timeout_ms == 5000
        # ...and the store describes itself instead of raising.
        assert "psycopg" in str(store.describe()["error"])
        # The driver-free parts still work.
        assert db_postgres.translate_placeholders("SELECT ? AS a") == "SELECT %s AS a"
    finally:
        store.close()


def test_an_empty_dsn_is_rejected() -> None:
    """An empty DSN would let libpq fall back to the PG* environment variables."""
    with pytest.raises(ValueError):
        PostgresDatabase("")
    with pytest.raises(ValueError):
        PostgresDatabase("   ")


def test_postgres_database_can_be_constructed_without_a_server() -> None:
    """Construction is lazy: no connection is opened until a statement runs."""
    store = PostgresDatabase("postgresql://postgres:sekrit@127.0.0.1:55432/runtime")
    try:
        assert store.busy_timeout_ms == 5000
        assert store.in_transaction() is False
        assert store.transaction_depth() == 0
    finally:
        store.close()


# --------------------------------------------------------------------------------------
# the connection adapter every statement funnels through
# --------------------------------------------------------------------------------------


class _RecordingConnection:
    """Stand-in for a psycopg connection: records what it is asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> str:
        self.calls.append((sql, params))
        return "cursor"

    def close(self) -> None:
        self.closed = True


def test_adapter_translates_question_marks_and_forwards_parameters() -> None:
    """The connection a projection receives speaks SQLite, not psycopg."""
    raw = _RecordingConnection()
    connection = TranslatingConnection(raw)
    assert connection.execute("SELECT ? AS a, '100%' AS b", (7,)) == "cursor"
    assert raw.calls == [("SELECT %s AS a, '100%%' AS b", (7,))]


def test_adapter_sends_no_parameters_when_there_are_none() -> None:
    """Without parameters psycopg must receive ``None``, or it would read ``%``."""
    raw = _RecordingConnection()
    TranslatingConnection(raw).execute("SELECT '100%' AS b")
    assert raw.calls == [("SELECT '100%' AS b", None)]


def test_adapter_delegates_everything_else_to_psycopg() -> None:
    """Native psycopg access (and closing) stays reachable through the adapter."""
    raw = _RecordingConnection()
    connection = TranslatingConnection(raw)
    assert connection.raw is raw
    connection.close()
    assert raw.closed is True


# --------------------------------------------------------------------------------------
# describe() and redaction, without a server
# --------------------------------------------------------------------------------------


def test_redact_masks_uri_and_libpq_credentials() -> None:
    """Both spellings of a password are masked before a message can be returned."""
    assert "hunter2" not in redact("could not connect to postgresql://postgres:hunter2@h/db")
    assert "***" in redact("could not connect to postgresql://postgres:hunter2@h/db")
    assert "hunter2" not in redact("host=h port=5432 password=hunter2 dbname=d")
    assert redact("connection failed: no route to host") == "connection failed: no route to host"


def _describe_of_an_unreachable_store() -> dict[str, Any]:
    """Build a store that cannot connect, and describe it.

    The DSN is rejected while it is parsed - before any socket is opened - so the
    test stays fast and needs no server.
    """
    store = PostgresDatabase("postgresql://postgres:hunter2@127.0.0.1:55432/runtime?bogus=1")
    try:
        return store.describe()
    finally:
        store.close()


def test_describe_reports_a_failure_without_raising() -> None:
    """A health check must be able to answer "down" as a value, not an exception."""
    described = _describe_of_an_unreachable_store()
    assert described["dialect"] == "postgres"
    assert described["server_version"] is None
    assert described["database"] is None
    assert described["writer_lock_key"] == WRITER_LOCK_KEY
    assert "error" in described


def test_describe_never_returns_the_dsn_or_a_password() -> None:
    """The DSN is a secret: it may not appear in health output in any spelling."""
    described = _describe_of_an_unreachable_store()
    payload = json.dumps(described, ensure_ascii=False)
    assert "hunter2" not in payload
    assert "bogus=1" not in payload
    assert "postgresql://postgres@" not in payload


def test_describe_after_close_reports_a_failure() -> None:
    """A closed store reports that it is closed instead of silently reconnecting."""
    store = PostgresDatabase("postgresql://postgres:hunter2@127.0.0.1:55432/runtime")
    store.close()
    try:
        described = store.describe()
        assert described["server_version"] is None
        assert "closed" in described["error"]
    finally:
        store.close()


# --------------------------------------------------------------------------------------
# the two rewritten spellings, on the backend that did not need the rewrite
# --------------------------------------------------------------------------------------


def _seed_working_situation(connection: Any) -> None:
    """Insert four active items with distinct salience through either backend."""
    stamp = isoformat(utcnow())
    for index, salience in enumerate((0.9, 0.7, 0.5, 0.3)):
        connection.execute(
            "INSERT INTO working_situation_items(item_id, kind, content, salience, "
            "created_at, updated_at) VALUES(?, 'fact', ?, ?, ?, ?)",
            (f"item-{index}", f"content {index}", salience, stamp, stamp),
        )


def test_the_prune_statement_still_runs_unchanged_on_sqlite() -> None:
    """SQLite keeps the source line exactly as written, ``LIMIT -1`` and all.

    The counterpart of the PostgreSQL test below: one source statement, two
    dialects, and the same two least salient items evicted on both. The rewrite
    lives in the PostgreSQL translation layer, so nothing about the SQLite path
    changes.
    """
    db = Database(":memory:")
    try:
        db.migrate()
        projection = SituationProjection(db)
        with db.transaction() as connection:
            _seed_working_situation(connection)
            assert projection.prune(connection, keep=2) == 2
        remaining = {
            str(row["item_id"])
            for row in db.query(
                "SELECT item_id FROM working_situation_items WHERE status = 'active'"
            )
        }
        assert remaining == {"item-0", "item-1"}
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# integration: everything below needs a PostgreSQL server
# --------------------------------------------------------------------------------------

_DSN = os.environ.get("CR_TEST_PG_DSN", "").strip()

requires_postgres = pytest.mark.skipif(
    not _DSN or not PSYCOPG_AVAILABLE,
    reason="set CR_TEST_PG_DSN and install psycopg[binary] to run the PostgreSQL tests",
)


def _runtime_tables() -> list[str]:
    """Return the table names the shared schema creates, read from SQLite at runtime.

    Deriving them from the reference backend means the cleanup below can only ever
    touch tables the Runtime itself owns, and it is the same list the PostgreSQL
    schema is compared against.
    """
    reference = Database(":memory:")
    try:
        reference.migrate()
        rows = reference.query("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        return [str(row["name"]) for row in rows if not str(row["name"]).startswith("sqlite_")]
    finally:
        reference.close()


def _drop_runtime_tables(store: PostgresDatabase) -> None:
    """Drop the Runtime's tables so a test starts from an empty database."""
    for table in _runtime_tables():
        store.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def _postgres_tables(store: PostgresDatabase) -> set[str]:
    """Return the table names visible through the connection's search path."""
    rows = store.query(
        "SELECT tablename FROM pg_tables WHERE schemaname = ANY(current_schemas(false))"
    )
    return {str(row["tablename"]) for row in rows}


def _advisory_lock_count(store: PostgresDatabase) -> int:
    """Count the sessions holding the Runtime's writer lock, from another connection."""
    row = store.query_one(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1 "
        "AND classid::bigint = ? AND objid::bigint = ?",
        (WRITER_LOCK_KEY >> 32, WRITER_LOCK_KEY & 0xFFFFFFFF),
    )
    return int(row["n"]) if row is not None else 0


def _insert_event(connection: Any, event_id: str) -> None:
    """Insert a minimal ``raw_events`` row through the connection under test."""
    connection.execute(
        "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
        "VALUES(?, 'user_message', ?, 'user', ?)",
        (event_id, isoformat(utcnow()), isoformat(utcnow())),
    )


def _present(store: PostgresDatabase, event_id: str) -> bool:
    """Return whether ``event_id`` is in ``raw_events``, read on a fresh statement."""
    return store.query_one(
        "SELECT 1 AS present FROM raw_events WHERE event_id = ?", (event_id,)
    ) is not None


@pytest.fixture()
def pg() -> Iterator[PostgresDatabase]:
    """A migrated PostgreSQL store over an empty database."""
    store = PostgresDatabase(_DSN, busy_timeout_ms=5000)
    try:
        _drop_runtime_tables(store)
        store.migrate()
        yield store
    finally:
        try:
            _drop_runtime_tables(store)
        finally:
            store.close()


@pytest.fixture()
def probe() -> Iterator[PostgresDatabase]:
    """A second connection to the same database, for durability and lock checks."""
    store = PostgresDatabase(_DSN, busy_timeout_ms=5000)
    try:
        yield store
    finally:
        store.close()


@requires_postgres
def test_migrate_on_an_empty_database(pg: PostgresDatabase, probe: PostgresDatabase) -> None:
    """Migration creates the whole schema, records its version and is idempotent."""
    tables = _postgres_tables(pg)
    assert tables == set(_runtime_tables())
    assert len(tables) == 21

    row = pg.query_one("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    assert row is not None and int(row["value"]) == SCHEMA_VERSION

    # ADDED_COLUMNS are applied by ALTER TABLE ... IF NOT EXISTS, so a second run
    # must be a no-op rather than an error.
    assert pg.migrate() == SCHEMA_VERSION
    assert {"epoch_at", "last_exchange_at"} <= pg.column_names("runtime_state")
    assert pg.column_names("no_such_table") == set()

    # A separate session sees the same committed schema.
    assert _postgres_tables(probe) == tables


@requires_postgres
def test_table_set_and_columns_match_the_ones_sqlite_creates(pg: PostgresDatabase) -> None:
    """Both backends produce the same tables, with the same columns, from one DDL."""
    reference = Database(":memory:")
    try:
        reference.migrate()
        assert _postgres_tables(pg) == set(_runtime_tables())
        for table in _runtime_tables():
            assert pg.column_names(table) == reference.column_names(table), table
    finally:
        reference.close()


@requires_postgres
def test_insert_and_query_round_trip(pg: PostgresDatabase, probe: PostgresDatabase) -> None:
    """Text, numbers, DDL defaults and JSON text survive a round trip."""
    metadata = {"note": "中文", "count": 3, "nested": {"ok": True}}
    pg.execute(
        "INSERT INTO raw_events(event_id, seq, event_type, timestamp, actor, "
        "metadata_json, source_event_ids, runtime_version, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ev-1",
            7,
            "user_message",
            "2026-03-01T09:00:00+00:00",
            "user",
            dumps(metadata),
            dumps(["ev-0"]),
            3,
            "2026-03-01T09:00:01+00:00",
        ),
    )
    pg.execute(
        "INSERT INTO active_emotion_events(emotion_event_id, source_event_id, direction, "
        "intensity, activation, created_at) VALUES(?, ?, ?, ?, ?, ?)",
        ("em-1", "ev-1", "positive", 0.4321, 0.8765, "2026-03-01T09:00:00+00:00"),
    )

    row = probe.query_one("SELECT * FROM raw_events WHERE event_id = ?", ("ev-1",))
    data = row_to_dict(row, "raw_events")
    assert data is not None
    assert data["seq"] == 7 and data["runtime_version"] == 3
    assert data["metadata_json"] == metadata
    assert data["source_event_ids"] == ["ev-0"]
    # ISO-8601 text still sorts chronologically, which is why the column stays TEXT.
    assert data["timestamp"] < data["created_at"]
    # The column really is text holding JSON, parsed in Python - not a jsonb column.
    stored = probe.query_one("SELECT metadata_json FROM raw_events WHERE event_id = ?", ("ev-1",))
    assert loads(stored["metadata_json"]) == metadata

    emotion = row_to_dict(
        probe.query_one(
            "SELECT * FROM active_emotion_events WHERE emotion_event_id = ?", ("em-1",)
        ),
        "active_emotion_events",
    )
    assert emotion is not None
    assert emotion["direction"] == "positive"
    # REAL is single precision on PostgreSQL, so the value is compared as a float.
    assert emotion["intensity"] == pytest.approx(0.4321, abs=1e-6)
    assert emotion["target"] == "user"  # DDL default, applied by the server
    assert emotion["status"] == "active"


@requires_postgres
def test_released_savepoint_defers_its_post_commit_hook(
    pg: PostgresDatabase, probe: PostgresDatabase
) -> None:
    """A released savepoint keeps its rows and its hook, whose turn comes last."""
    events: list[str] = []
    with pg.transaction() as connection:
        _insert_event(connection, "outer")
        with pg.transaction() as inner:
            _insert_event(inner, "inner")
            pg.post_commit(lambda: events.append("inner"))
            pg.post_commit(lambda: events.append("inner-2"))
        # Released, not committed: the rows are visible to this transaction only,
        # and the hooks are now queued on the outer level.
        assert events == []
        assert pg.transaction_depth() == 1
        assert _present(pg, "inner") is True
        assert _present(probe, "inner") is False
    assert events == ["inner", "inner-2"]
    assert _present(probe, "inner") is True and _present(probe, "outer") is True


@requires_postgres
def test_rolled_back_savepoint_discards_its_writes_and_hooks(
    pg: PostgresDatabase, probe: PostgresDatabase
) -> None:
    """An inner rollback takes exactly its own rows and hooks, and nothing else."""
    events: list[str] = []
    with pg.transaction() as connection:
        _insert_event(connection, "kept")
        with pytest.raises(RuntimeError):
            with pg.transaction() as inner:
                _insert_event(inner, "discarded")
                pg.post_commit(lambda: events.append("post-commit"))
                pg.on_release(lambda: events.append("release"))
                pg.on_rollback(lambda: events.append("rollback"))
                raise RuntimeError("inner boom")
        # The enclosing transaction is still usable, and the savepoint is gone.
        assert pg.transaction_depth() == 1
        assert _present(pg, "discarded") is False
        _insert_event(connection, "after")
    assert events == ["rollback"]  # only the discarded level's rollback hook ran
    assert _present(probe, "kept") is True and _present(probe, "after") is True
    assert _present(probe, "discarded") is False


@requires_postgres
def test_rolled_back_outermost_transaction_discards_everything(
    pg: PostgresDatabase, probe: PostgresDatabase
) -> None:
    """A rollback of the whole transaction writes nothing and runs no commit hook."""
    events: list[str] = []
    with pytest.raises(RuntimeError):
        with pg.transaction() as connection:
            _insert_event(connection, "ghost")
            with pg.transaction() as inner:
                _insert_event(inner, "ghost-inner")
            pg.post_commit(lambda: events.append("post-commit"))
            raise RuntimeError("outer boom")
    assert events == []
    assert pg.transaction_depth() == 0
    assert pg.in_transaction() is False
    assert _present(probe, "ghost") is False and _present(probe, "ghost-inner") is False
    # The connection survived the rollback and is still usable.
    with pg.transaction() as connection:
        _insert_event(connection, "after-recovery")
    assert _present(probe, "after-recovery") is True


@requires_postgres
def test_advisory_lock_is_taken_only_for_an_immediate_transaction(
    pg: PostgresDatabase, probe: PostgresDatabase
) -> None:
    """``immediate=True`` holds the writer lock; the deferred form does not."""
    assert _advisory_lock_count(probe) == 0
    with pg.transaction(immediate=False):
        assert _advisory_lock_count(probe) == 0
    assert _advisory_lock_count(probe) == 0
    with pg.transaction(immediate=True):
        assert _advisory_lock_count(probe) == 1
    # Transaction-scoped: the server releases it at COMMIT, with no unlock to forget.
    assert _advisory_lock_count(probe) == 0
    with pytest.raises(RuntimeError):
        with pg.transaction(immediate=True):
            assert _advisory_lock_count(probe) == 1
            raise RuntimeError("boom")
    assert _advisory_lock_count(probe) == 0


@requires_postgres
def test_advisory_lock_survives_a_rolled_back_savepoint(
    pg: PostgresDatabase, probe: PostgresDatabase
) -> None:
    """PostgreSQL drops a lock taken *after* a savepoint when that savepoint is rewound.

    The writer lock is therefore taken in ``_begin``, before any savepoint exists:
    this test pins that placement, because a lock taken inside a nested level would
    silently disappear here and let a second writer in.
    """
    with pg.transaction(immediate=True):
        assert _advisory_lock_count(probe) == 1
        with pytest.raises(RuntimeError):
            with pg.transaction():
                raise RuntimeError("inner boom")
        assert _advisory_lock_count(probe) == 1


@requires_postgres
def test_busy_timeout_is_applied_as_lock_timeout(pg: PostgresDatabase) -> None:
    """``busy_timeout_ms`` becomes the session's lock timeout, and nothing else."""
    with pg.transaction():
        row = pg.query_one(
            "SELECT current_setting('lock_timeout') AS lock_timeout, "
            "current_setting('statement_timeout') AS statement_timeout, "
            "current_setting('TimeZone') AS timezone"
        )
    assert row["lock_timeout"] == "5s"
    # Left unset on purpose: SQLite's busy_timeout never aborted slow statements.
    assert row["statement_timeout"] == "0"
    assert row["timezone"] == "UTC"


@requires_postgres
def test_a_blocked_writer_fails_after_its_busy_timeout() -> None:
    """The second writer of a held lock gives up instead of hanging, and recovers."""
    holder = PostgresDatabase(_DSN, busy_timeout_ms=5000)
    waiter = PostgresDatabase(_DSN, busy_timeout_ms=300)
    try:
        _drop_runtime_tables(holder)
        holder.migrate()
        with holder.transaction(immediate=True):
            started = time.monotonic()
            with pytest.raises(Exception) as caught:
                with waiter.transaction(immediate=True):
                    pass
            elapsed = time.monotonic() - started
        assert "lock timeout" in str(caught.value).lower()
        assert 0.2 <= elapsed < 5.0
        # The failed BEGIN left no aborted transaction behind on the shared
        # connection, so the waiter is immediately usable again.
        assert waiter.query_one("SELECT 1 AS ok")["ok"] == 1
    finally:
        _drop_runtime_tables(holder)
        waiter.close()
        holder.close()


@requires_postgres
def test_the_writer_lock_serialises_two_connections() -> None:
    """Two threads doing read-modify-write on one row lose no update.

    Without the advisory lock the two transactions interleave and the classic lost
    update appears; with it, each read-modify-write pair runs to completion before
    the other starts.
    """
    iterations = 12
    first = PostgresDatabase(_DSN, busy_timeout_ms=10000)
    second = PostgresDatabase(_DSN, busy_timeout_ms=10000)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    try:
        _drop_runtime_tables(first)
        first.migrate()
        first.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?)",
            ("test_counter", "0", isoformat(utcnow())),
        )

        def bump(store: PostgresDatabase) -> None:
            try:
                for _ in range(iterations):
                    barrier.wait(timeout=10)
                    with store.transaction() as connection:
                        row = connection.execute(
                            "SELECT value FROM schema_meta WHERE key = ?", ("test_counter",)
                        ).fetchone()
                        current = int(row["value"])
                        # Widen the window between the read and the write so a
                        # missing lock really would interleave.
                        time.sleep(0.003)
                        connection.execute(
                            "UPDATE schema_meta SET value = ? WHERE key = ?",
                            (current + 1, "test_counter"),
                        )
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)
                try:
                    barrier.abort()
                except Exception:
                    pass

        threads = [
            threading.Thread(target=bump, args=(first,), name="writer-a"),
            threading.Thread(target=bump, args=(second,), name="writer-b"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads), "a writer hung"
        assert errors == []
        final = first.query_one("SELECT value FROM schema_meta WHERE key = ?", ("test_counter",))
        assert int(final["value"]) == 2 * iterations
    finally:
        _drop_runtime_tables(first)
        second.close()
        first.close()


@requires_postgres
def test_describe_reports_the_server_and_database(pg: PostgresDatabase) -> None:
    """A reachable store describes itself without ever echoing the DSN."""
    described = pg.describe()
    assert described["dialect"] == "postgres"
    assert described["database"] == "runtime"
    assert isinstance(described["server_version"], str) and described["server_version"]
    assert "error" not in described
    payload = json.dumps(described)
    assert "runtime@127.0.0.1" not in payload and "password" not in payload.lower()


@requires_postgres
def test_the_real_event_log_statement_runs_on_postgres(pg: PostgresDatabase) -> None:
    """``EventLog.read`` orders ties by insertion order through ``ctid``.

    The production statement ends ``ORDER BY timestamp {order}, rowid {order}``,
    which PostgreSQL cannot run. Three events sharing one timestamp must still come
    back in the order they were appended - that is what the tiebreaker is for.
    """
    log = EventLog(pg)
    stamp = datetime.fromisoformat("2026-03-01T09:00:00+00:00")
    for name in ("a", "b", "c"):
        log.append(
            "user_message",
            actor="user",
            content=name,
            timestamp=stamp,
            event_id=f"ev-{name}",
        )
    assert [event.content for event in log.read(EventQuery(limit=10))] == ["a", "b", "c"]
    assert [
        event.content for event in log.read(EventQuery(limit=10, newest_first=True))
    ] == ["c", "b", "a"]
    # The other query shapes that share the statement go through it too.
    assert log.count("user_message") == 3
    assert [event.content for event in log.recent(limit=2)] == ["b", "c"]


@requires_postgres
def test_the_real_prune_statement_runs_on_postgres(pg: PostgresDatabase) -> None:
    """``SituationProjection.prune`` runs its ``LIMIT -1 OFFSET`` statement.

    SQLite spells "no limit" as ``LIMIT -1``; the translation drops the clause so
    the same source line works here, and the least salient items beyond ``keep``
    are the ones evicted.
    """
    projection = SituationProjection(pg)
    with pg.transaction() as connection:
        _seed_working_situation(connection)
        assert projection.prune(connection, keep=2) == 2
    remaining = {
        str(row["item_id"])
        for row in pg.query("SELECT item_id FROM working_situation_items WHERE status = 'active'")
    }
    assert remaining == {"item-0", "item-1"}
    assert projection.list_active() != []
