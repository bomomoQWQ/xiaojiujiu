"""Tests for the chat window.

Everything runs with ``tty=False``: the redraw path is cosmetic, while the
command handling and the message routing are the parts that can actually be
wrong. Testing those through a real terminal would add noise, not coverage.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest

from cf.clock import ControllableClock, parse_duration
from cf.host import SESSION_DEFAULT, Delivered, Platform
from cf.tui import ChatTUI

START = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)


class FakeSend:
    """Records what the TUI sent and returns a canned reply."""

    def __init__(self, reply: str = "在的。") -> None:
        """Store the reply to return."""
        self.reply = reply
        self.calls: list[tuple[str, str]] = []
        self.raises: Exception | None = None

    def __call__(self, text: str, session: str) -> str:
        """Record one turn."""
        self.calls.append((text, session))
        if self.raises is not None:
            raise self.raises
        return self.reply


@pytest.fixture()
def window():
    """A non-tty chat window with a scripted sender."""
    platform = Platform()
    clock = ControllableClock(START, scale=0.0)
    send = FakeSend()
    tui = ChatTUI(
        platform=platform,
        clock=clock,
        send=send,
        status_provider=lambda: {
            "mood_valence": -0.25,
            "impulse": 0.4,
            "restraint": 0.6,
            "pressure": 0.2,
            "unresolved": 2,
            "candidates_active": 1,
            "unfinished_open": 1,
        },
        tty=False,
    )
    return tui, platform, clock, send


def run_command(tui: ChatTUI, text: str) -> str:
    """Run one slash command and capture what it printed."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        tui._command(text)
    return buffer.getvalue()


class TestStatusLine:
    """The status bar is the operator's only live view of the Runtime."""

    def test_shows_time_mood_drive_and_counts(self, window) -> None:
        """Every field the operator needs is on one line."""
        tui, _, _, _ = window
        line = tui.status_line()
        assert "09-15 09:00" in line
        assert "-0.25" in line
        assert "0.40/0.60/0.20" in line
        assert "未决 2" in line
        assert "候选 1" in line
        assert "未结 1" in line

    def test_a_stopped_clock_says_so(self, window) -> None:
        """``×0`` is reported as stopped rather than as a rate of zero."""
        tui, _, clock, _ = window
        assert "停" in tui.status_line()
        clock.set_scale(60.0)
        assert "×60" in tui.status_line()
        clock.freeze()
        assert "停" in tui.status_line()

    def test_missing_variables_degrade_to_a_dash(self) -> None:
        """Before the first heartbeat there is nothing to show, not a crash."""
        tui = ChatTUI(
            platform=Platform(),
            clock=ControllableClock(START, scale=0.0),
            send=FakeSend(),
            status_provider=lambda: {},
            tty=False,
        )
        assert "—" in tui.status_line()


class TestSending:
    """Typing a message must reach the host and show the reply."""

    def test_plain_text_is_sent_to_the_current_session(self, window) -> None:
        """The session is the open one, in AstrBot's UMO form."""
        tui, _, _, send = window
        tui._say("在吗")
        assert send.calls == [("在吗", SESSION_DEFAULT)]

    def test_the_reply_is_printed_once(self, window) -> None:
        """The reply arrives through the platform callback, so ``_say`` must not
        print it again -- the operator would see every answer twice."""
        tui, platform, _, _ = window
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            tui._say("在吗")
            platform.deliver(SESSION_DEFAULT, "在的。", kind="reply", at=START)
        assert buffer.getvalue().count("在的。") == 1

    def test_an_empty_reply_is_announced(self, window) -> None:
        """A silent model is reported rather than looking like a hang."""
        tui, _, _, send = window
        send.reply = ""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            tui._say("在吗")
        assert "没有返回内容" in buffer.getvalue()

    def test_a_failed_turn_does_not_end_the_chat(self, window) -> None:
        """An exception is printed and the window stays open."""
        tui, _, _, send = window
        send.raises = RuntimeError("宿主炸了")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            tui._say("在吗")
        assert "这一轮失败了" in buffer.getvalue()
        assert "宿主炸了" in buffer.getvalue()


class TestProactiveDisplay:
    """The behaviour a normal REPL does not have: unasked-for messages."""

    def test_a_proactive_message_is_printed_with_a_marker(self, window) -> None:
        """It is labelled as unprompted and stamped with its time."""
        tui, platform, _, _ = window
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            platform.deliver(SESSION_DEFAULT, "面试怎么样？", kind="proactive", at=START)
        out = buffer.getvalue()
        assert "主动" in out
        assert "面试怎么样？" in out
        assert "09-15 09:00" in out

    def test_the_user_s_own_message_is_not_echoed(self, window) -> None:
        """The operator typed it; echoing it back is noise."""
        tui, platform, _, _ = window
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            platform.deliver(SESSION_DEFAULT, "我打的字", kind="user", at=START)
        assert buffer.getvalue() == ""

    def test_a_previous_callback_is_still_called(self) -> None:
        """Wrapping the platform does not steal deliveries from someone else."""
        seen: list[Delivered] = []
        platform = Platform(on_deliver=seen.append)
        ChatTUI(platform=platform, clock=ControllableClock(START), send=FakeSend(), tty=False)
        platform.open(SESSION_DEFAULT)
        platform.deliver(SESSION_DEFAULT, "hi", kind="proactive", at=START)
        assert len(seen) == 1


