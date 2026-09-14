"""Context bridge tests: strict deadline, caching, and safe degradation."""

from __future__ import annotations

import unittest

from companion_runtime.bridge import ContextBridge, CONTEXT_TAG
from companion_runtime.protocol import (
    TRIGGER_LLM_REQUEST,
    ContextRequest,
    ContextSnapshot,
)
from companion_runtime.settings import Settings
from tests.fakes import FakeClock, FakeTransport, RecordingLog, wait_until


def _request(session: str = "webchat:FriendMessage:u1") -> ContextRequest:
    return ContextRequest(
        adapter_id="default",
        session=session,
        trigger=TRIGGER_LLM_REQUEST,
        platform="webchat",
    )


class ContextBridgeTests(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, transport: FakeTransport, clock: FakeClock | None = None, **overrides):
        options = {
            "context_timeout_ms": 100,
            "context_cache_ttl_ms": 30000,
            "inject_max_chars": 2000,
            "context_prefetch": True,
        }
        options.update(overrides)
        settings = Settings.from_mapping(options)
        return ContextBridge(
            transport=transport,
            settings=settings,
            clock=clock or FakeClock(),
            log=RecordingLog(),
        )

    async def test_wraps_runtime_text_in_a_labelled_block(self) -> None:
        # Patch v0.2 renamed the psychological section: the injected text now
        # states the character's long-term state *before* the turn (background),
        # not how the current message should feel.
        section = "【进入本轮前的长期状态（背景）】"
        transport = FakeTransport(
            snapshot=ContextSnapshot(text=f"{section}\n平静", version="7"),
        )
        bridge = self._bridge(transport)
        text = await bridge.text_for_llm_request(_request())
        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn(f"<{CONTEXT_TAG} version=\"7\">", text)
        self.assertTrue(text.endswith(f"</{CONTEXT_TAG}>"))
        self.assertIn(section, text)
        self.assertEqual(transport.context_calls, 1)

    async def test_second_request_is_served_from_cache(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="ctx"))
        bridge = self._bridge(transport)
        await bridge.text_for_llm_request(_request())
        await bridge.text_for_llm_request(_request())
        self.assertEqual(transport.context_calls, 1)
        self.assertEqual(bridge.stats.cache_hits, 1)
        self.assertEqual(bridge.stats.fetches, 1)

    async def test_expired_cache_refetches(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="ctx"))
        clock = FakeClock()
        bridge = self._bridge(transport, clock, context_cache_ttl_ms=1000)
        await bridge.text_for_llm_request(_request())
        clock.advance(2.0)
        await bridge.text_for_llm_request(_request())
        self.assertEqual(transport.context_calls, 2)

    async def test_slow_fetch_is_abandoned_within_the_deadline(self) -> None:
        transport = FakeTransport(
            snapshot=ContextSnapshot(text="too late"),
            context_delay_s=0.5,
        )
        bridge = self._bridge(transport, context_timeout_ms=50)
        text = await bridge.text_for_llm_request(_request())
        self.assertIsNone(text)
        self.assertEqual(bridge.stats.timeouts, 1)
        self.assertEqual(bridge.stats.empty, 1)

    async def test_transport_error_degrades_to_none(self) -> None:
        transport = FakeTransport(context_error=RuntimeError("connection refused"))
        bridge = self._bridge(transport)
        text = await bridge.text_for_llm_request(_request())
        self.assertIsNone(text)
        self.assertEqual(bridge.stats.errors, 1)

    async def test_stale_cache_is_used_when_the_refetch_fails(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="stale but useful"))
        clock = FakeClock()
        bridge = self._bridge(transport, clock, context_cache_ttl_ms=1000)
        await bridge.text_for_llm_request(_request())
        clock.advance(2.0)
        transport.context_error = RuntimeError("runtime restarting")
        text = await bridge.text_for_llm_request(_request())
        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("stale but useful", text)
        self.assertEqual(bridge.stats.stale_fallbacks, 1)

    async def test_empty_snapshot_falls_back_to_stale_cache(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="first"))
        clock = FakeClock()
        bridge = self._bridge(transport, clock, context_cache_ttl_ms=1000)
        await bridge.text_for_llm_request(_request())
        clock.advance(2.0)
        transport.snapshot = ContextSnapshot()
        text = await bridge.text_for_llm_request(_request())
        assert text is not None
        self.assertIn("first", text)

    async def test_injection_is_truncated_to_the_configured_limit(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="x" * 500))
        bridge = self._bridge(transport, inject_max_chars=100)
        text = await bridge.text_for_llm_request(_request())
        assert text is not None
        self.assertLess(len(text), 200)
        self.assertIn("…", text)

    async def test_limit_of_zero_keeps_everything(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="x" * 500))
        bridge = self._bridge(transport, inject_max_chars=0)
        text = await bridge.text_for_llm_request(_request())
        assert text is not None
        self.assertIn("x" * 500, text)

    async def test_version_attribute_is_sanitized(self) -> None:
        transport = FakeTransport(
            snapshot=ContextSnapshot(text="ctx", version='1"><script>'),
        )
        bridge = self._bridge(transport)
        text = await bridge.text_for_llm_request(_request())
        assert text is not None
        self.assertNotIn("<script>", text)
        self.assertIn('version="1script"', text)

    async def test_prefetch_warms_the_cache_without_blocking(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="warm"))
        bridge = self._bridge(transport)
        self.assertTrue(bridge.prefetch(_request()))
        self.assertTrue(await wait_until(lambda: bridge.stats.prefetches == 1))
        text = await bridge.text_for_llm_request(_request())
        self.assertIsNotNone(text)
        self.assertEqual(transport.context_calls, 1)
        self.assertEqual(bridge.stats.cache_hits, 1)

    async def test_prefetch_is_skipped_when_disabled(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="warm"))
        bridge = self._bridge(transport, context_prefetch=False)
        self.assertFalse(bridge.prefetch(_request()))

    async def test_cached_text_requires_a_fresh_entry(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="fresh"))
        clock = FakeClock()
        bridge = self._bridge(transport, clock, context_cache_ttl_ms=1000)
        self.assertIsNone(bridge.cached_text(_request().session))
        await bridge.text_for_llm_request(_request())
        self.assertIsNotNone(bridge.cached_text(_request().session))
        clock.advance(5.0)
        self.assertIsNone(bridge.cached_text(_request().session))

    async def test_invalidate_clears_the_cache(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="ctx"))
        bridge = self._bridge(transport)
        await bridge.text_for_llm_request(_request())
        bridge.invalidate(_request().session)
        await bridge.text_for_llm_request(_request())
        self.assertEqual(transport.context_calls, 2)

    async def test_cache_is_bounded_by_session_count(self) -> None:
        transport = FakeTransport(snapshot=ContextSnapshot(text="ctx"))
        bridge = self._bridge(transport, context_cache_max_sessions=2)
        for index in range(4):
            await bridge.text_for_llm_request(_request(f"session-{index}"))
        self.assertIsNone(bridge.cached_text("session-0"))
        self.assertIsNone(bridge.cached_text("session-1"))
        self.assertIsNotNone(bridge.cached_text("session-3"))

    async def test_aclose_cancels_inflight_prefetches(self) -> None:
        transport = FakeTransport(
            snapshot=ContextSnapshot(text="slow"),
            context_delay_s=0.5,
        )
        bridge = self._bridge(transport)
        bridge.prefetch(_request())
        await bridge.aclose()
        self.assertEqual(bridge._inflight, set())

if __name__ == "__main__":
    unittest.main()
