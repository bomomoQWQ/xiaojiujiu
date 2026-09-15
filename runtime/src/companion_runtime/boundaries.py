"""Boundary state machine.

Boundaries are *hard constraints*, not another term in the motivational game.
A user saying "don't contact me proactively today" must not be overridable by a
pressure value of 0.99. The state machine therefore offers:

* detection of explicit boundaries in user language (high precision rules, the
  entry barrier that runs before the semantic model has finished);
* a small lifecycle (temporal / topic / permanent / conditional, plus revocation
  and expiry);
* an authorization verdict that the motivational layer must consult.

A user-initiated message re-opens *reply* permission, but deliberately does not
restore *proactive* permission for the rest of the window.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from .config import RuntimeConfig
from .memory import DEDUPE_MIN_SHARED_TOKENS
from .projections import BoundaryProjection
from .typing import (
    Boundary,
    BoundaryType,
    EventType,
    RawEvent,
    RuntimeState,
    UnfinishedMatter,
    new_id,
)
from .utility import clamp, summarize_text, topic_tokens, utcnow

LOGGER = logging.getLogger("companion_runtime.boundaries")

#: The scopes that are *about something* rather than about contact in general. Only
#: these carry a bound referent (:attr:`~companion_runtime.typing.Boundary.subject`),
#: and only these are enforced against individual candidates.
TOPIC_SCOPES = ("topic_avoid", "repeated_interrogation")


@dataclass(slots=True)
class BoundaryPattern:
    """A high-precision language rule that declares a boundary."""

    pattern: re.Pattern[str]
    boundary_type: str
    allow_proactive: bool
    allow_reply: bool
    scope: str = "all_topics"
    hours: float | None = 24.0
    note: str = ""


def _compile(expression: str) -> re.Pattern[str]:
    """Compile a boundary pattern with case-insensitive matching."""
    return re.compile(expression, re.IGNORECASE)


#: Deliberately conservative: a false positive silently removes the agent's
#: ability to be proactive, a false negative merely delays detection until the
#: semantic layer catches up. Both are acceptable; over-triggering on generic
#: words is not.
BOUNDARY_PATTERNS: tuple[BoundaryPattern, ...] = (
    BoundaryPattern(
        _compile(
            r"(今天|今晚|这几天|今天内|以后|永远|再也|从今往后)?\s*"
            r"(都)?\s*(不要|别|不用|不需要)\s*(再)?\s*(都)?\s*"
            r"(主动)?\s*(联系|找我|发消息|打扰|来消息)"
        ),
        BoundaryType.TEMPORAL.value,
        allow_proactive=False,
        allow_reply=True,
        note="user asked not to be contacted proactively",
    ),
    BoundaryPattern(
        _compile(r"don'?t\s+(message|contact|text|dm)\s+me"),
        BoundaryType.TEMPORAL.value,
        allow_proactive=False,
        allow_reply=True,
        note="user asked not to be contacted proactively",
    ),
    BoundaryPattern(
        _compile(r"no\s+proactive\s+(messages?|contact)"),
        BoundaryType.TEMPORAL.value,
        allow_proactive=False,
        allow_reply=True,
        note="explicit no-proactive instruction",
    ),
    BoundaryPattern(
        _compile(
            r"(以后|永远|再也|从今往后)\s*(都)?\s*(不要|别|不用)\s*(再)?\s*"
            r"(主动|联系|找我|发消息)"
        ),
        BoundaryType.PERMANENT.value,
        allow_proactive=False,
        allow_reply=True,
        hours=None,
        note="permanent no-proactive instruction",
    ),
    BoundaryPattern(
        _compile(r"(永远|再也|从今往后)\s*(都)?\s*(不要|别|不用)?"),
        BoundaryType.PERMANENT.value,
        allow_proactive=False,
        allow_reply=True,
        hours=None,
        note="permanent no-proactive instruction",
    ),
    # The catch-all temporal rule is deliberately last: it is the broadest, and
    # the deduplication keeps the first (most specific) match per scope.
    BoundaryPattern(
        _compile(
            r"(今天|今晚|这几天|今天内)?\s*(都)?\s*(不要|别|不用|不需要)\s*(再)?\s*"
            r"(都)?\s*(主动)?\s*(联系|找我|发消息|打扰|来消息)"
        ),
        BoundaryType.TEMPORAL.value,
        allow_proactive=False,
        allow_reply=True,
        note="user asked not to be contacted proactively",
    ),
    BoundaryPattern(
        # ``(再)?`` after the negative particle is load-bearing, not decoration: the most
        # natural Chinese phrasing of this instruction is "别再追问我在干嘛", and without
        # it the particle branch (别/不要/不要再/不许) could not consume the 再, so the
        # rule matched "别一直问我这个" and *missed* "别再追问我在干嘛" - i.e. the
        # ``repeated_interrogation`` scope, and with it §52's topic-level gate for this
        # pattern, was unreachable from ordinary language. Found by asking whether a
        # real sentence declares a boundary, not by reading the regex.
        _compile(
            r"(别|不要|不许)\s*(再)?\s*(一直|老是|总是|反复)?\s*"
            r"(问|追问|打听)\s*(我)?\s*(这个|这件事|在干嘛|在哪|在做什么)"
        ),
        BoundaryType.TOPIC.value,
        allow_proactive=True,
        allow_reply=True,
        scope="repeated_interrogation",
        hours=72.0,
        note="user is sensitive about repeated interrogation",
    ),
    BoundaryPattern(
        _compile(r"stop\s+(asking|pestering)"),
        BoundaryType.TOPIC.value,
        allow_proactive=True,
        allow_reply=True,
        scope="repeated_interrogation",
        hours=72.0,
        note="user is sensitive about repeated interrogation",
    ),
    BoundaryPattern(
        _compile(r"(暂时|先)\s*(不要|别)\s*(跟我)?\s*(说|聊)\s*(这个|这件事)"),
        BoundaryType.TOPIC.value,
        allow_proactive=True,
        allow_reply=True,
        scope="topic_avoid",
        hours=48.0,
        note="user asked to avoid a topic for now",
    ),
    BoundaryPattern(
        _compile(r"(今天|今晚|这几天)\s*(我)?\s*(想)?\s*(自己|一个人)\s*(待|呆)着"),
        BoundaryType.CONDITIONAL.value,
        allow_proactive=False,
        allow_reply=True,
        hours=12.0,
        note="user wants space for now",
    ),
)

REVOCATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    _compile(r"(可以|能|欢迎)\s*(主动|随时)\s*(联系|找我|发消息)"),
    _compile(r"(撤回|取消|收回)\s*(刚才|之前)?\s*(的)?\s*(要求|话|边界)"),
    _compile(r"(没事了|不需要了|不用了)\s*[,，]?\s*(可以|能)?\s*(主动|联系|找我)"),
    _compile(r"you\s+can\s+(message|contact)\s+me"),
)


def detect_boundaries(
    event: RawEvent,
    *,
    state: RuntimeState,
    config: RuntimeConfig,
    now: datetime | None = None,
    referent: str | None = None,
) -> list[Boundary]:
    """Detect explicit boundaries declared by one event.

    Args:
        event: Candidate event (usually a user message).
        state: Current runtime state; ``boundary_respect`` scales the window.
        config: Runtime configuration.
        now: Reference time, defaults to the event timestamp.
        referent: What a deictic instruction is about, when the caller could work it
            out (:func:`referent_for`). It is attached only to the *topic* scopes: a
            boundary about a topic without a topic is only a category, and the decision
            gate has nothing to compare a candidate against.

    Returns:
        Newly declared boundaries (possibly empty).
    """
    if event.event_type != EventType.USER_MESSAGE.value:
        return []
    text = event.content or ""
    if not text.strip():
        return []
    reference = now or event.timestamp
    found: dict[str, tuple[int, Boundary]] = {}
    for index, rule in enumerate(BOUNDARY_PATTERNS):
        if not rule.pattern.search(text):
            continue
        hours = rule.hours
        if hours is not None:
            # A character with strong boundary respect honours the window a
            # little longer rather than shorter.
            hours *= 0.85 + 0.3 * state.values.boundary_respect
        expires = reference + timedelta(hours=hours) if hours is not None else None
        candidate = Boundary(
            boundary_id=new_id("boundary"),
            type=rule.boundary_type,
            scope=rule.scope,
            allow_reply=rule.allow_reply,
            allow_proactive=rule.allow_proactive,
            starts_at=reference,
            expires_at=expires,
            source_event_id=event.event_id,
            note=rule.note,
            subject=referent if rule.scope in TOPIC_SCOPES else None,
        )
        strength = _rule_strength(rule, hours)
        current = found.get(rule.scope)
        # One boundary per scope. The strongest rule wins, so a permanent
        # instruction is never silently downgraded to a 24h window by the
        # broad temporal rule matching the same sentence.
        if current is None or strength > current[0]:
            found[rule.scope] = (strength, candidate)
    return [boundary for _strength, boundary in found.values()]


def referent_for(
    *,
    previous_events: Sequence[RawEvent],
    matters: Sequence[UnfinishedMatter],
) -> str | None:
    """Return what a deictic instruction ("暂时不要跟我说这个") is about.

    The boundary rules match the *instruction*, never its object: "这个" points at
    whatever was being discussed. Two sources are consulted, most precise first:

    1. **the open matter built from that very message** - a matter records the events
       it came from, so event identity settles it without any text comparison. This is
       the common case and the one text overlap gets wrong: "我明天下午三点面试，结束了
       告诉你" and the matter title "等待面试结果" share exactly one bigram;
    2. **an open matter whose title overlaps what was said** - for a referent that was
       discussed rather than just promised.

    With nothing to go on the answer is ``None``, and every caller must treat that as
    "do not guess": an unbound topic boundary constrains nothing until it expires,
    which is the conservative direction. Guessing here would silence a subject the user
    never named, and the delivery-time gate still stops anything that would mention the
    avoided topic by name.

    There used to be a third step - "the last thing the user said", used verbatim and
    with no overlap requirement - and it was removed because it contradicted this
    docstring rather than merely being imprecise. Measured consequence: the user wrote
    "有件事想说清楚，不要一直追问我在干嘛，我不太喜欢被盯着。" and the boundary was bound
    to the *politeness formula that preceded it* ("谢谢你听我说这些。"), which shares no
    bigram with the instruction. A wrong binding is worse than none: the boundary looked
    enforced while `blocks_candidate` could never match it, so the subject it claimed to
    protect was silently unprotected. A deictic "这个" whose topic is genuinely not
    recoverable now yields no subject, and the honest consequence is that this boundary
    gates nothing - which the delivery-time gate compensates for.

    Args:
        previous_events: Earlier user messages, newest first.
        matters: Open unfinished matters.

    Returns:
        The subject text, or ``None`` when it cannot be established.
    """
    for event in previous_events:
        text = (event.content or "").strip()
        if not text:
            continue
        for matter in matters:
            title = (matter.title or "").strip()
            if not title:
                continue
            if event.event_id in set(matter.source_event_ids or ()):
                return title
        for matter in matters:
            title = (matter.title or "").strip()
            if not title:
                continue
            if len(topic_tokens(text) & topic_tokens(title)) >= DEDUPE_MIN_SHARED_TOKENS:
                return title
    return None


#: Candidate types that *ask the user something*. This is the boundary layer's view of
#: "question-shaped", used by :func:`blocks_candidate` for the
#: ``repeated_interrogation`` scope: "别再一直追问我在干嘛" is an instruction about being
#: interrogated, not about one subject, so what it rules out is the shape of the
#: candidate rather than its topic.
QUESTION_CANDIDATE_TYPES = ("follow_up", "curious_question")


def blocks_candidate(
    boundaries: Sequence[Boundary],
    *,
    now: datetime,
    subject: str,
    is_question: bool,
) -> tuple[str, str] | None:
    """Return ``(boundary_id, reason)`` when a topic boundary forbids one candidate.

    Topic boundaries carry ``allow_proactive=True`` - being told "don't bring *this*
    up" is not being told to fall silent - so they cannot be enforced by the
    proactivity gate that :func:`evaluate` implements. They are enforced here, on the
    candidate, *before* the utility comparison: a candidate the user has ruled out must
    not be weighed against the others at all, because weighing it is what spends the
    decision on something that can never be sent.

    Two scopes are handled, and only these two:

    * ``topic_avoid`` - a candidate about the same thing as the bound subject is
      blocked. Without a bound subject nothing is blocked (see :func:`referent_for`);
    * ``repeated_interrogation`` - a *question-shaped* candidate about the same thing as
      the bound subject is blocked. Both halves matter: the instruction is about being
      interrogated *on that subject*, and blocking every question for the window - the
      first version of this - silenced legitimate follow-ups about everything else. That
      was measured, not theorised: it stopped the autonomous round from queuing any
      render work at all in the resilience simulation.

    Args:
        boundaries: Boundaries currently in force.
        now: Reference time.
        subject: The candidate's subject text (its target and intent).
        is_question: Whether the candidate would ask the user something.

    Returns:
        The blocking boundary's identifier and a stable reason string, or ``None``.
    """
    candidate_tokens = topic_tokens(subject)
    for boundary in boundaries:
        if not boundary.is_active(now):
            continue
        if boundary.scope not in TOPIC_SCOPES:
            continue
        bound = (boundary.subject or "").strip()
        if not bound:
            continue
        if boundary.scope == "repeated_interrogation" and not is_question:
            continue
        if len(candidate_tokens & topic_tokens(bound)) >= DEDUPE_MIN_SHARED_TOKENS:
            return boundary.boundary_id, boundary.scope
    return None


def _rule_strength(rule: BoundaryPattern, hours: float | None) -> int:
    """Score how strong a matched rule is, for deduplication within a scope."""
    strength = 0
    if rule.boundary_type == BoundaryType.PERMANENT.value:
        strength += 100
    elif rule.boundary_type == BoundaryType.CONDITIONAL.value:
        strength += 10
    if hours is None:
        strength += 50
    if not rule.allow_proactive:
        strength += 5
    return strength


def detect_revocation(event: RawEvent, *, active: Sequence[Boundary]) -> list[str]:
    """Return the identifiers of boundaries explicitly revoked by an event.

    Args:
        event: Candidate event.
        active: Currently active boundaries.

    Returns:
        Boundary identifiers that the user has clearly lifted.
    """
    if event.event_type != EventType.USER_MESSAGE.value:
        return []
    text = event.content or ""
    resolved: list[str] = []
    for rule in REVOCATION_PATTERNS:
        if not rule.search(text):
            continue
        for boundary in active:
            if boundary.type == BoundaryType.PERMANENT.value and "撤回" not in text and "取消" not in text:
                # A permanent boundary is not lifted by a casual "you can message me".
                continue
            resolved.append(boundary.boundary_id)
    return resolved


def revoke(
    projection: BoundaryProjection,
    connection: sqlite3.Connection,
    boundary_ids: Sequence[str],
    *,
    now: datetime | None = None,
) -> list[Boundary]:
    """Revoke boundaries by identifier.

    Args:
        projection: Boundary storage.
        connection: Write connection.
        boundary_ids: Boundaries to revoke.
        now: Revocation time.

    Returns:
        The revoked boundaries.
    """
    stamp = now or utcnow()
    revoked: list[Boundary] = []
    for identifier in boundary_ids:
        boundary = projection.get(identifier)
        if boundary is None or boundary.revoked_at is not None:
            continue
        boundary.revoked_at = stamp
        projection.upsert(connection, boundary)
        revoked.append(boundary)
    return revoked


@dataclass(slots=True)
class BoundaryVerdict:
    """Result of consulting the boundary state machine."""

    allow_proactive: bool
    allow_reply: bool
    blocking_ids: list[str]
    constraints: list[str]
    reason: str
    allow_any: bool = True

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable rendering."""
        return {
            "allow_proactive": self.allow_proactive,
            "allow_reply": self.allow_reply,
            "allow_any": self.allow_any,
            "blocking_ids": list(self.blocking_ids),
            "constraints": list(self.constraints),
            "reason": self.reason,
        }


