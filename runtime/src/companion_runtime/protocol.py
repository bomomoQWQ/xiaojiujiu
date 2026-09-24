"""Protocol layer: the constitution of the Runtime.

Two responsibilities:

1. **Proposal classification** - every asynchronous result arrives as a proposal
   with a ``based_on_version`` and a set of source events. Before it can change
   anything, it is classified:

   * ``APPLY``  - still fully valid, apply as-is;
   * ``REBASE`` - semantically valid, but its effects must be recomputed against
     the *current* state (the world moved on, the evidence did not);
   * ``DISCARD`` - the premises were directly overturned; keep the result in
     history but change nothing.

   Sensitivity differs per result type: a shallow tag is almost always APPLY, an
   event appraisal is usually REBASE, a psychological explanation expires easily,
   a rendered proactive message must be re-coordinated.

2. **Concurrency re-coordination** - if the user speaks while the character is
   between ``committed`` and ``sent``, the attempt is not blindly dropped. It is
   resolved as ``KEEP`` / ``MERGE`` / ``RERENDER`` / ``RESOLVED`` / ``ABORT``.

Nothing here writes Runtime state directly: it returns a decision object and the
reducer applies it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .config import RuntimeConfig
from .typing import (
    AttemptState,
    EventType,
    ProtocolAction,
    RawEvent,
    ReconcileAction,
    TaskKind,
)
from .user_model import QUESTION_TYPES
from .utility import delta_seconds, ensure_aware, utcnow

LOGGER = logging.getLogger("companion_runtime.protocol")

#: How sensitive each task kind is to a version change.
TASK_SENSITIVITY: Mapping[str, str] = {
    TaskKind.SHALLOW_TAG.value: "low",
    TaskKind.MEMORY_SUMMARY.value: "low",
    TaskKind.EMOTION_EVAL.value: "medium",
    TaskKind.USER_MODEL_SUMMARY.value: "medium",
    TaskKind.CANDIDATE_GEN.value: "high",
    TaskKind.EMOTION_EXPLAIN.value: "high",
    TaskKind.PROACTIVE_DRAFT.value: "critical",
    #: A deep refresh reasons about old events, so its conclusions do not decay
    #: the way a live appraisal does. Marking it "high" would rebase (and damp)
    #: a reinterpretation merely because the character kept living for a few more
    #: rounds, which would quietly erase the very thing the refresh discovered.
    TaskKind.DEEP_REFRESH.value: "low",
}

#: How many versions may pass before a result of a given sensitivity must be
#: rebased rather than applied directly. The budget is the count of *tolerable*
#: gaps: a result is still fresh for gaps ``0 .. budget - 1``, and a gap that
#: reaches the budget is already stale. ``critical`` (0) therefore tolerates no
#: drift at all, while its gap of 0 -- dispatched against the very version that
#: is current -- is currency rather than drift and stays APPLY. See
#: :func:`staleness_threshold`, which turns these numbers into that boundary.
STALENESS_BUDGET: Mapping[str, int] = {
    "low": 50,
    "medium": 6,
    "high": 2,
    "critical": 0,
}

RETRACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(刚才|刚刚|之前|前面)?\s*(说错了|说反了|不算|开玩笑|撤回|我改主意了)"),
    re.compile(r"(其实|实际上)\s*(我)?\s*(有空|有时间|可以|没问题)"),
    re.compile(r"(i (was|wasn'?t) (wrong|kidding)|never ?mind|scratch that)", re.IGNORECASE),
)

TOPIC_OVERLAP_STOPWORDS = frozenset(
    {"的", "了", "我", "你", "是", "在", "有", "和", "就", "也", "the", "a", "is", "to", "i", "you"}
)


@dataclass(slots=True)
class Proposal:
    """An asynchronous result submitted by a model or background job."""

    task_id: str
    task_type: str
    based_on_version: int
    payload: dict[str, Any] = field(default_factory=dict)
    source_event_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    kind: str = "generic"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "based_on_version": self.based_on_version,
            "payload": dict(self.payload),
            "source_event_ids": list(self.source_event_ids),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "kind": self.kind,
        }


@dataclass(slots=True)
class Classification:
    """The protocol verdict for one proposal."""

    action: str
    reason: str
    sensitivity: str
    version_gap: int
    missing_event_ids: list[str] = field(default_factory=list)
    rebase_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "action": self.action,
            "reason": self.reason,
            "sensitivity": self.sensitivity,
            "version_gap": self.version_gap,
            "missing_event_ids": list(self.missing_event_ids),
            "rebase_notes": list(self.rebase_notes),
        }


def sensitivity_of(task_type: str) -> str:
    """Return the version sensitivity class of a task type."""
    return TASK_SENSITIVITY.get(task_type, "medium")


def staleness_threshold(sensitivity: str) -> int:
    """Return the version gap at which a sensitivity class counts as stale.

    One boundary, used by everything that asks "is this result still current?",
    so the protocol can never answer APPLY and DISCARD for the same gap:

    * the returned value is the *inclusive* rebase boundary -- a gap equal to it
      is stale and the result must be re-coordinated (``gap >= threshold``),
      while every smaller gap applies as-is;
    * it is never below 1, because a gap of 0 is not drift: the result was
      dispatched against the version that is still current, and re-coordinating
      it would rebase work that is exactly as fresh as the state it targets.
      That is what keeps the ``critical`` budget of 0 meaningful (any real drift
      rebases) without making it self-defeating (a current draft still applies).

    Args:
        sensitivity: A sensitivity class name (``low``/``medium``/``high``/
        ``critical``); an unknown class falls back to the ``medium`` budget.

    Returns:
        The smallest version gap that is not fresh.
    """
    return max(1, STALENESS_BUDGET.get(sensitivity, STALENESS_BUDGET["medium"]))


def _looks_like_retraction(text: str) -> bool:
    """Return whether a user message retracts an earlier statement."""
    return any(pattern.search(text or "") for pattern in RETRACTION_PATTERNS)


def _topic_tokens(text: str) -> set[str]:
    """Return content tokens of a text, minus stopwords."""
    from .utility import topic_tokens

    return topic_tokens(text) - TOPIC_OVERLAP_STOPWORDS


def classify(
    proposal: Proposal,
    *,
    current_version: int,
    source_events: Sequence[RawEvent],
    missing_event_ids: Sequence[str] = (),
    newer_user_events: Sequence[RawEvent] = (),
    config: RuntimeConfig | None = None,
) -> Classification:
    """Classify a proposal as APPLY, REBASE or DISCARD.

    Rules, in order of precedence:

    1. a missing source event means the premise has no evidence left -> DISCARD;
    2. a newer user message that retracts the premise of the source events ->
       DISCARD;
    3. a version gap *inside* the sensitivity budget (strictly below
       :func:`staleness_threshold`) -> APPLY;
    4. otherwise -> REBASE, with notes explaining what must be recomputed.

    Rule 3 is inclusive of gap 0 for every sensitivity, including the
    ``critical`` one whose budget is 0: a gap equal to the budget is already
    stale, but "no drift at all" is never stale.

    Args:
        proposal: The submitted result.
        current_version: Current runtime version.
        source_events: The source events that still exist.
        missing_event_ids: Source event identifiers that no longer exist.
        newer_user_events: User events that arrived after the task was created.
        config: Runtime configuration (unused today, kept for calibration).

    Returns:
        A :class:`Classification`.
    """
    sensitivity = sensitivity_of(proposal.task_type)
    gap = max(0, int(current_version) - int(proposal.based_on_version))
    classification = Classification(
        action=ProtocolAction.APPLY.value,
        reason="fresh",
        sensitivity=sensitivity,
        version_gap=gap,
        missing_event_ids=list(missing_event_ids),
    )

    if missing_event_ids:
        classification.action = ProtocolAction.DISCARD.value
        classification.reason = "source_events_missing"
        return classification

    if proposal.task_type == TaskKind.PROACTIVE_DRAFT.value and newer_user_events:
        classification.action = ProtocolAction.REBASE.value
        classification.reason = "user_spoke_during_rendering"
        classification.rebase_notes.append("re-coordinate with the new user message")
        return classification

    for event in newer_user_events:
        if not _looks_like_retraction(event.content or ""):
            continue
        if _retracts(proposal, event, source_events):
            classification.action = ProtocolAction.DISCARD.value
            classification.reason = "premise_retracted"
            classification.rebase_notes.append(f"retracted by {event.event_id}")
            return classification

    # A gap that reaches the sensitivity's threshold is stale; a smaller one is
    # inside the budget. ``critical`` (threshold 1) is what makes this a
    # threshold rather than a plain budget comparison: its own gap of 0 still
    # applies, any real drift does not.
    if gap >= staleness_threshold(sensitivity):
        classification.action = ProtocolAction.REBASE.value
        classification.reason = f"stale_by_{gap}_versions"
        classification.rebase_notes.append("recompute effects against current state")
        return classification

    if newer_user_events and sensitivity in {"high", "critical"}:
        classification.action = ProtocolAction.REBASE.value
        classification.reason = "newer_context_available"
        classification.rebase_notes.append("new evidence arrived after dispatch")
    return classification


def _retracts(proposal: Proposal, event: RawEvent, source_events: Sequence[RawEvent]) -> bool:
    """Return whether ``event`` retracts the premise of ``proposal``.

    The test is topical: the retraction must touch the same subject matter as the
    proposal's source events, otherwise "never mind" about something unrelated
    would wrongly kill a valid result.
    """
    proposal_text = " ".join(
        [str(proposal.payload.get("summary") or ""), str(proposal.payload.get("intent") or "")]
    )
    source_text = " ".join((source.content or "") for source in source_events)
    event_tokens = _topic_tokens(event.content or "")
    reference = _topic_tokens(proposal_text + " " + source_text)
    if not reference:
        return True
    return bool(event_tokens & reference)


# --------------------------------------------------------------------------------------
# Concurrency re-coordination
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ReconcileDecision:
    """Outcome of re-coordinating an in-flight action attempt."""

    action: str
    reason: str
    notes: list[str] = field(default_factory=list)
    satisfied_by_event_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "action": self.action,
            "reason": self.reason,
            "notes": list(self.notes),
            "satisfied_by_event_ids": list(self.satisfied_by_event_ids),
        }


def reconcile(
    *,
    attempt_state: str,
    attempt_intent: str,
    attempt_goal: str = "",
    candidate_type: str = "contact",
    candidate_invalidate_when: Sequence[str] = (),
    new_events: Sequence[RawEvent],
    now: datetime | None = None,
) -> ReconcileDecision:
    """Decide what happens to a committed attempt when the user suddenly speaks.

    Order of tests:

    * the user already satisfied the intent (``RESOLVED``);
    * the user's message makes the intent inappropriate (``ABORT``);
    * the message retracts the premise the intent was built on (``ABORT``);
    * the message can be absorbed by the same intent (``MERGE``);
    * otherwise the intent holds but the wording is stale (``RERENDER``).

    Args:
        attempt_state: Current state of the attempt.
        attempt_intent: The intended action in natural language.
        attempt_goal: Why the character wanted to act.
        candidate_type: Candidate type backing the attempt.
        candidate_invalidate_when: Natural-language invalidation conditions.
        new_events: User events that arrived during the in-flight window.
        now: Reference time.

    Returns:
        A :class:`ReconcileDecision`; ``KEEP`` when nothing needs to change.
    """
    if not new_events:
        return ReconcileDecision(action=ReconcileAction.KEEP.value, reason="no_new_events")

    intent_tokens = _topic_tokens(attempt_intent) | _topic_tokens(attempt_goal)
    decision = ReconcileDecision(action=ReconcileAction.KEEP.value, reason="intent_unaffected")

    for event in new_events:
        text = event.content or ""
        event_tokens = _topic_tokens(text)

        # 1. The user did the thing we were about to ask about.
        # Question-shaped candidates only: the user may have just answered the very thing
        # this intent was going to ask about. Read from the shared vocabulary rather than a
        # fourth hand-written list - the old set here omitted ``question``, which is a
        # synonym of ``curious_question`` (docs/BUSINESS_LOGIC_AUDIT.md §4).
        if candidate_type in QUESTION_TYPES and event_tokens & intent_tokens:
            if _satisfies(text, candidate_type):
                return ReconcileDecision(
                    action=ReconcileAction.RESOLVED.value,
                    reason="user_already_satisfied_intent",
                    notes=["intent目标已被用户抢先满足"],
                    satisfied_by_event_ids=[event.event_id],
                )

        # 2. The user's situation makes the intent inappropriate.
        if _is_crisis(text):
            return ReconcileDecision(
                action=ReconcileAction.ABORT.value,
                reason="user_situation_changed_severely",
                notes=["用户处境发生重大变化，原表达不再合适，应先关心"],
            )

        # 3. The user retracts the premise. The retraction must touch the same
        # subject matter, otherwise "never mind" about something unrelated would
        # wrongly kill a valid intention. When neither side carries a clear
        # subject, the retraction is honoured rather than ignored.
        overlap = event_tokens & intent_tokens
        if _looks_like_retraction(text) and (overlap or not intent_tokens or not event_tokens):
            return ReconcileDecision(
                action=ReconcileAction.ABORT.value,
                reason="premise_retracted",
                notes=["用户撤回了原意图所依赖的前提"],
            )

        # 4. An explicit boundary arrives mid-flight: never send.
        if _declares_boundary(text):
            return ReconcileDecision(
                action=ReconcileAction.ABORT.value,
                reason="boundary_declared_mid_flight",
                notes=["发送前出现显式边界，硬约束优先"],
            )

        # 5. The message can be absorbed: same subject, no contradiction.
        if event_tokens & intent_tokens:
            decision = ReconcileDecision(
                action=ReconcileAction.MERGE.value,
                reason="topic_overlap",
                notes=["用户新消息与原意图同主题，合流回应"],
            )
            continue

        # 6. User spoke first: intent holds, wording must change.
        if decision.action == ReconcileAction.KEEP.value:
            decision = ReconcileDecision(
                action=ReconcileAction.RERENDER.value,
                reason="user_spoke_first",
                notes=["用户先开口了，原措辞需要重写"],
            )

    if attempt_state == AttemptState.READY_TO_SEND.value and decision.action == ReconcileAction.KEEP.value:
        decision.action = ReconcileAction.RERENDER.value
        decision.reason = "user_spoke_before_send"
        decision.notes.append("消息尚未发出，用户先发来消息")
    return decision


def _satisfies(text: str, candidate_type: str) -> bool:
    """Return whether a user message already answers a follow-up style intent."""
    from .unfinished import RESOLUTION_PATTERNS

    for pattern, _reason, _topics in RESOLUTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _is_crisis(text: str) -> bool:
    """Return whether a message describes a serious negative event.

    A crisis always takes precedence over a prepared message: whatever the
    character was about to say is no longer the right thing to say.
    """
    markers = (
        "家里出事",
        "出事了",
        "去世",
        "抢救",
        "急诊",
        "住院",
        "崩溃",
        "很难受",
        "emergency",
    )
    return any(marker in (text or "") for marker in markers)


def _declares_boundary(text: str) -> bool:
    """Return whether a message declares a hard boundary."""
    from .boundaries import BOUNDARY_PATTERNS

    return any(rule.pattern.search(text or "") for rule in BOUNDARY_PATTERNS)


# --------------------------------------------------------------------------------------
# Rebase helpers
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RebaseResult:
    """The recomputed effect of a proposal after a REBASE classification."""

    payload: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {"payload": dict(self.payload), "notes": list(self.notes)}


def rebase_emotion_evaluation(
    payload: Mapping[str, Any],
    *,
    mood_valence: float,
    mood_arousal: float,
    hours_elapsed: float,
) -> RebaseResult:
    """Recompute the emotional effect of an appraisal against current mood.

    The *appraisal* is kept (the facts did not change); only its effect is
    re-evaluated. A tired character reacts more strongly to the same event, and a
    long-delayed effect is damped because it is no longer news.

    Args:
        payload: The appraisal payload from the background task.
        mood_valence: Current mood valence.
        mood_arousal: Current mood arousal.
        hours_elapsed: Hours since the task was dispatched.

    Returns:
        A :class:`RebaseResult` with an adjusted payload.
    """
    adjusted = dict(payload)
    sensitivity = 1.0 + 0.45 * abs(mood_valence) + 0.25 * mood_arousal
    staleness = max(0.25, 1.0 - min(6.0, hours_elapsed) / 12.0)
    for key in ("impact", "activation"):
        if key in adjusted and isinstance(adjusted[key], (int, float)):
            adjusted[key] = max(0.0, min(1.0, float(adjusted[key]) * sensitivity * staleness))
    notes = [f"sensitivity={sensitivity:.3f}", f"staleness={staleness:.3f}"]
    return RebaseResult(payload=adjusted, notes=notes)


def rebase_candidate_payload(
    payload: Mapping[str, Any], *, live_unfinished_ids: Sequence[str], live_memory_ids: Sequence[str]
) -> RebaseResult:
    """Filter candidate operations so they only reference still-valid sources.

    A candidate whose sources have disappeared is dropped rather than rebased:
    a thought cannot be grounded in evidence that no longer exists.

    Args:
        payload: ``{"operations": [...]}`` from the semantic API.
        live_unfinished_ids: Unfinished matter identifiers that still exist.
        live_memory_ids: Memory identifiers that still exist.

    Returns:
        A :class:`RebaseResult` with the surviving operations.
    """
    operations = list(payload.get("operations") or [])
    survivors: list[dict[str, Any]] = []
    dropped: list[str] = []
    for operation in operations:
        candidate = dict(operation.get("candidate") or {})
        sources = list(candidate.get("sources") or operation.get("sources") or [])
        grounded = True
        for source in sources:
            if source.startswith("unfinished:") and source.split(":", 1)[1] not in live_unfinished_ids:
                grounded = False
            elif source.startswith("memory:") and source.split(":", 1)[1] not in live_memory_ids:
                grounded = False
        if grounded:
            survivors.append(operation)
        else:
            dropped.append(str(candidate.get("intent") or operation.get("op")))
    notes = [f"dropped_ungrounded={len(dropped)}"] if dropped else []
    return RebaseResult(payload={"operations": survivors} | {k: v for k, v in payload.items() if k != "operations"}, notes=notes)


def expired_explanation(created_at: datetime | None, now: datetime, ttl_seconds: float) -> bool:
    """Return whether a cached psychological explanation has expired."""
    if created_at is None:
        return True
    return delta_seconds(now, created_at) > ttl_seconds


def should_discard_explanation(proposal: Proposal, *, current_version: int) -> bool:
    """Return whether a psychological explanation must be thrown away.

    The explanation is the most perishable result the Runtime holds, so it is not
    merely re-coordinated when the world moves: it is dropped. The boundary is
    the *same* one :func:`classify` applies to its staleness decision (a gap that
    reaches the ``high`` threshold), because an explanation that ``classify``
    would refuse to apply as fresh must not survive as a cached one -- two
    disagreeing boundaries is how a stale explanation keeps being served after
    the protocol has already decided it is out of date.
    """
    gap = max(0, int(current_version) - int(proposal.based_on_version))
    return sensitivity_of(proposal.task_type) == "high" and gap >= staleness_threshold("high")
