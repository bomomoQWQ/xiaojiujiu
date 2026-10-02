from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from companion_runtime.capability_witness import (
    ArtifactStatus,
    ArtifactWitness,
    InMemoryWitnessRegistry,
    TaskStatus,
    WitnessRequirement,
    WitnessValidationError,
    WitnessValidator,
    candidate_witness_requirement,
    rendered_completion_requirement,
)
from companion_runtime.capability_witness_schema import CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS
from companion_runtime.user_model_v2_schema import MIGRATIONS, USER_MODEL_SCHEMA_VERSION

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
HASH = "a" * 64


def requirement(**changes):
    values = dict(
        scope_key="scope:a",
        capability="web_retrieval",
        operation="research",
        task_run_id="task:1",
        artifact_sha256=HASH,
        artifact_type="research_report",
        source_refs=("source:1",),
    )
    values.update(changes)
    return WitnessRequirement(**values)


def witness(**changes):
    values = dict(
        witness_id="witness:1",
        scope_key="scope:a",
        capability="web_retrieval",
        operation="research",
        task_run_id="task:1",
        task_status=TaskStatus.SUCCEEDED,
        artifact_sha256=HASH,
        artifact_type="research_report",
        artifact_status=ArtifactStatus.ACTIVE,
        created_at=NOW,
        source_refs=("source:1",),
    )
    values.update(changes)
    return ArtifactWitness(**values)


def test_exact_success_artifact_is_positive_control():
    row = witness()
    assert WitnessValidator(InMemoryWitnessRegistry(row)).validate(requirement()) == row


@pytest.mark.parametrize(
    "row, expected",
    [
        (witness(task_status=TaskStatus.NOT_STARTED), "did not succeed"),
        (witness(artifact_status=ArtifactStatus.TOMBSTONED), "not active"),
        (witness(scope_key="scope:b"), "scope witness mismatch"),
    ],
)
def test_not_run_cross_scope_and_tombstone_are_rejected(row, expected):
    with pytest.raises(WitnessValidationError, match=expected):
        WitnessValidator(InMemoryWitnessRegistry(row)).validate(requirement())


def test_hash_mismatch_is_rejected_as_missing_exact_witness():
    with pytest.raises(WitnessValidationError, match="missing"):
        WitnessValidator(InMemoryWitnessRegistry(witness())).validate(
            requirement(artifact_sha256="b" * 64)
        )


def test_operation_candidate_requires_contract_but_contact_expression_do_not():
    contact = SimpleNamespace(
        action_template="contact.v1",
        capability_refs=("external_message",),
        envelope=(("subject_ref", "hello"),),
        input_refs=("source:1",),
    )
    assert candidate_witness_requirement(contact, scope_key="scope:a") is None

    research = SimpleNamespace(
        action_template="research.v1",
        capability_refs=("external_message", "research"),
        envelope=(
            ("capability", "web_retrieval"),
            ("operation", "research"),
            ("task_run_id", "task:1"),
            ("artifact_sha256", HASH),
            ("artifact_type", "research_report"),
        ),
        input_refs=("source:1",),
    )
    assert candidate_witness_requirement(research, scope_key="scope:a") == requirement()


def test_completion_statement_needs_exact_witness_but_ordinary_expression_does_not():
    assert rendered_completion_requirement(
        "今天想到你了", action={}, scope_key="scope:a"
    ) is None
    with pytest.raises(WitnessValidationError, match="no witness"):
        rendered_completion_requirement("我研究完了", action={}, scope_key="scope:a")
    action = {
        "completion_witness": {
            "capability": "web_retrieval",
            "operation": "research",
            "task_run_id": "task:1",
            "artifact_sha256": HASH,
            "artifact_type": "research_report",
            "source_refs": ["source:1"],
        }
    }
    assert rendered_completion_requirement(
        "我研究完了", action=action, scope_key="scope:a"
    ) == requirement()


def test_pg_v22_schema_has_required_ledger_fields_and_constraints():
    assert USER_MODEL_SCHEMA_VERSION >= 22
    assert next(item for item in MIGRATIONS if item[0] == 22) == (
        22, CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS
    )
    ddl = "\n".join(CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS)
    for field in (
        "scope_key", "capability", "operation", "task_run_id", "task_status",
        "artifact_sha256", "artifact_type", "artifact_status", "created_at", "source_refs",
    ):
        assert field in ddl
    assert "tombstoned" in ddl
    assert "UNIQUE (scope_key, task_run_id, artifact_sha256)" in ddl