def evaluate(
    boundaries: Sequence[Boundary],
    *,
    now: datetime,
    state: RuntimeState,
    is_proactive: bool,
    scope: str | None = None,
) -> BoundaryVerdict:
    """Consult the active boundaries for a prospective action.

    Args:
        boundaries: Boundaries currently in force.
        now: Reference time.
        state: Runtime state; kept so the verdict can be reused as a global kill
            switch without changing the signature, and so future policy can read
            the value profile.
        is_proactive: Whether the action is an unprompted contact.
        scope: Optional topic scope of the action.

    Returns:
        A :class:`BoundaryVerdict`; ``allow_proactive`` is the hard gate used by
        the motivational layer.
    """
    blocking: list[str] = []
    constraints: list[str] = []
    # Hard boundaries are the *only* thing that can deny proactive permission.
    # The stored flag is deliberately not consulted here, because that flag is
    # itself derived from this function - reading it back would make the value
    # self-latching and an expired boundary could never release it.
    allow_proactive = True
    allow_reply = True

    for boundary in boundaries:
        if not boundary.is_active(now):
            continue
        scope_match = boundary.scope in {"all_topics", scope or "all_topics"}
        # A boundary that forbids proactive contact removes proactive permission
        # regardless of what kind of action is being evaluated: the caller asked
        # about permission, not about this particular action's shape.
        if scope_match and not boundary.allow_proactive:
            allow_proactive = False
            blocking.append(boundary.boundary_id)
            if boundary.note:
                constraints.append(boundary.note)
        if not boundary.allow_reply:
            allow_reply = False
            constraints.append(boundary.note or "reply not permitted")

    if not allow_proactive:
        reason = "blocked_by_boundary" if blocking else "proactive_disabled"
    elif not allow_reply:
        reason = "reply_blocked"
    else:
        reason = "permitted"

    return BoundaryVerdict(
        allow_proactive=allow_proactive,
        allow_reply=allow_reply,
        blocking_ids=blocking,
        constraints=constraints,
        reason=reason,
        allow_any=allow_proactive or allow_reply,
    )


