"""Concrete adapter from the existing :class:`Runtime` to the v2 coordinator.

The adapter reuses only the legacy foreground, candidate, hard-boundary and delivery
state machines.  It never reads ``Runtime.user_model`` or calls legacy motivation.
Post-legacy hooks in :mod:`api_v1` bypass this adapter, which prevents replaying a
foreground, render, or delivery transition that the wire handler already applied.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from . import boundaries as boundary_module
from .langchao_authority_repository import LangchaoAuthorityRepository
from .motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
from .repeat_v2 import RepeatSubjectV2, UserMatterEventKind, UserMatterEventV2
from .runtime_v2 import (
    BoundaryVerdictV2,
    CandidateV2,
    CommitReceiptV2,
    LegacyUserEventResult,
    SendAckV2,
)
from .typing import CandidateIntent
from .user_model_v2_labels import TargetObservationV2
from .user_model_v2_types import Target
from .utility import ensure_aware, utcnow


class ConcreteLegacyRuntimeV2Bridge:
    """Wrap one existing Runtime without consuming its learned model outputs."""

    def __init__(
        self,
        runtime: Any,
        *,
        coefficients: UserUtilityCoefficientsV2 | None = None,
        scope_key: str | None = None,
        dispatch_coordinator: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.scope_key = scope_key
        self.dispatch_coordinator = dispatch_coordinator
        self.coefficients = coefficients or UserUtilityCoefficientsV2(
            v_reply=1.0, v_continue=0.5, c_negative=1.0
        )
        self._legacy_candidates: dict[str, CandidateIntent] = {}

    def ingest_user_event(self, event: Mapping[str, Any]) -> LegacyUserEventResult:
        """Run the ordinary foreground exactly once, then translate its recorded facts."""

        stamp = _event_time(event)
        outcome = self.runtime.process_user_message(
            content=str(event.get("text") or event.get("content") or ""),
            conversation_id=str(event.get("session") or event.get("conversation_id") or "")
            or None,
            event_id=str(event.get("event_id") or "") or None,
            timestamp=stamp,
            metadata=dict(event.get("metadata") or {}),
        )
        return self.after_user_event(event=event, legacy_outcome=outcome)

    def after_user_event(
        self, *, event: Mapping[str, Any], legacy_outcome: Any
    ) -> LegacyUserEventResult:
        """Translate only mechanical facts from a completed foreground outcome.

        A user message attributed by the legacy state machine to one sent attempt is
        reliable structural reply evidence.  No acceptance/negative observation is
        manufactured: the legacy semantic/user-model output is not a reliable explicit
        feedback detector, so those targets remain unknown until a dedicated detector
        exists.
        """

        raw = getattr(legacy_outcome, "event", None)
        event_id = str(getattr(raw, "event_id", None) or event.get("event_id") or "")
        if not event_id:
            raise ValueError("a completed legacy user event must have an event_id")
        occurred_at = ensure_aware(getattr(raw, "timestamp", None)) or _event_time(event)
        duplicate = bool(getattr(legacy_outcome, "duplicate", False))
        attributed = str(getattr(legacy_outcome, "attributed_attempt_id", None) or "")
        exposure_id = (
            str(uuid5(NAMESPACE_URL, f"prepared-exposure:{self.runtime.config.conversation_id}:{attributed}"))
            if attributed else ""
        )
        observations: tuple[TargetObservationV2, ...] = ()
        if attributed and not duplicate:
            observations = (
                TargetObservationV2(
                    event_id=event_id,
                    target=Target.REPLY,
                    occurred_at=occurred_at,
                    value=True,
                    candidate_exposure_ids=(exposure_id,),
                    explicit=False,
                ),
            )

        # A resolved legacy matter is a mechanical user-originated progress signal.
        # Creation is not treated as REOPEN: the foreground rules cannot reliably
        # distinguish a genuinely reopened matter from a newly introduced one.
        matter_events = tuple(
            UserMatterEventV2(
                event_id=f"{event_id}:progress:{matter_id}",
                occurred_at_utc=occurred_at,
                kind=UserMatterEventKind.PROGRESS,
                concern_id=str(matter_id),
            )
            for matter_id in dict.fromkeys(
                str(item)
                for item in (getattr(legacy_outcome, "unfinished_resolved", ()) or ())
                if str(item).strip()
            )
        )
        return LegacyUserEventResult(
            event_id=event_id,
            occurred_at=occurred_at,
            observations=observations,
            matter_events=matter_events,
            duplicate=duplicate,
        )

    def candidates(self, *, scope_key: str, now: datetime) -> Sequence[CandidateV2]:
        del scope_key
        legacy = self.runtime.projections.candidates.list_active(
            limit=self.runtime.config.candidate.max_active
        )
        if not legacy:
            legacy = self.runtime.refresh_candidates_for_v2(now=now)
        self._legacy_candidates = {item.candidate_id: item for item in legacy}
        return tuple(self._candidate(item) for item in legacy)

    def boundary_verdict(
        self, *, candidate: CandidateV2, scope_key: str, now: datetime
    ) -> BoundaryVerdictV2:
        del scope_key
        item = self._legacy_candidate(candidate.candidate_id)
        state = self.runtime.state()
        active = self.runtime.projections.boundaries.active(now)
        global_verdict = boundary_module.evaluate(
            active, now=now, state=state, is_proactive=True
        )
        reasons: list[str] = []
        if not global_verdict.allow_proactive:
            reasons.extend(str(value) for value in global_verdict.blocking_ids)
            if not reasons:
                reasons.append("proactive_disabled")
        violation = boundary_module.blocks_candidate(
            active,
            now=now,
            subject=f"{item.target or ''} {item.intent or ''}",
            is_question=item.type in boundary_module.QUESTION_CANDIDATE_TYPES,
        )
        if violation is not None:
            boundary_id, reason = violation
            reasons.append(f"{boundary_id}:{reason}")
        return BoundaryVerdictV2(
            blocked=bool(reasons), reasons=tuple(dict.fromkeys(reasons))
        )

    def commit_candidate(
        self, *, decision_id: str, candidate: CandidateV2, now: datetime
    ) -> CommitReceiptV2:
        return self.commit_candidate_with_snapshot(
            decision_id=decision_id,
            candidate=candidate,
            now=now,
            persist_snapshot=lambda _receipt: None,
        )

    def commit_candidate_with_snapshot(
        self,
        *,
        decision_id: str,
        candidate: CandidateV2,
        now: datetime,
        persist_snapshot: Any,
    ) -> CommitReceiptV2:
        """Commit legacy delivery and its v2 recovery placeholder atomically.

        ``persist_snapshot`` is invoked inside the existing database transaction after the
        receipt identities exist.  It must only perform writes on that same connection;
        the coordinator supplies the repository write and does not call external services.
        """

        item = self._legacy_candidate(candidate.candidate_id)
        if not self.scope_key:
            raise RuntimeError("production v2 dispatch requires a scoped live authority coordinator")
        attempt_id = str(uuid5(NAMESPACE_URL, f"runtime-v2-attempt:{self.scope_key}:{decision_id}"))
        outbox_id = str(uuid5(NAMESPACE_URL, f"runtime-v2-render:{self.scope_key}:{decision_id}"))
        claim_id = str(uuid5(NAMESPACE_URL, f"runtime-v2-claim:{self.scope_key}:{decision_id}"))
        candidate_version = hashlib.sha256(
            json.dumps(dict(candidate.action), sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        with self.runtime.write_session():
            with self.runtime.db.transaction() as connection:
                raw = getattr(connection, "raw", connection)
                coordinator = self.dispatch_coordinator or LangchaoAuthorityRepository(
                    raw, scope_key=self.scope_key
                )
                coordinator.create_live_dispatch_claim(
                    claim_id=claim_id,
                    round_id=decision_id,
                    candidate_id=candidate.candidate_id,
                    candidate_version=candidate_version,
                    attempt_id=attempt_id,
                    render_outbox_id=outbox_id,
                    idempotency_key=f"runtime-v2:{decision_id}",
                    expected_engine="runtime_v2",
                    created_at=now,
                )
                state = self.runtime.projections.runtime.ensure()
                attempt_id, outbox_id = self.runtime._commit_attempt(
                    connection, chosen=item, state=state, now=now,
                    attempt_id=attempt_id, outbox_id=outbox_id,
                )
                row = self.runtime.projections.outbox.get(outbox_id)
                if row is None:
                    raise RuntimeError("legacy commit did not create its render outbox row")
                payload = dict(row.payload)
                payload.update({"decision_id": decision_id, "action": dict(candidate.action)})
                row.payload = payload
                self.runtime.projections.outbox.enqueue(connection, row)
                receipt = CommitReceiptV2(
                    decision_id=decision_id,
                    candidate_id=candidate.candidate_id,
                    attempt_id=attempt_id,
                    render_outbox_id=outbox_id,
                )
                persist_snapshot(receipt)
        return receipt

    def commit_langchao_candidate_with_snapshot(
        self,
        *,
        round_id: str,
        langchao_candidate_id: str,
        candidate_revision: int,
        candidate_version: str,
        source_candidate: CandidateV2,
        now: datetime,
        persist_snapshot: Any,
    ) -> CommitReceiptV2:
        """Atomically claim and commit the exact legacy candidate selected by 浪潮.

        ``source_candidate`` is provenance only: its action must still be the exact
        adapter view of the stored legacy candidate.  This prevents a live caller from
        substituting generated text or changing an action after numerical selection.
        """

        if not self.scope_key:
            raise RuntimeError("production 浪潮 dispatch requires a scoped live authority coordinator")
        if not isinstance(candidate_revision, int) or isinstance(candidate_revision, bool) or candidate_revision < 1:
            raise ValueError("candidate_revision must be a positive integer")
        if not candidate_version.strip():
            raise ValueError("candidate_version is required")
        item = self._legacy_candidate(source_candidate.candidate_id)
        exact = self._candidate(item)
        if exact != source_candidate:
            raise ValueError("浪潮 source candidate identity/provenance no longer matches legacy")
        attempt_id = str(uuid5(NAMESPACE_URL, f"langchao-attempt:{self.scope_key}:{round_id}"))
        outbox_id = str(uuid5(NAMESPACE_URL, f"langchao-render:{self.scope_key}:{round_id}"))
        claim_id = str(uuid5(NAMESPACE_URL, f"langchao-claim:{self.scope_key}:{round_id}"))
        with self.runtime.write_session():
            with self.runtime.db.transaction() as connection:
                raw = getattr(connection, "raw", connection)
                coordinator = self.dispatch_coordinator or LangchaoAuthorityRepository(
                    raw, scope_key=self.scope_key
                )
                # v18 is the mechanical delivery witness and therefore names the
                # exact legacy candidate inserted into action_attempts.  The v15
                # semantic claim below separately binds the selected 浪潮 revision.
                coordinator.create_live_dispatch_claim(
                    claim_id=claim_id,
                    round_id=round_id,
                    candidate_id=source_candidate.candidate_id,
                    candidate_version=candidate_version,
                    attempt_id=attempt_id,
                    render_outbox_id=outbox_id,
                    idempotency_key=f"langchao:{round_id}",
                    expected_engine="langchao",
                    created_at=now,
                )
                coordinator.create_dispatch_claim(
                    dispatch_id=f"semantic:{claim_id}",
                    candidate_id=langchao_candidate_id,
                    candidate_revision=candidate_revision,
                    attempt_id=attempt_id,
                    idempotency_key=f"langchao-semantic:{round_id}",
                    created_at=now,
                )
                state = self.runtime.projections.runtime.ensure()
                attempt_id, outbox_id = self.runtime._commit_attempt(
                    connection, chosen=item, state=state, now=now,
                    attempt_id=attempt_id, outbox_id=outbox_id,
                )
                row = self.runtime.projections.outbox.get(outbox_id)
                if row is None:
                    raise RuntimeError("legacy commit did not create its render outbox row")
                payload = dict(row.payload)
                payload.update({
                    "decision_id": round_id,
                    "engine": "langchao",
                    "langchao_candidate_id": langchao_candidate_id,
                    "langchao_candidate_revision": candidate_revision,
                    "source_candidate_id": source_candidate.candidate_id,
                    "action": dict(source_candidate.action),
                })
                row.payload = payload
                self.runtime.projections.outbox.enqueue(connection, row)
                receipt = CommitReceiptV2(
                    decision_id=round_id,
                    candidate_id=source_candidate.candidate_id,
                    attempt_id=attempt_id,
                    render_outbox_id=outbox_id,
                )
                persist_snapshot(receipt)
        return receipt

    def mark_rendered(self, *, decision_id: str, outbox_id: str, now: datetime) -> None:
        del decision_id
        result = self.runtime.reducer.complete_render(outbox_id=outbox_id, text="", now=now)
        if not result.applied:
            raise RuntimeError(f"legacy render transition was not applied: {result.reason}")

    def acknowledge_send(self, ack: SendAckV2) -> bool:
        result = self.runtime.reducer.mark_delivered(
            outbox_id=ack.send_outbox_id,
            now=ack.acknowledged_at,
            success=ack.sent,
            error=None if ack.sent else "delivery_failed",
        )
        return bool(result.get("delivered") and not result.get("duplicate"))

    def _legacy_candidate(self, candidate_id: str) -> CandidateIntent:
        item = self._legacy_candidates.get(candidate_id)
        if item is None:
            item = self.runtime.projections.candidates.get(candidate_id)
        if item is None:
            raise KeyError(f"unknown legacy candidate: {candidate_id}")
        return item

    def _candidate(self, item: CandidateIntent) -> CandidateV2:
        action = {
            "type": item.type,
            "intent": item.intent,
            "goal": item.goal,
            "target": item.target,
            "proactive": True,
            "follow_up": item.type in {"follow_up", "check_in"},
            "question": item.type in boundary_module.QUESTION_CANDIDATE_TYPES,
            "emotional_expression": item.type in {"share", "emotional_expression"},
            "topic_shift": item.type == "curious_question",
        }
        concern = next(
            (
                source.split(":", 1)[1]
                for source in item.sources
                if source.startswith("unfinished:") and source.split(":", 1)[1]
            ),
            None,
        )
        low_pressure_types = {"share", "contact", "reminder"}
        continuous_types = {"follow_up", "check_in"}
        sensitive_types = {"repair", "apology", "confront"}
        internal = float(item.internal_need) + max(
            float(item.unfinished_relevance), float(item.emotion_relevance)
        )
        source_ids: list[str] = []
        for source in item.sources:
            source_ids.extend(self.runtime._event_ids_behind(source))
        return CandidateV2(
            candidate_id=item.candidate_id,
            action=action,
            internal_utility=internal,
            coefficients=self.coefficients,
            repeat_subject=RepeatSubjectV2(
                concern_id=concern,
                action_goal_id=(item.goal.strip() or None),
            ),
            policy=CandidatePolicyV2(
                low_pressure=item.type in low_pressure_types,
                low_frequency=item.type not in continuous_types,
                easy_to_ignore=item.type in low_pressure_types,
                continuous_follow_up=item.type in continuous_types,
                sensitive=item.type in sensitive_types,
            ),
            source_event_ids=tuple(dict.fromkeys(source_ids)),
        )


def _event_time(event: Mapping[str, Any]) -> datetime:
    value = event.get("occurred_at") or event.get("timestamp")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    stamp = ensure_aware(value) or utcnow()
    if stamp.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    return stamp


# Public protocol-shaped name requested by the v2 composition boundary.
LegacyRuntimeV2BridgeAdapter = ConcreteLegacyRuntimeV2Bridge

__all__ = ["ConcreteLegacyRuntimeV2Bridge", "LegacyRuntimeV2BridgeAdapter"]
