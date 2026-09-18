"""Tests for the deep cognition refresh channel (patch v0.2, sections 18-21).

The refresh is the only automated path by which generated text can change
long-term state, so these tests concentrate on the gates rather than the happy
path: an ungrounded suggestion must die, a bad bundle must not void a good one,
and a refresh must never be reachable from the ingest path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Mapping

import pytest

from companion_runtime import deep_refresh as dr
from companion_runtime.providers import DeepRefreshSuggestions
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME, build_config


def _runtime(**overrides: Any) -> Runtime:
    """Build an in-memory Runtime for refresh tests."""
    config = build_config()
    config.semantic.deep_refresh_min_interval_seconds = 0.0
    config.semantic.unresolved_backlog_threshold = 2
    for key, value in overrides.items():
        setattr(config.semantic, key, value)
    return Runtime(config=config)


class _StubProvider:
    """Provider double returning a fixed bundle, with knobs for failure modes."""

    name = "stub"

    def __init__(self, suggestions: Any = None, *, available: bool = True, raises: bool = False):
        self.suggestions = suggestions
        self._available = available
        self.raises = raises
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def deep_refresh(self, request: Any, *, timeout_s: float | None = None) -> Any:
        self.calls += 1
        if self.raises:
            raise RuntimeError("provider exploded")
        return self.suggestions

    def explain_state(self, payload: Mapping[str, Any], *, state_key: str = "") -> Any:
        return None

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": self._available}


class TestTriggerPriority:
    """Patch section 21 defines an order, and only the top match is reported."""

    def test_nothing_matching_means_no_refresh(self) -> None:
        trigger = dr.evaluate_triggers(unresolved_count=0)
        assert trigger.should_refresh is False
        assert trigger.reason == "not_needed"

    @pytest.mark.parametrize(
        ("kwargs", "reason", "priority"),
        [
            ({"unresolved_count": 99}, "unresolved_backlog", 1),
            ({"major_event": True}, "major_event", 2),
            ({"matter_due": True}, "matter_due", 3),
            ({"candidate_pool_size": 0}, "candidate_pool_empty", 4),
            (
                {"wants_proactive": True, "proactive_grounded": False},
                "proactive_without_grounding",
                5,
            ),
            ({"history_suspect": True}, "history_may_be_wrong", 6),
            ({"user_evidence_overturns": True}, "user_evidence_overturns", 7),
            ({"hours_since_last_refresh": 24.0}, "idle_refresh", 8),
        ],
    )
    def test_each_rule_fires_with_its_documented_priority(
        self, kwargs: dict[str, Any], reason: str, priority: int
    ) -> None:
        trigger = dr.evaluate_triggers(**kwargs)
        assert trigger.should_refresh is True
        assert trigger.reason == reason
        assert trigger.priority == priority

    def test_the_most_urgent_match_wins(self) -> None:
        trigger = dr.evaluate_triggers(
            unresolved_count=99,
            major_event=True,
            matter_due=True,
            hours_since_last_refresh=48.0,
        )
        assert trigger.reason == "unresolved_backlog"
        assert trigger.priority == 1

    def test_a_grounded_proactive_intent_is_not_a_trigger(self) -> None:
        trigger = dr.evaluate_triggers(wants_proactive=True, proactive_grounded=True)
        assert trigger.should_refresh is False

    def test_threshold_comes_from_config(self) -> None:
        class Cfg:
            unresolved_backlog_threshold = 5
            deep_refresh_idle_hours = 12.0
            deep_refresh_min_interval_seconds = 3600.0

        assert dr.evaluate_triggers(unresolved_count=4, config=Cfg()).should_refresh is False
        assert dr.evaluate_triggers(unresolved_count=5, config=Cfg()).should_refresh is True

    def test_min_interval_beats_every_other_reason(self) -> None:
        """Cost control outranks urgency: no back-to-back refreshes."""
        class Cfg:
            unresolved_backlog_threshold = 1
            deep_refresh_idle_hours = 1.0
            deep_refresh_min_interval_seconds = 3600.0

        trigger = dr.evaluate_triggers(unresolved_count=99, major_event=True, hours_since_last_refresh=0.5, config=Cfg())
        assert trigger.should_refresh is False
        assert trigger.reason == "min_interval_not_elapsed"

    def test_priority_table_matches_the_patch_order(self) -> None:
        assert dr.TRIGGER_PRIORITY[0] == "unresolved_backlog"
        assert dr.TRIGGER_PRIORITY[-1] == "idle_refresh"
        assert len(set(dr.TRIGGER_PRIORITY)) == len(dr.TRIGGER_PRIORITY)


class TestGrounding:
    """Grounding is what makes it safe to let generated text touch state."""

    def _suggestions(self, **fields: Any) -> DeepRefreshSuggestions:
        return DeepRefreshSuggestions(provider="stub", degraded=False, **fields)

    def test_a_grounded_memory_survives(self) -> None:
        operations, violations = dr.ground_suggestions(
            self._suggestions(memory_suggestions=[{"summary": "他喜欢下雨天", "sources": ["evt_1"]}]),
            resolvable=lambda identifier: identifier == "evt_1",
        )
        assert violations == []
        assert [item.kind for item in operations] == ["memory"]
        assert operations[0].sources == ["evt_1"]

    def test_an_invented_source_is_rejected(self) -> None:
        operations, violations = dr.ground_suggestions(
            self._suggestions(memory_suggestions=[{"summary": "他养过一只猫", "sources": ["evt_ghost"]}]),
            resolvable=lambda identifier: False,
        )
        assert operations == []
        assert violations == [
            {"kind": "memory", "reason": "ungrounded_sources", "sources": ["evt_ghost"]}
        ]

    def test_a_matter_grounded_only_in_other_matters_is_a_restatement(self) -> None:
        """Echoing the open matters back as "new" ones is refused.

        A refresh is handed the open matters as input, so a model can re-emit them and
        cite their ids as sources; both sources resolve, so plain grounding lets it
        through and the list grows. Measured on a real beta: one person's open matters
        went from 7 to 13 in a night, every addition a rewrite of an existing one that
        ``_same_subject`` could not catch. A matter must cite at least one non-matter
        source.
        """
        operations, violations = dr.ground_suggestions(
            self._suggestions(
                unfinished_matter_suggestions=[
                    {"title": "复述旧事", "sources": ["unf_1"]},
                    {"title": "复述旧事，换个说法", "sources": ["unf_1", "unf_2"]},
                    {"title": "有事件支撑", "sources": ["unf_1", "evt_9"]},
                ]
            ),
            resolvable=lambda identifier: True,
            is_matter=lambda identifier: identifier in {"unf_1", "unf_2"},
        )
        assert [item.payload.get("title") for item in operations] == ["有事件支撑"]
        assert violations == [
            {"kind": "unfinished_matter", "reason": "matter_restatement", "sources": ["unf_1"]},
            {
                "kind": "unfinished_matter",
                "reason": "matter_restatement",
                "sources": ["unf_1", "unf_2"],
            },
        ]

    def test_the_restatement_guard_only_applies_when_a_matter_resolver_is_given(self) -> None:
        """Callers without the resolver keep the old behaviour (grounding only)."""
        operations, violations = dr.ground_suggestions(
            self._suggestions(unfinished_matter_suggestions=[{"title": "复述", "sources": ["unf_1"]}]),
            resolvable=lambda identifier: True,
        )
        assert violations == []
        assert [item.kind for item in operations] == ["unfinished_matter"]

    def test_missing_sources_are_rejected_for_kinds_that_require_them(self) -> None:
        operations, violations = dr.ground_suggestions(
            self._suggestions(unfinished_matter_suggestions=[{"title": "等他回消息"}]),
            resolvable=lambda identifier: True,
        )
        assert operations == []
        assert violations[0]["reason"] == "missing_sources"

    def test_the_interpretation_cache_needs_no_source(self) -> None:
        """It summarises the whole state rather than one event."""
        suggestions = self._suggestions(
            psychological_interpretation={"experience": "最近偏沉", "focus": "在等一个回应"}
        )
        operations, violations = dr.ground_suggestions(
            suggestions, resolvable=lambda identifier: False
        )
        assert violations == []
        assert [item.kind for item in operations] == ["psychological_interpretation"]

    def test_malformed_items_are_reported_not_crashed_on(self) -> None:
        operations, violations = dr.ground_suggestions(
            self._suggestions(reinterpretations=["not a mapping", {"sources": ["evt_1"]}]),
            resolvable=lambda identifier: True,
        )
        assert operations == []
        assert {item["reason"] for item in violations} == {"not_a_mapping", "empty_payload"}

    def test_operations_are_capped(self) -> None:
        many = [{"summary": f"m{index}", "sources": ["evt_1"]} for index in range(20)]
        operations, violations = dr.ground_suggestions(
            self._suggestions(memory_suggestions=many),
            resolvable=lambda identifier: True,
            max_candidate_operations=3,
        )
        assert len(operations) == 3
        assert violations[-1]["reason"] == "exceeded_max_operations"

    def test_unknown_fields_are_ignored(self) -> None:
        operations, _violations = dr.ground_suggestions(
            {"totally_unknown_field": [{"summary": "x", "sources": ["evt_1"]}]},
            resolvable=lambda identifier: True,
        )
        assert operations == []

    def test_every_operation_kind_is_reachable(self) -> None:
        payload = {
            "reinterpretations": [{"content": "其实是失望", "sources": ["evt_1"]}],
            "candidate_intent_operations": [{"intent": "问一句", "sources": ["evt_1"]}],
            "memory_suggestions": [{"summary": "记得这件事", "sources": ["evt_1"]}],
            "unfinished_matter_suggestions": [{"title": "等他解释", "sources": ["evt_1"]}],
            "user_model_evidence_suggestions": [{"statement": "在意回应速度", "sources": ["evt_1"]}],
            "psychological_interpretation": {"experience": "有点沉"},
        }
        operations, violations = dr.ground_suggestions(payload, resolvable=lambda identifier: True)
        assert violations == []
        assert {item.kind for item in operations} == set(dr.OPERATION_KINDS)


class TestRequestAssembly:
    """The bundle is read-only and contains what patch section 19 lists."""

    def test_request_carries_the_documented_sections(self) -> None:
        runtime = _runtime()
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            request = dr.build_request(runtime=runtime, now=BASE_TIME + timedelta(minutes=1))
            payload = request.to_dict()
            assert payload["unresolved_events"]
            assert "mood" in payload and "valence" in payload["mood"]
            for key in (
                "situation",
                "active_emotions",
                "memories",
                "unfinished",
                "candidates",
                "key_quotes",
            ):
                assert key in payload
        finally:
            runtime.close()

    def test_building_a_request_does_not_write(self) -> None:
        runtime = _runtime()
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            before = runtime.version()
            dr.build_request(runtime=runtime, now=BASE_TIME + timedelta(minutes=1))
            assert runtime.version() == before
        finally:
            runtime.close()

    def test_key_quotes_are_grounded_in_real_events(self) -> None:
        runtime = _runtime()
        try:
            outcome = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            request = dr.build_request(runtime=runtime, now=BASE_TIME, key_quote_limit=3)
            assert request.key_quotes
            assert request.key_quotes[0]["event_id"] == outcome.event.event_id
            assert request.key_quotes[0]["content"] == "算了，也没什么。"
        finally:
            runtime.close()


class TestRefreshOrchestration:
    """The Runtime-level pipeline, including every way it can decline."""

    def test_disabled_config_skips(self) -> None:
        runtime = _runtime(deep_refresh_enabled=False)
        try:
            outcome = runtime.deep_refresh(now=BASE_TIME, force=True)
            assert outcome.ran is False
            assert outcome.reason == "disabled"
        finally:
            runtime.close()

    def test_unavailable_provider_skips(self) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _StubProvider(available=False)
        try:
            assert runtime.deep_refresh(now=BASE_TIME, force=True).reason == "provider_unavailable"
        finally:
            runtime.close()

    def test_no_trigger_means_no_call(self) -> None:
        runtime = _runtime()
        provider = _StubProvider(DeepRefreshSuggestions())
        runtime.semantic_provider = provider
        try:
            outcome = runtime.deep_refresh(now=BASE_TIME)
            assert outcome.ran is False
            assert outcome.reason == "not_needed"
            assert provider.calls == 0, "an unneeded refresh must not spend a request"
        finally:
            runtime.close()

    def test_a_raising_provider_is_not_fatal(self) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _StubProvider(raises=True)
        try:
            outcome = runtime.deep_refresh(now=BASE_TIME, force=True)
            assert outcome.ran is False
            assert outcome.reason == "provider_error"
        finally:
            runtime.close()

    def test_empty_suggestions_are_reported(self) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _StubProvider(DeepRefreshSuggestions(degraded=False))
        try:
            assert runtime.deep_refresh(now=BASE_TIME, force=True).reason == "empty_suggestions"
        finally:
            runtime.close()

    def test_all_ungrounded_suggestions_apply_nothing(self) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _StubProvider(
            DeepRefreshSuggestions(
                degraded=False,
                memory_suggestions=[{"summary": "编造的事", "sources": ["evt_ghost"]}],
            )
        )
        try:
            outcome = runtime.deep_refresh(now=BASE_TIME, force=True)
            assert outcome.ran is False
            assert outcome.reason == "all_suggestions_ungrounded"
            assert outcome.violations
            assert runtime.projections.memory.pending_candidates() == []
        finally:
            runtime.close()

    def test_a_grounded_reinterpretation_settles_the_backlog(self) -> None:
        runtime = _runtime()
        try:
            first = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            assert runtime.projections.semantics.unresolved_count() == 1
            runtime.semantic_provider = _StubProvider(
                DeepRefreshSuggestions(
                    degraded=False,
                    reinterpretations=[
                        {
                            "content": "当时他可能是失望的，我没有接住。",
                            "realized_text": "现在意识到，那句算了后面是失望。",
                            "sources": [first.event.event_id],
                        }
                    ],
                )
            )
            outcome = runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert outcome.ran is True
            assert outcome.reason == "applied"
            assert outcome.applied.get("reinterpretation") == 1
            assert outcome.settled_events == 1
            assert runtime.projections.semantics.unresolved_count() == 0
            assert runtime.projections.interpretations.list_reappraisals(limit=5)
        finally:
            runtime.close()

    def test_history_is_not_rewritten_by_a_reinterpretation(self) -> None:
        """Invariant 7 still holds on the refresh path."""
        runtime = _runtime()
        try:
            first = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            original = runtime.events.get(first.event.event_id)
            runtime.semantic_provider = _StubProvider(
                DeepRefreshSuggestions(
                    degraded=False,
                    reinterpretations=[
                        {"content": "重新理解：那是失望。", "sources": [first.event.event_id]}
                    ],
                )
            )
            runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=5), force=True)
            after = runtime.events.get(first.event.event_id)
            assert after is not None and original is not None
            assert after.content == original.content
            assert after.to_dict() == original.to_dict()
        finally:
            runtime.close()

    def test_the_interpretation_cache_is_updated_and_reused(self) -> None:
        runtime = _runtime()
        stored_at = BASE_TIME + timedelta(minutes=5)
        try:
            event = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            runtime.semantic_provider = _StubProvider(
                DeepRefreshSuggestions(
                    degraded=False,
                    psychological_interpretation={
                        "experience": "最近底色偏沉，还有没消化的事。",
                        "focus": "在意回应是不是真的接住了。",
                        "conflict": "想靠近又习惯性收着。",
                        "impulse": "总体想确认，但不急。",
                        "inhibition": "长期偏克制。",
                        "expression": "底色收着。",
                    },
                    reinterpretations=[
                        {"content": "那是失望。", "sources": [event.event.event_id]}
                    ],
                )
            )
            runtime.deep_refresh(now=stored_at, force=True)
            active = runtime.projections.emotion.list_active()
            from companion_runtime.emotion import EmotionExplainer

            key = EmotionExplainer.cache_key(runtime.state(), active)
            # The entry is stamped with the refresh's own clock, so it is read back
            # with that same ``now``. Querying at an earlier instant would make the
            # entry look like it came from the future - which the cache refuses, as
            # ``test_a_cache_entry_stamped_after_the_reference_time_is_rejected``
            # pins separately.
            cached = runtime.projections.emotion.cached_explanation(key, stored_at, 3600)
            assert cached is not None
            assert "偏沉" in cached["experience"]
            # Reading it again later, still inside the TTL, reuses the same entry.
            later = runtime.projections.emotion.cached_explanation(
                key, stored_at + timedelta(minutes=30), 3600
            )
            assert later is not None and later["experience"] == cached["experience"]
        finally:
            runtime.close()

    def test_a_cache_entry_stamped_after_the_reference_time_is_rejected(self) -> None:
        """A future-dated explanation must never be served as permanently fresh.

        The freshness test is ``age > ttl``, and a future stamp makes ``age``
        negative - smaller than any TTL. Without an explicit non-negative-age check,
        a clock step, a replayed timeline or a hand-edited row would be treated as
        valid forever. This is the projection-level contract; the refresh path above
        depends on it when it reads back an entry it stored at its own clock time.
        """
        runtime = _runtime()
        stored_at = BASE_TIME + timedelta(minutes=5)
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            active = runtime.projections.emotion.list_active()
            from companion_runtime.emotion import EmotionExplainer

            key = EmotionExplainer.cache_key(runtime.state(), active)
            with runtime.db.transaction() as conn:
                runtime.projections.emotion.store_explanation(
                    conn,
                    cache_key=key,
                    payload={"experience": "写在未来的一条解释。"},
                    source="deep_refresh",
                    now=stored_at,
                )
            cache = runtime.projections.emotion
            # Before its own timestamp: rejected rather than served.
            assert cache.cached_explanation(key, BASE_TIME, 3600) is None
            assert cache.cached_explanation(key, stored_at - timedelta(seconds=1), 3600) is None
            # At and after it, inside the TTL: served.
            assert cache.cached_explanation(key, stored_at, 3600) is not None
            assert (
                cache.cached_explanation(key, stored_at + timedelta(minutes=1), 3600)
                is not None
            )
            # Past the TTL: expired as usual, so the guard did not disable expiry.
            assert (
                cache.cached_explanation(key, stored_at + timedelta(hours=2), 3600) is None
            )
        finally:
            runtime.close()

    def test_only_referenced_events_leave_the_backlog(self) -> None:
        """Regression: a refresh must not close events it never mentioned.

        An early version settled every event handed to the proposal, so one
        reinterpretation silently marked the entire backlog as understood. The
        live end-to-end check caught it; this test keeps it caught.
        """
        runtime = _runtime()
        try:
            first = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            unrelated = runtime.process_user_message(
                content="随便吧，都行。", timestamp=BASE_TIME + timedelta(minutes=1)
            )
            assert runtime.projections.semantics.unresolved_count() == 2
            runtime.semantic_provider = _StubProvider(
                DeepRefreshSuggestions(
                    degraded=False,
                    reinterpretations=[
                        {"content": "那是失望。", "sources": [first.event.event_id]}
                    ],
                )
            )
            runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=5), force=True)
            remaining = {
                item["event_id"] for item in runtime.projections.semantics.list_unresolved()
            }
            assert first.event.event_id not in remaining
            assert unrelated.event.event_id in remaining, "an unrelated event was wrongly closed"
            assert runtime.projections.semantics.unresolved_count() == 1
        finally:
            runtime.close()

    def test_a_partially_bad_bundle_still_applies_the_good_part(self) -> None:
        """One malformed operation must not void a useful refresh."""
        runtime = _runtime()
        try:
            event = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            runtime.semantic_provider = _StubProvider(
                DeepRefreshSuggestions(
                    degraded=False,
                    reinterpretations=[{"content": "那是失望。", "sources": [event.event.event_id]}],
                    memory_suggestions=[{"summary": "无源之事"}],
                )
            )
            outcome = runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert outcome.ran is True
            assert outcome.applied.get("reinterpretation") == 1
            assert any(item["reason"] == "missing_sources" for item in outcome.violations)
        finally:
            runtime.close()

    def test_force_overrides_the_trigger_but_not_availability(self) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _StubProvider(available=False)
        try:
            assert runtime.deep_refresh(now=BASE_TIME, force=True).ran is False
        finally:
            runtime.close()

    def test_outcome_is_json_serialisable(self) -> None:
        runtime = _runtime()
        try:
            outcome = runtime.deep_refresh(now=BASE_TIME, force=True)
            json.dumps(outcome.to_dict())
        finally:
            runtime.close()


class TestRefreshIsOffTheIngestPath:
    """The refresh must never be reachable while handling a user message."""

    def test_ingest_does_not_trigger_a_refresh(self) -> None:
        runtime = _runtime()
        provider = _StubProvider(DeepRefreshSuggestions(degraded=False))
        runtime.semantic_provider = provider
        try:
            for index in range(6):
                runtime.process_user_message(
                    content="算了，也没什么。", timestamp=BASE_TIME + timedelta(minutes=index)
                )
            assert runtime.projections.semantics.unresolved_count() >= 2
            assert provider.calls == 0, "ingest must never spend a refresh"
        finally:
            runtime.close()

    def test_the_backlog_is_visible_on_health(self) -> None:
        from fastapi.testclient import TestClient

        from companion_runtime.api import create_app

        runtime = _runtime()
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            client = TestClient(create_app(runtime, runtime.config))
            body = client.get("/health").json()
            assert body["semantics"]["unresolved"] == 1
            assert "semantic_provider" in body
        finally:
            runtime.close()
