"""Low-frequency deep cognition refresh (architecture patch v0.2, sections 18-21).

The Runtime's per-turn path is deliberately cheap: explicit events are settled
coarsely and everything else is recorded as ``unresolved``. That design is only
sound if something eventually goes back and reads the backlog - and that is this
module.

Three responsibilities, kept separate on purpose:

* :func:`evaluate_triggers` decides *whether* a refresh is worth spending on,
  using the priority order from patch section 21;
* :func:`build_request` assembles the read-only input bundle;
* :func:`ground_suggestions` turns whatever came back into operations that name
  only entities that actually exist.

The last one is the load-bearing part. A refresh is generated text, and generated
text about someone's emotional history is exactly the kind of material that
sounds right and is not. Nothing crosses into state without resolving its
sources first; anything that fails is dropped and reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .utility import isoformat

LOGGER = logging.getLogger("companion_runtime.deep_refresh")

__all__ = [
    "GroundedOperation",
    "RefreshTrigger",
    "TRIGGER_PRIORITY",
    "build_request",
    "evaluate_triggers",
    "ground_suggestions",
]

#: Trigger reasons in the priority order of patch section 21. Lower is more
#: urgent, and :func:`evaluate_triggers` returns the most urgent match only:
#: reporting every reason would let a cheap signal outrank an important one.
TRIGGER_PRIORITY: tuple[str, ...] = (
    "unresolved_backlog",
    "major_event",
    "matter_due",
    "candidate_pool_empty",
    "proactive_without_grounding",
    "history_may_be_wrong",
    "user_evidence_overturns",
    "idle_refresh",
)

#: Operation kinds the reducer knows how to apply. Anything else is discarded
#: during grounding rather than passed through "just in case".
OPERATION_KINDS = frozenset(
    {
        "reinterpretation",
        "psychological_interpretation",
        "candidate_intent",
        "memory",
        "unfinished_matter",
        "user_model_evidence",
    }
)

#: Kinds that must name at least one resolvable source. Only the interpretation
#: cache is exempt: it summarises the whole state rather than a specific event.
KINDS_REQUIRING_SOURCES = frozenset(
    {
        "reinterpretation",
        "candidate_intent",
        "memory",
        "unfinished_matter",
        "user_model_evidence",
    }
)

#: Suggestion fields that are a single object rather than a list of operations.
SINGLE_FIELDS: Mapping[str, str] = {
    "psychological_interpretation": "psychological_interpretation",
}

#: Mapping from a suggestion field on the provider payload to an operation kind.
FIELD_TO_KIND: Mapping[str, str] = {
    "reinterpretations": "reinterpretation",
    "candidate_intent_operations": "candidate_intent",
    "memory_suggestions": "memory",
    "unfinished_matter_suggestions": "unfinished_matter",
    "user_model_evidence_suggestions": "user_model_evidence",
}


@dataclass(slots=True)
class RefreshTrigger:
    """Whether a deep refresh should run, and why.

    Attributes:
        should_refresh: The verdict.
        reason: Machine-readable cause; ``not_needed`` when nothing applied.
        priority: 1 is most urgent; matches the order in
            :data:`TRIGGER_PRIORITY`.
        unresolved_count: Backlog size seen by the evaluation.
        detail: Extra context for operators.
    """

    should_refresh: bool
    reason: str
    priority: int = 99
    unresolved_count: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "should_refresh": self.should_refresh,
            "reason": self.reason,
            "priority": self.priority,
            "unresolved_count": self.unresolved_count,
            "detail": self.detail,
        }


def evaluate_triggers(
    *,
    unresolved_count: int = 0,
    major_event: bool = False,
    matter_due: bool = False,
    candidate_pool_size: int | None = None,
    wants_proactive: bool = False,
    proactive_grounded: bool = True,
    history_suspect: bool = False,
    user_evidence_overturns: bool = False,
    hours_since_last_refresh: float | None = None,
    has_previous_refresh: bool = True,
    has_material: bool = True,
    config: Any = None,
) -> RefreshTrigger:
    """Decide whether a deep refresh is warranted, and why.

    The checks run in priority order and the first match wins. That matters
    because several conditions are usually true at once - a long absence with a
    backlog and an empty candidate pool - and the caller needs to know which one
    actually justified the spend.

    Args:
        unresolved_count: Events waiting for interpretation.
        major_event: A relationship-significant event occurred recently.
        matter_due: An unfinished matter has reached its due time.
        candidate_pool_size: Number of active candidate intentions. ``None`` means
            "not checked" and never triggers, which is the difference between an
            empty pool and an unasked question.
        wants_proactive: The motivation layer wants to act.
        proactive_grounded: Whether that intent has a resolvable semantic basis.
        history_suspect: An earlier interpretation may be wrong.
        user_evidence_overturns: New user evidence contradicts a stored reading.
        hours_since_last_refresh: Time since the previous refresh. ``None`` means
            "never refreshed", which is *not* the same as zero: a Runtime that has
            never spent must not be paced by an interval it has not yet started.
            A negative value is treated as unknown for the same reason - it would
            otherwise read as "just refreshed" and stall the rule forever.
        has_previous_refresh: Whether a refresh has actually been attempted before,
            which is what gives the minimum-interval guard a real baseline. It
            defaults to ``True`` because a caller that measures elapsed time is by
            definition describing a previous refresh; a caller with no baseline
            passes ``False`` and is not paced by an interval it never started.
        has_material: Whether there is anything for a refresh to reason about - open
            matters or unresolved events. When there is nothing, the speculative
            ``idle_refresh`` rule is disarmed: a Runtime that has never been spoken
            to has been idle since its creation epoch, and "idle" must not be read
            as "worth spending a request to rediscover that nothing has happened".
        config: A :class:`~companion_runtime.config.SemanticConfig`, or any object
            exposing the same attribute names. ``None`` uses the documented
            defaults so the function is usable from tests without a Runtime.

    Returns:
        A :class:`RefreshTrigger`; ``should_refresh`` is ``False`` when no rule
        matched, which is the common case.
    """
    backlog_threshold = int(getattr(config, "unresolved_backlog_threshold", 8))
    idle_hours = float(getattr(config, "deep_refresh_idle_hours", 12.0))
    min_interval = float(getattr(config, "deep_refresh_min_interval_seconds", 3600.0))

    elapsed = hours_since_last_refresh
    if elapsed is not None and elapsed < 0.0:
        # A negative interval is nonsense rather than a measurement; refusing to
        # use it is safer than clamping it to the smallest possible value, which
        # would mean "we refreshed a moment ago" and disable the whole feature.
        LOGGER.warning("Ignoring negative hours_since_last_refresh=%r", hours_since_last_refresh)
        elapsed = None

    # A refresh is never allowed to run back to back regardless of the reason:
    # the cost is real and the backlog does not change that fast. The rule needs a
    # genuine baseline - a caller that has never refreshed has nothing to pace.
    if has_previous_refresh and elapsed is not None and elapsed * 3600.0 < min_interval:
        return RefreshTrigger(
            False,
            "min_interval_not_elapsed",
            priority=99,
            unresolved_count=unresolved_count,
            detail=f"{elapsed:.2f}h since the last refresh",
        )

    checks: tuple[tuple[str, bool], ...] = (
        ("unresolved_backlog", unresolved_count >= backlog_threshold),
        ("major_event", major_event),
        ("matter_due", matter_due),
        ("candidate_pool_empty", candidate_pool_size is not None and candidate_pool_size <= 0),
        ("proactive_without_grounding", wants_proactive and not proactive_grounded),
        ("history_may_be_wrong", history_suspect),
        ("user_evidence_overturns", user_evidence_overturns),
        ("idle_refresh", has_material and elapsed is not None and elapsed >= idle_hours),
    )
    for reason, matched in checks:
        if matched:
            return RefreshTrigger(
                True,
                reason,
                priority=TRIGGER_PRIORITY.index(reason) + 1,
                unresolved_count=unresolved_count,
            )
    return RefreshTrigger(
        False, "not_needed", priority=99, unresolved_count=unresolved_count
    )


@dataclass(slots=True)
class GroundedOperation:
    """A suggested change that survived grounding.

    Attributes:
        kind: One of :data:`OPERATION_KINDS`.
        payload: The operation body, passed to the reducer unchanged.
        sources: Identifiers the operation is grounded in.
    """

    kind: str
    payload: dict[str, Any]
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {"kind": self.kind, "payload": dict(self.payload), "sources": list(self.sources)}


def _as_mapping(item: Any) -> dict[str, Any] | None:
    """Return ``item`` as a plain dict when it is a mapping, else ``None``."""
    if isinstance(item, Mapping):
        return {str(key): value for key, value in item.items()}
    return None


def _sources_of(item: Mapping[str, Any]) -> list[str]:
    """Extract the source identifiers an operation claims."""
    raw = item.get("sources") or item.get("source_ids") or item.get("source_event_ids") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(value) for value in raw if isinstance(value, (str, int))]
    return []


def _payload_of(item: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the operation body, tolerating a flattened suggestion."""
    nested = item.get("payload")
    if isinstance(nested, Mapping):
        return {str(key): value for key, value in nested.items()}
    # Providers may return the body inline; drop the routing keys so the reducer
    # sees only the fields it documents.
    return {
        str(key): value
        for key, value in item.items()
        if key not in {"kind", "sources", "source_ids", "source_event_ids"}
    }


