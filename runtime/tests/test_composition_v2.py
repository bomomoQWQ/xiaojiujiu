"""Composition tests for the PostgreSQL-only Runtime v2 factory."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from uuid import uuid4

import pytest

from companion_runtime.composition_v2 import (
    PostgresUserModelV2PredictionRepository,
    build_v2_composition,
)
from companion_runtime.config import RuntimeConfig
from companion_runtime.db_postgres import PSYCOPG_AVAILABLE
from companion_runtime.runtime_v2 import V2RuntimeCoordinator
from companion_runtime.user_model_v2_service import UserModelV2Service
from companion_runtime.user_model_v2_types import Target


class DummyLegacyBridge:
    """Construction-only protocol fake: the factory must retain, not invoke, it."""


class DummyRuntimeRepository:
    """Construction-only protocol fake: coordinator calls are tested elsewhere."""


class FakeConnection:
    pass


class FakeDatabase:
    dialect = "postgres"

    def __init__(self, *, version=12, fail=False):
        self.version = version
        self.fail = fail
        self.connection = FakeConnection()
        self.migrate_calls = 0
        self.closed = False

    def migrate(self):
        self.migrate_calls += 1
        if self.fail:
            raise RuntimeError("migration failed")
        return self.version

    def _connection(self):
        return self.connection

    def close(self):
        self.closed = True


class FakeServiceRepository:
    def __init__(self, connection, repository):
        self.connection = connection
        self.repository = repository


class FakePredictionRepository:
    def __init__(self, repository):
        self.repository = repository

    def get_active_parameter_snapshot(self, *, scope_key, target):
        return None


def configured(schema="runtime_v2"):
    config = RuntimeConfig()
    config.storage.dsn = "postgresql://runtime:secret@db/runtime"
    config.storage.schema = schema
    return config


def test_factory_builds_v2_graph_from_injected_protocols_and_reports_health() -> None:
    database = FakeDatabase()
    legacy = DummyLegacyBridge()
    runtime_repository = DummyRuntimeRepository()
    composition = build_v2_composition(
        configured(),
        scope_key="user:42/channel:direct",
        legacy_bridge=legacy,  # type: ignore[arg-type]
        runtime_repository=runtime_repository,  # type: ignore[arg-type]
        database_factory=lambda _storage: database,
        service_repository_factory=FakeServiceRepository,
        prediction_repository_factory=FakePredictionRepository,
    )

    assert database.migrate_calls == 1
    assert isinstance(composition.user_model_service, UserModelV2Service)
    assert composition.user_model_service.repository is composition.service_repository
    assert composition.prediction_service.repository is composition.prediction_repository
    assert isinstance(composition.coordinator, V2RuntimeCoordinator)
    assert composition.coordinator.legacy is legacy
    assert composition.coordinator.repository is runtime_repository
    assert composition.coordinator.user_model is composition.user_model_service
    health = composition.health.to_dict()
    assert health["storage"] == {"dialect": "postgres", "schema": "runtime_v2"}
    assert health["migrations"]["current_version"] == 12
    assert health["migrations"]["up_to_date"] is True
    assert health["versions"]["decision_policy"].startswith("runtime-v2")
    assert health["jev"] == {
        "enabled": False,
        "available": False,
        "version": "disabled-v2",
    }
    composition.close()
    assert database.closed is True


def test_factory_rejects_non_postgres_before_constructing_any_dependency() -> None:
    calls = []
    with pytest.raises(ValueError, match="PostgreSQL storage is required"):
        build_v2_composition(
            RuntimeConfig(),
            scope_key="scope",
            legacy_bridge=DummyLegacyBridge(),  # type: ignore[arg-type]
            runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
            database_factory=lambda storage: calls.append(storage),
        )
    assert calls == []


def test_factory_reuses_borrowed_database_without_closing_it() -> None:
    database = FakeDatabase()
    composition = build_v2_composition(
        configured(), scope_key="scope",
        legacy_bridge=DummyLegacyBridge(),  # type: ignore[arg-type]
        runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
        database=database,
        service_repository_factory=FakeServiceRepository,
        prediction_repository_factory=FakePredictionRepository,
    )
    assert composition.database is database
    assert composition.owns_database is False
    composition.close()
    assert database.closed is False


def test_factory_does_not_close_borrowed_database_when_build_fails() -> None:
    database = FakeDatabase(fail=True)
    with pytest.raises(RuntimeError, match="migration failed"):
        build_v2_composition(
            configured(), scope_key="scope",
            legacy_bridge=DummyLegacyBridge(),  # type: ignore[arg-type]
            runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
            database=database,
        )
    assert database.closed is False


def test_factory_closes_owned_database_when_migration_fails() -> None:
    database = FakeDatabase(fail=True)
    with pytest.raises(RuntimeError, match="migration failed"):
        build_v2_composition(
            configured(),
            scope_key="scope",
            legacy_bridge=DummyLegacyBridge(),  # type: ignore[arg-type]
            runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
            database_factory=lambda _storage: database,
        )
    assert database.closed is True


def test_composition_module_has_no_legacy_model_or_motivation_import() -> None:
    source_path = Path(__file__).parents[1] / "src" / "companion_runtime" / "composition_v2.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert not any(name.endswith(".user_model") or name == "user_model" for name in imported)
    assert not any(name.endswith(".motivation") or name == "motivation" for name in imported)
    assert "UserInteractionModel" not in source_path.read_text(encoding="utf-8").replace(
        "``UserInteractionModel``", ""
    )


def test_prediction_adapter_reads_each_target_head_from_active_snapshot() -> None:
    class Repository:
        def get_active_parameters(self, *, scope_key):
            assert scope_key == "scope"
            return {
                "parameter_snapshot_id": "snapshot-7",
                "parameters": {
                    "reply": {"target": "reply", "feature_version": "v"},
                    "negative": {"target": "negative", "feature_version": "v"},
                },
            }

    adapter = PostgresUserModelV2PredictionRepository(Repository())  # type: ignore[arg-type]
    reply = adapter.get_active_parameter_snapshot(scope_key="scope", target=Target.REPLY)
    assert reply is not None
    assert reply.parameter_snapshot_id == "snapshot-7"
    assert reply.payload["target"] == "reply"
    assert adapter.get_active_parameter_snapshot(
        scope_key="scope", target=Target.CONTINUE
    ) is None


@pytest.mark.integration
def test_real_postgres_factory_is_optional_and_migrates_v2_schema() -> None:
    dsn = os.environ.get("CR_TEST_PG_DSN")
    if not dsn or not PSYCOPG_AVAILABLE:
        pytest.skip("set CR_TEST_PG_DSN to a disposable PostgreSQL database")
    import psycopg

    schema = f"composition_v2_{uuid4().hex}"
    config = configured(schema)
    config.storage.dsn = dsn
    composition = None
    try:
        composition = build_v2_composition(
            config,
            scope_key="integration:composition",
            legacy_bridge=DummyLegacyBridge(),  # type: ignore[arg-type]
            runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
        )
        assert composition.health.storage["dialect"] == "postgres"
        assert composition.health.migrations["up_to_date"] is True
        with psycopg.connect(dsn, autocommit=True) as connection:
            row = connection.execute(
                "SELECT max(version) FROM " + '"' + schema + '".schema_migrations_v2'
            ).fetchone()
        assert row[0] == composition.health.migrations["current_version"]
    finally:
        if composition is not None:
            composition.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE')
