"""Fail-closed capability/task/artifact witness contracts.

A declared capability is not evidence that an operation ran.  These immutable
records bind an exact successful task to an exact, hash-addressed artifact in one
scope.  Claim gates use :class:`WitnessValidator` immediately before dispatch or
before accepting rendered completion text; stale, cross-scope and tombstoned rows
therefore fail closed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Only a fallback for legacy renderers which do not emit the semantic declaration
# below. Keep it conservative (completed external work, not plans/questions/user
# accomplishments), while covering ordinary English and Chinese wording.
_COMPLETION_CLAIMS = re.compile(
    r"(?:"
    r"我(?:已经|已|刚刚?|这边)?(?:把|将)?(?:研究|检索|搜索|调查|查询|查找|核查|分析|整理|处理)"
    r"(?:工作|任务|资料|结果|报告|这项工作|这件事)?(?:做完|完了|完成|弄完|处理完|查完|整理完|搞定|办妥|做好|完成了|好了|出来了)|"
    r"(?:研究|检索|搜索|调查|查询|查找|核查|分析|整理|工具执行|报告|结果)"
    r"(?:工作|任务)?(?:已经|已|现已)?(?:完成|完成了|成功|结束|搞定|办妥|做好|准备好了|出来了)|"
    r"(?:资料|结果|报告)(?:我)?(?:已经|已)?(?:查妥|整理好|准备好|做完|完成|出来)(?:了)?|"
    r"(?:i(?:'ve| have)|we(?:'ve| have))\s+(?:already\s+)?"
    r"(?:completed|finished|concluded|wrapped\s+up|carried\s+out|done)\b|"
    r"(?:i|we)\s+(?:got|have)\s+(?:the\s+)?(?:research|search|lookup|investigation|analysis|report|task)\s+done\b|"
    r"(?:the\s+)?(?:research|search|lookup|investigation|analysis|report|task|results?)\s+"
    r"(?:is|are|has\s+been|have\s+been)\s+(?:now\s+|already\s+)?"
    r"(?:complete|completed|finished|done|ready)\b|"
    r"(?:finished|completed|wrapped\s+up)\s+(?:the\s+)?"
    r"(?:research|search|lookup|investigation|analysis|report|task)\b"
    r")",
    re.IGNORECASE,
)
_RENDER_COMPLETION_KEYS = frozenset(
    {"claims_completion", "task_ref", "witness_requirement"}
)


@dataclass(frozen=True, slots=True)
class CompletionMetadata:
    """Authoritative renderer declaration for task-completion semantics.

    Every render result carries all three fields, including ordinary messages.  A
    non-completion is represented explicitly as ``False, None, None``; absence is
    not a negative claim and is accepted only behind the legacy/test switch.
    """

    claims_completion: bool
    task_ref: str | None
    witness_requirement: Mapping[str, Any] | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CompletionMetadata":
        missing = _RENDER_COMPLETION_KEYS - value.keys()
        if missing:
            raise WitnessValidationError(
                "incomplete completion metadata: " + ",".join(sorted(missing))
            )
        claims_completion = value.get("claims_completion")
        if not isinstance(claims_completion, bool):
            raise WitnessValidationError("claims_completion must be boolean")
        task_ref = value.get("task_ref")
        raw_requirement = value.get("witness_requirement")
        if not claims_completion:
            if task_ref is not None or raw_requirement is not None:
                raise WitnessValidationError("non-completion metadata carries task witness")
            return cls(False, None, None)
        if not isinstance(task_ref, str) or not task_ref.strip():
            raise WitnessValidationError("completion metadata has no task_ref")
        if not isinstance(raw_requirement, Mapping):
            raise WitnessValidationError("completion metadata has no witness requirement")
        return cls(True, task_ref.strip(), raw_requirement)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims_completion": self.claims_completion,
            "task_ref": self.task_ref,
            "witness_requirement": (
                None if self.witness_requirement is None else dict(self.witness_requirement)
            ),
        }


class TaskStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactStatus(str, Enum):
    ACTIVE = "active"
    TOMBSTONED = "tombstoned"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, kw_only=True)
class WitnessRequirement:
    """The exact evidence an operation claim promises to have."""

    scope_key: str
    capability: str
    operation: str
    task_run_id: str
    artifact_sha256: str
    artifact_type: str
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("scope_key", "capability", "operation", "task_run_id", "artifact_type"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not isinstance(self.artifact_sha256, str) or not _SHA256.fullmatch(
            self.artifact_sha256
        ):
            raise ValueError("artifact_sha256 must be lowercase SHA-256")
        if not isinstance(self.source_refs, tuple) or not self.source_refs:
            raise ValueError("source_refs must be a non-empty tuple")
        if any(not isinstance(ref, str) or not ref.strip() for ref in self.source_refs):
            raise ValueError("source_refs must contain non-empty strings")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("source_refs must not contain duplicates")


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactWitness:
    """An immutable task result and its exact artifact identity."""

    witness_id: str
    scope_key: str
    capability: str
    operation: str
    task_run_id: str
    task_status: TaskStatus
    artifact_sha256: str
    artifact_type: str
    artifact_status: ArtifactStatus
    created_at: datetime
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        WitnessRequirement(
            scope_key=self.scope_key,
            capability=self.capability,
            operation=self.operation,
            task_run_id=self.task_run_id,
            artifact_sha256=self.artifact_sha256,
            artifact_type=self.artifact_type,
            source_refs=self.source_refs,
        )
        if not isinstance(self.witness_id, str) or not self.witness_id.strip():
            raise ValueError("witness_id is required")
        if not isinstance(self.task_status, TaskStatus):
            raise TypeError("task_status must be TaskStatus")
        if not isinstance(self.artifact_status, ArtifactStatus):
            raise TypeError("artifact_status must be ArtifactStatus")
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() != timezone.utc.utcoffset(self.created_at)
        ):
            raise ValueError("created_at must be timezone-aware UTC")


class WitnessReader(Protocol):
    def get_witness(self, *, task_run_id: str, artifact_sha256: str) -> ArtifactWitness | None: ...


class WitnessValidationError(RuntimeError):
    """An operation/completion claim has no current exact witness."""


class WitnessValidator:
    def __init__(self, reader: WitnessReader) -> None:
        self.reader = reader

    def validate(self, requirement: WitnessRequirement) -> ArtifactWitness:
        """Re-read and validate the exact witness; never trust a cached verdict."""
        row = self.reader.get_witness(
            task_run_id=requirement.task_run_id,
            artifact_sha256=requirement.artifact_sha256,
        )
        if row is None:
            raise WitnessValidationError("exact artifact witness is missing")
        exact = (
            ("scope", row.scope_key, requirement.scope_key),
            ("capability", row.capability, requirement.capability),
            ("operation", row.operation, requirement.operation),
            ("task", row.task_run_id, requirement.task_run_id),
            ("artifact hash", row.artifact_sha256, requirement.artifact_sha256),
            ("artifact type", row.artifact_type, requirement.artifact_type),
            ("source refs", row.source_refs, requirement.source_refs),
        )
        for name, actual, expected in exact:
            if actual != expected:
                raise WitnessValidationError(f"{name} witness mismatch")
        if row.task_status is not TaskStatus.SUCCEEDED:
            raise WitnessValidationError("witness task did not succeed")
        if row.artifact_status is not ArtifactStatus.ACTIVE:
            raise WitnessValidationError("witness artifact is not active")
        return row


def requirement_from_mapping(value: Mapping[str, Any], *, scope_key: str) -> WitnessRequirement:
    """Parse an explicit claim envelope, binding it to the caller's scope."""
    return WitnessRequirement(
        scope_key=scope_key,
        capability=str(value.get("capability") or ""),
        operation=str(value.get("operation") or ""),
        task_run_id=str(value.get("task_run_id") or ""),
        artifact_sha256=str(value.get("artifact_sha256") or ""),
        artifact_type=str(value.get("artifact_type") or ""),
        source_refs=tuple(value.get("source_refs") or ()),
    )


