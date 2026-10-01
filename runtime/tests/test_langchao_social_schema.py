"""DDL contract tests for isolated 「浪潮」 social memory v1."""

from __future__ import annotations

import re

from companion_runtime.langchao_social_schema import (
    LANGCHAO_SOCIAL_SCHEMA_STATEMENTS,
    LANGCHAO_SOCIAL_SCHEMA_VERSION,
    schema_statements,
)
from companion_runtime.user_model_v2_migrations import migration_records
from companion_runtime.user_model_v2_schema import (
    MIGRATIONS,
    USER_MODEL_SCHEMA_VERSION,
    schema_statements as registry_schema_statements,
)

TABLES = (
    "langchao_social_sources",
    "langchao_social_build_runs",
    "langchao_social_proposals",
    "langchao_social_items",
    "langchao_social_links",
    "langchao_social_state_events",
    "langchao_social_projection_state",
    "langchao_social_projection_heads",
    "langchao_social_item_sources",
    "langchao_social_link_sources",
)


def normalise(sql: str) -> str:
    return " ".join(sql.split()).upper()


def ddl() -> str:
    return normalise("\n".join(schema_statements()))


def test_schema_is_standalone_append_only_and_scope_composite() -> None:
    assert LANGCHAO_SOCIAL_SCHEMA_VERSION == 1
    assert isinstance(LANGCHAO_SOCIAL_SCHEMA_STATEMENTS, tuple)
    assert schema_statements() == LANGCHAO_SOCIAL_SCHEMA_STATEMENTS
    text = ddl()
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table.upper()}" in text
    assert "ON DELETE CASCADE" not in text
    assert text.count("ON DELETE RESTRICT") >= 10
    assert "APPEND-ONLY" in text
    assert "BEFORE UPDATE OR DELETE" in text
    assert "SCOPE_KEY" in text
    assert "PRIMARY KEY (SCOPE_KEY," in text


def test_schema_covers_enum_values_and_never_has_adopted_goal_state() -> None:
    text = ddl()
    for value in (
        "SHARED_MATTER", "PARTICIPATION_FACT", "CONFIRMED_ARRANGEMENT",
        "BOUNDARY_REFERENCE", "INTERPRETIVE_BASIS", "UNCERTAINTY",
        "COUNTEREVIDENCE", "GOAL_DRAFT",
    ):
        assert f"'{value}'" in text
    goal_check = re.search(
        r"CK_LANGCHAO_SOCIAL_GOAL_DRAFT_NOT_ADOPTED CHECK \((.*?)\)", text
    )
    assert goal_check is not None
    assert "ADOPTED" not in goal_check.group(1)


def test_revision_cas_projection_and_idempotent_hashes_are_explicit() -> None:
    text = ddl()
    assert "EXPECTED_REVISION BIGINT" in text
    assert "RESULT_REVISION BIGINT NOT NULL" in text
    assert "POINTER_VERSION BIGINT NOT NULL" in text
    assert "UNIQUE (SCOPE_KEY, BUILDER_VERSION, INPUT_SHA256)" in text
    assert text.count("PAYLOAD_SHA256") >= 8
    assert "BASED_ON_STATE_EVENT_ID" in text


def test_source_tombstone_and_dependency_invalidation_plan_is_durable() -> None:
    text = ddl()
    assert "SOURCE_STATUS IN ('ACTIVE', 'TOMBSTONED', 'INVALIDATED')" in text
    assert "TOMBSTONED_AT TIMESTAMPTZ" in text
    assert "INVALIDATED_AT TIMESTAMPTZ" in text
    assert "INVALIDATION_REASON TEXT" in text
    assert "'SOURCE_TOMBSTONED'" in text
    assert "'SOURCE_INVALIDATED'" in text
    assert "'DEPENDENCY_INVALIDATED'" in text
    assert "LANGCHAO_SOCIAL_ITEM_SOURCES" in text
    assert "LANGCHAO_SOCIAL_LINK_SOURCES" in text


def test_every_cross_reference_carries_scope_and_restricts_deletion() -> None:
    raw = "\n".join(schema_statements())
    foreign_keys = re.findall(
        r"FOREIGN KEY\s*\((.*?)\)\s*REFERENCES\s+\w+\s*\((.*?)\)\s*ON DELETE (\w+)",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert foreign_keys
    for local, remote, policy in foreign_keys:
        assert "scope_key" in local.lower()
        assert "scope_key" in remote.lower()
        assert policy.upper() == "RESTRICT"


def test_social_schema_is_registry_migration_v17_with_stable_checksum() -> None:
    assert USER_MODEL_SCHEMA_VERSION >= 17
    assert MIGRATIONS[16] == (17, LANGCHAO_SOCIAL_SCHEMA_STATEMENTS)
    assert tuple(version for version, _statements in MIGRATIONS[:17]) == tuple(range(1, 18))
    flattened = registry_schema_statements()
    start = flattened.index(LANGCHAO_SOCIAL_SCHEMA_STATEMENTS[0])
    assert flattened[start:start + len(LANGCHAO_SOCIAL_SCHEMA_STATEMENTS)] == (
        LANGCHAO_SOCIAL_SCHEMA_STATEMENTS
    )

    first = next(record for record in migration_records() if record.version == 17)
    second = next(record for record in migration_records() if record.version == 17)
    assert first.statements == LANGCHAO_SOCIAL_SCHEMA_STATEMENTS
    assert len(first.checksum) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", first.checksum)
    assert first.checksum == second.checksum