def ground_suggestions(
    suggestions: Any,
    *,
    resolvable: Callable[[str], bool],
    is_matter: Callable[[str], bool] | None = None,
    max_candidate_operations: int = 8,
) -> tuple[list[GroundedOperation], list[dict[str, Any]]]:
    """Convert a provider's suggestions into operations that are safe to apply.

    Grounding is the whole reason a deep refresh can be allowed to touch state:
    every operation must name entities that exist. A model that invents a memory
    about a conversation that never happened produces an operation whose sources
    do not resolve, and it is discarded here rather than stored.

    Args:
        suggestions: A ``DeepRefreshSuggestions``-shaped object, or a mapping with
            the same six fields.
        resolvable: Callback returning whether an identifier exists.
        is_matter: Callback returning whether an identifier names an unfinished
            matter. When given, a new matter may not be grounded *only* in other
            matters (see the restatement guard below).
        max_candidate_operations: Upper bound on returned operations, so one
            verbose response cannot rewrite the whole state at once.

    Returns:
        ``(operations, violations)``. Violations are structured records, not
        strings, so they can be surfaced on ``/health`` and counted.
    """
    operations: list[GroundedOperation] = []
    violations: list[dict[str, Any]] = []

    # Single-object fields are handled first: an interpretation summarises the
    # whole state, so it is not a list of operations and needs no sources.
    for field_name, kind in SINGLE_FIELDS.items():
        value = _suggestions_field(suggestions, field_name)
        body = _as_mapping(value)
        if body is None:
            continue
        payload = _payload_of(body)
        if payload:
            operations.append(GroundedOperation(kind=kind, payload=payload, sources=[]))

    for field_name, kind in FIELD_TO_KIND.items():
        items = _suggestion_list(suggestions, field_name)
        for item in items:
            body = _as_mapping(item)
            if body is None:
                violations.append({"kind": kind, "reason": "not_a_mapping", "sources": []})
                continue
            sources = _sources_of(body)
            payload = _payload_of(body)
            if not payload:
                violations.append({"kind": kind, "reason": "empty_payload", "sources": sources})
                continue

            if kind in KINDS_REQUIRING_SOURCES:
                if not sources:
                    violations.append(
                        {"kind": kind, "reason": "missing_sources", "sources": []}
                    )
                    continue
                unresolved = [source for source in sources if not resolvable(source)]
                if unresolved:
                    violations.append(
                        {
                            "kind": kind,
                            "reason": "ungrounded_sources",
                            "sources": unresolved,
                        }
                    )
                    continue

            # A matter grounded *only* in other matters restates what the Runtime
            # already holds rather than discovering anything: a refresh that is handed
            # the open matters as input will happily echo them back as "new" ones and
            # cite their ids as sources. Measured on a real beta, one person's open
            # matters grew from 7 to 13 overnight, all six additions restatements of
            # existing ones, and `_same_subject` could not catch the rewrites. A matter
            # has to cite at least one non-matter source (an event, a memory).
            if kind == "unfinished_matter" and is_matter is not None:
                matter_sources = [source for source in sources if is_matter(source)]
                if len(matter_sources) == len(sources):
                    violations.append(
                        {
                            "kind": kind,
                            "reason": "matter_restatement",
                            "sources": sources,
                        }
                    )
                    continue

            operations.append(GroundedOperation(kind=kind, payload=payload, sources=sources))

    if len(operations) > max_candidate_operations:
        dropped = operations[max_candidate_operations:]
        operations = operations[:max_candidate_operations]
        violations.append(
            {
                "kind": "limit",
                "reason": "exceeded_max_operations",
                "sources": [item.kind for item in dropped],
            }
        )
    return operations, violations


