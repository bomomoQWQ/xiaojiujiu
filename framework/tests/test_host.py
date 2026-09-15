"""Tests for the simulated AstrBot host.

The pure tests cover the fake platform and the event stand-in; the end-to-end
ones drive the *shipped* plugin through its real hooks, so they need the program
checkout and skip without it.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import pytest

from cf.host import (
    PLUGIN_CONFIG,
    SESSION_DEFAULT,
    AstrBotHost,
    Delivered,
    HostContext,
    Platform,
    StubMessageEvent,
    deliveries_by_kind,
)
from cf.main_llm import ScriptedMainLLM
from conftest import FRAMEWORK_ROOT, PROGRAM_SRC, program_available

PLUGIN_ROOT = FRAMEWORK_ROOT.parent / "astrbot_plugin_companion_runtime"

plugin_available = pytest.mark.skipif(
    not (PLUGIN_ROOT / "main.py").is_file(), reason="the AstrBot plugin checkout is not available"
)


@pytest.fixture()
def astrbot_stubs():
    """Put the plugin's AstrBot stubs on ``sys.path`` for the duration of a test.

    ``StubMessageEvent.get_result`` imports ``MessageEventResult`` from the stub
    package, exactly as the real event does. That import only works once the stub
    directory is importable, which ``AstrBotHost.start`` arranges -- so a test that
    exercises the stand-in on its own has to arrange it too. Making ``get_result``
    swallow the ImportError instead would be worse than a failing test: it would
    turn "the plugin reports nothing" into a silent success, which is the exact
    failure this suite already has a regression test for.
    """
    import sys

    stubs = PLUGIN_ROOT / "tests" / "stubs"
    added = str(stubs) not in sys.path
    if added:
        sys.path.insert(0, str(stubs))
    try:
        yield stubs
    finally:
        if added and str(stubs) in sys.path:
            sys.path.remove(str(stubs))

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)


class TestPlatform:
    """A chat app that only knows the sessions someone opened."""

    def test_delivery_to_an_unopened_session_fails(self) -> None:
        """Silently accepting everything would hide the commonest real failure."""
        platform = Platform()
        assert platform.deliver("webchat:FriendMessage:nope", "hi", kind="proactive", at=NOW) is False
        assert platform.messages == []

    def test_delivery_to_an_opened_session_succeeds(self) -> None:
        """Once opened, the session receives messages in order."""
        platform = Platform()
        platform.open(SESSION_DEFAULT)
        assert platform.deliver(SESSION_DEFAULT, "一", kind="reply", at=NOW) is True
        assert platform.deliver(SESSION_DEFAULT, "二", kind="proactive", at=NOW) is True
        assert [m.text for m in platform.messages] == ["一", "二"]

    def test_callback_sees_every_delivery(self) -> None:
        """The TUI is driven by this callback, so it must fire for each message."""
        seen: list[Delivered] = []
        platform = Platform(on_deliver=seen.append)
        platform.open(SESSION_DEFAULT)
        platform.deliver(SESSION_DEFAULT, "hi", kind="reply", at=NOW)
        assert len(seen) == 1 and seen[0].kind == "reply"

    def test_counts_by_kind(self) -> None:
        """``deliveries_by_kind`` summarises a transcript."""
        platform = Platform()
        platform.open(SESSION_DEFAULT)
        platform.deliver(SESSION_DEFAULT, "a", kind="user", at=NOW)
        platform.deliver(SESSION_DEFAULT, "b", kind="reply", at=NOW)
        platform.deliver(SESSION_DEFAULT, "c", kind="proactive", at=NOW)
        assert deliveries_by_kind(platform) == {"user": 1, "reply": 1, "proactive": 1}


class TestStubMessageEvent:
    """The stand-in must answer every accessor the shipped plugin makes."""

    def test_exposes_every_field_the_plugin_reads(self) -> None:
        """Including ``get_group_id``.

        This is a regression test with a specific history: the plugin calls
        ``get_group_id()`` on every observed message, the adapter is *required* to
        swallow observation failures, and a stand-in missing that method therefore
        broke the entire reporting path in silence -- the chat worked perfectly
        while the Runtime never heard about anything.
        """
        event = StubMessageEvent(text="hi", session=SESSION_DEFAULT, message_id="m1")
        assert event.get_platform_name() == "webchat"
        assert event.get_message_type().value == "FriendMessage"
        assert event.get_sender_id() == "default"
        assert event.get_sender_name() == "User"
        assert event.get_self_id() == "companion-bot"
        assert event.get_group_id() == ""
        assert event.unified_msg_origin == SESSION_DEFAULT
        assert event.message_obj.message_id == "m1"

    def test_group_id_is_populated_for_group_sessions(self) -> None:
        """A group session reports its group id."""
        event = StubMessageEvent(text="hi", session="webchat:GroupMessage:g123", message_id="m1")
        assert event.get_group_id() == "g123"

    @plugin_available
    def test_result_round_trips_through_get_result(self, astrbot_stubs) -> None:
        """The plugin reports the delivered text by reading ``get_result()``."""
        del astrbot_stubs
        event = StubMessageEvent(text="hi", session=SESSION_DEFAULT, message_id="m1", result_text="答复")
        assert event.get_result().get_plain_text() == "答复"

    def test_no_result_before_one_is_set(self) -> None:
        """An empty result is ``None``, which is what the plugin checks for.

        This branch returns before the stub import, so it needs no stubs -- which is
        also why the plugin can cheaply skip reporting when nothing was delivered.
        """
        event = StubMessageEvent(text="hi", session=SESSION_DEFAULT, message_id="m1")
        assert event.get_result() is None


class TestHostContext:
    """The three AstrBot APIs the plugin calls, and nothing else."""

    def test_provider_id_resolves_for_a_known_session(self) -> None:
        """A known session yields a provider id that round-trips to the session."""
        platform = Platform()
        platform.open(SESSION_DEFAULT)
        context = HostContext(platform=platform, llm=ScriptedMainLLM(), clock=None)
        import asyncio

        provider = asyncio.run(context.get_current_chat_provider_id(SESSION_DEFAULT))
        assert provider.endswith(SESSION_DEFAULT)

    def test_unknown_session_raises_like_astrbot(self) -> None:
        """An unmatched session fails, so the plugin's error path is real."""
        platform = Platform()
        context = HostContext(platform=platform, llm=ScriptedMainLLM(), clock=None)
        import asyncio

        with pytest.raises(RuntimeError):
            asyncio.run(context.get_current_chat_provider_id("webchat:FriendMessage:unknown"))


