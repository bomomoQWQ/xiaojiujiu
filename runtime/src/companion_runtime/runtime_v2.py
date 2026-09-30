"""Minimal runtime-v2 coordinator over legacy delivery boundaries.

The coordinator is deliberately a new composition root, not a subclass of the 3k-line
legacy :class:`Runtime`.  It owns only v2 ordering and decision policy:

* user events enter the legacy foreground boundary first (memory/candidates/boundaries), then
  settle v2 labels;
* endogenous decisions consume only v2 predictions, :mod:`motivation_v2`,
  :mod:`repeat_v2`, and :mod:`decision_v2_audit`;
* render/send still use the legacy attempt/outbox bridge; and
* a v2 exposure is prepared only after that bridge confirms a send acknowledgement.

There is intentionally no Jev integration and no import of ``user_model`` or ``motivation``.
The protocols below are the narrow seams a PostgreSQL composition root and the existing
``api_v1`` wire adapter need to implement.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence

from .decision_v2_audit import (
    CandidateAssessment,
    DecisionAuditRecorder,
    DecisionRun,
    DecisionStage,
)
from .motivation_v2 import (
    CandidatePolicyV2,
    UserUtilityCoefficientsV2,
    UserUtilityDecisionV2,
    user_utility,
)
from .repeat_v2 import (
    RepeatCostBreakdownV2,
    RepeatPolicyConfigV2,
    RepeatSubjectV2,
    SendAcknowledgedExposureV2,
    UserMatterEventV2,
    evaluate_repeat_v2,
)
from .user_model_v2_labels import SettlementContextV2, TargetObservationV2
from .user_model_v2_service import PreparedExposureV2, UserModelV2Service
from .user_model_v2_types import DeliveryBasis, Target, TargetPredictionV2

DECISION_POLICY_VERSION = "runtime-v2.0"
DECISION_CONTRACT_VERSION = "runtime-v2-coordinator.0"


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC (offset +00:00)")


def _softplus(value: float) -> float:
    """Stable ``log(1 + exp(value))`` used by the calibrated hazard."""

    if value > 50.0:
        return value
    if value < -50.0:
        return math.exp(value)
    return math.log1p(math.exp(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateV2:
    """Legacy-produced candidate facts consumed by the v2 decision path."""

    candidate_id: str
    action: Mapping[str, Any]
    internal_utility: float
    coefficients: UserUtilityCoefficientsV2
    repeat_subject: RepeatSubjectV2
    policy: CandidatePolicyV2
    source_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundaryVerdictV2:
    blocked: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionSetV2:
    """One persisted v2 prediction snapshot for a candidate."""

    snapshot_id: str
    parameter_version: str
    reply: TargetPredictionV2
    continuation: TargetPredictionV2
    negative: TargetPredictionV2

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or not self.parameter_version.strip():
            raise ValueError("prediction identity and parameter_version are required")
        expected = (
            (self.reply, Target.REPLY),
            (self.continuation, Target.CONTINUE),
            (self.negative, Target.NEGATIVE),
        )
        for prediction, target in expected:
            if prediction.target is not target:
                raise ValueError(f"prediction set requires target {target.value!r}")
        if len({item.scope_key for item, _target in expected}) != 1:
            raise ValueError("all predictions must have the same scope")


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyUserEventResult:
    """Facts produced by the existing foreground path, without legacy model output."""

    event_id: str
    occurred_at: datetime
    observations: tuple[TargetObservationV2, ...] = ()
    matter_events: tuple[UserMatterEventV2, ...] = ()
    duplicate: bool = False

    def __post_init__(self) -> None:
        _require_utc("occurred_at", self.occurred_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitReceiptV2:
    decision_id: str
    candidate_id: str
    attempt_id: str
    render_outbox_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SendAckV2:
    """Canonical send outcome translated from the ``api_v1`` action report."""

    decision_id: str
    attempt_id: str
    send_outbox_id: str
    acknowledged_at: datetime
    sent: bool
    action: Mapping[str, Any]
    context_provider: Callable[[], Mapping[str, Any]]
    source_event_ids: tuple[str, ...] = ()
    delivery_basis: DeliveryBasis = DeliveryBasis.DELIVERED

    def __post_init__(self) -> None:
        _require_utc("acknowledged_at", self.acknowledged_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionConfigV2:
    utility_threshold: float = 0.0
    # Freeze the calibrated legacy beta defaults while v2 policy is introduced.
    hazard_base: float = 0.000030
    hazard_beta: float = 4.0
    repeat: RepeatPolicyConfigV2 = field(default_factory=RepeatPolicyConfigV2)
    horizons: Mapping[Target, int] = field(
        default_factory=lambda: {
            Target.REPLY: 30 * 60,
            Target.ACCEPTANCE: 24 * 60 * 60,
            Target.CONTINUE: 2 * 60 * 60,
            Target.NEGATIVE: 24 * 60 * 60,
        }
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("utility_threshold", self.utility_threshold),
            ("hazard_base", self.hazard_base),
            ("hazard_beta", self.hazard_beta),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.hazard_base < 0:
            raise ValueError("hazard_base must be non-negative")
        if self.hazard_beta <= 0:
            raise ValueError("hazard_beta must be positive")
        if set(self.horizons) != set(Target):
            raise ValueError("horizons must contain every v2 target exactly once")


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateDecisionV2:
    candidate: CandidateV2
    predictions: PredictionSetV2
    user_utility: UserUtilityDecisionV2
    repeat: RepeatCostBreakdownV2
    net_utility: float
    blocked: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class EndogenousDecisionV2:
    decision_id: str
    acted: bool
    reason: str
    assessments: tuple[CandidateDecisionV2, ...]
    chosen_candidate_id: str | None = None
    commit: CommitReceiptV2 | None = None
    audit: Mapping[str, Any] = field(default_factory=dict)


class LegacyRuntimeV2Bridge(Protocol):
    """Only legacy capabilities retained by the v2 production path."""

    def ingest_user_event(self, event: Mapping[str, Any]) -> LegacyUserEventResult: ...

    def candidates(self, *, scope_key: str, now: datetime) -> Sequence[CandidateV2]: ...

    def boundary_verdict(
        self, *, candidate: CandidateV2, scope_key: str, now: datetime
    ) -> BoundaryVerdictV2: ...

    def commit_candidate(
        self, *, decision_id: str, candidate: CandidateV2, now: datetime
    ) -> CommitReceiptV2: ...

    def mark_rendered(self, *, decision_id: str, outbox_id: str, now: datetime) -> None: ...

    def acknowledge_send(self, ack: SendAckV2) -> bool: ...


class V2RuntimeRepository(Protocol):
    """PostgreSQL-facing queries not owned by the pure user-model service."""

    def prediction_for(
        self, *, scope_key: str, candidate: CandidateV2, now: datetime
    ) -> PredictionSetV2: ...

    def acknowledged_exposures(
        self, *, scope_key: str, now: datetime
    ) -> Sequence[SendAcknowledgedExposureV2]: ...

    def user_matter_events(
        self, *, scope_key: str, now: datetime
    ) -> Sequence[UserMatterEventV2]: ...

    def settleable_exposures(
        self, *, scope_key: str, observations: Sequence[TargetObservationV2], as_of: datetime
    ) -> Sequence[PreparedExposureV2]: ...

    def append_user_matter_events(
        self, *, scope_key: str, events: Sequence[UserMatterEventV2]
    ) -> None: ...

    def save_decision_audit(self, *, decision_id: str, audit: Mapping[str, Any]) -> None: ...


class V2RuntimeCoordinator:
    """Small orchestration layer suitable for ``api_v1`` delegation.

    The constructor deliberately has no legacy user model or legacy motivation dependency.
    A production instance should receive ``UserModelV2Service`` backed by PostgreSQL and a
    ``V2RuntimeRepository`` implemented by that same PostgreSQL adapter.
    """

    def __init__(
        self,
        *,
        scope_key: str,
        legacy: LegacyRuntimeV2Bridge,
        user_model: UserModelV2Service,
        repository: V2RuntimeRepository,
        config: DecisionConfigV2 | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not scope_key.strip():
            raise ValueError("scope_key is required")
        self.scope_key = scope_key
        self.legacy = legacy
        self.user_model = user_model
        self.repository = repository
        self.config = config or DecisionConfigV2()
        self.rng = rng or random.Random()
        self._audits: dict[str, DecisionAuditRecorder] = {}
        self._chosen: dict[str, CandidateV2] = {}

    def process_user_event(self, event: Mapping[str, Any]) -> LegacyUserEventResult:
        """Run legacy memory/candidate/boundary effects, then settle v2 observations."""

        result = self.legacy.ingest_user_event(event)
        if result.duplicate:
            return result
        if result.matter_events:
            self.repository.append_user_matter_events(
                scope_key=self.scope_key, events=result.matter_events
            )
        if not result.observations:
            return result
        prepared = self.repository.settleable_exposures(
            scope_key=self.scope_key,
            observations=result.observations,
            as_of=result.occurred_at,
        )
        context = SettlementContextV2(as_of=result.occurred_at)
        for exposure in prepared:
            self.user_model.settle_observation(
                prepared=exposure,
                observations=result.observations,
                context=context,
            )
        return result

    def decide_endogenous(
        self,
        *,
        decision_id: str,
        now: datetime,
        elapsed_allowed_seconds: float,
    ) -> EndogenousDecisionV2:
        """Evaluate legacy candidates using v2 predictions and policy only."""

        if not decision_id.strip():
            raise ValueError("decision_id is required")
        _require_utc("now", now)
        if elapsed_allowed_seconds < 0 or not math.isfinite(elapsed_allowed_seconds):
            raise ValueError("elapsed_allowed_seconds must be finite and non-negative")
        candidates = tuple(self.legacy.candidates(scope_key=self.scope_key, now=now))
        history = tuple(
            self.repository.acknowledged_exposures(scope_key=self.scope_key, now=now)
        )
        matter_events = tuple(
            self.repository.user_matter_events(scope_key=self.scope_key, now=now)
        )
        assessed: list[CandidateDecisionV2] = []
        audit_assessments: list[CandidateAssessment] = []
        for candidate in candidates:
            boundary = self.legacy.boundary_verdict(
                candidate=candidate, scope_key=self.scope_key, now=now
            )
            predictions = self.repository.prediction_for(
                scope_key=self.scope_key, candidate=candidate, now=now
            )
            utility = user_utility(
                reply=predictions.reply,
                continuation=predictions.continuation,
                negative=predictions.negative,
                coefficients=candidate.coefficients,
                candidate=candidate.policy,
                boundary_blocked=boundary.blocked,
                boundary_reasons=boundary.reasons,
            )
            repeat = evaluate_repeat_v2(
                subject=candidate.repeat_subject,
                exposure_history=history,
                now_utc=now,
                config=self.config.repeat,
                user_matter_events=matter_events,
            )
            net = float(candidate.internal_utility) + utility.utility - repeat.total_cost
            reasons = tuple(dict.fromkeys((*utility.reasons, *repeat.hard_limit_reasons)))
            blocked = utility.blocked or repeat.blocked
            item = CandidateDecisionV2(
                candidate=candidate,
                predictions=predictions,
                user_utility=utility,
                repeat=repeat,
                net_utility=net,
                blocked=blocked,
                reasons=reasons,
            )
            assessed.append(item)
            audit_assessments.append(
                CandidateAssessment(
                    candidate_id=candidate.candidate_id,
                    prediction_snapshot_id=predictions.snapshot_id,
                    used_bounds=utility.used_bounds.to_dict(),
                    utility_terms={
                        **utility.decomposition.to_dict(),
                        "internal": float(candidate.internal_utility),
                        "repeat_cost": -float(repeat.total_cost),
                        "net": net,
                    },
                    repeat_key=(
                        candidate.repeat_subject.action_goal_id
                        or candidate.repeat_subject.concern_id
                        or "candidate:" + candidate.candidate_id
                    ),
                    reasons=reasons or ("eligible",),
                )
            )

        eligible = [
            item
            for item in assessed
            if not item.blocked and item.net_utility >= self.config.utility_threshold
        ]
        chosen = max(eligible, key=lambda item: item.net_utility, default=None)
        advantage = (
            0.0 if chosen is None else chosen.net_utility - self.config.utility_threshold
        )
        # Preserve the calibrated legacy hazard form.  The supplied interval is the
        # accumulated *allowed* time (foreground pauses/boundary-blocked time excluded by
        # the scheduler), not wall time since the previous call.
        hazard_rate = (
            0.0
            if chosen is None
            else self.config.hazard_base * _softplus(self.config.hazard_beta * advantage)
        )
        cumulative = hazard_rate * elapsed_allowed_seconds
        probability = None if chosen is None else 1.0 - math.exp(-cumulative)
        draw = None if chosen is None else self.rng.random()
        parameter_version = (
            chosen.predictions.parameter_version
            if chosen is not None
            else (assessed[0].predictions.parameter_version if assessed else "none")
        )
        run = DecisionRun(
            decision_id=decision_id,
            scope=self.scope_key,
            policy_version=DECISION_POLICY_VERSION,
            contract_version=DECISION_CONTRACT_VERSION,
            feature_version="user-model-v2.0",
            parameter_version=parameter_version,
            D=advantage,
            lambda_rate=hazard_rate,
            delta_allowed_seconds=elapsed_allowed_seconds,
            cumulative_lambda=cumulative,
            trial_probability=probability,
            random_draw=draw,
            chosen=None if chosen is None else chosen.candidate.candidate_id,
        )
        recorder = DecisionAuditRecorder(run, tuple(audit_assessments))
        recorder.record(DecisionStage.WAKE, occurred_at=now)
        recorder.record(DecisionStage.PERMISSIONS, occurred_at=now)
        self._audits[decision_id] = recorder

        if chosen is None:
            recorder.record(
                DecisionStage.RECONCILED,
                occurred_at=now,
                details={"reason": "no_eligible_candidate"},
            )
            audit = self._save_audit(recorder)
            return EndogenousDecisionV2(
                decision_id=decision_id,
                acted=False,
                reason="no_eligible_candidate",
                assessments=tuple(assessed),
                audit=audit,
            )

        recorder.record(
            DecisionStage.CANDIDATE_ELIGIBLE,
            occurred_at=now,
            details={"candidate_id": chosen.candidate.candidate_id},
        )
        assert probability is not None and draw is not None
        recorder.record(
            DecisionStage.HAZARD_TRIAL_PERFORMED,
            occurred_at=now,
            details={"probability": probability, "draw": draw},
        )
        if not draw < probability:
            recorder.record(
                DecisionStage.RECONCILED,
                occurred_at=now,
                details={"reason": "hazard_not_won"},
            )
            audit = self._save_audit(recorder)
            return EndogenousDecisionV2(
                decision_id=decision_id,
                acted=False,
                reason="hazard_not_won",
                assessments=tuple(assessed),
                chosen_candidate_id=chosen.candidate.candidate_id,
                audit=audit,
            )

        recorder.record(DecisionStage.HAZARD_TRIAL_WON, occurred_at=now)
        commit = self.legacy.commit_candidate(
            decision_id=decision_id, candidate=chosen.candidate, now=now
        )
        recorder.record(
            DecisionStage.COMMITTED,
            occurred_at=now,
            details={"attempt_id": commit.attempt_id, "outbox_id": commit.render_outbox_id},
        )
        self._chosen[decision_id] = chosen.candidate
        audit = self._save_audit(recorder)
        return EndogenousDecisionV2(
            decision_id=decision_id,
            acted=True,
            reason="committed",
            assessments=tuple(assessed),
            chosen_candidate_id=chosen.candidate.candidate_id,
            commit=commit,
            audit=audit,
        )

    def mark_rendered(self, *, decision_id: str, outbox_id: str, now: datetime) -> None:
        """Mirror a successful legacy render into the v2 audit."""

        recorder = self._require_audit(decision_id)
        self.legacy.mark_rendered(decision_id=decision_id, outbox_id=outbox_id, now=now)
        recorder.record(
            DecisionStage.RENDERED, occurred_at=now, details={"outbox_id": outbox_id}
        )
        self._save_audit(recorder)

    def acknowledge_send(self, ack: SendAckV2) -> PreparedExposureV2 | None:
        """Apply the legacy acknowledgement, then and only then prepare a v2 exposure."""

        recorder = self._require_audit(ack.decision_id)
        confirmed = self.legacy.acknowledge_send(ack)
        sent = bool(ack.sent and confirmed)
        recorder.record(
            DecisionStage.SEND_ACK if sent else DecisionStage.SEND_FAIL,
            occurred_at=ack.acknowledged_at,
            details={"outbox_id": ack.send_outbox_id, "attempt_id": ack.attempt_id},
        )
        prepared: PreparedExposureV2 | None = None
        if sent:
            chosen = self._chosen[ack.decision_id]
            prepared = self.user_model.prepare_exposure(
                scope_key=self.scope_key,
                exposure_id=ack.attempt_id,
                idempotency_key=f"send-ack:{ack.send_outbox_id}",
                occurred_at=ack.acknowledged_at,
                action=ack.action,
                context_provider=ack.context_provider,
                delivery_confirmed=True,
                horizons=self.config.horizons,
                delivery_basis=ack.delivery_basis,
                source_event_ids=tuple(
                    dict.fromkeys((*chosen.source_event_ids, *ack.source_event_ids))
                ),
            )
        self._save_audit(recorder)
        return prepared

    def reconcile(self, *, decision_id: str, now: datetime, reason: str) -> None:
        recorder = self._require_audit(decision_id)
        recorder.record(DecisionStage.RECONCILED, occurred_at=now, details={"reason": reason})
        self._save_audit(recorder)

    def _require_audit(self, decision_id: str) -> DecisionAuditRecorder:
        try:
            return self._audits[decision_id]
        except KeyError as exc:
            raise KeyError(f"unknown in-process decision_id: {decision_id}") from exc

    def _save_audit(self, recorder: DecisionAuditRecorder) -> Mapping[str, Any]:
        payload = recorder.to_dict()
        self.repository.save_decision_audit(
            decision_id=recorder.run.decision_id, audit=payload
        )
        return payload


__all__ = [
    "BoundaryVerdictV2",
    "CandidateDecisionV2",
    "CandidateV2",
    "CommitReceiptV2",
    "DECISION_CONTRACT_VERSION",
    "DECISION_POLICY_VERSION",
    "DecisionConfigV2",
    "EndogenousDecisionV2",
    "LegacyRuntimeV2Bridge",
    "LegacyUserEventResult",
    "PredictionSetV2",
    "SendAckV2",
    "V2RuntimeCoordinator",
    "V2RuntimeRepository",
]