def _suggestions_field(suggestions: Any, name: str) -> Any:
    """Return one suggestion field from an object or mapping, defensively."""
    if isinstance(suggestions, Mapping):
        return suggestions.get(name)
    return getattr(suggestions, name, None)


def _suggestion_list(suggestions: Any, name: str) -> list[Any]:
    """Return one suggestion list, or an empty list when the field is absent."""
    value = _suggestions_field(suggestions, name)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def build_request(
    *,
    runtime: Any,
    now: datetime | None = None,
    limit: int = 20,
    key_quote_limit: int = 3,
) -> Any:
    """Assemble the read-only input bundle for one deep refresh.

    The bundle is exactly what patch section 19 prescribes: the accumulated
    unresolved events plus the current projections. It deliberately excludes the
    full conversation history - the refresh reasons about what the Runtime kept,
    not about re-reading the transcript.

    This function performs **no writes**. It reads projections and returns plain
    data, so it is safe to call while deciding whether to spend on a refresh.

    Args:
        runtime: A :class:`~companion_runtime.runtime.Runtime`.
        now: Reference time.
        limit: Maximum unresolved events to include.
        key_quote_limit: How many recent raw quotes to attach.

    Returns:
        A ``providers.DeepRefreshRequest``.
    """
    from .providers import DeepRefreshRequest

    stamp = now or _utcnow()
    state = runtime.state()
    projections = runtime.projections

    unresolved = projections.semantics.list_unresolved(limit=limit)
    # Ageing policy from ``semantic.unresolved_max_age_hours``: an event that has
    # sat unresolved for days stops justifying a refresh spend, but it is never
    # deleted - it stays in the append-only log and the backlog, exactly as patch
    # section 25 requires ("不理解可以延迟，但原始事件必须保留").
    max_age = float(getattr(runtime.config.semantic, "unresolved_max_age_hours", 72.0))
    unresolved = [item for item in unresolved if _age_hours(item, stamp) <= max_age]
    conversations: dict[str, Any] = {
        item.get("event_id"): item for item in unresolved if item.get("event_id")
    }

    key_quotes: list[dict[str, Any]] = []
    for event_id in list(conversations)[:key_quote_limit]:
        event = runtime.events.get(event_id)
        if event is None:
            continue
        key_quotes.append(
            {
                "event_id": event_id,
                "actor": event.actor,
                "content": event.content or "",
                "timestamp": isoformat(event.timestamp),
            }
        )

    summary = ""
    try:
        summary = str(projections.user_model.semantic_view().get("summary") or "")
    except Exception:  # noqa: BLE001 - a summary is a nice-to-have, never required
        summary = ""

    return DeepRefreshRequest(
        unresolved_events=[dict(item) for item in unresolved],
        situation={
            "facts": [item.get("content") for item in projections.situation.list_active(limit=8)],
            "unfinished": [
                item.to_dict() for item in projections.unfinished.list_open()[:8]
            ],
        },
        mood={
            "valence": round(state.mood_valence, 3),
            "arousal": round(state.mood_arousal, 3),
            "stability": round(state.mood_stability, 3),
            "impulse": round(state.approach_impulse, 3),
            "restraint": round(state.restraint, 3),
            "pressure": round(state.pressure, 3),
        },
        active_emotions=[
            event.to_dict() for event in projections.emotion.list_active()[:8]
        ],
        memories=[item.to_dict() for item in projections.memory.list_memories(limit=8)],
        unfinished=[item.to_dict() for item in projections.unfinished.list_open()[:8]],
        user_model_summary=summary,
        candidates=[item.to_dict() for item in projections.candidates.list_active(limit=8)],
        key_quotes=key_quotes,
    )


def _age_hours(item: Mapping[str, Any], now: datetime) -> float:
    """Return how long ago an unresolved record was created.

    Args:
        item: A row from ``SemanticProjection.list_unresolved``.
        now: Reference time.

    Returns:
        Age in hours; unparseable or missing timestamps count as fresh, so a
        record is never dropped from the refresh set because of a bad field.
    """
    from .utility import parse_datetime

    raw = item.get("created_at")
    if not raw:
        return 0.0
    try:
        created = parse_datetime(raw)
    except (ValueError, TypeError):
        # A malformed timestamp must never hide evidence from the refresh; treat
        # it as fresh and let the caller's ordering deal with it.
        return 0.0
    if created is None:
        return 0.0
    return max(0.0, (now - created).total_seconds() / 3600.0)


def _utcnow() -> datetime:
    """Return the current aware UTC time (imported lazily to keep imports flat)."""
    from .utility import utcnow

    return utcnow()
