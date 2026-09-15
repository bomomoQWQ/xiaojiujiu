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
from .typing import (
    ActionAttempt,
    AttemptState,
    CandidateIntent,
    MemoryStatus,
    RuntimeState,
    UnfinishedStatus,
)
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
#
# Patch v0.2 renamed the psychological section: it no longer tells the main LLM
# how the current message should feel, it only describes the long-term weather
# the character carries into the turn.
SECTION_PSYCH = "【进入本轮前的长期状态（背景）】"
SECTION_SITUATION = "【当前工作局势】"
SECTION_INTENT = "【当前最终意图】"
SECTION_SITUATION_INTENT = "【刚刚差点要说的话】"
SECTION_MEMORY = "【必要记忆】"
SECTION_BOUNDARY = "【表达边界】"
SECTION_TIME = "【时间连续性】"

#: Preamble that fixes the precedence order from patch v0.2 section 7. The main
#: LLM must treat the current user message and the current facts as authoritative
#: and this block as background colour, never as an instruction to keep feeling
#: something the user's new words have just contradicted.
#:
#: All seven levels are spelled out rather than summarised: a model reading a
#: compressed chain will happily collapse "persistent state" and "the current
#: message" into one priority, which is the exact failure this ordering prevents.
PRIORITY_PREAMBLE = (
    "【使用说明】以下是 Runtime 注入的临时背景，帮助你知道自己从哪来，"
    "不是对当前这句话的指令。严格按以下优先级理解一切输入：\n"
    "1. 宿主角色设定与安全约束\n"
    "2. 当前用户原话\n"
    "3. 当前确定事实\n"
    "4. 显式边界\n"
    "5. Runtime 持久心理状态（下面这一段）\n"
    "6. 心理解释缓存\n"
    "7. 你自己的自然发挥\n"
    "如果第 2 项与第 5、6 项冲突，以第 2 项为准：不要因为旧状态写着「失落」"
    "就继续机械地低落，也不要因为旧状态写着「靠近」就无视对方刚说的拒绝。"
    "你的即时反应由你自己根据当前语境完成。"
)

#: How long a *closed* attempt stays worth recalling. A committed intention that
#: the user pre-empted is only interesting while it is still "just now"; after this
#: window it is history rather than a conversational cue.
RECENT_INTENT_WINDOW_SECONDS = 120.0


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
    """Return the most activated memories as prompt-ready items.

    Archived memories are filtered out: archival is the Runtime's own statement that
    a memory is no longer part of what the character knows, so re-injecting it into
    the prompt would contradict the decision that produced it.
    """
    pool = projections.memory.list_activated_memories(limit=limit)
    memories = projections.memory.get_memories([item.memory_id for item in pool])
    selected: list[dict[str, Any]] = []
    for activated in pool:
        memory = memories.get(activated.memory_id)
        if memory is None or memory.status != MemoryStatus.ACTIVE.value:
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
    closed = attempt.state in CLOSED_ATTEMPT_STATES
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
        # A closed attempt is reported as an *almost-said* memory, never as the
        # current final intent: claiming an abandoned intention is still live would
        # misrepresent the state to the main LLM.
        "closed": closed,
        "closed_state": attempt.state if closed else None,
        "closed_reason": attempt.failure_reason if closed else None,
        "closed_seconds_ago": lead_seconds if closed else None,
        "pending_candidate_count": pending_count,
    }


#: Terminal attempt states. An attempt in one of these has stopped occupying
#: attention, but may still be recent enough to be worth recalling.
CLOSED_ATTEMPT_STATES: frozenset[str] = frozenset(
    {
        AttemptState.RESOLVED.value,
        AttemptState.ABORTED.value,
        AttemptState.EXPIRED.value,
        AttemptState.FAILED.value,
    }
)


