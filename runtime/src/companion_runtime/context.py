"""Context assembly: the temporary Runtime injection for the main LLM.

The main LLM must never receive a database dump, and it must never receive the
hidden psychological context as permanent history. This module builds a
*throw-away* bundle per turn, containing only:

* current psychological state (natural language, via the emotion explainer);
* the working situation (facts and inferences kept separate);
* the final intent, when one exists;
* a few activated memories;
* expression boundaries and constraints.

After the turn the bundle is discarded; only the visible user and assistant
messages enter the permanent conversation history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from . import candidate as candidate_module
from .config import RuntimeConfig
from .projections import Projections
from .typing import ActionAttempt, AttemptState, CandidateIntent, RuntimeState, UnfinishedStatus
from .utility import clamp, isoformat, local_now, utcnow

LOGGER = logging.getLogger("companion_runtime.context")

__all__ = [
    "ContextBundle",
    "build",
    "build_situation",
    "build_time_context",
    "select_memories",
    "describe_intent",
    "render_block",
    "assert_ephemeral",
]

#: Section headers used in the rendered prompt block.
SECTION_PSYCH = "【当前心理状态】"
SECTION_SITUATION = "【当前工作局势】"
SECTION_INTENT = "【当前最终意图】"
SECTION_MEMORY = "【必要记忆】"
SECTION_BOUNDARY = "【表达边界】"
SECTION_TIME = "【时间连续性】"


@dataclass(slots=True)
class ContextBundle:
    """The complete, temporary per-turn Runtime injection."""

    generated_at: datetime
    version: int
    psychological: dict[str, Any] = field(default_factory=dict)
    situation: dict[str, Any] = field(default_factory=dict)
    intent: dict[str, Any] | None = None
    memories: list[dict[str, Any]] = field(default_factory=list)
    boundaries: list[dict[str, Any]] = field(default_factory=list)
    time_context: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    attention_hints: list[str] = field(default_factory=list)
    ephemeral: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "generated_at": isoformat(self.generated_at),
            "version": self.version,
            "ephemeral": self.ephemeral,
            "psychological": dict(self.psychological),
            "situation": dict(self.situation),
            "intent": dict(self.intent) if self.intent else None,
            "memories": [dict(item) for item in self.memories],
            "boundaries": [dict(item) for item in self.boundaries],
            "time_context": dict(self.time_context),
            "constraints": list(self.constraints),
            "attention_hints": list(self.attention_hints),
        }


def build_situation(
    projections: Projections,
    *,
    now: datetime,
    limit: int = 12,
) -> dict[str, Any]:
    """Project the working situation into facts, inferences and active items.

    Args:
        projections: Projection bundle.
        now: Reference time.
        limit: Maximum number of items.

    Returns:
        A mapping with ``facts``, ``inferences``, ``active_items`` and
        ``unfinished``.
    """
    items = projections.situation.list_active(limit=limit)
    facts: list[str] = []
    inferences: list[dict[str, Any]] = []
    for item in items:
        content = str(item.get("content") or "")
        if item.get("kind") == "fact":
            facts.append(content)
        else:
            inferences.append(
                {
                    "content": content,
                    "confidence": round(float(item.get("confidence") or 0.0), 3),
                    "source": item.get("source_id"),
                }
            )
    unfinished = []
    for matter in projections.unfinished.list_open():
        unfinished.append(
            {
                "unfinished_id": matter.unfinished_id,
                "title": matter.title,
                "status": matter.status,
                "waiting_until": isoformat(matter.waiting_until),
                "priority": round(matter.priority, 3),
            }
        )
    return {
        "facts": facts,
        "inferences": inferences,
        "unfinished": unfinished,
        "generated_at": isoformat(now),
    }


def build_time_context(state: RuntimeState, *, now: datetime) -> dict[str, Any]:
    """Return the explicit time-continuity block.

    The Runtime must be able to say "it has been 8 hours" without guessing.
    """
    def hours_since(value: datetime | None) -> float | None:
        """Return hours between ``value`` and now, or ``None``."""
        if value is None:
            return None
        return round(max(0.0, (now - value).total_seconds()) / 3600.0, 2)

    return {
        "now": isoformat(now),
        "local_now": isoformat(local_now(now)),
        "local_hour": local_now(now).hour,
        "hours_since_last_user_message": hours_since(state.last_user_message_at),
        "hours_since_last_contact": hours_since(state.last_contact_at),
        "last_tick_at": isoformat(state.last_tick_at),
        "runtime_version": state.version,
    }


def select_memories(
    projections: Projections,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Return the most activated memories as prompt-ready items."""
    pool = projections.memory.list_activated(limit=limit)
    memories = projections.memory.get_memories([item.memory_id for item in pool])
    selected: list[dict[str, Any]] = []
    for activated in pool:
        memory = memories.get(activated.memory_id)
        if memory is None:
            continue
        selected.append(
            {
                "memory_id": memory.memory_id,
                "kind": memory.kind,
                "summary": memory.summary,
                "activation": round(activated.activation, 3),
                "importance": round(memory.importance, 3),
                "topics": list(memory.topics[:4]),
            }
        )
    return selected


