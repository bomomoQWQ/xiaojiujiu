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
    ) -> None:
        self.unified_msg_origin = session
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id="msg-1")
        self.is_at_or_wake_command = wake
        self._result_text = result_text

    def get_platform_name(self) -> str:
        return "webchat"

    def get_message_type(self) -> Any:
        return SimpleNamespace(value="private")

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

    def __init__(self, *, provider_id: str = "openai/gpt-4o", completion: str = "主动消息") -> None:
        self.provider_id = provider_id
        self.completion = completion
        self.sent: list[tuple[str, Any]] = []
        self.generated: list[dict[str, Any]] = []
        self.provider_lookups: list[str] = []

    async def get_current_chat_provider_id(self, umo: str | None = None) -> str:
        self.provider_lookups.append(str(umo))
        return self.provider_id

    async def llm_generate(self, **kwargs: Any) -> Any:
        from astrbot.api.provider import LLMResponse

        self.generated.append(kwargs)
        return LLMResponse(self.completion)

    async def send_message(self, session: Any, chain: Any) -> bool:
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
        transport.snapshot = ContextSnapshot(text="【当前心理状态】\n克制，想联系", version="9")

        from astrbot.api.provider import ProviderRequest

        request = ProviderRequest(prompt="在吗")
        await self._handler("on_llm_request")(self.plugin, StubMessageEvent(), request)

        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertTrue(part._no_save, "hidden context must never be persisted")
        self.assertTrue(part.text.startswith("<companion_runtime_context"))
        self.assertIn("【当前心理状态】", part.text)
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

    async def test_status_text_reports_counters_without_the_token(self) -> None:
        plugin = await self._plugin(runtime_token="top-secret-token")
        text = plugin._status_text()
        self.assertIn("adapter_id: test-adapter", text)
        self.assertIn("token: configured", text)
        self.assertNotIn("top-secret-token", text)

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