def _recently_closed_attempt(
    projections: Projections, now: datetime, *, window_seconds: float | None = None
) -> tuple[ActionAttempt | None, CandidateIntent | None]:
    """Return a very recent closed attempt and its candidate, if any.

    Only attempts that reached ``committed`` are considered: an intention the
    character never actually formed is not something it "almost said".

    Args:
        projections: Projection bundle.
        now: Reference time.
        window_seconds: Recall window; defaults to
            :data:`RECENT_INTENT_WINDOW_SECONDS`.

    Returns:
        ``(attempt, candidate)``, either of which may be ``None``.
    """
    window = RECENT_INTENT_WINDOW_SECONDS if window_seconds is None else window_seconds
    for candidate_attempt in projections.attempts.list_all(limit=10):
        if candidate_attempt.state not in CLOSED_ATTEMPT_STATES:
            continue
        if candidate_attempt.committed_at is None:
            continue
        age = (now - candidate_attempt.committed_at).total_seconds()
        if age < 0 or age > window:
            # Too old to be "just now": it is history, not a conversational cue.
            return None, None
        backing = (
            projections.candidates.get(candidate_attempt.candidate_id)
            if candidate_attempt.candidate_id
            else None
        )
        return candidate_attempt, backing
    return None, None


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
    if attempt is None:
        # Nothing is in flight, but an intention may have been closed a moment ago
        # because the user spoke first. That is exactly the "I was about to say
        # something" case, and it must survive the re-coordination that closed the
        # attempt, otherwise the cue is lost at the moment it becomes interesting.
        attempt, candidate = _recently_closed_attempt(projections, stamp)
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
    """Generate (or reuse) the psychological explanation through the explainer.

    The semantic provider is passed in so that a configured one can fill the
    interpretation cache. It is optional by contract: with the default
    ``DisabledProvider`` the explainer simply renders the deterministic template,
    which is the standard deployment.
    """
    from .emotion import EmotionExplainer

    explainer = EmotionExplainer(
        runtime.projections.emotion,
        runtime.config,
        provider=_optional_explanation_provider(runtime),
    )
    active = runtime.projections.emotion.list_active()
    state = runtime.state()
    return explainer.explain(state=state, active=active, now=now, rng=runtime.rng)


def _optional_explanation_provider(runtime: Any) -> Any:
    """Return the Runtime's provider only when it can actually explain.

    A disabled or unavailable provider must not be handed to the explainer at all:
    the explainer treats any provider as "try me first", and a provider that is
    configured but down would add a pointless failed call on the context path.
    """
    provider = getattr(runtime, "semantic_provider", None)
    if provider is None:
        return None
    try:
        if not provider.available():
            return None
    except Exception:  # noqa: BLE001 - an unavailable provider is simply absent
        return None
    return provider


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
    lines: list[str] = [PRIORITY_PREAMBLE, ""]

    psych = bundle.psychological
    # Only emit the section when there is actual prose. A bundle that carries just
    # bookkeeping keys (``source``/``cache_key``) means the fallback was disabled
    # or no provider answered, and an empty header would be noise in the prompt.
    prose_keys = ("experience", "focus", "conflict", "impulse", "inhibition", "expression")
    if any(psych.get(key) for key in prose_keys):
        lines.append(SECTION_PSYCH)
        for label, key in (
            ("长期感受", "experience"),
            ("在意", "focus"),
            ("拉扯", "conflict"),
            ("倾向", "impulse"),
            ("克制", "inhibition"),
            ("表达底色", "expression"),
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
        closed = bool(bundle.intent.get("closed"))
        lines.append(SECTION_INTENT if not closed else SECTION_SITUATION_INTENT)
        # The label differs between the two cases on purpose: a render prompt states
        # what to write now with the same "- 想做的事：" line, and an identically
        # labelled *background* line above it is read as the instruction by whatever
        # reads first. That happened in practice - a check-up reminder in one chat was
        # written about the interview the character had last wanted to mention - so the
        # stale intent is marked as background rather than repeating the live label.
        lines.append(
            f"- 想做的事：{bundle.intent.get('intent')}"
            if not closed
            else f"- 之前想做的事（背景，不是现在的任务）：{bundle.intent.get('intent')}"
        )
        if bundle.intent.get("goal"):
            lines.append(f"- 目的：{bundle.intent.get('goal')}")
        lead = bundle.intent.get("lead_seconds_before_user_message")
        if closed:
            seconds = bundle.intent.get("closed_seconds_ago")
            if seconds is not None and seconds >= 0:
                lines.append(
                    f"- 在用户开口前 {round(seconds, 1)} 秒，我已经决定要这么做；"
                    "这个意图已经结束，不需要再执行"
                )
            lines.append("- 这只是一件刚刚发生的小事，是否提起由当前语境决定，不要硬凹")
        elif bundle.intent.get("committed_not_yet_sent") and lead is not None and lead >= 0:
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
    lines.append(
        "以上是仅本轮注入的临时背景，不要直接复述，也不要写进长期对话历史；"
        "它描述的是你进入本轮之前的长期状态，不是本轮该怎么反应。"
    )
    return "\n".join(lines)


def assert_ephemeral(bundle: ContextBundle) -> None:
    """Guard that a bundle is never treated as permanent history.

    Raises:
        AssertionError: If ``ephemeral`` was cleared, which would mean a caller
            intends to persist hidden state into the conversation transcript.
    """
    if not bundle.ephemeral:
        raise AssertionError("Runtime context bundles must remain ephemeral")
