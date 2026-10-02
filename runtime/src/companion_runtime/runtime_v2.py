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
from .actual_action_v21 import actual_action_for_exposure
from .render_plan_v1 import freeze_action_render_plan
from .user_model_v2_types import DeliveryBasis, Target, TargetPredictionV2

DECISION_POLICY_VERSION = "runtime-v2.0"
DECISION_CONTRACT_VERSION = "runtime-v2-coordinator.0"
COMMITTED_DECISION_SNAPSHOT_VERSION = 1


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
    """Legacy-produced candidate with its render design frozen before prediction."""

    candidate_id: str
    action: Mapping[str, Any]
    internal_utility: float
    coefficients: UserUtilityCoefficientsV2
    repeat_subject: RepeatSubjectV2
    policy: CandidatePolicyV2
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, Mapping):
            raise TypeError("candidate action must be a mapping")
        object.__setattr__(self, "action", freeze_action_render_plan(self.action))


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
    dispatch_claim_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CommittedDecisionV2:
    """Durable facts needed to finish one committed delivery after restart."""

    decision_id: str
    scope_key: str
    candidate: CandidateV2
    predictions: PredictionSetV2
    cold_start_exploration: bool
    audit: Mapping[str, Any]
    attempt_id: str
    render_outbox_id: str
    committed_at: datetime
    snapshot_version: int = COMMITTED_DECISION_SNAPSHOT_VERSION
    terminal_ack_id: str | None = None
    terminal_status: str | None = None


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
    actual_action_witness: Mapping[str, Any] | None = None
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
    #: Bounded cold-start exploration.  While every target head is still cold the
    #: conservative bounds charge the full negative range against a structurally zero
    #: benefit, so a candidate can never clear ``utility_threshold`` -- yet the only
    #: way to earn data is to send once.  ``user_utility`` already decides whether a
    #: candidate is *safe* to explore (low pressure, low frequency, ignorable,
    #: non-sensitive, not a continuous follow-up); this switch lets that verdict
    #: actually authorise a delivery, capped by an explicit spend budget.
    cold_start_exploration_reason: str = "limited_support_safe_exploration_allowed"
    cold_start_exploration_limit: int = 3
    cold_start_exploration_window: timedelta = timedelta(hours=24)
    #: A rolling daily cap alone makes the allowance spend in a burst: once the window
    #: frees, nothing stops three unprompted messages landing within a couple of hours.
    #: Spacing is what turns "three per day" into "spread across the day".
    cold_start_exploration_min_spacing: timedelta = timedelta(hours=8)
    #: An exploration delivery has no measured edge, so its hazard advantage would be
    #: negative and the calibrated base rate (3e-5/s) would postpone the first send by
    #: days.  Give the exploration at least this much advantage; the budget, not the
    #: hazard, is what bounds how often she may spend it.
    cold_start_exploration_advantage: float = 2.0

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
        if not isinstance(self.cold_start_exploration_reason, str) or not (
            self.cold_start_exploration_reason.strip()
        ):
            raise ValueError("cold_start_exploration_reason must be a non-empty string")
        if (
            isinstance(self.cold_start_exploration_limit, bool)
            or not isinstance(self.cold_start_exploration_limit, int)
            or self.cold_start_exploration_limit < 0
        ):
            raise ValueError("cold_start_exploration_limit must be a non-negative integer")
        if not isinstance(self.cold_start_exploration_window, timedelta) or (
            self.cold_start_exploration_window <= timedelta(0)
        ):
            raise ValueError("cold_start_exploration_window must be a positive timedelta")
        if not isinstance(self.cold_start_exploration_min_spacing, timedelta) or (
            self.cold_start_exploration_min_spacing < timedelta(0)
        ):
            raise ValueError(
                "cold_start_exploration_min_spacing must be a non-negative timedelta"
            )
        if (
            isinstance(self.cold_start_exploration_advantage, bool)
            or not isinstance(self.cold_start_exploration_advantage, (int, float))
            or not math.isfinite(float(self.cold_start_exploration_advantage))
        ):
            raise ValueError("cold_start_exploration_advantage must be finite")


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
    """Only legacy capabilities retained by the v2 production path.

    ``ingest_user_event`` is used by callers that enter through the v2 composition
    root itself.  The AstrBot v1 adapter already ran the legacy foreground path, so
    it must instead call :meth:`V2RuntimeCoordinator.after_legacy_user_event` with
    the translated legacy outcome.  Keeping those boundaries separate prevents a
    wire event from being appended twice.
    """

    def ingest_user_event(self, event: Mapping[str, Any]) -> LegacyUserEventResult: ...

    def after_user_event(
        self, *, event: Mapping[str, Any], legacy_outcome: Any
    ) -> LegacyUserEventResult: ...

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

    def save_committed_decision(self, *, committed: CommittedDecisionV2) -> None: ...

    def recover_committed_decision(
        self, *, scope_key: str, decision_id: str
    ) -> CommittedDecisionV2 | None: ...

    def mark_committed_decision_ack_once(
        self,
        *,
        scope_key: str,
        decision_id: str,
        ack_id: str,
        status: str,
        acknowledged_at: datetime,
    ) -> bool: ...

    def get_acknowledged_prepared_exposure(
        self, *, scope_key: str, idempotency_key: str
    ) -> PreparedExposureV2 | None: ...

    def prepare_exposure_and_expectation(
        self,
        *,
        user_model: UserModelV2Service,
        scope_key: str,
        exposure_id: str,
        idempotency_key: str,
        occurred_at: datetime,
        action: Mapping[str, Any],
        context_provider: Callable[[], Mapping[str, Any]],
        horizons: Mapping[Target, int],
        delivery_basis: DeliveryBasis,
        source_event_ids: tuple[str, ...],
        concern_id: str | None = None,
        action_goal_id: str | None = None,
        cold_start_exploration: bool = False,
    ) -> PreparedExposureV2: ...


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
        goal_lifecycle_service: Any | None = None,
    ) -> None:
        if not scope_key.strip():
            raise ValueError("scope_key is required")
        self.scope_key = scope_key
        self.legacy = legacy
        self.user_model = user_model
        self.repository = repository
        self.config = config or DecisionConfigV2()
        self.rng = rng or random.Random()
        self.goal_lifecycle_service = goal_lifecycle_service
        self.goal_terminal_producer: Any | None = None
        self._audits: dict[str, DecisionAuditRecorder] = {}
        self._chosen: dict[str, CandidateV2] = {}
        self._explored: dict[str, bool] = {}

    def _exploration_available(self, *, now: datetime) -> bool:
        """Return whether cold-start exploration may be spent right now.

        Two independent caps, because they answer different questions: the rolling
        window answers "how much of this allowance has this person already received
        today", and the spacing answers "would spending it now arrive as a second
        unprompted message minutes after the first".

        A repository that cannot answer either question is read as "nothing spent
        yet".  That is the deliberate direction: the counter exists only to *limit*
        exploration, so a test double without it keeps the allowance reachable
        instead of silently disabling the only path out of a cold start.
        """

        if self.config.cold_start_exploration_limit <= 0:
            return False
        counter = getattr(self.repository, "count_cold_start_explorations", None)
        if callable(counter):
            spent = int(
                counter(
                    scope_key=self.scope_key,
                    since=now - self.config.cold_start_exploration_window,
                )
                or 0
            )
            if spent >= self.config.cold_start_exploration_limit:
                return False
        if self.config.cold_start_exploration_min_spacing > timedelta(0):
            last = getattr(self.repository, "last_cold_start_exploration_at", None)
            if callable(last):
                previous = last(scope_key=self.scope_key)
                if previous is not None:
                    _require_utc("last cold-start exploration", previous)
                    if (
                        now - previous < self.config.cold_start_exploration_min_spacing
                    ):
                        return False
        return True

    def produce_goal_terminal_event(
        self,
        *,
        evidence: Mapping[str, Any],
        goal: Any,
        candidates: Sequence[Any] = (),
    ) -> Any | None:
        """Route explicit terminal evidence to the production lifecycle service.

        The coordinator is the common runtime event boundary.  It intentionally
        ignores summaries, elapsed-time hints, and every unrecognised event kind;
        only a producer that supplies explicit ``completed`` or ``cancelled``
        evidence can close a goal.
        """

        kind = evidence.get("kind")
        if kind not in {"completed", "cancelled"}:
            return None
        if self.goal_lifecycle_service is None:
            raise RuntimeError("goal lifecycle service is not configured")
        occurred_at = evidence.get("occurred_at")
        if not isinstance(occurred_at, datetime):
            raise ValueError("terminal evidence occurred_at must be a datetime")
        actual_outcomes = evidence.get("actual_outcomes", ())
        if actual_outcomes is None:
            actual_outcomes = ()
        if not isinstance(actual_outcomes, (tuple, list)):
            raise TypeError("actual_outcomes must be a sequence")
        evidence_refs = evidence.get("evidence_refs", ())
        if not isinstance(evidence_refs, (tuple, list)):
            raise TypeError("evidence_refs must be a sequence")
        from .langchao_goal_lifecycle_service import GoalLifecycleEvent

        event = GoalLifecycleEvent(
            kind=str(kind), occurred_at=occurred_at,
            scope_key=str(evidence.get("scope_key") or ""),
            goal_id=str(evidence.get("goal_id") or ""),
            episode_id=str(evidence.get("episode_id") or ""),
            evidence_refs=tuple(evidence_refs),
            actual_outcomes=tuple(actual_outcomes),
        )
        if (
            event.scope_key != self.scope_key
            or event.scope_key != getattr(goal, "scope_key", None)
            or event.goal_id != getattr(goal, "goal_id", None)
            or event.episode_id != getattr(goal, "episode_id", None)
        ):
            raise ValueError("terminal evidence does not match exact scope/goal/episode")
        return self.goal_lifecycle_service.apply_terminal_event(
            event, goal=goal, candidates=tuple(candidates),
        )

    def cancel_goal(self, fact: Any) -> Any | None:
        """Route one explicit structured cancellation fact to the terminal producer."""
        if self.goal_terminal_producer is None:
            raise RuntimeError("goal terminal producer is not configured")
        return self.goal_terminal_producer.from_cancellation(fact)

    def process_user_event(self, event: Mapping[str, Any]) -> LegacyUserEventResult:
        """Run legacy ingest once, then settle the resulting v2 observations."""

        return self._settle_after_user_event(self.legacy.ingest_user_event(event))

    def after_legacy_user_event(
        self, *, event: Mapping[str, Any], legacy_outcome: Any
    ) -> LegacyUserEventResult:
        """Continue v2 processing after another entry point completed legacy ingest.

        This is the hook for ``api_v1``.  It deliberately never calls
        ``legacy.ingest_user_event``: the wire handler has already invoked
        ``Runtime.process_user_message`` and doing so again would double-write the
        raw event and its foreground projections.  The bridge translates that
        recorded legacy outcome into the small, v2-owned result contract.
        """

        result = self.legacy.after_user_event(
            event=event, legacy_outcome=legacy_outcome
        )
        return self._settle_after_user_event(result)

    def _settle_after_user_event(
        self, result: LegacyUserEventResult
    ) -> LegacyUserEventResult:
        """Persist matter events and settle observations from one legacy outcome."""

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

    def assess_endogenous(
        self,
        *,
        decision_id: str,
        now: datetime,
        elapsed_allowed_seconds: float,
    ) -> EndogenousDecisionV2:
        """Purely assess candidates without hazard, commit, or decision-audit writes.

        This is the comparator phase used when another engine owns live authority.  It
        deliberately returns the same candidate assessments as the ordinary v2 path,
        but stops before drawing randomness or constructing/persisting an audit.
        """

        return self.decide_endogenous(
            decision_id=decision_id,
            now=now,
            elapsed_allowed_seconds=elapsed_allowed_seconds,
            commit=False,
        )

    def decide_endogenous(
        self,
        *,
        decision_id: str,
        now: datetime,
        elapsed_allowed_seconds: float,
        commit: bool = True,
    ) -> EndogenousDecisionV2:
        """Evaluate candidates and, unless ``commit=False``, run hazard/commit."""

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

        if not isinstance(commit, bool):
            raise TypeError("commit must be bool")
        if not commit:
            # Important ordering invariant: no exploration counter query, random draw,
            # DecisionAuditRecorder construction, audit persistence, or legacy commit
            # is reachable from the comparator phase.
            eligible = [
                item for item in assessed
                if not item.blocked and item.net_utility >= self.config.utility_threshold
            ]
            comparator = max(eligible, key=lambda item: item.net_utility, default=None)
            return EndogenousDecisionV2(
                decision_id=decision_id,
                acted=False,
                reason="assessment_only",
                assessments=tuple(assessed),
                chosen_candidate_id=(
                    None if comparator is None else comparator.candidate.candidate_id
                ),
            )

        exploration_available = self._exploration_available(now=now)

        eligible = [
            item
            for item in assessed
            if not item.blocked and item.net_utility >= self.config.utility_threshold
        ]
        chosen = max(eligible, key=lambda item: item.net_utility, default=None)
        # A hard boundary, a repeat limit or an unsafe candidate is never overridden:
        # exploration only replaces the *numeric* threshold, which is uninformative
        # while every head is cold.
        explored = False
        if chosen is None and exploration_available:
            explorable = [
                item
                for item in assessed
                if not item.blocked
                and self.config.cold_start_exploration_reason in item.reasons
            ]
            chosen = max(explorable, key=lambda item: item.net_utility, default=None)
            explored = chosen is not None
        advantage = (
            0.0
            if chosen is None
            else max(
                chosen.net_utility - self.config.utility_threshold,
                self.config.cold_start_exploration_advantage if explored else 0.0,
            )
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
            details={
                "candidate_id": chosen.candidate.candidate_id,
                "cold_start_exploration": explored,
            },
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
        save_committed = getattr(self.repository, "save_committed_decision", None)

        def persist_committed(commit: CommitReceiptV2) -> Mapping[str, Any]:
            recorder.record(
                DecisionStage.COMMITTED,
                occurred_at=now,
                details={"attempt_id": commit.attempt_id, "outbox_id": commit.render_outbox_id},
            )
            audit_payload = self._save_audit(recorder)
            if callable(save_committed):
                save_committed(
                    committed=CommittedDecisionV2(
                        decision_id=decision_id,
                        scope_key=self.scope_key,
                        candidate=chosen.candidate,
                        predictions=chosen.predictions,
                        cold_start_exploration=explored,
                        audit=audit_payload,
                        attempt_id=commit.attempt_id,
                        render_outbox_id=commit.render_outbox_id,
                        committed_at=now,
                    )
                )
            return audit_payload

        atomic_commit = getattr(self.legacy, "commit_candidate_with_snapshot", None)
        if callable(atomic_commit) and callable(save_committed):
            commit = atomic_commit(
                decision_id=decision_id,
                candidate=chosen.candidate,
                now=now,
                persist_snapshot=persist_committed,
            )
            audit = recorder.to_dict()
        else:
            # Compatibility for old protocol fakes. Production's concrete bridge exposes
            # the atomic hook above so attempt/outbox and recovery placeholder commit in
            # one PostgreSQL transaction.
            commit = self.legacy.commit_candidate(
                decision_id=decision_id, candidate=chosen.candidate, now=now
            )
            audit = persist_committed(commit)
        self._chosen[decision_id] = chosen.candidate
        self._explored[decision_id] = explored
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
        """Apply a render through the legacy bridge, then record it in v2."""

        self.legacy.mark_rendered(decision_id=decision_id, outbox_id=outbox_id, now=now)
        self.after_legacy_rendered(decision_id=decision_id, outbox_id=outbox_id, now=now)

    def after_legacy_rendered(
        self, *, decision_id: str, outbox_id: str, now: datetime
    ) -> None:
        """Record a render which the v1 wire already applied to legacy state."""

        recorder = self._require_audit(decision_id)
        recorder.record(
            DecisionStage.RENDERED, occurred_at=now, details={"outbox_id": outbox_id}
        )
        self._save_audit(recorder)

    def acknowledge_send(self, ack: SendAckV2) -> PreparedExposureV2 | None:
        """Apply the legacy acknowledgement, then and only then prepare a v2 exposure."""

        confirmed = self.legacy.acknowledge_send(ack)
        return self.after_legacy_send_ack(ack, confirmed=confirmed)

    def after_legacy_send_ack(
        self, ack: SendAckV2, *, confirmed: bool = True
    ) -> PreparedExposureV2 | None:
        """Record a send acknowledgement which the v1 wire already applied.

        Unlike :meth:`acknowledge_send`, this method never calls the legacy bridge.
        It is therefore safe after ``Reducer.mark_delivered`` and cannot charge the
        legacy delivery or append its outgoing events twice.  If the process restarted
        after commit, all decision facts are restored from the immutable committed
        snapshot; the current candidate pool and prediction parameters are never queried.
        """

        sent = bool(ack.sent and confirmed)
        ack_idempotency_key = f"send-ack:{ack.send_outbox_id}"
        committed = self._recover_committed(ack.decision_id)
        if committed is not None:
            if committed.scope_key != self.scope_key:
                raise ValueError("committed decision belongs to a different scope")
            if committed.attempt_id != ack.attempt_id:
                raise ValueError("send acknowledgement attempt does not match committed decision")
            if committed.terminal_ack_id is not None:
                if committed.terminal_ack_id != ack.send_outbox_id:
                    raise ValueError("committed decision already has a different terminal acknowledgement")
                if committed.terminal_status != ("sent" if sent else "failed"):
                    raise ValueError("committed decision terminal acknowledgement conflicts")
                if sent:
                    recover = getattr(self.repository, "get_acknowledged_prepared_exposure", None)
                    if callable(recover):
                        return recover(
                            scope_key=self.scope_key, idempotency_key=ack_idempotency_key
                        )
                return None
        recovered_prepared: PreparedExposureV2 | None = None
        if sent:
            recover = getattr(self.repository, "get_acknowledged_prepared_exposure", None)
            if callable(recover):
                recovered_prepared = recover(
                    scope_key=self.scope_key, idempotency_key=ack_idempotency_key
                )
        recorder = self._require_audit(ack.decision_id)
        recorder.record(
            DecisionStage.SEND_ACK if sent else DecisionStage.SEND_FAIL,
            occurred_at=ack.acknowledged_at,
            details={"outbox_id": ack.send_outbox_id, "attempt_id": ack.attempt_id},
        )
        prepared: PreparedExposureV2 | None = recovered_prepared
        if sent and prepared is None:
            chosen = self._chosen[ack.decision_id]
            sources = tuple(dict.fromkeys((*chosen.source_event_ids, *ack.source_event_ids)))
            exposure_action = (
                actual_action_for_exposure(chosen.action, ack.actual_action_witness)
                if isinstance(ack.actual_action_witness, Mapping)
                else dict(chosen.action)
            )
            prepare_and_freeze = getattr(
                self.repository, "prepare_exposure_and_expectation", None
            )
            if callable(prepare_and_freeze):
                prepared = prepare_and_freeze(
                    user_model=self.user_model,
                    scope_key=self.scope_key,
                    exposure_id=ack.attempt_id,
                    idempotency_key=ack_idempotency_key,
                    occurred_at=ack.acknowledged_at,
                    action=exposure_action,
                    context_provider=ack.context_provider,
                    horizons=self.config.horizons,
                    delivery_basis=getattr(ack, "delivery_basis", DeliveryBasis.DELIVERED),
                    source_event_ids=sources,
                    concern_id=chosen.repeat_subject.concern_id,
                    action_goal_id=chosen.repeat_subject.action_goal_id,
                    cold_start_exploration=bool(self._explored.get(ack.decision_id, False)),
                )
            else:
                # Compatibility for protocol fakes. Production PostgreSQL repositories
                # implement the atomic method above and freeze the prediction in the same
                # transaction as exposure creation.
                prepared = self.user_model.prepare_exposure(
                    scope_key=self.scope_key,
                    exposure_id=ack.attempt_id,
                    idempotency_key=ack_idempotency_key,
                    occurred_at=ack.acknowledged_at,
                    action=exposure_action,
                    context_provider=ack.context_provider,
                    delivery_confirmed=True,
                    horizons=self.config.horizons,
                    delivery_basis=getattr(ack, "delivery_basis", DeliveryBasis.DELIVERED),
                    source_event_ids=sources,
                )
                if prepared is not None:
                    record_exposure = getattr(
                        self.repository, "record_acknowledged_exposure", None
                    )
                    if callable(record_exposure):
                        record_exposure(
                            exposure_id=prepared.exposure.exposure_id,
                            acknowledged_at=ack.acknowledged_at,
                            concern_id=chosen.repeat_subject.concern_id,
                            action_goal_id=chosen.repeat_subject.action_goal_id,
                            cold_start_exploration=bool(
                                self._explored.get(ack.decision_id, False)
                            ),
                        )
        # Delivery completes this decision's irreversible funnel. Persist the
        # terminal reconciliation stage in the same post-legacy hook so black-box
        # and operators never see a permanently half-finished successful audit.
        recorder.record(
            DecisionStage.RECONCILED,
            occurred_at=ack.acknowledged_at,
            details={"reason": "sent" if sent else "send_failed"},
        )
        self._save_audit(recorder)
        mark_terminal = getattr(self.repository, "mark_committed_decision_ack_once", None)
        if callable(mark_terminal):
            marked = mark_terminal(
                scope_key=self.scope_key,
                decision_id=ack.decision_id,
                ack_id=ack.send_outbox_id,
                status="sent" if sent else "failed",
                acknowledged_at=ack.acknowledged_at,
            )
            if not marked:
                winner = self._recover_committed(ack.decision_id, refresh=True)
                if winner is None or winner.terminal_ack_id != ack.send_outbox_id or (
                    winner.terminal_status != ("sent" if sent else "failed")
                ):
                    raise ValueError("committed decision terminal acknowledgement conflicts")
        return prepared

    def reconcile(self, *, decision_id: str, now: datetime, reason: str) -> None:
        recorder = self._require_audit(decision_id)
        recorder.record(DecisionStage.RECONCILED, occurred_at=now, details={"reason": reason})
        self._save_audit(recorder)

    def _recover_committed(
        self, decision_id: str, *, refresh: bool = False
    ) -> CommittedDecisionV2 | None:
        if not refresh and decision_id in self._audits:
            return None
        recover = getattr(self.repository, "recover_committed_decision", None)
        if not callable(recover):
            return None
        committed = recover(scope_key=self.scope_key, decision_id=decision_id)
        if committed is None:
            return None
        if committed.snapshot_version != COMMITTED_DECISION_SNAPSHOT_VERSION:
            raise ValueError("unsupported committed decision snapshot version")
        if committed.decision_id != decision_id:
            raise ValueError("committed decision snapshot identity mismatch")
        if committed.scope_key != self.scope_key:
            raise ValueError("committed decision belongs to a different scope")
        recorder = DecisionAuditRecorder.from_dict(committed.audit)
        if recorder.run.decision_id != decision_id or recorder.run.scope != self.scope_key:
            raise ValueError("committed decision audit identity mismatch")
        if recorder.current_stage not in {
            DecisionStage.COMMITTED,
            DecisionStage.RENDERED,
            DecisionStage.SEND_ACK,
            DecisionStage.SEND_FAIL,
            DecisionStage.RECONCILED,
        }:
            raise ValueError("committed decision audit is not committed")
        self._audits[decision_id] = recorder
        self._chosen[decision_id] = committed.candidate
        self._explored[decision_id] = committed.cold_start_exploration
        return committed

    def _require_audit(self, decision_id: str) -> DecisionAuditRecorder:
        recorder = self._audits.get(decision_id)
        if recorder is None:
            self._recover_committed(decision_id)
            recorder = self._audits.get(decision_id)
        if recorder is None:
            raise KeyError(f"unknown decision_id: {decision_id}")
        return recorder

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
    "CommittedDecisionV2",
    "COMMITTED_DECISION_SNAPSHOT_VERSION",
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
