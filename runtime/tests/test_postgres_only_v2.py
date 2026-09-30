"""Startup contract for the PostgreSQL-only v2 Runtime."""

from __future__ import annotations

import pytest

from companion_runtime.config import StorageConfig, load_config
from companion_runtime.db import open_database


def test_storage_requires_a_postgresql_dsn() -> None:
    with pytest.raises(ValueError, match="PostgreSQL storage is required"):
        StorageConfig().require_postgres_dsn()


def test_storage_rejects_a_sqlite_or_arbitrary_dsn() -> None:
    for value in ("sqlite:///tmp/runtime.db", "/tmp/runtime.db", "mysql://localhost/runtime"):
        with pytest.raises(ValueError, match="must be a PostgreSQL DSN"):
            StorageConfig(dsn=value).require_postgres_dsn()


def test_environment_supplies_the_required_dsn_and_schema() -> None:
    config = load_config(
        env={
            "CR_STORAGE__DSN": "postgresql://runtime:secret@db/runtime",
            "CR_STORAGE__SCHEMA": "runtime_v2",
        }
    )
    assert config.storage.require_postgres_dsn() == "postgresql://runtime:secret@db/runtime"
    assert config.storage.schema == "runtime_v2"


def test_open_database_fails_before_importing_or_connecting_without_postgres() -> None:
    with pytest.raises(ValueError, match="CR_STORAGE__DSN"):
        open_database(StorageConfig())


def test_legacy_sqlite_path_is_explicitly_import_only() -> None:
    config = StorageConfig(legacy_sqlite_path="/archive/stopped-v1.sqlite3")
    assert config.legacy_sqlite_path.endswith("stopped-v1.sqlite3")
    with pytest.raises(ValueError, match="PostgreSQL storage is required"):
        open_database(config)
