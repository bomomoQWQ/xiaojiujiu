"""Every configuration knob in SemanticConfig must actually do something.

The v0.2 mapping audit found three knobs that were declared, documented, and read
by nothing - a configuration that lies is worse than no configuration, because an
operator will tune it and see no effect. These tests pin each one to observable
behaviour so the same drift cannot recur silently.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from companion_runtime import context as context_module
from companion_runtime import deep_refresh as dr
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME, build_config


def _runtime(**semantic: Any) -> Runtime:
    """Build an in-memory Runtime on the simulated timeline."""
    config = build_config()
    for key, value in semantic.items():
        setattr(config.semantic, key, value)
    return Runtime(config=config, created_at=BASE_TIME)


class TestTemplateFallback:
    """``semantic.template_fallback`` decides whether prose is invented."""

    def test_enabled_by_default(self) -> None:
        runtime = _runtime()
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            assert bundle.psychological["experience"]
            assert bundle.psychological["source"] == "template"
        finally:
            runtime.close()

    def test_disabled_omits_the_psychological_section(self) -> None:
        runtime = _runtime(template_fallback=False)
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            prose = ("experience", "focus", "conflict", "impulse", "inhibition", "expression")
            assert not any(bundle.psychological.get(key) for key in prose)
            block = context_module.render_block(bundle)
            assert context_module.SECTION_PSYCH not in block
        finally:
            runtime.close()


class TestInterpretationMaxAge:
    """``semantic.interpretation_max_age_seconds`` paces the explanation cache."""

    def test_it_overrides_the_task_level_ttl(self) -> None:
        runtime = _runtime(interpretation_max_age_seconds=1.0)
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            active = runtime.projections.emotion.list_active()
            from companion_runtime.emotion import EmotionExplainer

            key = EmotionExplainer.cache_key(runtime.state(), active)
            with runtime.db.transaction() as conn:
                runtime.projections.emotion.store_explanation(
                    conn,
                    cache_key=key,
                    payload={"experience": "cached"},
                    source="template",
                    now=BASE_TIME,
                )
            # The default task TTL is 1800s; the semantic override is 1s.
            expired = runtime.projections.emotion.cached_explanation(
                key, BASE_TIME + timedelta(seconds=30), 1.0
            )
            still_valid = runtime.projections.emotion.cached_explanation(
                key, BASE_TIME + timedelta(seconds=30), 1800.0
            )
            assert expired is None
            assert still_valid is not None
        finally:
            runtime.close()

    def test_the_explainer_uses_the_configured_value(self) -> None:
        from companion_runtime.emotion import EmotionExplainer

        runtime = _runtime(interpretation_max_age_seconds=42.0)
        try:
            explainer = EmotionExplainer(runtime.projections.emotion, runtime.config)
            assert explainer._explanation_ttl_seconds() == 42.0
        finally:
            runtime.close()

    def test_it_falls_back_to_the_task_ttl_when_unset(self) -> None:
        from companion_runtime.emotion import EmotionExplainer

        runtime = _runtime(interpretation_max_age_seconds=0.0)
        try:
            explainer = EmotionExplainer(runtime.projections.emotion, runtime.config)
            assert explainer._explanation_ttl_seconds() == runtime.config.task.explain_cache_ttl_seconds
        finally:
            runtime.close()


class TestUnresolvedMaxAge:
    """``semantic.unresolved_max_age_hours`` bounds what a refresh is asked to read."""

    def test_fresh_events_are_included(self) -> None:
        runtime = _runtime(unresolved_max_age_hours=72.0)
        try:
            outcome = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            request = dr.build_request(runtime=runtime, now=BASE_TIME + timedelta(hours=2))
            assert [item["event_id"] for item in request.unresolved_events] == [
                outcome.event.event_id
            ]
        finally:
            runtime.close()

    def test_aged_events_drop_out_of_the_refresh_set(self) -> None:
        runtime = _runtime(unresolved_max_age_hours=1.0)
        try:
            outcome = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            request = dr.build_request(runtime=runtime, now=BASE_TIME + timedelta(hours=5))
            assert request.unresolved_events == []
            # ...but the evidence is never deleted, only deprioritised.
            assert runtime.projections.semantics.unresolved_count() == 1
            assert runtime.events.get(outcome.event.event_id) is not None
        finally:
            runtime.close()

    def test_a_bad_timestamp_never_drops_an_event(self) -> None:
        """A malformed field must not silently hide evidence."""
        runtime = _runtime(unresolved_max_age_hours=1.0)
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            with runtime.db.transaction() as conn:
                conn.execute("UPDATE event_semantics SET created_at = 'not-a-date'")
            request = dr.build_request(runtime=runtime, now=BASE_TIME + timedelta(days=30))
            assert len(request.unresolved_events) == 1
        finally:
            runtime.close()


class TestPriorityPreambleIsComplete:
    """Patch section 7 lists seven levels; the prompt must not compress them."""

    def test_all_seven_levels_are_named(self) -> None:
        preamble = context_module.PRIORITY_PREAMBLE
        for level in range(1, 8):
            assert f"{level}." in preamble, f"level {level} missing"
        for token in (
            "宿主角色设定",
            "当前用户原话",
            "当前确定事实",
            "显式边界",
            "持久心理状态",
            "心理解释缓存",
            "自然发挥",
        ):
            assert token in preamble, token

    def test_the_preamble_states_that_the_current_turn_wins(self) -> None:
        assert "以第 2 项为准" in context_module.PRIORITY_PREAMBLE
