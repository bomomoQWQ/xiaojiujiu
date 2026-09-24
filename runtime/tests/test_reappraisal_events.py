"""A reappraisal must be visible in the event log, not only in a projection (design §67).

Design §67 asks for a ``reappraisal_event``: "如果新解释对现在有意义 → 生成
``reappraisal_event``". The Runtime wrote a ``reappraisals`` row and a new interpretation
version, but never appended an event - so ``EventType.REAPPRAISAL`` was a
declared-but-never-emitted member and the two records of one fact disagreed: the
projection knew that the character had understood something later, the history of record
did not. An operator reading the log, or a host replaying it, could not see it at all.

These tests pin the append, the agreement between the two records, and the identifier
namespace (which used to be ``mem_``, i.e. a memory, for a record that is not one).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

import pytest

from companion_runtime.eventlog import EventQuery
from companion_runtime.providers import DeepRefreshSuggestions
from companion_runtime.runtime import Runtime
from companion_runtime.typing import EventType, is_event_identifier

from conftest import BASE_TIME, build_config

AMBIGUOUS = "算了，也没什么。"
REINTERPRETATION = "当时他可能是失望的，我没有接住。"
REALIZED = "现在意识到，那句算了后面是失望。"


def _runtime() -> Runtime:
    config = build_config()
    config.semantic.deep_refresh_min_interval_seconds = 0.0
    config.semantic.unresolved_backlog_threshold = 1
    return Runtime(config=config)


class _StubProvider:
    """A provider double returning exactly one reinterpretation."""

    name = "stub"

    def __init__(self, suggestions: Any) -> None:
        self.suggestions = suggestions
        self.calls = 0

    def available(self) -> bool:
        return True

    def deep_refresh(self, request: Any, *, timeout_s: float | None = None) -> Any:
        self.calls += 1
        return self.suggestions

    def explain_state(self, payload: Mapping[str, Any], *, state_key: str = "") -> Any:
        return None

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": True}


def _reinterpreted(runtime: Runtime) -> str:
    """Drive one grounded reinterpretation through the real refresh path.

    Returns the identifier of the event that was reinterpreted.
    """
    first = runtime.process_user_message(content=AMBIGUOUS, timestamp=BASE_TIME)
    event_id = first.event.event_id
    runtime.semantic_provider = _StubProvider(
        DeepRefreshSuggestions(
            degraded=False,
            reinterpretations=[
                {"content": REINTERPRETATION, "realized_text": REALIZED, "sources": [event_id]}
            ],
        )
    )
    outcome = runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=5), force=True)
    assert outcome.applied.get("reinterpretation") == 1, (
        f"the fixture did not reach the reinterpretation handler: {outcome.to_dict()}"
    )
    return event_id


def test_a_reinterpretation_appends_a_reappraisal_event() -> None:
    """The log is the history of record; the fact has to be in it."""
    runtime = _runtime()
    try:
        target_event_id = _reinterpreted(runtime)

        events = runtime.events.read(
            EventQuery(event_types=[EventType.REAPPRAISAL.value], limit=10)
        )
        assert events, "a reinterpretation left no reappraisal event in the log"

        event = events[0]
        assert event.content == REINTERPRETATION
        assert event.metadata["target_event_id"] == target_event_id
        assert event.metadata["previous_interpretation"] is None, "the first version supersedes nothing"
        assert event.source_event_ids == [target_event_id]
    finally:
        runtime.close()


def test_the_log_entry_and_the_projection_row_are_the_same_fact() -> None:
    """Two records of one event must not disagree, and must name each other."""
    runtime = _runtime()
    try:
        _reinterpreted(runtime)

        rows = runtime.projections.interpretations.list_reappraisals(limit=5)
        assert len(rows) == 1, f"expected exactly one reappraisal row, got {len(rows)}"
        row = rows[0]

        event = runtime.events.read(
            EventQuery(event_types=[EventType.REAPPRAISAL.value], limit=1)
        )[0]

        assert row["new_interpretation"] == event.content
        assert row["delta_summary"] == REALIZED
        assert event.metadata["reappraisal_id"] == row["reappraisal_id"], (
            "the log entry must name the projection row it mirrors"
        )
        assert event.metadata["interpretation_id"], "the superseding version must be named"
    finally:
        runtime.close()


def test_a_reappraisal_identifier_is_not_a_memory_identifier() -> None:
    """``new_id('memory')`` for a reappraisal made it look like a memory.

    Every Runtime record shares one ``<prefix>_<hex>`` shape and they are not
    interchangeable: grounding, for one, decides from the prefix whether an identifier
    names a memory, a memory candidate, a candidate intent or an event.
    """
    runtime = _runtime()
    try:
        _reinterpreted(runtime)
        row = runtime.projections.interpretations.list_reappraisals(limit=1)[0]
        identifier = row["reappraisal_id"]

        assert identifier.startswith("rap_"), f"unexpected prefix: {identifier}"
        assert not identifier.startswith("mem_"), "a reappraisal is not a memory"
        assert is_event_identifier(identifier) is False, "it is not a raw event either"

        event = runtime.events.read(
            EventQuery(event_types=[EventType.REAPPRAISAL.value], limit=1)
        )[0]
        assert is_event_identifier(event.event_id) is True
    finally:
        runtime.close()


def test_the_backlog_is_drained_and_the_event_remains_afterwards() -> None:
    """The event is history, not a queue entry: settling the event must not erase it."""
    runtime = _runtime()
    try:
        _reinterpreted(runtime)
        assert runtime.projections.semantics.unresolved_count() == 0

        for _ in range(3):
            runtime.lazy_tick(BASE_TIME + timedelta(hours=1))
        events = runtime.events.read(
            EventQuery(event_types=[EventType.REAPPRAISAL.value], limit=10)
        )
        assert len(events) == 1, "the log must keep exactly one reappraisal, not one per tick"
    finally:
        runtime.close()
