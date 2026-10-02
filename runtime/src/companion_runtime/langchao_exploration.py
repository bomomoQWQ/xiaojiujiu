"""Finite, evidence-bearing exploration work-segment contracts.

An exploration segment is admissible only when it names a real problem, lists concrete
steps, and closes with either a capability witness, an artifact witness, or an explicit
no-conclusion record.  Process value is a role-design allowance for one semantic segment;
it does not grow with tokens, tool calls, elapsed time, retries, or persuasive wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

EXPLORATION_CONTRACT_VERSION = "langchao.exploration.v1"
EXPLORATION_PROCESS_VALUE = 0.25
EXPLORATION_PROCESS_CAP = 0.25


class ExplorationResultKind(str, Enum):
    CAPABILITY = "capability"
    ARTIFACT = "artifact"
    NO_CONCLUSION = "no_conclusion"


def _text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _strings(name: str, values: tuple[str, ...], *, nonempty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if nonempty and not values:
        raise ValueError(f"{name} must not be empty")
    for value in values:
        _text(f"{name} item", value)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExplorationWorkSegment:
    """One completed, bounded unit of exploration work.

    ``result_ref`` is a witness reference, not a claim that a discovery occurred.
    ``NO_CONCLUSION`` is the honest terminal shape when the finite steps found nothing.
    """

    segment_id: str
    problem_ref: str
    question: str
    executable_steps: tuple[str, ...]
    result_kind: ExplorationResultKind
    result_ref: str
    evidence_refs: tuple[str, ...]
    contract_version: str = EXPLORATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("segment_id", "problem_ref", "question", "result_ref"):
            _text(name, getattr(self, name))
        _strings("executable_steps", self.executable_steps, nonempty=True)
        _strings("evidence_refs", self.evidence_refs, nonempty=True)
        if not isinstance(self.result_kind, ExplorationResultKind):
            raise TypeError("result_kind must be an ExplorationResultKind")
        if self.contract_version != EXPLORATION_CONTRACT_VERSION:
            raise ValueError(f"contract_version must be {EXPLORATION_CONTRACT_VERSION!r}")
        if self.result_ref not in self.evidence_refs:
            raise ValueError("result_ref must be included in evidence_refs")

    @property
    def records_progress(self) -> bool:
        """Only witnessed capability/artifact results are progress claims."""
        return self.result_kind in {
            ExplorationResultKind.CAPABILITY,
            ExplorationResultKind.ARTIFACT,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "problem_ref": self.problem_ref,
            "question": self.question,
            "executable_steps": list(self.executable_steps),
            "result_kind": self.result_kind.value,
            "result_ref": self.result_ref,
            "evidence_refs": list(self.evidence_refs),
            "records_progress": self.records_progress,
            "process_value": EXPLORATION_PROCESS_VALUE,
            "process_cap": EXPLORATION_PROCESS_CAP,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True)
class ExplorationProcessClaim:
    """Deterministic process claim for a set of semantic segment identities."""

    admitted_segment_ids: tuple[str, ...]
    duplicate_segment_ids: tuple[str, ...]
    amount: float
    cap: float = EXPLORATION_PROCESS_CAP


def claim_exploration_process(
    segments: tuple[ExplorationWorkSegment, ...],
    *,
    already_settled_segment_ids: tuple[str, ...] = (),
) -> ExplorationProcessClaim:
    """Claim the fixed allowance at most once and never above the fixed cap.

    The function deliberately has no token/call/time inputs.  Replays and multiple
    differently worded records for the same ``segment_id`` cannot increase value.
    """

    if not isinstance(segments, tuple) or any(not isinstance(item, ExplorationWorkSegment) for item in segments):
        raise TypeError("segments must be a tuple of ExplorationWorkSegment values")
    _strings("already_settled_segment_ids", already_settled_segment_ids)
    settled = set(already_settled_segment_ids)
    seen: set[str] = set()
    admitted: list[str] = []
    duplicates: list[str] = []
    for segment in segments:
        if segment.segment_id in settled or segment.segment_id in seen:
            duplicates.append(segment.segment_id)
            continue
        seen.add(segment.segment_id)
        admitted.append(segment.segment_id)
    amount = min(EXPLORATION_PROCESS_CAP, EXPLORATION_PROCESS_VALUE * len(admitted))
    return ExplorationProcessClaim(tuple(admitted), tuple(duplicates), amount)


__all__ = [
    "EXPLORATION_CONTRACT_VERSION",
    "EXPLORATION_PROCESS_CAP",
    "EXPLORATION_PROCESS_VALUE",
    "ExplorationProcessClaim",
    "ExplorationResultKind",
    "ExplorationWorkSegment",
    "claim_exploration_process",
]
