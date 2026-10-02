from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from companion_runtime.capability_witness import (
    ArtifactStatus,
    ArtifactWitness,
    InMemoryWitnessRegistry,
    PostgresWitnessRepository,
    TaskStatus,
    WitnessRequirement,
    WitnessValidationError,
    WitnessValidator,
    candidate_witness_requirement,
    rendered_completion_requirement,
)
from companion_runtime.capability_witness_schema import CAPABILITY_WITNESS_SCHEMA_V22_STATEMENTS
from companion_runtime.config import RuntimeConfig
from companion_runtime.db import Database
from companion_runtime.runtime import Runtime
from companion_runtime.typing import CandidateIntent
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
    registry = InMemoryWitnessRegistry()
    registry.put_witness(row)
    assert WitnessValidator(registry).validate(requirement()) == row


def test_in_memory_repository_rejects_conflicting_exact_identity():
    registry = InMemoryWitnessRegistry(witness())
    with pytest.raises(ValueError, match="conflicting"):
        registry.put_witness(witness(capability="other"))


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


def _render(runtime, *, candidate_id, text, action):
    candidate = CandidateIntent(
        candidate_id=candidate_id, type="share", intent="分享", goal="表达", sources=[]
    )
    with runtime.db.transaction() as connection:
        runtime.projections.candidates.upsert(connection, candidate)
        attempt_id, render_id = runtime._commit_attempt(
            connection, chosen=candidate, state=runtime.projections.runtime.ensure(), now=NOW
        )
        render = runtime.projections.outbox.get(render_id)
        render.payload = {**render.payload, "action": action}
        runtime.projections.outbox.enqueue(connection, render)
    return attempt_id, runtime.reducer.complete_render(outbox_id=render_id, text=text, now=NOW)


def test_runtime_constructor_wires_reducer_completion_gate_positive_and_missing():
    config = RuntimeConfig()
    config.storage.mirror_raw_events = False
    config.storage.database_path = ":memory:"
    config.conversation_id = "scope:a"
    registry = InMemoryWitnessRegistry(witness())
    runtime = Runtime(
        config, database=Database(":memory:"), created_at=NOW, witness_reader=registry
    )
    action = {
        "scope_key": "scope:a",
        "completion_witness": {
            "capability": "web_retrieval", "operation": "research",
            "task_run_id": "task:1", "artifact_sha256": HASH,
            "artifact_type": "research_report", "source_refs": ["source:1"],
        }
    }
    try:
        _attempt, positive = _render(
            runtime, candidate_id="completion:positive", text="我研究完了", action=action
        )
        assert positive.outbox_id is not None
        _attempt, ordinary = _render(
            runtime, candidate_id="completion:ordinary", text="今天想到你了", action={}
        )
        assert ordinary.outbox_id is not None
        action["completion_witness"]["artifact_sha256"] = "b" * 64
        _attempt, missing = _render(
            runtime, candidate_id="completion:missing", text="我研究完了", action=action
        )
        assert missing.outbox_id is None
        assert "missing" in (missing.reason or "")
    finally:
        runtime.close()


def completion_witness_mapping():
    return {
        "capability": "web_retrieval",
        "operation": "research",
        "task_run_id": "task:1",
        "artifact_sha256": HASH,
        "artifact_type": "research_report",
        "source_refs": ["source:1"],
    }


def test_render_metadata_completion_declaration_is_authoritative_and_requires_witness():
    metadata = {
        "claims_completion": True,
        "task_ref": "task:1",
        "witness_requirement": completion_witness_mapping(),
    }
    assert rendered_completion_requirement(
        "Here is an update.", action={}, scope_key="scope:a", render_metadata=metadata
    ) == requirement()
    with pytest.raises(WitnessValidationError, match="no witness requirement"):
        rendered_completion_requirement(
            "Neutral wording.", action={}, scope_key="scope:a",
            render_metadata={
                "claims_completion": True, "task_ref": "task:1",
                "witness_requirement": None,
            },
        )
    with pytest.raises(WitnessValidationError, match="does not match"):
        rendered_completion_requirement(
            "Neutral wording.", action={}, scope_key="scope:a",
            render_metadata={**metadata, "task_ref": "task:other"},
        )