requires_program = pytest.mark.skipif(
    not program_available(), reason="the program checkout or uvicorn is not available"
)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A booted harness with the scripted stand-in and no background beat."""
    from cf.harness import Harness, HarnessConfig

    run_dir = tmp_path_factory.mktemp("host-e2e")
    harness = Harness(
        HarnessConfig(
            run_dir=run_dir,
            program_src=PROGRAM_SRC,
            start_time="2026-09-15T09:00:00Z",
            time_scale=0.0,
            heartbeat_interval_s=0,  # driven by hand; see Harness._start_heartbeat
            echo_logs=False,
        )
    )
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()


@requires_program
class TestLiveHost:
    """The shipped plugin, loaded against the stubs and driven through its hooks."""

    def test_plugin_loads_and_registers_its_handlers(self, live) -> None:
        """The real hook names are registered."""
        assert live.host is not None
        assert sorted(live.host.handlers) == ["on_after_message_sent", "on_llm_request", "on_message_observed"]
        assert live.host.running is True

    def test_a_turn_produces_a_reply(self, live) -> None:
        """One user turn returns the acting layer's text and lands in the platform."""
        reply = live.user_turn("在吗", session=SESSION_DEFAULT)
        assert reply
        kinds = [m.kind for m in live.platform.messages]
        assert "user" in kinds and "reply" in kinds

    def test_the_runtime_actually_hears_the_user(self, live) -> None:
        """The regression that mattered: the observation path must reach the Runtime.

        A missing accessor on the event stand-in made every observation throw, and
        the adapter is required to swallow those failures -- so nothing in the chat
        window looked wrong. Asserting on the Runtime's own event count is the only
        way to catch it.
        """
        before = live.program.state()["version"]
        live.user_turn("我明天下午三点面试，结束了告诉你。", session=SESSION_DEFAULT)
        deadline = time.monotonic() + 15
        version = before
        while time.monotonic() < deadline:
            version = live.program.state()["version"]
            if version > before:
                break
            time.sleep(0.3)
        assert version > before, "the Runtime never received the message"

    def test_a_dated_promise_becomes_an_unfinished_matter(self, live) -> None:
        """Rule-based detection creates the matter the character must follow up on."""
        live.user_turn("我明天下午三点面试，结束了告诉你。", session=SESSION_DEFAULT)
        deadline = time.monotonic() + 20
        titles: list[str] = []
        while time.monotonic() < deadline:
            matters = live.program.get("/unfinished").get("matters", [])
            titles = [m["title"] for m in matters]
            if titles:
                break
            time.sleep(0.3)
        assert any("面试" in title for title in titles), titles

    def test_the_runtime_decides_to_speak_on_its_own(self, live) -> None:
        """The background half: no user turn, and the character still acts.

        The clock is stepped by hand rather than advanced in one jump because the
        hazard is drawn *per round*: one enormous step is a single draw, and a
        single draw at a per-second rate is not the same experiment as two days of
        waking up.
        """
        from cf.clock import parse_duration

        acted = None
        step = parse_duration("2h")
        for _ in range(14):
            live.clock.advance(step)
            outcome = (live.endogenous({"force": True}).get("decision") or {}).get("outcome") or {}
            if outcome.get("acted"):
                acted = outcome
                break
        assert acted is not None, "the character never decided to speak"
        assert acted["reason"] == "hazard_triggered"

    def test_a_proactive_message_reaches_the_platform(self, live) -> None:
        """And the whole adapter path delivers it: lease, render, authorize, send."""
        deadline = time.monotonic() + 60
        proactive: list[Delivered] = []
        while time.monotonic() < deadline:
            proactive = [m for m in live.platform.messages if m.kind == "proactive"]
            if proactive:
                break
            time.sleep(0.5)
        assert proactive, "the proactive message never arrived"
        assert proactive[0].text.strip()
        assert proactive[0].session.startswith("webchat:")

    def test_the_render_used_the_runtime_prompt(self, live) -> None:
        """The proactive text came from a render call, not from the reply path."""
        kinds = live.llm.stats()["by_kind"]
        assert kinds.get("render", 0) >= 1
        calls = [c for c in live.llm.calls if c.kind == "render"]
        assert calls and "- 想做的事：" in calls[-1].prompt

    def test_injected_context_is_temporary(self, live) -> None:
        """Every injected part carries ``mark_as_temp``.

        Asserted inside ``Host._injected_text`` too, because hidden Runtime
        context must never be persisted into conversation history -- and a check
        that only runs inside a passing test is not a check.
        """
        turns = live.logbook.read_trace(kinds=("user_turn",))
        assert turns, "no turn was traced"
        assert turns[-1]["injected_chars"] > 0, "the Runtime injected nothing at all"

    def test_the_host_stops_cleanly(self, tmp_path) -> None:
        """Teardown leaves no plugin tasks and no live loop."""
        from cf.harness import Harness, HarnessConfig

        harness = Harness(
            HarnessConfig(
                run_dir=tmp_path / "teardown",
                program_src=PROGRAM_SRC,
                start_time="2026-09-15T09:00:00Z",
                time_scale=0.0,
                heartbeat_interval_s=0,
                echo_logs=False,
            )
        )
        harness.start()
        try:
            harness.user_turn("你好", session=SESSION_DEFAULT)
        finally:
            summary = harness.stop()
        assert harness.host is None
        assert summary["program_source_untouched"]["untouched"] is True