def candidate_witness_requirement(candidate: Any, *, scope_key: str) -> WitnessRequirement | None:
    """Read the opt-in witness envelope from a candidate.

    Ordinary contact/expression candidates have no ``witness_required`` marker and
    are deliberately unaffected.  Retrieval/tool/research candidates must set the
    marker and all exact fields.
    """
    envelope = dict(getattr(candidate, "envelope", ()) or ())
    template = str(getattr(candidate, "action_template", "") or "").lower()
    capabilities = {str(value).lower() for value in getattr(candidate, "capability_refs", ())}
    operation_candidate = (
        bool(envelope.get("witness_required", False))
        or template.startswith(("research.", "retrieval.", "tool."))
        or bool(capabilities & {"research", "retrieval", "tool_execution"})
    )
    if not operation_candidate:
        return None
    return WitnessRequirement(
        scope_key=scope_key,
        capability=str(envelope.get("capability") or ""),
        operation=str(envelope.get("operation") or ""),
        task_run_id=str(envelope.get("task_run_id") or ""),
        artifact_sha256=str(envelope.get("artifact_sha256") or ""),
        artifact_type=str(envelope.get("artifact_type") or ""),
        source_refs=tuple(getattr(candidate, "input_refs", ()) or ()),
    )


def rendered_completion_requirement(
    text: str,
    *,
    action: Mapping[str, Any] | None,
    scope_key: str,
    render_metadata: Mapping[str, Any] | None = None,
    semantic_review: Mapping[str, Any] | None = None,
    allow_legacy_inference: bool = False,
) -> WitnessRequirement | None:
    """Return the exact witness requirement for a rendered completion claim.

    The structured declaration is authoritative and mandatory by default.  Only
    callers that explicitly enable legacy/test compatibility may fall back to a
    semantic review or conservative text detection.
    """
    metadata = render_metadata if isinstance(render_metadata, Mapping) else {}
    declared_keys = _RENDER_COMPLETION_KEYS.intersection(metadata)
    if declared_keys:
        declaration = CompletionMetadata.from_mapping(metadata)
        if not declaration.claims_completion:
            return None
        requirement = requirement_from_mapping(
            declaration.witness_requirement or {}, scope_key=scope_key
        )
        if requirement.task_run_id != declaration.task_ref:
            raise WitnessValidationError("completion task_ref does not match witness requirement")
        return requirement

    if not allow_legacy_inference:
        raise WitnessValidationError("completion metadata is required")

    review = semantic_review if isinstance(semantic_review, Mapping) else None
    if review is not None:
        status = str(review.get("status") or "unknown").lower()
        reviewed_claim = review.get("claims_task_completion", "unknown")
        if status != "approved" or reviewed_claim == "unknown":
            raise WitnessValidationError("completion semantics are unknown")
        if not isinstance(reviewed_claim, bool):
            raise WitnessValidationError("reviewed completion claim must be boolean")
        if not reviewed_claim:
            return None

    if review is None:
        match = _COMPLETION_CLAIMS.search(text or "")
        if match is None:
            return None
        lowered = (text or "").lower()
        prefix = lowered[max(0, match.start() - 24):match.start()]
        suffix = lowered[match.end():match.end() + 12]
        # The fallback must not turn questions, future/conditional statements, or
        # explicit second-person accomplishments into assistant completion claims.
        if (
            "?" in lowered or "？" in lowered
            or re.search(r"\b(?:have|did|can|could|will|would)\s+you\b", prefix)
            or re.search(r"(?:完成|做完|结束)(?:后|以后|之后|时)", match.group(0) + suffix)
            or re.search(r"(?:你|您).{0,8}$", prefix)
        ):
            return None
    raw = (action or {}).get("completion_witness")
    if not isinstance(raw, Mapping):
        raise WitnessValidationError("completion statement has no witness requirement")
    return requirement_from_mapping(raw, scope_key=scope_key)


