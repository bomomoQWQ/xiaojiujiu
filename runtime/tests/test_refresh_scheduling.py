"""Regression tests for the two gaps the v0.2 mapping audit exposed.

Both were "the code exists but nothing calls it" defects, which unit tests for the
component itself cannot catch:

* the deep refresh had no scheduler, so ``unresolved`` accumulated forever and
  "later I understood" never actually happened on its own;
* the explainer was always constructed without a provider, so a configured
  ``RemoteAPIProvider`` could never fill the interpretation cache.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

import pytest

from companion_runtime.providers import DeepRefreshSuggestions
from companion_runtime.runtime import Runtime

from conftest import BASE_TIME, build_config


class _Provider:
    """Provider double that always answers with one grounded reinterpretation."""

    name = "audit_stub"

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.refresh_calls = 0
        self.explain_calls = 0

    def available(self) -> bool:
        return self._available

    def deep_refresh(self, request: Any, *, timeout_s: float | None = None) -> Any:
        self.refresh_calls += 1
        sources = [
            str(item["event_id"]) for item in request.unresolved_events if item.get("event_id")
        ][:1]
        if not sources:
            return DeepRefreshSuggestions(provider=self.name, degraded=False)
        return DeepRefreshSuggestions(
            provider=self.name,
            degraded=False,
            reinterpretations=[{"content": "后来重新理解了这件事。", "sources": sources}],
        )

    def explain_state(self, payload: Mapping[str, Any], *, state_key: str = "") -> Any:
        self.explain_calls += 1
        return {
            "experience": "来自 provider 的长期底色。",
            "focus": "在意回应。",
            "conflict": "想靠近又收着。",
            "impulse": "总体想确认。",
            "inhibition": "长期克制。",
            "expression": "底色收着。",
        }

    def health(self) -> dict[str, Any]:
        return {"provider": self.name, "available": self._available}


class _ExplodingProvider(_Provider):
    """Available, but the remote call itself fails.

    ``_Provider(available=False)`` never reaches the remote call at all - the
    Runtime rejects it up front - so it cannot make a refresh *fail*.
    """

    def deep_refresh(self, request: Any, *, timeout_s: float | None = None) -> Any:
        self.refresh_calls += 1
        raise RuntimeError("remote model is down")


def _runtime(**semantic_overrides: Any) -> Runtime:
    """Build an in-memory Runtime on the simulated timeline.

    The creation epoch is pinned to :data:`BASE_TIME`; otherwise the Runtime's
    clock starts at wall-clock time, which is *after* every timestamp these tests
    use and would make elapsed-time maths meaningless.
    """
    config = build_config()
    config.semantic.unresolved_backlog_threshold = 1
    config.semantic.deep_refresh_min_interval_seconds = 0.0
    for key, value in semantic_overrides.items():
        setattr(config.semantic, key, value)
    return Runtime(config=config, created_at=BASE_TIME)


class TestRefreshRunsUnattended:
    """Unresolved events must eventually be revisited without an operator."""

    def test_the_endogenous_round_attempts_a_refresh(self) -> None:
        runtime = _runtime()
        provider = _Provider()
        runtime.semantic_provider = provider
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert outcome.deep_refresh, "the round must report the refresh attempt"
            assert outcome.deep_refresh["ran"] is True
            assert provider.refresh_calls == 1
        finally:
            runtime.close()

    def test_the_backlog_is_actually_drained_by_the_heartbeat(self) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _Provider()
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            assert runtime.projections.semantics.unresolved_count() == 1
            runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert runtime.projections.semantics.unresolved_count() == 0
        finally:
            runtime.close()

    def test_the_round_reports_a_declined_refresh_honestly(self) -> None:
        """No provider must read as "declined", never as "succeeded"."""
        runtime = _runtime()
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert outcome.deep_refresh["ran"] is False
            assert outcome.deep_refresh["reason"] == "provider_unavailable"
            # The backlog stays open, which is the honest outcome.
            assert runtime.projections.semantics.unresolved_count() == 1
        finally:
            runtime.close()

    def test_a_refresh_can_be_suppressed_per_round(self) -> None:
        runtime = _runtime()
        provider = _Provider()
        runtime.semantic_provider = provider
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            outcome = runtime.endogenous_round(
                now=BASE_TIME + timedelta(minutes=5), force=True, deep_refresh=False
            )
            assert outcome.deep_refresh == {}
            assert provider.refresh_calls == 0
        finally:
            runtime.close()

    def test_a_failing_refresh_does_not_break_the_round(self) -> None:
        """The proactive decision matters more than the refresh.

        An *unavailable* provider is rejected before it is ever called, so the old
        version of this test (``_Provider(available=False)`` plus ``"acted" in
        decision``) never made anything fail and asserted a key that every decision
        carries. Here the provider answers that it is available and then raises, so
        the round's fail-open path is the thing under test.
        """
        runtime = _runtime()
        provider = _ExplodingProvider()
        runtime.semantic_provider = provider
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert provider.refresh_calls == 1, "the provider must actually have been asked"
            assert outcome.deep_refresh["ran"] is False
            assert outcome.deep_refresh["reason"] == "provider_error"
            # The round still decided, and the backlog is kept rather than lost.
            assert "acted" in outcome.decision["outcome"]
            assert outcome.decision["outcome"]["reason"]
            assert runtime.projections.semantics.unresolved_count() == 1
        finally:
            runtime.close()

    def test_disabled_config_stops_the_heartbeat_refresh(self) -> None:
        runtime = _runtime(deep_refresh_enabled=False)
        provider = _Provider()
        runtime.semantic_provider = provider
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            outcome = runtime.endogenous_round(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert outcome.deep_refresh["reason"] == "disabled"
            assert provider.refresh_calls == 0
        finally:
            runtime.close()


class TestRefresIsNotSpentIdly:
    """A brand-new Runtime must not refresh to rediscover that nothing happened."""

    def test_a_fresh_runtime_does_not_refresh(self) -> None:
        runtime = _runtime()
        provider = _Provider()
        runtime.semantic_provider = provider
        try:
            outcome = runtime.deep_refresh(now=BASE_TIME)
            assert outcome.ran is False
            assert outcome.reason == "not_needed"
            assert provider.refresh_calls == 0
        finally:
            runtime.close()

    def test_an_empty_runtime_does_not_take_the_heartbeat_refresh(self) -> None:
        runtime = _runtime()
        provider = _Provider()
        runtime.semantic_provider = provider
        try:
            runtime.endogenous_round(now=BASE_TIME + timedelta(hours=48), force=True)
            assert provider.refresh_calls == 0
        finally:
            runtime.close()

    def test_the_signals_are_computed_not_required_from_the_caller(self) -> None:
        """Otherwise the default deployment would never fire a refresh at all."""
        runtime = _runtime()
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            signals = runtime._refresh_signals(now=BASE_TIME + timedelta(hours=30))
            assert "candidate_pool_size" in signals
            assert "matter_due" in signals
            assert signals["hours_since_last_refresh"] > 0
        finally:
            runtime.close()

    def test_a_due_matter_is_detected_without_a_caller(self) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _Provider()
        try:
            runtime.process_user_message(
                content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
            )
            matter = runtime.projections.unfinished.list_open()[0]
            due_at = matter.waiting_until + timedelta(hours=1)
            # The waiting -> due transition happens on the tick, which is exactly
            # the order the heartbeat uses.
            runtime.lazy_tick(due_at)
            signals = runtime._refresh_signals(now=due_at)
            assert signals["matter_due"] is True
        finally:
            runtime.close()

    def test_the_minimum_interval_is_respected_across_rounds(self) -> None:
        """A heartbeat loop must not spend on every tick."""
        runtime = _runtime(deep_refresh_min_interval_seconds=3600.0)
        provider = _Provider()
        runtime.semantic_provider = provider
        try:
            # Two deferred events: the provider understands one, so material
            # remains and the second round is genuinely paced rather than idle.
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            runtime.process_user_message(
                content="随便吧，都行。", timestamp=BASE_TIME + timedelta(minutes=1)
            )
            first = runtime.endogenous_round(
                now=BASE_TIME + timedelta(minutes=5), force=True
            )
            assert first.deep_refresh.get("ran") is True
            second = runtime.endogenous_round(
                now=BASE_TIME + timedelta(minutes=10), force=True
            )
            assert second.deep_refresh.get("ran") is False
            assert second.deep_refresh.get("reason") == "min_interval_not_elapsed"
            assert provider.refresh_calls == 1
        finally:
            runtime.close()


class TestExplainerUsesTheProvider:
    """A configured provider must actually be able to fill the interpretation cache."""

    def test_runtime_explanation_uses_an_available_provider(self) -> None:
        from companion_runtime import context as context_module

        runtime = _runtime()
        provider = _Provider()
        runtime.semantic_provider = provider
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            assert provider.explain_calls == 1
            assert "provider" in bundle.psychological["experience"]
        finally:
            runtime.close()

    def test_a_disabled_provider_is_not_consulted(self) -> None:
        from companion_runtime import context as context_module
        from companion_runtime.emotion import (
            TEMPLATES_NEGATIVE,
            TEMPLATES_NEUTRAL,
            TEMPLATES_POSITIVE,
        )

        runtime = _runtime()
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            # The deterministic template is the standard deployment.
            assert bundle.psychological["source"] == "template"
            assert bundle.psychological["experience"] in (
                *TEMPLATES_POSITIVE,
                *TEMPLATES_NEGATIVE,
                *TEMPLATES_NEUTRAL,
            )
        finally:
            runtime.close()

    def test_an_unavailable_provider_is_not_consulted(self) -> None:
        from companion_runtime import context as context_module

        runtime = _runtime()
        provider = _Provider(available=False)
        runtime.semantic_provider = provider
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            assert provider.explain_calls == 0
            assert bundle.psychological["source"] == "template"
        finally:
            runtime.close()

    def test_a_broken_provider_still_yields_a_template(self) -> None:
        """Fail-open: an exception from the provider must not break the turn."""
        from companion_runtime import context as context_module

        class _Broken(_Provider):
            def explain_state(self, payload: Mapping[str, Any], *, state_key: str = "") -> Any:
                raise RuntimeError("provider exploded")

        runtime = _runtime()
        runtime.semantic_provider = _Broken()
        try:
            runtime.process_user_message(content="谢谢你！", timestamp=BASE_TIME)
            bundle = context_module.build(runtime=runtime, now=BASE_TIME)
            assert bundle.psychological["experience"]
        finally:
            runtime.close()


class TestDeepRefreshProtocolPolicy:
    """A reinterpretation of old events must not decay like a live appraisal."""

    @pytest.mark.parametrize("sensitivity", ["low"])
    def test_sensitivity_is_registered(self, sensitivity: str) -> None:
        from companion_runtime.protocol import TASK_SENSITIVITY, sensitivity_of
        from companion_runtime.typing import TaskKind

        assert TaskKind.DEEP_REFRESH.value in TASK_SENSITIVITY
        assert sensitivity_of(TaskKind.DEEP_REFRESH.value) == sensitivity

    def test_a_slightly_stale_refresh_is_applied_not_damped(self) -> None:
        """Otherwise 'I understood later' would be quietly erased by living on."""
        from companion_runtime.protocol import Proposal, classify
        from companion_runtime.typing import TaskKind

        proposal = Proposal(
            task_id="t1",
            task_type=TaskKind.DEEP_REFRESH.value,
            based_on_version=10,
            payload={},
            created_at=BASE_TIME,
        )
        decision = classify(proposal, current_version=11, source_events=[])
        assert decision.action == "apply"
