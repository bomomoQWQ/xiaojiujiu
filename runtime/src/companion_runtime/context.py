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
from typing import Any, Callable, Sequence

from . import candidate as candidate_module
from . import memory as memory_module
from .config import RuntimeConfig
from .projections import Projections
from .typing import (
    ActionAttempt,
    AttemptState,
    CandidateIntent,
    MemoryKind,
    MemoryStatus,
    RuntimeState,
    UnfinishedStatus,
)
from .utility import clamp, display_local, isoformat, local_now, utcnow

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
SECTION_PSYCH = "【我进来之前的状态（背景）】"
SECTION_SITUATION = "【眼下的事】"
SECTION_INTENT = "【我现在想做】"
SECTION_SITUATION_INTENT = "【刚刚差点说出口的话】"
SECTION_MEMORY = "【我记得的事】"
SECTION_BOUNDARY = "【用户划过的线】"
SECTION_TIME = "【时间上的事】"

#: Weekday names for the local clock line. The host's own reminder spells them in
#: English ("Weekday: Friday") and only exists on chat turns; a proactive render has no
#: host reminder at all, so this line is the only clock she gets there.
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: Memory kinds that describe who the user *is* rather than what happened once. They
#: are injected from importance when nothing recalled them, so the character does not
#: lose the basics between two mentions.
DURABLE_KINDS = frozenset(
    {
        MemoryKind.STABLE_KNOWLEDGE.value,
        MemoryKind.USER_PREFERENCE.value,
        MemoryKind.RELATIONSHIP.value,
    }
)

#: How important a durable memory must be to be injected without being recalled.
#: ``kind_importance`` alone gives a preference 0.70 and stable knowledge 0.75, and
#: consolidation averages that with the candidate's own value, so this admits facts
#: the rules scored well and keeps weak ones out until something recalls them.
DURABLE_MIN_IMPORTANCE = 0.6

#: How long a newly formed memory is guaranteed a slot in the prompt.
#:
#: The working set saturates: a handful of memories that share words with the live
#: matters are recalled every round, their activation pins at 1.0, and a fact the user
#: stated ten minutes ago - activation 0.55, ranked eighth - never made it into a
#: four-line section. "What I just learned about you" is not something to rank against
#: a saturated pool; it is the whole point of listening.
FRESH_WINDOW_HOURS = 24.0

