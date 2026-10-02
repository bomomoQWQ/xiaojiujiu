"""Fail-closed capability/task/artifact witness contracts.

A declared capability is not evidence that an operation ran.  These immutable
records bind an exact successful task to an exact, hash-addressed artifact in one
scope.  Claim gates use :class:`WitnessValidator` immediately before dispatch or
before accepting rendered completion text; stale, cross-scope and tombstoned rows
therefore fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Intentionally narrow: ordinary contact/expression (including a user's completion)
# is not an external-operation claim.  New claim families must be explicitly added.
_COMPLETION_CLAIMS = re.compile(
    r"(?:我(?:已经)?(?:研究|检索|搜索|调查)(?:完了|完成了)|"
    r"(?:研究|检索|搜索|调查|工具执行)(?:已经)?(?:完成|成功))"
)


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
    text: str, *, action: Mapping[str, Any] | None, scope_key: str
) -> WitnessRequirement | None:
    """Return the exact requirement for a rendered real-world completion claim.

    ``None`` means the text is not a completion claim.  A matching claim without an
    explicit ``completion_witness`` raises, preventing prose from inventing a run.
    """
    if not _COMPLETION_CLAIMS.search(text or ""):
        return None
    raw = (action or {}).get("completion_witness")
    if not isinstance(raw, Mapping):
        raise WitnessValidationError("completion statement has no witness requirement")
    return requirement_from_mapping(raw, scope_key=scope_key)


class InMemoryWitnessRegistry:
    """Small deterministic fake used by tests and non-PG adapters."""

    def __init__(self, *rows: ArtifactWitness) -> None:
        self.rows = {(row.task_run_id, row.artifact_sha256): row for row in rows}

    def get_witness(self, *, task_run_id: str, artifact_sha256: str) -> ArtifactWitness | None:
        return self.rows.get((task_run_id, artifact_sha256))


__all__ = [
    "ArtifactStatus", "ArtifactWitness", "InMemoryWitnessRegistry", "TaskStatus",
    "WitnessRequirement", "WitnessValidationError", "WitnessValidator",
    "candidate_witness_requirement", "rendered_completion_requirement",
    "requirement_from_mapping",
]