def test_render_metadata_negative_declaration_is_authoritative():
    assert rendered_completion_requirement(
        "I've finished the research.", action={}, scope_key="scope:a",
        render_metadata={
            "claims_completion": False, "task_ref": None, "witness_requirement": None,
        },
    ) is None


@pytest.mark.parametrize(
    "text",
    [
        "I've completed the research and attached the report.",
        "The investigation is now finished.",
        "我已经把查询任务搞定了。",
        "资料我整理好了，结果在这里。",
    ],
)
def test_legacy_text_fallback_covers_english_chinese_and_paraphrases(text):
    with pytest.raises(WitnessValidationError, match="no witness"):
        rendered_completion_requirement(text, action={}, scope_key="scope:a")
    assert rendered_completion_requirement(
        text, action={"completion_witness": completion_witness_mapping()}, scope_key="scope:a"
    ) == requirement()


@pytest.mark.parametrize(
    "text",
    [
        "I can research that next.",
        "Have you finished the research?",
        "研究完成后我会告诉你。",
        "你已经完成调查了，做得很好。",
        "今天想到你了。",
    ],
)
def test_legacy_text_fallback_does_not_flag_non_completion_statements(text):
    assert rendered_completion_requirement(text, action={}, scope_key="scope:a") is None


def test_unknown_semantic_review_fails_closed_without_metadata():
    with pytest.raises(WitnessValidationError, match="semantics are unknown"):
        rendered_completion_requirement(
            "A neutral update.", action={}, scope_key="scope:a",
            semantic_review={"status": "unknown", "claims_task_completion": "unknown"},
        )


def test_approved_semantic_completion_review_requires_witness():
    review = {"status": "approved", "claims_task_completion": True}
    with pytest.raises(WitnessValidationError, match="no witness"):
        rendered_completion_requirement(
            "A neutral update.", action={}, scope_key="scope:a", semantic_review=review
        )
    assert rendered_completion_requirement(
        "A neutral update.", action={"completion_witness": completion_witness_mapping()},
        scope_key="scope:a", semantic_review=review,
    ) == requirement()


class _Cursor:
    def __init__(self, *, one=None, all_rows=()):
        self.one = one
        self.all_rows = list(all_rows)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all_rows


class _WitnessConnection:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if sql.lstrip().startswith("INSERT"):
            return _Cursor(one={"witness_id": "inserted"})
        return _Cursor(all_rows=(() if self.row is None else (self.row,)))


def test_postgres_reader_decodes_exact_scoped_row_and_missing():
    row = {
        "witness_id": "witness:1", "scope_key": "scope:a", "capability": "web_retrieval",
        "operation": "research", "task_run_id": "task:1", "task_status": "succeeded",
        "artifact_sha256": HASH, "artifact_type": "research_report",
        "artifact_status": "active", "created_at": NOW, "source_refs": ["source:1"],
    }
    connection = _WitnessConnection(row)
    repository = PostgresWitnessRepository(connection, scope_key="scope:a")
    assert repository.get_witness(task_run_id="task:1", artifact_sha256=HASH) == witness()
    assert connection.calls[-1][1] == ("task:1", HASH, "scope:a")
    assert PostgresWitnessRepository(
        _WitnessConnection(), scope_key="scope:a"
    ).get_witness(task_run_id="task:1", artifact_sha256=HASH) is None


def test_postgres_repository_inserts_immutable_witness():
    connection = _WitnessConnection()
    repository = PostgresWitnessRepository(connection, scope_key="scope:a")
    repository.put_witness(witness())
    sql, params = connection.calls[0]
    assert "INSERT INTO capability_artifact_witnesses" in sql
    assert params[1:5] == ("scope:a", "web_retrieval", "research", "task:1")
    with pytest.raises(ValueError, match="different scope"):
        repository.put_witness(witness(scope_key="scope:b"))


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
