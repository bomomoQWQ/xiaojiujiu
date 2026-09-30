"""Disabled-by-default semantic judgement port for Runtime v2.

The real Jev/provider integration is intentionally deferred.  This module only
specifies the proposal contract and supplies deterministic disabled/fake adapters
for tests.  Providers may propose a judgement with grounded evidence; they never
write labels, parameters, expectations, emotion state, or decisions directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence


class JudgementStatus(str, Enum):
    """Provider result without conflating unavailable/unknown with neutral."""

    PROPOSED = "proposed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True, kw_only=True)
class JudgementRequestV2:
    """One bounded question over explicitly allowed evidence."""

    request_id: str
    question: str
    scope_key: str
    source_event_ids: tuple[str, ...]
    evidence_fragments: tuple[str, ...]
    contract_version: str
    state_version: int

    def __post_init__(self) -> None:
        for name in ("request_id", "question", "scope_key", "contract_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.state_version, int) or isinstance(self.state_version, bool):
            raise TypeError("state_version must be an integer")
        if self.state_version < 0:
            raise ValueError("state_version must be non-negative")
        if not isinstance(self.source_event_ids, tuple) or not isinstance(
            self.evidence_fragments, tuple
        ):
            raise TypeError("sources and evidence fragments must be tuples")
        if len(self.source_event_ids) != len(self.evidence_fragments):
            raise ValueError("each evidence fragment must have one source event id")
        if any(not item.strip() for item in (*self.source_event_ids, *self.evidence_fragments)):
            raise ValueError("sources and evidence fragments must be non-empty")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids must not contain duplicates")


@dataclass(frozen=True, slots=True, kw_only=True)
class JudgementProposalV2:
    """Grounded provider proposal; never an authoritative label write."""

    request_id: str
    status: JudgementStatus
    detector_version: str
    source_event_ids: tuple[str, ...]
    proposed_value: bool | None = None
    explanation: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.detector_version.strip():
            raise ValueError("request_id and detector_version must be non-empty")
        if not isinstance(self.status, JudgementStatus):
            raise TypeError("status must be a JudgementStatus")
        if self.status is JudgementStatus.PROPOSED:
            if not isinstance(self.proposed_value, bool):
                raise ValueError("a proposed judgement requires a boolean value")
            if not self.source_event_ids:
                raise ValueError("a proposed judgement requires grounded source events")
        elif self.proposed_value is not None:
            raise ValueError("non-proposed judgements cannot carry a value")
        if any(not item.strip() for item in self.source_event_ids):
            raise ValueError("source_event_ids must contain non-empty strings")


class SemanticJudgeV2(Protocol):
    """Small capability port implemented by disabled/fake/future Jev adapters."""

    def available(self) -> bool: ...

    def judge(self, request: JudgementRequestV2) -> JudgementProposalV2: ...


class DisabledSemanticJudgeV2:
    """Production default while real Jev integration remains postponed."""

    VERSION = "disabled-v2"

    def available(self) -> bool:
        return False

    def judge(self, request: JudgementRequestV2) -> JudgementProposalV2:
        if not isinstance(request, JudgementRequestV2):
            raise TypeError("request must be a JudgementRequestV2")
        return JudgementProposalV2(
            request_id=request.request_id,
            status=JudgementStatus.UNAVAILABLE,
            detector_version=self.VERSION,
            source_event_ids=(),
            explanation="semantic judgement is disabled",
        )


class FakeSemanticJudgeV2:
    """Deterministic test adapter keyed by request id; performs no network I/O."""

    VERSION = "fake-v2"

    def __init__(self, proposals: Sequence[JudgementProposalV2] = ()) -> None:
        self._proposals = {item.request_id: item for item in proposals}
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def judge(self, request: JudgementRequestV2) -> JudgementProposalV2:
        if not isinstance(request, JudgementRequestV2):
            raise TypeError("request must be a JudgementRequestV2")
        self.calls.append(request.request_id)
        proposal = self._proposals.get(request.request_id)
        if proposal is None:
            return JudgementProposalV2(
                request_id=request.request_id,
                status=JudgementStatus.UNKNOWN,
                detector_version=self.VERSION,
                source_event_ids=(),
                explanation="no fake judgement configured",
            )
        if not set(proposal.source_event_ids).issubset(request.source_event_ids):
            return JudgementProposalV2(
                request_id=request.request_id,
                status=JudgementStatus.REJECTED,
                detector_version=self.VERSION,
                source_event_ids=(),
                explanation="proposal referenced evidence outside the request",
            )
        return proposal


__all__ = [
    "DisabledSemanticJudgeV2",
    "FakeSemanticJudgeV2",
    "JudgementProposalV2",
    "JudgementRequestV2",
    "JudgementStatus",
    "SemanticJudgeV2",
]