class InMemoryWitnessRegistry:
    """Small deterministic reader/repository used by tests and non-PG adapters."""

    def __init__(self, *rows: ArtifactWitness) -> None:
        self.rows: dict[tuple[str, str], ArtifactWitness] = {}
        for row in rows:
            self.put_witness(row)

    def put_witness(self, witness: ArtifactWitness) -> None:
        key = (witness.task_run_id, witness.artifact_sha256)
        existing = self.rows.get(key)
        if existing is not None and existing != witness:
            raise ValueError("conflicting exact artifact witness")
        self.rows[key] = witness

    def get_witness(self, *, task_run_id: str, artifact_sha256: str) -> ArtifactWitness | None:
        return self.rows.get((task_run_id, artifact_sha256))


class PostgresWitnessRepository:
    """PostgreSQL v22 reader/repository for immutable exact witnesses."""

    def __init__(self, connection: Any, *, scope_key: str) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key is required")
        self.connection = connection
        self.scope_key = scope_key

    def put_witness(self, witness: ArtifactWitness) -> None:
        if witness.scope_key != self.scope_key:
            raise ValueError("witness belongs to a different scope")
        cursor = self.connection.execute(
            """INSERT INTO capability_artifact_witnesses
               (witness_id, scope_key, capability, operation, task_run_id, task_status,
                artifact_sha256, artifact_type, artifact_status, created_at, source_refs)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
               ON CONFLICT (scope_key, task_run_id, artifact_sha256) DO NOTHING
               RETURNING witness_id""",
            (witness.witness_id, witness.scope_key, witness.capability, witness.operation,
             witness.task_run_id, witness.task_status.value, witness.artifact_sha256,
             witness.artifact_type, witness.artifact_status.value, witness.created_at,
             json.dumps(witness.source_refs, ensure_ascii=False, separators=(",", ":"))),
        )
        if cursor.fetchone() is None:
            existing = self.get_witness(
                task_run_id=witness.task_run_id,
                artifact_sha256=witness.artifact_sha256,
            )
            if existing != witness:
                raise ValueError("conflicting exact artifact witness")

    def get_witness(
        self, *, task_run_id: str, artifact_sha256: str
    ) -> ArtifactWitness | None:
        sql = """SELECT witness_id,scope_key,capability,operation,task_run_id,task_status,
                        artifact_sha256,artifact_type,artifact_status,created_at,source_refs
                 FROM capability_artifact_witnesses
                 WHERE task_run_id=%s AND artifact_sha256=%s"""
        sql += " AND scope_key=%s"
        params: tuple[Any, ...] = (task_run_id, artifact_sha256, self.scope_key)
        rows = self.connection.execute(sql, params).fetchall()
        return None if not rows else self._decode(rows[0])

    @staticmethod
    def _decode(row: Any) -> ArtifactWitness:
        def value(name: str, index: int) -> Any:
            return row[name] if isinstance(row, Mapping) else row[index]

        refs = value("source_refs", 10)
        if isinstance(refs, str):
            refs = json.loads(refs)
        created_at = value("created_at", 9)
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc)
        return ArtifactWitness(
            witness_id=str(value("witness_id", 0)), scope_key=str(value("scope_key", 1)),
            capability=str(value("capability", 2)), operation=str(value("operation", 3)),
            task_run_id=str(value("task_run_id", 4)),
            task_status=TaskStatus(str(value("task_status", 5))),
            artifact_sha256=str(value("artifact_sha256", 6)),
            artifact_type=str(value("artifact_type", 7)),
            artifact_status=ArtifactStatus(str(value("artifact_status", 8))),
            created_at=created_at, source_refs=tuple(str(ref) for ref in refs),
        )


__all__ = [
    "ArtifactStatus", "ArtifactWitness", "CompletionMetadata", "InMemoryWitnessRegistry",
    "PostgresWitnessRepository", "TaskStatus", "WitnessReader", "WitnessRequirement",
    "WitnessValidationError", "WitnessValidator", "candidate_witness_requirement",
    "rendered_completion_requirement", "requirement_from_mapping",
]
