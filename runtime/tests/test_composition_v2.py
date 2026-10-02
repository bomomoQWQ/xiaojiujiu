"""Composition tests for the PostgreSQL-only Runtime v2 factory."""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from companion_runtime.composition_v2 import (
    PostgresUserModelV2PredictionRepository,
    build_v2_composition,
)
from companion_runtime.config import RuntimeConfig
from companion_runtime.db_postgres import PSYCOPG_AVAILABLE
from companion_runtime.runtime_v2 import V2RuntimeCoordinator
from companion_runtime.user_model_v2_schema import USER_MODEL_SCHEMA_VERSION
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

    def __init__(self, *, version=USER_MODEL_SCHEMA_VERSION, fail=False):
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


AUTHORITY_BOOTSTRAPS = []


class FakeAuthorityRepository:
    def __init__(self, connection, *, scope_key):
        self.connection = connection
        self.scope_key = scope_key
        self.active = None

    def get_active(self):
        return self.active

    def bootstrap(self):
        AUTHORITY_BOOTSTRAPS.append(self.scope_key)
        self.active = {
            "engine_key": "runtime_v2", "mode": "live", "may_dispatch": True,
            "revision": 1,
        }
        return self.active


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
        authority_repository_factory=FakeAuthorityRepository,
    )

    assert database.migrate_calls == 1
    assert AUTHORITY_BOOTSTRAPS[-1] == "user:42/channel:direct"
    assert isinstance(composition.user_model_service, UserModelV2Service)
    assert composition.user_model_service.repository is composition.service_repository
    assert composition.prediction_service.repository is composition.prediction_repository
    assert isinstance(composition.coordinator, V2RuntimeCoordinator)
    assert composition.coordinator.legacy is legacy
    assert composition.coordinator.repository is runtime_repository
    assert composition.coordinator.user_model is composition.user_model_service
    assert composition.witness_repository.connection is database.connection
    assert composition.witness_repository.scope_key == "user:42/channel:direct"
    assert composition.goal_lifecycle_service is not None
    assert composition.coordinator.goal_lifecycle_service is composition.goal_lifecycle_service
    assert composition.privacy_deletion_repository is None
    assert composition.privacy_deletion_coordinator is None
    assert composition.privacy_deletion_authorize is None
    assert not hasattr(composition.goal_lifecycle_service, "outbox")
    assert not hasattr(composition.goal_lifecycle_service, "create_dispatch_claim")
    config_defaults = RuntimeConfig()
    assert config_defaults.langchao.internal_exploration_enabled is True
    assert config_defaults.langchao.external_exploration_enabled is False
    health = composition.health.to_dict()
    assert health["storage"] == {"dialect": "postgres", "schema": "runtime_v2"}
    assert health["migrations"]["current_version"] == USER_MODEL_SCHEMA_VERSION
    assert health["migrations"]["up_to_date"] is True
    assert health["versions"]["decision_policy"].startswith("runtime-v2")
    assert health["jev"] == {
        "enabled": False,
        "available": False,
        "version": "disabled-v2",
    }
    composition.close()
    assert database.closed is True


def test_privacy_deletion_composition_is_opt_in_scope_bound_and_authorized() -> None:
    config = configured()
    config.privacy_deletion.enabled = True
    config.privacy_deletion.bearer_token_sha256 = hashlib.sha256(b"allowed").hexdigest()
    composition = build_v2_composition(
        config, scope_key="scope:private",
        legacy_bridge=DummyLegacyBridge(),  # type: ignore[arg-type]
        runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
        database=FakeDatabase(), service_repository_factory=FakeServiceRepository,
        prediction_repository_factory=FakePredictionRepository,
        authority_repository_factory=FakeAuthorityRepository,
    )

    assert composition.privacy_deletion_repository.scope_key == "scope:private"
    assert composition.privacy_deletion_coordinator.repository is composition.privacy_deletion_repository
    assert composition.privacy_deletion_authorize("scope:private", "Bearer allowed") is True
    assert composition.privacy_deletion_authorize("scope:private", "Bearer denied") is False


def test_enabled_privacy_deletion_rejects_missing_authorization_configuration() -> None:
    config = configured()
    config.privacy_deletion.enabled = True
    with pytest.raises(ValueError, match="bearer_token_sha256"):
        build_v2_composition(
            config, scope_key="scope",
            legacy_bridge=DummyLegacyBridge(),  # type: ignore[arg-type]
            runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
            database=FakeDatabase(), service_repository_factory=FakeServiceRepository,
            prediction_repository_factory=FakePredictionRepository,
            authority_repository_factory=FakeAuthorityRepository,
        )


def test_active_shadow_authority_composes_shadow_runner_without_feature_switch(monkeypatch) -> None:
    class ShadowAuthorityRepository(FakeAuthorityRepository):
        def __init__(self, connection, *, scope_key):
            super().__init__(connection, scope_key=scope_key)
            self.active = {
                "engine_key": "langchao", "mode": "shadow", "may_dispatch": False,
                "revision": 9,
            }

    runner = SimpleNamespace(run=lambda *_args, **_kwargs: None)
    calls = []

    def build_runner(**kwargs):
        calls.append(kwargs)
        return runner

    import companion_runtime.langchao_shadow_wiring as wiring
    monkeypatch.setattr(wiring, "build_langchao_shadow_runner", build_runner)
    config = configured()
    assert config.langchao.shadow_enabled is False
    legacy = DummyLegacyBridge()
    legacy.runtime = object()
    composition = build_v2_composition(
        config, scope_key="scope", legacy_bridge=legacy,  # type: ignore[arg-type]
        runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
        database=FakeDatabase(), service_repository_factory=FakeServiceRepository,
        prediction_repository_factory=FakePredictionRepository,
        authority_repository_factory=ShadowAuthorityRepository,
    )

    assert composition.langchao_shadow_runner is runner
    assert composition.authority_round_router.langchao_shadow_runner is runner
    assert len(calls) == 1 and calls[0]["scope_key"] == "scope"


def test_factory_wires_same_witness_reader_to_reducer_and_live(monkeypatch) -> None:
    readers = []
    reducer = SimpleNamespace(set_witness_reader=lambda value: readers.append(value))
    legacy = DummyLegacyBridge()
    legacy.runtime = SimpleNamespace(reducer=reducer)
    config = configured()
    config.langchao.live_enabled = True
    config.langchao.live_scope_allowlist = ["scope"]
    runner = SimpleNamespace(repository=object())
    calls = []

    import companion_runtime.langchao_live_wiring as wiring
    monkeypatch.setattr(
        wiring, "build_langchao_live_runner",
        lambda **kwargs: calls.append(kwargs) or runner,
    )
    composition = build_v2_composition(
        config, scope_key="scope", legacy_bridge=legacy,  # type: ignore[arg-type]
        runtime_repository=DummyRuntimeRepository(),  # type: ignore[arg-type]
        database=FakeDatabase(), service_repository_factory=FakeServiceRepository,
        prediction_repository_factory=FakePredictionRepository,
        authority_repository_factory=FakeAuthorityRepository,
    )
    assert readers == [composition.witness_repository]
    assert calls[0]["witness_reader"] is composition.witness_repository


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
        authority_repository_factory=FakeAuthorityRepository,
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