#: Preamble that fixes the precedence order from patch v0.2 section 7. The main
#: LLM must treat the current user message and the current facts as authoritative
#: and this block as background colour, never as an instruction to keep feeling
#: something the user's new words have just contradicted.
#:
#: All seven levels are spelled out rather than summarised: a model reading a
#: compressed chain will happily collapse "persistent state" and "the current
#: message" into one priority, which is the exact failure this ordering prevents.
PRIORITY_PREAMBLE = (
    "【说明】下面是我进这句话之前的状态，不是这一句该怎么回。分量从大到小是这么排的：\n"
    "1. 我的人设和安全底线\n"
    "2. 用户刚说的这句话\n"
    "3. 眼下确定的事实\n"
    "4. 用户明确划过的界线\n"
    "5. 我长期以来的心理状态（就是下面这一段）\n"
    "6. 我对眼下心情的解释\n"
    "7. 我自己的临场发挥\n"
    "第 2 条要是跟第 5、6 条顶上了，听第 2 条：别因为旧状态写着「失落」"
    "就接着闷着，也别因为写着「靠近」就当没听见用户刚说的拒绝。"
    "这一句怎么接，我自己看着办。"
    # The one time fact the acting layer gets, and it is *provenance*, not a clock:
    # "he said this at …" is what makes a relative phrase legible later ("我明天要去面试"
    # + 说于 11-14 19:36 -> 明天 = 11-15). The anchor is always written by the code; the
    # model is never asked to guess one. The clock ("what time is it") is not here at all -
    # only a proactive render states that, in its own instruction section.
    "\n凡是引用用户原话的地方，都带着他说那句话的时刻（「用户在 …… 说：」或「（他在 …… 说的）」）："
    "他话里的「明天」「下周」「三点」，都是按那一刻算的。"
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


#: How a quoted line is attributed in the block. Both the words and the moment come from
#: the raw event, so the attribution can be checked against history - which is the whole
#: point of stamping it.
ATTRIBUTION = "（他在 %s 说的）"

#: A fact that already reads "用户说：…" is rewritten to name the moment instead of
#: carrying a second, clumsier attribution after it: "用户在 <时刻> 说：…".
QUOTE_PREFIX = "用户说："


def said_at_resolver(runtime: Any) -> Callable[[str], str]:
    """Return a resolver from a source event id to the moment it happened, as a display.

    Fail-open: an id that no longer resolves yields an empty string and the line is
    written without an attribution. A prompt must never be the reason a turn breaks.
    """

    def resolve(event_id: str) -> str:
        if not event_id:
            return ""
        try:
            event = runtime.events.get(event_id)
        except Exception:  # noqa: BLE001 - attribution is never worth a failed turn
            return ""
        return display_local(event.timestamp) if event is not None else ""

    return resolve


def build_situation(
    projections: Projections,
    *,
    now: datetime,
    limit: int = 12,
    said_at: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Project the working situation into facts, inferences and unfinished matters.

    "Currently active information" is deliberately *not* part of this mapping. The
    activated memories are assembled by :func:`select_memories` into the bundle's
    own ``memories`` section, which is the one place the prompt reads them from;
    duplicating them here would give the same fact two representations in one
    injected block, and the design keeps the working situation to facts, inferences
    and unfinished matters.

    Args:
        projections: Projection bundle.
        now: Reference time.
        limit: Maximum number of situation items.

    Returns:
        A mapping with ``facts``, ``inferences``, ``unfinished`` and
        ``generated_at``.
    """
    items = projections.situation.list_active(limit=limit)
    facts: list[str] = []
    inferences: list[dict[str, Any]] = []
    for item in items:
        content = str(item.get("content") or "")
        # A fact that quotes the user carries when he said it. It is provenance, not a
        # clock: "用户说：我明天下午三点面试" is unreadable a day later without it.
        if said_at is not None and str(item.get("source_kind") or "") == "event":
            stamp = said_at(str(item.get("source_id") or ""))
            if stamp and content.startswith(QUOTE_PREFIX):
                content = f"用户在 {stamp} 说：{content[len(QUOTE_PREFIX):]}"
            elif stamp:
                content = f"{content}{ATTRIBUTION % stamp}"
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

    # ``isoformat()`` normalises to UTC on purpose (it is the storage format), so the
    # local clock must not go through it: the block used to print a UTC stamp on a line
    # labelled 当前本地时间, which put every prompt of ours eight hours off. Measured: a
    # proactive message sent at 04:01 CST came out as 晚上好, i.e. it had read 20:01 UTC.
    # Native ``datetime.isoformat()`` keeps the offset, and the display line is what the
    # model actually reads.
    local = local_now(now)
    return {
        "now": isoformat(now),
        "local_now": local.isoformat(),
        # One formatter, shared with the anchor attached to a quoted utterance, so the
        # clock a render states and the "he said this at …" stamp read identically.
        "local_display": display_local(now),
        "local_hour": local.hour,
        "hours_since_last_user_message": hours_since(state.last_user_message_at),
        "hours_since_last_contact": hours_since(state.last_contact_at),
        "last_tick_at": isoformat(state.last_tick_at),
        "runtime_version": state.version,
    }


def select_memories(
    projections: Projections,
    *,
    limit: int = 4,
    cue: Any = None,
    store: Any = None,
    now: datetime | None = None,
    fresh_hours: float = FRESH_WINDOW_HOURS,
    said_at: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Return the memories to put in front of the acting layer, best first.

    Four sources, because they answer four different questions:

    1. **what this moment brings back** - a memory recalled by the current cue. This
       is how a fact the user stated days ago can still be answered when they ask
       about it, and it is why a faded memory counts here (:meth:`MemoryStore.retrieve`
       is not restricted to the working set);
    2. **what I just learned** - a memory formed in the last
       :data:`FRESH_WINDOW_HOURS`. It is a separate source rather than an entry in the
       ranking because the ranking saturates: memories that share words with the live
       matters are recalled every round and pin at activation 1.0, so a fact stated
       minutes ago ranked eighth of eight and never reached a four-line section;
    3. **what has been on the character's mind** - the activation pool;
    4. **who the user is** - durable facts (stable knowledge, preferences, relational
       experiences) by importance, so the character does not lose the basics just
       because nothing recalled them for two days.

    Slots are handed out one source at a time, in that order, so no single source can
    take the whole section.

    Archived memories are excluded, and so are superseded ones: a newer statement
    replaced them, and the prompt must carry what the character currently believes.

    Args:
        projections: The Runtime's projections.
        limit: Maximum number of memories to hand over.
        cue: Retrieval cue for the current moment (``None`` disables source 1).
        store: :class:`~companion_runtime.memory.MemoryStore` used for source 1.
        now: Reference time for "just learned" (``None`` disables source 2).
        fresh_hours: How long a newly formed memory is guaranteed a slot
            (``memory.fresh_window_hours``; ``0`` disables source 2).

    Returns:
        Prompt-ready items with a ``selection`` field naming the source they came from.
    """
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(memory: Any, activation: float, source: str) -> None:
        """Append one memory once, from whichever source reached it first."""
        if memory is None or memory.memory_id in seen or len(selected) >= limit:
            return
        if memory.status != MemoryStatus.ACTIVE.value and source != "cue":
            # Only a cue may surface a faded memory: demotion means "not on the
            # character's mind", and the working set and the durable baseline are
            # statements about exactly that.
            return
        if memory_module.is_superseded(memory):
            return
        if memory_module.is_recall_check(memory):
            # A memory whose summary came from a question must not spend one of the
            # four slots. These were measured filling all four while the disclosure the
            # user actually asked about had been retrieved (rank 4 of 10) and still lost
            # the budget, because questions about the same relationship share their
            # wording and so recall each other in a pack.
            #
            # It is still retrievable -- ``MemoryStore.retrieve`` is untouched and
            # ``_retrievable`` still reports it -- it simply does not compete for the
            # prompt. The frame itself lives on in ``structured`` as relationship
            # evidence, which is what the owner asked for.
            return
        seen.add(memory.memory_id)
        # The moment the user said the thing this memory was drawn from - anchored at the
        # *source utterance*, not at when the character consolidated it.
        stamp = ""
        if said_at is not None:
            for event_id in memory.source_event_ids or []:
                stamp = said_at(str(event_id))
                if stamp:
                    break
        selected.append(
            {
                "memory_id": memory.memory_id,
                "kind": memory.kind,
                "summary": memory.summary,
                "said_at": stamp,
                "activation": round(activation, 3),
                "importance": round(memory.importance, 3),
                "topics": list(memory.topics[:4]),
                "selection": source,
            }
        )

    relevant: list[tuple[Any, float, str]] = []
    if cue is not None and store is not None:
        for hit in store.retrieve(cue, limit=limit):
            # ``recalled`` is the retrieval layer's own verdict that a cue brought this
            # memory to mind (two shared bigrams, or a short cue it contains). The
            # score alone cannot decide it: importance and recency carry every memory
            # past any threshold.
            if hit.score >= store.activation_threshold and hit.recalled:
                relevant.append((hit.memory, hit.score, "cue"))

    just_learned: list[tuple[Any, float, str]] = []
    if now is not None and fresh_hours > 0.0:
        cutoff = now - timedelta(hours=fresh_hours)
        fresh = [
            memory
            for memory in projections.memory.list_memories(
                status=MemoryStatus.ACTIVE.value, limit=200
            )
            if memory.created_at is not None and memory.created_at >= cutoff
        ]
        fresh.sort(key=lambda memory: memory.created_at, reverse=True)
        just_learned.extend((memory, 0.0, "recent") for memory in fresh)

    working: list[tuple[Any, float, str]] = []
    pool = projections.memory.list_activated_memories(limit=limit * 2)
    memories = projections.memory.get_memories([item.memory_id for item in pool])
    for activated in pool:
        working.append((memories.get(activated.memory_id), activated.activation, "activation"))

    durable: list[tuple[Any, float, str]] = []
    for memory in projections.memory.list_memories(
        status=MemoryStatus.ACTIVE.value, limit=200
    ):
        if memory.kind in DURABLE_KINDS and memory.importance >= DURABLE_MIN_IMPORTANCE:
            durable.append((memory, 0.0, "durable"))

    sources = (relevant, just_learned, working, durable)
    depth = 0
    while len(selected) < limit and any(depth < len(source) for source in sources):
        for source in sources:
            if depth < len(source):
                memory, activation, label = source[depth]
                add(memory, activation, label)
        depth += 1

    return selected[:limit]


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

    # Recall is cued by the *situation*, not only by the newest sentence (design §20),
    # so the cue is built the same way the Runtime's own heartbeat builds it.
    cue = memory_module.build_cue(
        state=state,
        recent_events=runtime.events.recent(4),
        unfinished=projections.unfinished.list_open(),
        active_emotions=active_emotions,
        now=stamp,
        situation_terms=memory_module.situation_terms(projections),
    )

    boundary_views: list[dict[str, Any]] = []
    constraints: list[str] = []
    if include_boundaries:
        for boundary in projections.boundaries.active(stamp):
            boundary_views.append(boundary.to_dict())
            if boundary.note:
                constraints.append(boundary.note)

    said_at = said_at_resolver(runtime)
    bundle = ContextBundle(
        generated_at=stamp,
        version=state.version,
        psychological=dict(explanation or {}),
        situation=build_situation(projections, now=stamp, said_at=said_at),
        intent=describe_intent(attempt, candidate, pending_count=len(live_candidates), now=stamp),
        memories=select_memories(
            projections,
            limit=4,
            cue=cue,
            store=getattr(runtime, "memory_store", None),
            now=stamp,
            fresh_hours=getattr(runtime.config.memory, "fresh_window_hours", FRESH_WINDOW_HOURS),
            said_at=said_at,
        ),
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
        hints.append(f"有 {len(due)} 件事到点了")
    if state.pressure > 0.5:
        hints.append("憋得有点满，有点想开口")
    if state.mood_valence < -0.25:
        hints.append("底色偏闷，说出来会收着")
    if bundle.intent and bundle.intent.get("committed_not_yet_sent"):
        hints.append("我已经决定要说、还没说出去，别重复问")
    if bundle.boundaries:
        hints.append("有用户划过的线还立着")
    return hints


#: How the runtime's own stored fact records read once they are put in front of her.
#:
#: The stored text is a *record* - "未尽之事：等待面试结果" is a row, not a sentence she
#: would think in - and this block is her own notes to herself. So the prefix is said her
#: way at render time rather than the stored row being rewritten: history keeps exactly
#: what was written, and the voice lives in one table here.
#:
#: Longest first, or "未尽之事已了结：" would be eaten by "未尽之事：".
FACT_VOICE: tuple[tuple[str, str], ...] = (
    ("未尽之事已了结：", "已经了结的："),
    ("未尽之事：", "还没了结的："),
    ("关系信号：", "我感觉到："),
)


#: Prefixes that already label the fact themselves, so the line must not be wrapped in
#: another label. Without this a stored row reads "我确定的事：还没了结的：等待面试结果" -
#: two labels on one fact, which is how the first draft of this voice shipped.
SELF_LABELLED: tuple[str, ...] = (
    "用户",
    "未尽之事",
    "关系信号",
    "我主动联系了用户",
)


def _fact_line(fact: str) -> str:
    """Return one stored fact as a line said the way she would say it.

    Stored facts are records - "未尽之事：等待面试结果" - and a record's prefix is not a
    sentence. Translated here rather than in storage: history keeps exactly what was
    written, and the voice lives in one table.
    """
    text = str(fact or "")
    for prefix, spoken in FACT_VOICE:
        if text.startswith(prefix):
            return spoken + text[len(prefix):]
    if text.startswith(SELF_LABELLED):
        return text
    return f"我确定的事：{text}"


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
            ("我最近大概是这样", "experience"),
            ("心里搁着", "focus"),
            ("卡着的地方", "conflict"),
            ("想做的", "impulse"),
            ("忍着的", "inhibition"),
            ("说出来会是", "expression"),
        ):
            value = psych.get(key)
            if value:
                lines.append(f"- {label}：{value}")
        lines.append("")

    situation = bundle.situation
    if situation.get("facts") or situation.get("inferences"):
        lines.append(SECTION_SITUATION)
        for fact in situation.get("facts", [])[:6]:
            lines.append(f"- {_fact_line(fact)}")
        for inference in situation.get("inferences", [])[:4]:
            lines.append(
                f"- 我猜（把握 {inference.get('confidence')}）：{inference.get('content')}"
            )
        for matter in situation.get("unfinished", [])[:4]:
            lines.append(f"- 还没了结的：{matter.get('title')}（{matter.get('status')}）")
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
            f"- 我想做的：{bundle.intent.get('intent')}"
            if not closed
            else f"- 之前想做的（背景，不是现在的任务）：{bundle.intent.get('intent')}"
        )
        if bundle.intent.get("goal"):
            lines.append(f"- 为的是：{bundle.intent.get('goal')}")
        lead = bundle.intent.get("lead_seconds_before_user_message")
        if closed:
            seconds = bundle.intent.get("closed_seconds_ago")
            if seconds is not None and seconds >= 0:
                lines.append(
                    f"- 用户开口前 {round(seconds, 1)} 秒我就决定要这么做了；"
                    "这个念头已经过去，不用再执行"
                )
            lines.append("- 这只是刚发生的一件小事，提不提看眼下，别硬凹")
        elif bundle.intent.get("committed_not_yet_sent") and lead is not None and lead >= 0:
            lines.append(f"- 用户消息到之前 {round(lead, 1)} 秒我就决定要这么做了（还没发出去）")
        lines.append("")

    if bundle.memories:
        lines.append(SECTION_MEMORY)
        for memory in bundle.memories:
            stamp = str(memory.get("said_at") or "")
            attribution = ATTRIBUTION % stamp if stamp else ""
            lines.append(f"- {memory.get('summary')}{attribution}")
        lines.append("")

    if bundle.constraints:
        lines.append(SECTION_BOUNDARY)
        for constraint in bundle.constraints:
            lines.append(f"- {constraint}")
        if bundle.boundaries:
            lines.append("- 用户划过的线就是线，什么情绪都不能越")
        lines.append("")

    if bundle.attention_hints:
        lines.append("【我该留神】")
        for hint in bundle.attention_hints:
            lines.append(f"- {hint}")
        lines.append("")

    lines.append(SECTION_TIME)
    time_context = bundle.time_context
    if time_context.get("hours_since_last_user_message") is not None:
        lines.append(f"- 距离上次用户消息：{time_context['hours_since_last_user_message']} 小时")
    if time_context.get("hours_since_last_contact") is not None:
        lines.append(f"- 距离上次主动联系：{time_context['hours_since_last_contact']} 小时")
    # Deliberately *no* clock line here. A duration ("8 hours since") is cognition - it
    # describes the state the character is in. A clock ("it is 17:53") is a parameter of
    # the performance, and there is exactly one place that performs: the proactive render
    # prompt, which states it itself (``api_v1.RENDER_CLOCK_PREFIX``). Stating it here as
    # well gave it two authorities, and the weaker one always lost - this block closes by
    # saying it describes the state *before* this turn and is not how to react in it, so
    # the clock arrived pre-de-authorised. Measured on the beta: a 17:53 render still
    # greeted the user with 早呀. ``time_context["local_display"]`` is still built, still
    # local rather than UTC, and is what the render states.
    lines.append("")
    lines.append(
        "以上都只是我进来之前的状态，是一轮的临时背景，别照抄，也别写进长期记录；"
        "它说的是之前，不是这一句该怎么回。"
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