class TestCommands:
    """Slash commands keep the operator out of a second terminal."""

    def test_help_lists_the_commands(self, window) -> None:
        """``/help`` names the things that exist."""
        tui, _, _, _ = window
        out = run_command(tui, "/help")
        for name in ("/time", "/advance", "/scale", "/freeze", "/vars", "/session", "/quit"):
            assert name in out

    def test_advance_moves_the_clock(self, window) -> None:
        """``/advance 8h`` is the "the user went away" operation."""
        tui, _, clock, _ = window
        out = run_command(tui, "/advance 8h")
        assert clock.now() == START + parse_duration("8h")
        assert "17:00" in out

    def test_set_jumps_to_an_instant(self, window) -> None:
        """``/set`` is the absolute form."""
        tui, _, clock, _ = window
        run_command(tui, "/set 2027-01-02T03:04:05Z")
        assert clock.now().year == 2027

    def test_scale_and_freeze(self, window) -> None:
        """``/scale 0`` and ``/freeze`` both stop time, by different means."""
        tui, _, clock, _ = window
        run_command(tui, "/scale 60")
        assert clock.scale == 60.0
        run_command(tui, "/freeze")
        assert clock.frozen is True
        run_command(tui, "/unfreeze")
        assert clock.frozen is False

    def test_a_bad_duration_is_reported_not_swallowed(self, window) -> None:
        """A typo says so, and the clock does not move."""
        tui, _, clock, _ = window
        out = run_command(tui, "/advance eight hours")
        assert "看不懂" in out
        assert clock.now() == START

    def test_vars_lists_the_variable_block(self, window) -> None:
        """``/vars`` renders the same numbers the status line summarises."""
        tui, _, _, _ = window
        out = run_command(tui, "/vars")
        assert "心境·愉悦" in out
        assert "未决事件" in out
        assert "-0.25" in out

    def test_vars_before_any_heartbeat_says_so(self) -> None:
        """An empty snapshot explains itself instead of printing nothing."""
        tui = ChatTUI(
            platform=Platform(),
            clock=ControllableClock(START),
            send=FakeSend(),
            status_provider=lambda: {},
            tty=False,
        )
        assert "还没有心跳数据" in run_command(tui, "/vars")

    def test_session_switch_changes_the_target(self, window) -> None:
        """``/session work`` opens and targets a second chat."""
        tui, platform, _, send = window
        run_command(tui, "/session work")
        assert "webchat:FriendMessage:work" in platform.sessions()
        tui._say("在吗")
        assert send.calls[-1][1] == "webchat:FriendMessage:work"

    def test_say_command_is_equivalent_to_typing(self, window) -> None:
        """``/say`` exists so a scripted session can drive turns without a tty."""
        tui, _, _, send = window
        run_command(tui, "/say 你好")
        assert send.calls == [("你好", SESSION_DEFAULT)]

    def test_unknown_command_is_reported(self, window) -> None:
        """An unknown verb names itself and points at ``/help``."""
        tui, _, _, _ = window
        assert "没有这个命令" in run_command(tui, "/nope")

    @pytest.mark.parametrize("word", ["/quit", "/exit", "/q"])
    def test_quit_words_stop_the_loop(self, window, word) -> None:
        """Every documented spelling of quit stops the loop."""
        tui, _, _, _ = window
        assert tui._command(word) is True

    def test_non_quit_commands_keep_the_loop_running(self, window) -> None:
        """Nothing else stops it."""
        tui, _, _, _ = window
        assert tui._command("/help") is False
        assert tui._command("/time") is False


class TestExtraControl:
    """The CLI can graft its own commands onto the window."""

    def test_a_grafted_command_runs_and_prints(self, window) -> None:
        """A control handler's return value is what the operator sees."""
        tui, _, _, _ = window
        tui.control["endogenous"] = lambda arg: f"回合完成 arg={arg!r}"
        assert "回合完成" in run_command(tui, "/endogenous force")

    def test_a_grafted_command_without_arguments(self, window) -> None:
        """An argument-less graft gets an empty string, not None."""
        tui, _, _, _ = window
        tui.control["beat"] = lambda arg: f"arg={arg!r}"
        assert "arg=''" in run_command(tui, "/beat")