def nearest_expiry(boundaries: Sequence[Boundary], now: datetime) -> datetime | None:
    """Return the earliest future expiry among the given boundaries."""
    candidates = [
        boundary.expires_at
        for boundary in boundaries
        if boundary.expires_at is not None and boundary.expires_at > now
    ]
    return min(candidates) if candidates else None


def decay_and_persist(
    projection: BoundaryProjection,
    connection: sqlite3.Connection,
    *,
    now: datetime,
    expiring_tolerance_seconds: float = 0.0,
) -> dict[str, int]:
    """Report boundary bookkeeping for the current tick.

    Boundaries are not deleted when they expire; they simply stop applying and
    remain in history as evidence of what the user once asked for.

    Returns:
        Counts of active and expired boundaries.
    """
    every = projection.list_all(include_revoked=True)
    active = [b for b in every if b.is_active(now)]
    expired = [
        b
        for b in every
        if b.expires_at is not None
        and b.expires_at <= now
        and b.revoked_at is None
        and (now - b.expires_at).total_seconds() >= expiring_tolerance_seconds
    ]
    return {"active": len(active), "expired": len(expired), "total": len(every)}


def clamp_window(hours: float) -> float:
    """Clamp a boundary window into a sane range."""
    return clamp(hours, 0.5, 24.0 * 365)