@requires_program
class TestVariableObservation:
    """Watching the Runtime is a different concern from making time pass."""

    def test_variables_are_available_with_the_heartbeat_disabled(self, tmp_path) -> None:
        """Disabling the beat must not blank the status line.

        Regression: the variables behind the status bar used to be refreshed only
        as a side effect of a heartbeat, so ``--heartbeat-interval 0`` -- which a
        caller stepping the clock by hand wants -- left every field as a dash.
        """
        from cf.harness import Harness, HarnessConfig

        harness = Harness(
            HarnessConfig(
                run_dir=tmp_path / "observe",
                program_src=PROGRAM_SRC,
                start_time="2026-09-15T09:00:00Z",
                time_scale=0.0,
                heartbeat_interval_s=0,
                status_interval_s=0.2,
                echo_logs=False,
            )
        )
        harness.start()
        try:
            deadline = time.monotonic() + 15
            variables: dict = {}
            while time.monotonic() < deadline:
                variables = harness.last_variables()
                if variables:
                    break
                time.sleep(0.2)
            assert variables, "the status variables never refreshed"
            assert "mood_valence" in variables
            assert "next_wake_at" in variables
            assert variables["allow_proactive"] in (True, False)
            # The beat really is off, so this came from the observer alone.
            assert harness.status()["beats"] == 0
        finally:
            harness.stop()

    def test_the_refresher_stops_with_the_harness(self, tmp_path) -> None:
        """Teardown joins the observer thread rather than leaking it."""
        from cf.harness import Harness, HarnessConfig

        harness = Harness(
            HarnessConfig(
                run_dir=tmp_path / "observe-stop",
                program_src=PROGRAM_SRC,
                start_time="2026-09-15T09:00:00Z",
                heartbeat_interval_s=0,
                status_interval_s=0.2,
                echo_logs=False,
            )
        )
        harness.start()
        harness.stop()
        assert harness._refresh_thread is None


