"""一条连接只能有一个事务所有者。

共享 PostgreSQL 会话上，Runtime 自己的 BEGIN/SAVEPOINT 模板与仓库打开的原生
``connection.transaction()`` 块会互相拆台：原生块的 COMMIT 会把模板的事务提交掉，
模板的 SAVEPOINT 随之被销毁。生产里的表现就是 v1 lease/context 失败和调度轮失败。

这里用真实的 PostgreSQL 证明：原生 ``transaction()`` 现在退化成嵌套保存点，
外层事务仍然归模板所有。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from companion_runtime.db_postgres import PostgresDatabase  # noqa: E402
from companion_runtime.user_model_v2_migrations import migrate  # noqa: E402

_DSN = os.environ.get("CR_TEST_PG_DSN") or os.environ.get("LANGCHAO_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="no PostgreSQL test DSN configured")

NOW = datetime(2027, 1, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store():
    schema = "v26owner_" + uuid.uuid4().hex[:12]
    setup = psycopg.connect(_DSN, autocommit=False)
    try:
        with setup.transaction():
            migrate(setup, schema=schema)
    finally:
        setup.close()

    database = PostgresDatabase(_DSN)
    database.schema_name = schema
    try:
        yield database
    finally:
        database.close()
        cleanup = psycopg.connect(_DSN, autocommit=True)
        try:
            cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            cleanup.close()


def _event(connection, event_id: str) -> None:
    connection.execute(
        "INSERT INTO raw_events(event_id, event_type, timestamp, actor, created_at) "
        "VALUES(?, 't', 'now', 'user', 'now')",
        (event_id,),
    )


def test_native_transaction_defers_to_the_template_owner(store) -> None:
    raw = store._connection().raw
    with store.transaction() as connection:
        _event(connection, "outer")
        depth_before = store.transaction_depth()
        assert depth_before >= 1

        # The repository-style native block must nest, not commit our transaction.
        with raw.transaction():
            _event(connection, "inner")

        # If the native block had committed, the template would no longer own a
        # transaction and the depth would have collapsed back to zero.
        assert store.transaction_depth() == depth_before
        assert store.in_transaction() is True

        # A failure after the native block must still roll the whole thing back.
        with pytest.raises(RuntimeError):
            with store.transaction():
                _event(connection, "doomed")
                raise RuntimeError("boom")

    rows = {
        row["event_id"] for row in store.query("SELECT event_id FROM raw_events")
    }
    assert rows == {"outer", "inner"}


def test_nested_savepoint_rolls_back_only_its_own_level(store) -> None:
    with store.transaction() as connection:
        _event(connection, "kept")
        with pytest.raises(RuntimeError):
            with store.transaction():
                _event(connection, "dropped")
                raise RuntimeError("boom")
    rows = {
        row["event_id"] for row in store.query("SELECT event_id FROM raw_events")
    }
    assert rows == {"kept"}
