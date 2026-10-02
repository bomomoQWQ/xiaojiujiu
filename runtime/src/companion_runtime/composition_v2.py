"""PostgreSQL-only composition root for the Runtime v2 path.

This module is the one place that constructs the v2 object graph.  It deliberately
has no import of the legacy ``user_model`` or ``motivation`` modules.  The remaining
legacy delivery/candidate boundary is a capability supplied by the caller through
:class:`~companion_runtime.runtime_v2.LegacyRuntimeV2Bridge`; the factory never
constructs a legacy Runtime or ``UserInteractionModel``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import RUNTIME_API_VERSION, __version__
from .capability_witness import PostgresWitnessRepository
from .config import RuntimeConfig
from .db import open_database
from .db_postgres import PostgresDatabase
from .langchao_authority_repository import LangchaoAuthorityRepository
from .runtime_v2 import (
    DECISION_CONTRACT_VERSION,
    DECISION_POLICY_VERSION,
    DecisionConfigV2,
    LegacyRuntimeV2Bridge,
    V2RuntimeCoordinator,
    V2RuntimeRepository,
)
from .semantic_judge_v2 import DisabledSemanticJudgeV2
from .user_model_v2_features import DEFAULT_FEATURE_SPEC_V2
from .user_model_v2_migrations import migration_records
from .user_model_v2_prediction import (
    ActiveParameterSnapshotV2,
    UserModelV2PredictionService,
)
from .user_model_v2_repository import UserModelV2Repository
from .user_model_v2_service import UserModelV2Service
from .user_model_v2_service_repository import PostgresUserModelV2ServiceRepository
from .user_model_v2_types import Target

DatabaseFactory = Callable[[Any], Any]
ServiceRepositoryFactory = Callable[[Any, UserModelV2Repository], Any]
PredictionRepositoryFactory = Callable[[UserModelV2Repository], Any]
RuntimeRepositoryFactory = Callable[..., V2RuntimeRepository]
AuthorityRepositoryFactory = Callable[..., Any]


class PostgresUserModelV2PredictionRepository:
    """Read independently fitted target heads from the active PostgreSQL snapshot.

    A parameter snapshot stores the four heads in one JSON object keyed by target
    name.  For compatibility with an early single-head fixture, a top-level payload
    carrying ``target`` is accepted only for that matching target.
    """

    def __init__(self, repository: UserModelV2Repository) -> None:
        self.repository = repository

    def get_active_parameter_snapshot(
        self, *, scope_key: str, target: Target
    ) -> ActiveParameterSnapshotV2 | None:
        row = self.repository.get_active_parameters(scope_key=scope_key)
        if row is None:
            return None
        snapshot_id = str(_row_value(row, "parameter_snapshot_id", 3))
        parameters = _json_object(_row_value(row, "parameters", 10), name="parameters")
        if parameters.get("target") == target.value:
            payload: Any = parameters
        else:
            payload = parameters.get(target.value)
        if payload is None:
            return None
        payload = _json_object(payload, name=f"parameters.{target.value}")
        return ActiveParameterSnapshotV2(
            parameter_snapshot_id=snapshot_id,
            target=target,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class V2Health:
    """Non-secret startup facts suitable for a health endpoint."""

    versions: Mapping[str, str]
    migrations: Mapping[str, Any]
    storage: Mapping[str, Any]
    jev: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "versions": dict(self.versions),
            "migrations": dict(self.migrations),
            "storage": dict(self.storage),
            "jev": dict(self.jev),
        }


@dataclass(slots=True)
class V2Composition:
    """Owned v2 services and their PostgreSQL lifetime."""

    database: Any
    repository: UserModelV2Repository
    service_repository: Any
    prediction_repository: Any
    user_model_service: UserModelV2Service
    prediction_service: UserModelV2PredictionService
    semantic_judge: DisabledSemanticJudgeV2
    coordinator: V2RuntimeCoordinator
    audit_repository: Any
    witness_repository: PostgresWitnessRepository
    health: V2Health
    enable_decision_run: bool = False
    decision_simulation_runner: Any | None = None
    langchao_shadow_runner: Any | None = None
    langchao_live_runner: Any | None = None
    authority_round_router: Any | None = None
    goal_lifecycle_service: Any | None = None
    social_service: Any | None = None
    owns_database: bool = True

    def close(self) -> None:
        if self.owns_database:
            self.database.close()

    def __enter__(self) -> "V2Composition":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def build_v2_composition(
    config: RuntimeConfig,
    *,
    scope_key: str,
    legacy_bridge: LegacyRuntimeV2Bridge,
    runtime_repository: V2RuntimeRepository | None = None,
    decision_config: DecisionConfigV2 | None = None,
    rng: Any | None = None,
    database: Any | None = None,
    owns_database: bool | None = None,
    database_factory: DatabaseFactory = open_database,
    service_repository_factory: ServiceRepositoryFactory = PostgresUserModelV2ServiceRepository,
    prediction_repository_factory: PredictionRepositoryFactory = (
        PostgresUserModelV2PredictionRepository
    ),
    runtime_repository_factory: RuntimeRepositoryFactory | None = None,
    authority_repository_factory: AuthorityRepositoryFactory = LangchaoAuthorityRepository,
) -> V2Composition:
    """Build and migrate the PostgreSQL-only v2 runtime graph.

    ``legacy_bridge`` and ``runtime_repository`` are mandatory injected protocols.
    In particular, this function never guesses how to construct the legacy bridge.
    Dependency factories exist solely to permit hermetic tests; the production
    defaults create :class:`PostgresDatabase` and the concrete PostgreSQL adapters.
    """

    if not isinstance(config, RuntimeConfig):
        raise TypeError("config must be a RuntimeConfig")
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key is required")

    # Fail before any factory call if configuration attempts SQLite/fallback storage.
    config.storage.require_postgres_dsn()
    database_was_injected = database is not None
    if database is None:
        database = database_factory(config.storage)
        effective_owns_database = True if owns_database is None else bool(owns_database)
    else:
        effective_owns_database = False if owns_database is None else bool(owns_database)
    try:
        if (
            not database_was_injected
            and database_factory is open_database
            and not isinstance(database, PostgresDatabase)
        ):
            raise TypeError("the production v2 composition requires PostgresDatabase")
        migration_version = int(database.migrate())
        compatibility_connection = database._connection()
        # v2 repositories use PostgreSQL-native ``%s`` SQL. The compatibility
        # adapter deliberately interprets SQLite ``?`` SQL for legacy projections,
        # so hand the v2 graph the underlying psycopg connection instead.
        connection = getattr(compatibility_connection, "raw", compatibility_connection)
        authority = authority_repository_factory(connection, scope_key=scope_key)
        if authority.get_active() is None:
            authority.bootstrap()
        if hasattr(legacy_bridge, "scope_key"):
            legacy_bridge.scope_key = scope_key
        repository = UserModelV2Repository(connection)
        witness_repository = PostgresWitnessRepository(connection, scope_key=scope_key)
        runtime = getattr(legacy_bridge, "runtime", None)
        reducer = getattr(runtime, "reducer", None)
        if reducer is not None:
            reducer.set_witness_reader(witness_repository)
        service_repository = service_repository_factory(connection, repository)
        prediction_repository = prediction_repository_factory(repository)
        user_model_service = UserModelV2Service(service_repository)
        prediction_service = UserModelV2PredictionService(prediction_repository)
        if runtime_repository is None:
            if runtime_repository_factory is None:
                from .runtime_repository_v2 import PostgresV2RuntimeRepository

                runtime_repository_factory = PostgresV2RuntimeRepository
            runtime_repository = runtime_repository_factory(
                connection,
                prediction_service=prediction_service,
                service_repository=service_repository,
                scope_key=scope_key,
            )
        semantic_judge = DisabledSemanticJudgeV2()
        coordinator = V2RuntimeCoordinator(
            scope_key=scope_key,
            legacy=legacy_bridge,
            user_model=user_model_service,
            repository=runtime_repository,
            config=decision_config,
            rng=rng,
        )
        expected = tuple(record.version for record in migration_records())
        # Validate before constructing optional runners. Invalid recipe/allowlist
        # configuration aborts composition rather than silently enabling a variant.
        attention_recipe = config.langchao.attention_recipe_for_scope(scope_key)
        social_enabled = bool(
            config.langchao.social_enabled
            or config.extras.get("langchao.social_enabled", False)
            or (isinstance(config.extras.get("langchao"), Mapping)
                and config.extras["langchao"].get("social_enabled", False))
        )
        social_service = None
        if social_enabled:
            from .langchao_social_wiring import build_langchao_social_service

            social_service = build_langchao_social_service(
                connection=connection, scope_key=scope_key, runtime=legacy_bridge.runtime,
            )
            legacy_bridge.social_service = social_service
        from .langchao_live_wiring import active_authority_coordinates

        active_engine, active_mode, active_may_dispatch = active_authority_coordinates(
            authority.get_active()
        )
        shadow_authority_active = (
            active_engine == "langchao"
            and active_mode == "shadow"
            and not active_may_dispatch
        )
        shadow_enabled = bool(
            shadow_authority_active
            or config.langchao.shadow_enabled
            or config.extras.get("langchao.shadow_enabled", False)
            or (isinstance(config.extras.get("langchao"), Mapping)
                and config.extras["langchao"].get("shadow_enabled", False))
        )
        langchao_shadow_runner = None
        if shadow_enabled:
            from .langchao_shadow_wiring import build_langchao_shadow_runner

            # Reuse runtime.db's raw PostgreSQL connection. Schema migrations are
            # unconditional above, while object construction remains explicitly opt-in.
            langchao_shadow_runner = build_langchao_shadow_runner(
                connection=connection, scope_key=scope_key, runtime=legacy_bridge.runtime,
                attention_recipe=attention_recipe,
                internal_exploration_enabled=config.langchao.internal_exploration_enabled,
            )
        live_enabled = bool(config.langchao.live_allowed(scope_key))
        langchao_live_runner = None
        if live_enabled:
            from .langchao_live_wiring import build_langchao_live_runner

            langchao_live_runner = build_langchao_live_runner(
                connection=connection, scope_key=scope_key,
                runtime=legacy_bridge.runtime, legacy_bridge=legacy_bridge,
                witness_reader=witness_repository,
                attention_recipe=attention_recipe,
                internal_exploration_enabled=config.langchao.internal_exploration_enabled,
            )
            from .langchao_user_outcomes import LangchaoUserOutcomeSettler

            user_model_service.outcome_observer = LangchaoUserOutcomeSettler(
                langchao_live_runner.repository
            )
            langchao_live_runner.exposure_repository = runtime_repository
            langchao_live_runner.user_model = user_model_service
            langchao_live_runner.horizons = coordinator.config.horizons
        from .langchao_goal_lifecycle_service import LangchaoGoalLifecycleService
        from .langchao_repository import LangchaoRepository

        def transition_unfinished_matter(matter_id: str, status: str, at: Any) -> None:
            cursor = connection.execute(
                """UPDATE unfinished_matters
                   SET status = %s, updated_at = %s,
                       resolution_note = COALESCE(resolution_note, %s)
                   WHERE unfinished_id = %s""",
                (status, at, f"langchao_goal_{status}", matter_id),
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise RuntimeError("terminal goal references a missing unfinished matter")

        # The production coordinator emits explicit terminal evidence into this
        # service. All collaborators share one connection and outer transaction.
        goal_lifecycle_service = LangchaoGoalLifecycleService(
            contract_repository=LangchaoRepository(connection, scope_key=scope_key),
            matter_transition=transition_unfinished_matter,
        )
        coordinator.goal_lifecycle_service = goal_lifecycle_service
        from .langchao_live_wiring import AuthorityRoutedEndogenousRound
        authority_round_router = AuthorityRoutedEndogenousRound(
            scope_key=scope_key, v2_coordinator=coordinator,
            authority_reader=authority,
            langchao_live_runner=langchao_live_runner,
            langchao_shadow_runner=langchao_shadow_runner,
            live_enabled=live_enabled,
            live_scope_allowlist=tuple(config.langchao.live_scope_allowlist),
        )
        health = V2Health(
            versions={
                "runtime": __version__,
                "api": RUNTIME_API_VERSION,
                "decision_policy": DECISION_POLICY_VERSION,
                "decision_contract": DECISION_CONTRACT_VERSION,
                "feature": DEFAULT_FEATURE_SPEC_V2.version,
            },
            migrations={
                "current_version": migration_version,
                "expected_versions": expected,
                "up_to_date": migration_version == (expected[-1] if expected else 0),
            },
            storage={
                "dialect": str(getattr(database, "dialect", "unknown")),
                "schema": config.storage.schema,
            },
            jev={
                "enabled": False,
                "available": semantic_judge.available(),
                "version": semantic_judge.VERSION,
            },
        )
        return V2Composition(
            database=database,
            repository=repository,
            service_repository=service_repository,
            prediction_repository=prediction_repository,
            user_model_service=user_model_service,
            prediction_service=prediction_service,
            semantic_judge=semantic_judge,
            coordinator=coordinator,
            audit_repository=runtime_repository,
            witness_repository=witness_repository,
            health=health,
            langchao_shadow_runner=langchao_shadow_runner,
            langchao_live_runner=langchao_live_runner,
            authority_round_router=authority_round_router,
            goal_lifecycle_service=goal_lifecycle_service,
            social_service=social_service,
            owns_database=effective_owns_database,
        )
    except BaseException:
        if effective_owns_database:
            database.close()
        raise


def _row_value(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def _json_object(value: Any, *, name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"stored {name} must be a JSON object")
    return value


__all__ = [
    "PostgresUserModelV2PredictionRepository",
    "V2Composition",
    "V2Health",
    "build_v2_composition",
]
