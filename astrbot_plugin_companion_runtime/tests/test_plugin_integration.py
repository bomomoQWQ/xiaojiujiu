"""Integration tests for ``main.py`` against a stubbed AstrBot surface.

The stub under ``tests/stubs/astrbot`` mirrors the AstrBot 4.28 public API this
plugin imports, so these tests can verify the *wiring* of the adapter without a
real AstrBot checkout:

* observed messages become event reports,
* ``on_llm_request`` injects a temporary ``TextPart`` behind the strict deadline,
* the outbox loop renders with the session's current provider,
* a send happens only after the Runtime authorizes it,
* ``initialize`` / ``terminate`` leave no background tasks behind.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from companion_runtime.protocol import (
    ACTION_RENDER,
    ACTION_SEND,
    AuthorizeDecision,
    ContextSnapshot,
    LeasedAction,
)

from tests.fakes import FakeTransport, wait_until

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STUBS_DIR = Path(__file__).resolve().parent / "stubs"
PACKAGE_NAME = "astrbot_plugin_companion_runtime"

SESSION = "webchat:FriendMessage:user-1"


def _load_plugin_module() -> Any:
    """Import the plugin's ``main`` module with relative imports intact."""
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    if str(STUBS_DIR) not in sys.path:
        sys.path.insert(0, str(STUBS_DIR))
    if PACKAGE_NAME not in sys.modules:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PLUGIN_ROOT)]
        sys.modules[PACKAGE_NAME] = package
    return importlib.import_module(f"{PACKAGE_NAME}.main")


class StubMessageEvent:
    """Minimal stand-in for ``AstrMessageEvent`` with the fields we read."""

    def __init__(
        self,
        *,
        text: str = "hi",
        session: str = SESSION,
        wake: bool = True,
        result_text: str = "",
        message_type: str = "FriendMessage",
    ) -> None:
        self.unified_msg_origin = session
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id="msg-1")
        self.is_at_or_wake_command = wake
        self._result_text = result_text
        #: AstrBot's real enum values ("FriendMessage", "GroupMessage", ...), not
        #: the plain scope words the Runtime protocol uses.
        self._message_type = message_type

    def get_platform_name(self) -> str:
        return "webchat"

    def get_message_type(self) -> Any:
        return SimpleNamespace(value=self._message_type)

    def get_sender_id(self) -> str:
        return "user-1"

    def get_sender_name(self) -> str:
        return "User"

    def get_self_id(self) -> str:
        return "bot-1"

    def get_group_id(self) -> str:
        return ""

    def get_result(self) -> Any:
        if not self._result_text:
            return None
        from astrbot.api.event import MessageEventResult
        from astrbot.api.message_components import Plain

        return MessageEventResult([Plain(self._result_text)])

    def plain_result(self, text: str) -> Any:
        """Mirror ``AstrMessageEvent.plain_result``."""
        from astrbot.api.event import MessageEventResult
        from astrbot.api.message_components import Plain

        return MessageEventResult([Plain(text)])


class StubContext:
    """Stand-in for the AstrBot ``Context`` handed to the plugin."""

    def __init__(
        self,
        *,
        provider_id: str = "openai/gpt-4o",
        completion: str = "主动消息",
        send_delay_s: float = 0.0,
    ) -> None:
        self.provider_id = provider_id
        self.completion = completion
        #: Lets a test hold a delivery in flight while it terminates the plugin.
        self.send_delay_s = send_delay_s
        self.sent: list[tuple[str, Any]] = []
        self.generated: list[dict[str, Any]] = []
        self.provider_lookups: list[str] = []
        self.send_started = asyncio.Event()

    async def get_current_chat_provider_id(self, umo: str | None = None) -> str:
        self.provider_lookups.append(str(umo))
        return self.provider_id

    async def llm_generate(self, **kwargs: Any) -> Any:
        from astrbot.api.provider import LLMResponse

        self.generated.append(kwargs)
        return LLMResponse(self.completion)

    async def send_message(self, session: Any, chain: Any) -> bool:
        self.send_started.set()
        if self.send_delay_s:
            await asyncio.sleep(self.send_delay_s)
        self.sent.append((str(session), chain))
        return True