@requires_program
class TestValueAxes:
    """The eight axes are the personality; leaving them unset must be visible."""

    @staticmethod
    def _boot(tmp_path, values):
        from cf.harness import Harness, HarnessConfig

        harness = Harness(
            HarnessConfig(
                run_dir=tmp_path,
                program_src=PROGRAM_SRC,
                start_time="2026-09-15T09:00:00Z",
                time_scale=0.0,
                heartbeat_interval_s=0,
                echo_logs=False,
                values=values,
            )
        )
        harness.start()
        return harness

    def test_defaults_are_recorded_even_when_nothing_is_overridden(self, tmp_path) -> None:
        """The trace states the effective profile, so "generic" is a visible choice."""
        harness = self._boot(tmp_path / "defaults", {})
        try:
            record = harness.logbook.read_trace(kinds=("values_configured",))[-1]
            assert record["overridden"] == {}
            assert record["effective"]["user_care"] == pytest.approx(0.85)
            assert set(record["effective"]) == {
                "autonomy",
                "boundary_respect",
                "emotional_expression",
                "relationship_maintenance",
                "user_care",
                "conflict_directness",
                "stability_commitment",
                "curiosity",
            }
        finally:
            harness.stop()

    def test_overrides_reach_the_runtime(self, tmp_path) -> None:
        """An override shows up in the Runtime's own projection."""
        harness = self._boot(tmp_path / "override", {"user_care": 0.2, "emotional_expression": 0.95})
        try:
            values = harness.program.state()["values"]
            assert values["user_care"] == pytest.approx(0.2)
            assert values["emotional_expression"] == pytest.approx(0.95)
            assert values["boundary_respect"] == pytest.approx(0.88), "untouched axes keep their default"
        finally:
            harness.stop()

    def test_an_unknown_axis_is_refused(self, tmp_path) -> None:
        """A typo is an error, not a silently ignored personality setting."""
        from cf.harness import Harness, HarnessConfig

        harness = Harness(
            HarnessConfig(
                run_dir=tmp_path / "bad",
                program_src=PROGRAM_SRC,
                start_time="2026-09-15T09:00:00Z",
                heartbeat_interval_s=0,
                echo_logs=False,
                values={"user_care": 0.5, "warmth": 0.9},
            )
        )
        with pytest.raises(RuntimeError, match="warmth"):
            harness.start()
        harness.logbook.close()

    def test_values_change_the_motivational_numbers(self, tmp_path) -> None:
        """Two profiles produce measurably different action probabilities.

        This is the whole point of configuring them: the axes are compiled into the
        dynamics rather than decorating the prompt, so a warmer character should be
        likelier to speak over the same scene.
        """
        def probabilities(values, name):
            harness = self._boot(tmp_path / name, values)
            try:
                harness.user_turn("我明天下午三点面试，结束了告诉你。", session=SESSION_DEFAULT)
                time.sleep(2)
                from cf.clock import parse_duration

                seen: list[float] = []
                for _ in range(10):
                    harness.clock.advance(parse_duration("2h"))
                    outcome = (harness.endogenous({"force": True}).get("decision") or {}).get("outcome") or {}
                    seen.append(float(outcome.get("action_probability") or 0.0))
                return sum(seen) / len(seen)
            finally:
                harness.stop()

        restrained = probabilities({}, "restrained")
        warm = probabilities(
            {
                "user_care": 0.98,
                "emotional_expression": 0.9,
                "boundary_respect": 0.35,
                "relationship_maintenance": 0.95,
                "autonomy": 0.9,
            },
            "warm",
        )
        assert warm > restrained, (warm, restrained)