def describe_intent(
    attempt: ActionAttempt | None,
    candidate: CandidateIntent | None,
    *,
    pending_count: int = 0,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Describe the final intent for the prompt, including "I already wanted this".

    When an attempt was committed moments before the user spoke, this is what lets
    the character say "you beat me to it" without being forced to.

    Args:
        attempt: In-flight or recent action attempt.
        candidate: Candidate backing the attempt.
        pending_count: Number of other live candidates.
        now: Reference time.

    Returns:
        A mapping or ``None`` when nothing is in flight.
    """
    if attempt is None:
        return None
    stamp = now or utcnow()
    lead_seconds = None
    if attempt.committed_at is not None:
        lead_seconds = round((stamp - attempt.committed_at).total_seconds(), 3)
    return {
        "attempt_id": attempt.attempt_id,
        "state": attempt.state,
        "intent": attempt.intent,
        "goal": attempt.goal,
        "constraints": list(candidate.constraints) if candidate else [],
        "committed_at": isoformat(attempt.committed_at),
        "lead_seconds_before_user_message": lead_seconds,
        "committed_not_yet_sent": attempt.state in {
            AttemptState.PROPOSED.value,
            AttemptState.COMMITTED.value,
            AttemptState.RENDERING.value,
            AttemptState.READY_TO_SEND.value,
        },
        "pending_candidate_count": pending_count,
    }


def build(
    *,
    runtime: Any,
    now: datetime | None = None,
    explanation: dict[str, Any] | None = None,
    include_boundaries: bool = True,
) -> ContextBundle:
    """Assemble the full temporary context bundle.

    Args:
        runtime: A :class:`~companion_runtime.runtime.Runtime`.
        now: Reference time.
        explanation: Pre-computed psychological explanation; generated when absent.
        include_boundaries: Include the expression-boundary block.

    Returns:
        A :class:`ContextBundle`.
    """
    stamp = now or utcnow()
    state = runtime.state()
    projections: Projections = runtime.projections

    active_emotions = projections.emotion.list_active()
    if explanation is None:
        explanation = runtime_explanation(runtime, stamp)

    attempts = projections.attempts.list_by_state(
        [
            AttemptState.PROPOSED.value,
            AttemptState.COMMITTED.value,
            AttemptState.RENDERING.value,
            AttemptState.READY_TO_SEND.value,
            AttemptState.SENT.value,
        ],
        limit=1,
    )
    attempt = attempts[0] if attempts else None
    candidate = (
        projections.candidates.get(attempt.candidate_id) if attempt and attempt.candidate_id else None
    )
    live_candidates = projections.candidates.list_active(limit=20)

    boundary_views: list[dict[str, Any]] = []
    constraints: list[str] = []
    if include_boundaries:
        for boundary in projections.boundaries.active(stamp):
            boundary_views.append(boundary.to_dict())
            if boundary.note:
                constraints.append(boundary.note)

    bundle = ContextBundle(
        generated_at=stamp,
        version=state.version,
        psychological=dict(explanation or {}),
        situation=build_situation(projections, now=stamp),
        intent=describe_intent(attempt, candidate, pending_count=len(live_candidates), now=stamp),
        memories=select_memories(projections, limit=4),
        boundaries=boundary_views,
        time_context=build_time_context(state, now=stamp),
        constraints=constraints,
    )
    bundle.attention_hints = _attention_hints(state, bundle, stamp)
    return bundle


def runtime_explanation(runtime: Any, now: datetime) -> dict[str, Any]:
    """Generate (or reuse) the psychological explanation through the explainer."""
    from .emotion import EmotionExplainer

    explainer = EmotionExplainer(runtime.projections.emotion, runtime.config)
    active = runtime.projections.emotion.list_active()
    state = runtime.state()
    return explainer.explain(state=state, active=active, now=now, rng=runtime.rng)


def _attention_hints(state: RuntimeState, bundle: ContextBundle, now: datetime) -> list[str]:
    """Return short hints about what deserves attention right now."""
    hints: list[str] = []
    due = [
        item
        for item in bundle.situation.get("unfinished", [])
        if item.get("status") == UnfinishedStatus.DUE.value
    ]
    if due:
        hints.append(f"有 {len(due)} 件未尽之事已到合适的时间点")
    if state.pressure > 0.5:
        hints.append("积压压力偏高，倾向于想开口")
    if state.mood_valence < -0.25:
        hints.append("背景心境偏负，表达时会偏收着")
    if bundle.intent and bundle.intent.get("committed_not_yet_sent"):
        hints.append("存在尚未发出的已提交意图，不要重复询问")
    if bundle.boundaries:
        hints.append("存在生效中的边界，必须遵守")
    return hints


def render_block(bundle: ContextBundle) -> str:
    """Render the bundle as the temporary prompt block for the main LLM.

    Only the sections the main LLM needs are emitted; internal numbers are hidden
    because language, not raw floats, is what the model responds to.

    Args:
        bundle: Context bundle to render.

    Returns:
        A markdown-ish text block, or an empty string when there is nothing to say.
    """
    if not bundle.psychological and not bundle.situation.get("facts"):
        return ""
    lines: list[str] = []

    psych = bundle.psychological
    if psych:
        lines.append(SECTION_PSYCH)
        for label, key in (
            ("感受", "experience"),
            ("在意", "focus"),
            ("拉扯", "conflict"),
            ("冲动", "impulse"),
            ("克制", "inhibition"),
            ("表达", "expression"),
        ):
            value = psych.get(key)
            if value:
                lines.append(f"- {label}：{value}")
        lines.append("")

    situation = bundle.situation
    if situation.get("facts") or situation.get("inferences"):
        lines.append(SECTION_SITUATION)
        for fact in situation.get("facts", [])[:6]:
            lines.append(f"- 事实：{fact}")
        for inference in situation.get("inferences", [])[:4]:
            lines.append(f"- 推断（置信度 {inference.get('confidence')}）：{inference.get('content')}")
        for matter in situation.get("unfinished", [])[:4]:
            lines.append(f"- 未尽之事：{matter.get('title')}（{matter.get('status')}）")
        lines.append("")

    if bundle.intent:
        lines.append(SECTION_INTENT)
        lines.append(f"- 想做的事：{bundle.intent.get('intent')}")
        if bundle.intent.get("goal"):
            lines.append(f"- 目的：{bundle.intent.get('goal')}")
        lead = bundle.intent.get("lead_seconds_before_user_message")
        if bundle.intent.get("committed_not_yet_sent") and lead is not None and lead >= 0:
            lines.append(f"- 在用户消息到达前 {round(lead, 1)} 秒，我已经决定这样做（尚未发出）")
        lines.append("")

    if bundle.memories:
        lines.append(SECTION_MEMORY)
        for memory in bundle.memories:
            lines.append(f"- {memory.get('summary')}")
        lines.append("")

    if bundle.constraints:
        lines.append(SECTION_BOUNDARY)
        for constraint in bundle.constraints:
            lines.append(f"- {constraint}")
        if bundle.boundaries:
            lines.append("- 边界是硬约束，任何动机都不能越过")
        lines.append("")

    if bundle.attention_hints:
        lines.append("【注意】")
        for hint in bundle.attention_hints:
            lines.append(f"- {hint}")
        lines.append("")

    lines.append(SECTION_TIME)
    time_context = bundle.time_context
    if time_context.get("hours_since_last_user_message") is not None:
        lines.append(f"- 距离上次用户消息：{time_context['hours_since_last_user_message']} 小时")
    if time_context.get("hours_since_last_contact") is not None:
        lines.append(f"- 距离上次主动联系：{time_context['hours_since_last_contact']} 小时")
    lines.append(f"- 当前本地时间：{time_context.get('local_now')}")
    lines.append("")
    lines.append("以上是仅本轮注入的临时内部状态，不要直接复述，也不要写进长期对话历史。")
    return "\n".join(lines)


def assert_ephemeral(bundle: ContextBundle) -> None:
    """Guard that a bundle is never treated as permanent history.

    Raises:
        AssertionError: If ``ephemeral`` was cleared, which would mean a caller
            intends to persist hidden state into the conversation transcript.
    """
    if not bundle.ephemeral:
        raise AssertionError("Runtime context bundles must remain ephemeral")