class StubRuntimeTransport(FakeTransport):
    """Fake transport that accepts the constructor the plugin uses."""

    instances: list[StubRuntimeTransport] = []

    def __init__(self, *, settings: Any = None, log: Any = None) -> None:
        super().__init__()
        self.settings = settings
        self.log = log
        StubRuntimeTransport.instances.append(self)


class PluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the plugin end to end against the AstrBot stub."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.main = _load_plugin_module()
        cls.filters = importlib.import_module("astrbot.api.event.filter")

    def setUp(self) -> None:
        real_astrbot = sys.modules.get("astrbot")
        if real_astrbot is not None and not str(getattr(real_astrbot, "__file__", "")).startswith(
            str(STUBS_DIR),
        ):
            self.skipTest("a real AstrBot installation is importable; stub would mislead")

    async def asyncSetUp(self) -> None:
        self._original_transport = self.main.AiohttpRuntimeTransport
        self.main.AiohttpRuntimeTransport = StubRuntimeTransport
        StubRuntimeTransport.instances = []
        self.context = StubContext()
        self.plugin: Any = None

    async def asyncTearDown(self) -> None:
        if self.plugin is not None:
            await self.plugin.terminate()
        self.main.AiohttpRuntimeTransport = self._original_transport
        # The scope filter instance is shared module state; a test that leaves it
        # widened would silently change AstrBot's pipeline for the next one.
        self.main._ObservationScopeFilter.observe_all = False

    async def _plugin(self, **config_overrides: Any) -> Any:
        config: dict[str, Any] = {
            "runtime_base_url": "http://127.0.0.1:8799",
            "adapter_id": "test-adapter",
            "context_timeout_ms": 50,
            "outbox_poll_interval_ms": 200,
            "request_timeout_ms": 300,
            "render_timeout_ms": 1000,
            "send_timeout_ms": 1000,
        }
        config.update(config_overrides)
        plugin = self.main.CompanionRuntimePlugin(context=self.context, config=config)
        await plugin.initialize()
        self.plugin = plugin
        return plugin

    def _handler(self, name: str) -> Any:
        return self.filters.handler_by_name(name)

    # -- observation -------------------------------------------------------

    async def test_observed_message_is_reported_to_the_runtime(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]

        await self._handler("on_message_observed")(
            self.plugin,
            StubMessageEvent(text="今晚可能不来了"),
        )

        self.assertTrue(await wait_until(lambda: len(transport.event_bodies) == 1))
        body = transport.event_bodies[0]
        self.assertEqual(body["adapter_id"], "test-adapter")
        record = body["events"][0]
        self.assertEqual(record["kind"], "user_message")
        self.assertEqual(record["text"], "今晚可能不来了")
        self.assertEqual(record["session"], SESSION)
        self.assertEqual(record["platform"], "webchat")
        self.assertTrue(record["preempts_proactive"])

    async def test_non_wake_message_is_not_reported_in_wake_mode(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]

        await self._handler("on_message_observed")(
            self.plugin,
            StubMessageEvent(text="群里的闲聊", wake=False),
        )

        self.assertEqual(transport.event_bodies, [])
        self.assertEqual(len(self.plugin._queue), 0)

    async def test_non_wake_message_is_reported_in_all_mode(self) -> None:
        await self._plugin(observe_mode="all")
        transport = StubRuntimeTransport.instances[-1]

        await self._handler("on_message_observed")(
            self.plugin,
            StubMessageEvent(text="群里的闲聊", wake=False),
        )

        self.assertTrue(await wait_until(lambda: len(transport.event_bodies) == 1))
        self.assertFalse(transport.event_bodies[0]["events"][0]["wake"])

    async def test_repeated_start_failures_stop_retrying(self) -> None:
        class ExplodingTransport:
            calls = 0

            def __init__(self, **kwargs: Any) -> None:
                del kwargs
                type(self).calls += 1
                raise RuntimeError("boom")

        self.main.AiohttpRuntimeTransport = ExplodingTransport
        plugin = self.main.CompanionRuntimePlugin(
            context=self.context,
            config={"runtime_base_url": "http://127.0.0.1:8799", "observe_mode": "all"},
        )
        self.plugin = plugin
        handler = self._handler("on_message_observed")

        for _ in range(6):
            await handler(plugin, StubMessageEvent())

        # Three attempts, then the adapter stays quiet instead of logging on
        # every single message.
        self.assertEqual(ExplodingTransport.calls, 3)
        self.assertTrue(plugin._gave_up)
        # Giving up is not a running state, and it must not keep AstrBot's own
        # pipeline widened for an adapter that never came up.
        self.assertFalse(plugin._started)
        self.assertFalse(self.main._ObservationScopeFilter.observe_all)

    async def test_scope_filter_never_wakes_a_sleeping_bot(self) -> None:
        await self._plugin()
        registration = self.filters.registration_for("on_message_observed")
        scope_filter = registration.filter_instance

        self.assertFalse(scope_filter.filter(StubMessageEvent(wake=False), {}))
        self.assertTrue(scope_filter.filter(StubMessageEvent(wake=True), {}))

    async def test_scope_filter_observes_everything_when_opted_in(self) -> None:
        await self._plugin(observe_mode="all")
        scope_filter = self.filters.registration_for("on_message_observed").filter_instance

        self.assertTrue(scope_filter.filter(StubMessageEvent(wake=False), {}))

    async def test_disabled_adapter_never_widens_the_host_pipeline(self) -> None:
        """``observe_all`` on a disabled plugin would wake every group message.

        The filter instance is created once per module and shared by every event,
        so a stale ``True`` left behind by an earlier instance, or published by a
        plugin that is switched off, keeps AstrBot marking non-wake messages as
        wake events on nobody's behalf.
        """
        scope_filter = self.filters.registration_for("on_message_observed").filter_instance
        self.main._ObservationScopeFilter.observe_all = True

        plugin = await self._plugin(enabled=False, observe_mode="all")

        self.assertIsNone(plugin._queue)
        self.assertFalse(self.main._ObservationScopeFilter.observe_all)
        self.assertFalse(scope_filter.filter(StubMessageEvent(wake=False), {}))
        self.assertTrue(scope_filter.filter(StubMessageEvent(wake=True), {}))

    async def test_terminate_resets_the_observation_scope(self) -> None:
        plugin = await self._plugin(observe_mode="all")
        scope_filter = self.filters.registration_for("on_message_observed").filter_instance
        self.assertTrue(scope_filter.filter(StubMessageEvent(wake=False), {}))

        await plugin.terminate()

        self.assertFalse(scope_filter.filter(StubMessageEvent(wake=False), {}))

    async def test_message_type_uses_the_runtime_vocabulary(self) -> None:
        """AstrBot reports chat *classes*; the Runtime needs private/group/other."""
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]

        for raw, expected in (
            ("FriendMessage", "private"),
            ("GroupMessage", "group"),
            ("OtherMessage", "other"),
        ):
            with self.subTest(message_type=raw):
                transport.event_bodies.clear()
                await self._handler("on_message_observed")(
                    plugin,
                    StubMessageEvent(text="hi", message_type=raw),
                )
                self.assertTrue(await wait_until(lambda: len(transport.event_bodies) == 1))
                record = transport.event_bodies[0]["events"][0]
                self.assertEqual(record["message_type"], expected)

    async def test_assistant_message_is_reported(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]

        await self._handler("on_after_message_sent")(
            self.plugin,
            StubMessageEvent(result_text="我在听"),
        )

        self.assertTrue(await wait_until(lambda: len(transport.event_bodies) == 1))
        record = transport.event_bodies[0]["events"][0]
        self.assertEqual(record["kind"], "assistant_message")
        self.assertEqual(record["text"], "我在听")

    async def test_disabled_plugin_does_nothing(self) -> None:
        plugin = await self._plugin(enabled=False)
        self.assertIsNone(plugin._queue)
        self.assertEqual(StubRuntimeTransport.instances, [])

        await self._handler("on_message_observed")(plugin, StubMessageEvent())
        request = SimpleNamespace(extra_user_content_parts=[])
        await self._handler("on_llm_request")(plugin, StubMessageEvent(), request)
        self.assertEqual(request.extra_user_content_parts, [])

    # -- injection ---------------------------------------------------------

    async def test_context_is_injected_as_a_temporary_part(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        # Patch v0.2: the injected block is the character's long-term state
        # before the turn (background), never an instruction for this turn.
        section = "【进入本轮前的长期状态（背景）】"
        transport.snapshot = ContextSnapshot(text=f"{section}\n克制，想联系", version="9")

        from astrbot.api.provider import ProviderRequest

        request = ProviderRequest(prompt="在吗")
        await self._handler("on_llm_request")(self.plugin, StubMessageEvent(), request)

        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertTrue(part._no_save, "hidden context must never be persisted")
        self.assertTrue(part.text.startswith("<companion_runtime_context"))
        self.assertIn(section, part.text)
        self.assertIn('version="9"', part.text)

    async def test_injection_times_out_without_touching_the_request(self) -> None:
        await self._plugin(context_timeout_ms=50)
        transport = StubRuntimeTransport.instances[-1]
        transport.snapshot = ContextSnapshot(text="too late")
        transport.context_delay_s = 0.5

        from astrbot.api.provider import ProviderRequest

        request = ProviderRequest(prompt="在吗")
        await self._handler("on_llm_request")(self.plugin, StubMessageEvent(), request)

        self.assertEqual(request.extra_user_content_parts, [])
        self.assertGreaterEqual(self.plugin._bridge.stats.timeouts, 1)

    async def test_injection_survives_a_runtime_outage(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.context_error = RuntimeError("connection refused")

        from astrbot.api.provider import ProviderRequest

        request = ProviderRequest(prompt="在吗")
        await self._handler("on_llm_request")(self.plugin, StubMessageEvent(), request)

        self.assertEqual(request.extra_user_content_parts, [])

    # -- outbox ------------------------------------------------------------

    async def test_authorized_send_is_delivered_and_reported(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.actions = [_leased(ACTION_SEND, payload={"text": "在忙吗"})]
        transport.authorize_decision = AuthorizeDecision(authorized=True)

        self.assertTrue(await wait_until(lambda: len(self.context.sent) == 1, timeout_s=3.0))

        session, chain = self.context.sent[0]
        self.assertEqual(session, SESSION)
        self.assertEqual(chain.get_plain_text(), "在忙吗")
        self.assertEqual(transport.authorize_requests[0].action_id, "act_1")
        # The message is delivered before the result is reported, so wait for the
        # report rather than assuming it lands in the same loop iteration.
        self.assertTrue(await wait_until(lambda: bool(transport.report_bodies), timeout_s=3.0))
        report = transport.report_bodies[0]
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["result"]["sent"])

    async def test_unauthorized_send_is_not_delivered(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.actions = [_leased(ACTION_SEND, payload={"text": "在忙吗"})]
        transport.authorize_decision = AuthorizeDecision(
            authorized=False,
            reason="aborted_by_user_message",
        )

        self.assertTrue(await wait_until(lambda: len(transport.report_bodies) == 1, timeout_s=3.0))

        self.assertEqual(self.context.sent, [])
        self.assertEqual(transport.report_bodies[0]["status"], "rejected")
        self.assertEqual(transport.report_bodies[0]["error"], "aborted_by_user_message")

    async def test_render_uses_the_sessions_current_provider(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.actions = [
            _leased(ACTION_RENDER, payload={"prompt": "写一句主动问候"}),
        ]

        self.assertTrue(await wait_until(lambda: len(transport.report_bodies) == 1, timeout_s=3.0))

        self.assertEqual(self.context.provider_lookups[0], SESSION)
        self.assertEqual(self.context.generated[0]["chat_provider_id"], "openai/gpt-4o")
        self.assertEqual(self.context.generated[0]["prompt"], "写一句主动问候")
        self.assertEqual(transport.report_bodies[0]["result"]["text"], "主动消息")
        self.assertEqual(self.context.sent, [])

    async def test_lease_request_declares_capabilities_and_ttl(self) -> None:
        await self._plugin(outbox_max_actions_per_poll=3, outbox_lease_ttl_ms=45000)
        transport = StubRuntimeTransport.instances[-1]

        self.assertTrue(await wait_until(lambda: bool(transport.lease_requests), timeout_s=3.0))

        request = transport.lease_requests[0]
        self.assertEqual(request.max_actions, 3)
        self.assertEqual(request.lease_ttl_ms, 45000)
        self.assertEqual(request.capabilities, (ACTION_RENDER, ACTION_SEND))

    async def test_unknown_action_type_is_skipped_and_reported(self) -> None:
        await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.actions = [_leased("teleport", payload={})]

        self.assertTrue(await wait_until(lambda: bool(transport.report_bodies), timeout_s=3.0))

        self.assertEqual(transport.report_bodies[0]["status"], "skipped")
        self.assertIn("unsupported_action_type", transport.report_bodies[0]["error"])

    # -- lifecycle ---------------------------------------------------------

    async def test_terminate_cancels_background_work_and_is_idempotent(self) -> None:
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        tasks = list(plugin._tasks)
        self.assertTrue(tasks)

        await plugin.terminate()

        for task in tasks:
            self.assertTrue(task.done())
        self.assertEqual(plugin._tasks, [])
        self.assertIsNone(plugin._queue)
        self.assertIsNone(plugin._bridge)
        self.assertIsNone(plugin._transport)
        self.assertTrue(transport.closed)
        await plugin.terminate()  # second call must be a no-op
        self.assertTrue(plugin._stopped)

    async def test_terminate_lets_an_authorized_send_finish_and_report(self) -> None:
        """The bounded graceful window is what keeps unload from duplicating a send.

        A delivery that the Runtime already authorized is irreversible: cancelling
        it mid-flight leaves a message that may have reached the user with no
        result report, and the Runtime -- which only knows what the adapter tells
        it -- would eventually hand the same action out again.
        """
        self.context.send_delay_s = 0.3
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.actions = [_leased(ACTION_SEND, payload={"text": "在忙吗"})]
        transport.authorize_decision = AuthorizeDecision(authorized=True)

        self.assertTrue(await wait_until(lambda: self.context.send_started.is_set(), timeout_s=3.0))

        await plugin.terminate()

        self.assertEqual(len(self.context.sent), 1, "an authorized delivery must not be cancelled")
        self.assertTrue(transport.report_bodies, "the finished delivery must be reported")
        self.assertEqual(transport.report_bodies[0]["status"], "ok")
        self.assertEqual(transport.report_bodies[0]["result"]["sent"], True)

    async def test_terminate_never_revives_the_adapter(self) -> None:
        """A hook that arrives after unload must not start new workers.

        AstrBot keeps dispatching to a plugin until the reload completes, so a
        late message used to be able to wire up a fresh transport and outbox loop
        *after* terminate had already cleared them: background work that nothing
        would ever cancel again.
        """
        plugin = await self._plugin()
        await plugin.terminate()
        created = len(StubRuntimeTransport.instances)

        await self._handler("on_message_observed")(plugin, StubMessageEvent())
        request = SimpleNamespace(extra_user_content_parts=[])
        await self._handler("on_llm_request")(plugin, StubMessageEvent(), request)
        await self._handler("on_after_message_sent")(
            plugin,
            StubMessageEvent(result_text="我在听"),
        )

        self.assertEqual(len(StubRuntimeTransport.instances), created)
        self.assertIsNone(plugin._queue)
        self.assertIsNone(plugin._bridge)
        self.assertIsNone(plugin._transport)
        self.assertIsNone(plugin._outbox)
        self.assertEqual(plugin._tasks, [])
        self.assertEqual(request.extra_user_content_parts, [])

    async def test_status_reports_a_terminated_adapter_as_terminated(self) -> None:
        plugin = await self._plugin()
        await plugin.terminate()

        text = await plugin._status_text()

        self.assertIn("state: terminated", text)

    async def test_status_text_reports_counters_without_the_token(self) -> None:
        plugin = await self._plugin(runtime_token="top-secret-token")
        text = await plugin._status_text()
        self.assertIn("adapter_id: test-adapter", text)
        self.assertIn("token: configured", text)
        self.assertNotIn("top-secret-token", text)

    async def test_status_text_reports_the_cognition_levels(self) -> None:
        """Patch v0.2: the report says which level is live and how much is deferred."""
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.health = {
            "semantic_provider": {"provider": "disabled", "available": False},
            "semantics": {"unresolved": 7, "by_status": {"unresolved": 7}},
        }
        text = await plugin._status_text()
        self.assertIn("semantic_provider: disabled (available=False)", text)
        self.assertIn("7 unresolved", text)
        # Deferral is normal operation, and the wording must not read as a fault.
        self.assertIn("normal", text)

    async def test_status_reads_the_runtimes_actual_field_name(self) -> None:
        """Regression: the Runtime reports ``provider``, not ``name``.

        The first version of this probe read ``name``, which the real endpoint
        never sends, so a live deployment printed ``unknown`` while every stub
        test passed. The stub default now mirrors the real payload.
        """
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.health = {"semantic_provider": {"provider": "remote_api", "available": True}}
        text = await plugin._status_text()
        self.assertIn("semantic_provider: remote_api (available=True)", text)
        self.assertNotIn("unknown", text)

    async def test_status_tolerates_the_legacy_name_field(self) -> None:
        """Accept the old key too, so a version skew mislabels nothing."""
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.health = {"semantic_provider": {"name": "remote_api", "available": True}}
        text = await plugin._status_text()
        self.assertIn("semantic_provider: remote_api (available=True)", text)

    async def test_status_default_stub_matches_the_real_health_shape(self) -> None:
        """A stub with the wrong field names could hide a protocol mismatch."""
        plugin = await self._plugin()
        text = await plugin._status_text()
        self.assertIn("semantic_provider: disabled (available=False)", text)
        self.assertNotIn("unknown", text)

    async def test_status_stays_usable_when_the_runtime_is_down(self) -> None:
        """The probe is advisory: no /health must never break the command."""
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.health = None
        text = await plugin._status_text()
        self.assertIn("cognition: unavailable", text)
        self.assertIn("adapter_id: test-adapter", text)

    async def test_status_survives_a_raising_health_probe(self) -> None:
        plugin = await self._plugin()
        transport = StubRuntimeTransport.instances[-1]
        transport.health_error = RuntimeError("boom")
        text = await plugin._status_text()
        self.assertIn("cognition: unavailable", text)

    async def test_status_command_returns_a_plain_result(self) -> None:
        await self._plugin()
        results = [
            result
            async for result in self.plugin.companion_runtime_status(StubMessageEvent())
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get_plain_text().splitlines()[0], "companion Runtime adapter")

    async def test_no_task_is_left_running_after_terminate(self) -> None:
        plugin = await self._plugin()
        outbox_task = plugin._tasks[0]
        await plugin.terminate()
        self.assertTrue(outbox_task.cancelled() or outbox_task.done())
        await asyncio.sleep(0)  # let cancellation settle


def _leased(
    action_type: str,
    *,
    payload: dict[str, Any] | None = None,
    lease_ttl_ms: int = 30000,
) -> LeasedAction:
    """Build a leased action as the Runtime would send it."""
    return LeasedAction(
        action_id="act_1",
        action_type=action_type,
        lease_id="lease_1",
        session=SESSION,
        attempt_id="attempt_1",
        lease_ttl_ms=lease_ttl_ms,
        payload=payload or {},
    )


if __name__ == "__main__":
    unittest.main()
