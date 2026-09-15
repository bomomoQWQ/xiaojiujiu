"""The chat window: a line REPL with a live status bar.

This is the part the operator actually sits in front of. It plays the user, and
it has to do one thing a normal REPL does not: **print messages nobody asked
for**. A companion that only ever answers is not the thing being tested, so
proactive messages must be able to land while the prompt is open, mid-sentence,
without the input line being corrupted or the keystrokes being eaten.

The approach is deliberately boring. There is one print lock; an asynchronous
message clears the current line, prints itself, and redraws the prompt with
whatever was already typed (read from ``readline``'s buffer). When stdout is not
a terminal -- piped to a file, or driven by a test -- the redraw is skipped and
messages are simply written in order, so a session can still be captured verbatim.

Slash commands keep the operator from needing a second terminal: time control,
variable inspection and session switching all work from the prompt.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from .clock import parse_duration, parse_when
from .host import Delivered, Platform, SESSION_DEFAULT

LOGGER = logging.getLogger("cf.tui")

PROMPT = "你> "
PROMPT_CONT = "   "  # continuation indent for wrapped input

#: ANSI helpers, applied only when the output is a terminal.
CLEAR_LINE = "\r\033[K"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
CYAN = "\033[36m"
YELLOW = "\033[33m"

HELP_TEXT = """\
可用命令（也可以直接打字和角色聊天）：
  /help              显示这份帮助
  /time              看虚拟时间与时钟状态
  /advance <时长>    推进时间，如 /advance 8h（这是"用户消失了一会儿"）
  /scale <倍率>      时间流速，如 /scale 60（0 = 停住）
  /freeze /unfreeze  钉住 / 恢复时间
  /vars              看 Runtime 当前变量（心境、I·R·P、未决事项…）
  /status            看 harness 状态（端口、mock 调用数、provider）
  /session <id>      切换会话，如 /session work（默认 default）
  /say <文本>        以用户身份说话（等价于直接打字）
  /quit              退出（Ctrl-D 同）"""


@dataclass
class Session:
    """One open chat window."""

    name: str
    umo: str


class ChatTUI:
    """A line-based chat window with an asynchronous status bar.

    Args:
        platform: The fake platform; its delivery callback is how proactive
            messages reach the screen.
        clock: The virtual clock, exposed through ``/time`` and friends.
        send: Called with the user's text; returns the character's reply.
        status_provider: Returns the latest variable snapshot, or ``{}``.
        control: Optional mapping of slash-command name to handler, so the CLI
            can expose the harness's own operations (host/endogenous/refresh).
        session_name: Initial session name.
        tty: Force terminal behaviour on or off; ``None`` detects it.
    """

    def __init__(
        self,
        *,
        platform: Platform,
        clock: Any,
        send: Callable[[str, str], str],
        status_provider: Callable[[], Mapping[str, Any]] | None = None,
        control: Mapping[str, Callable[[str], str]] | None = None,
        session_name: str = "default",
        tty: bool | None = None,
    ) -> None:
        """Wire the window and take over the platform's delivery callback."""
        self.platform = platform
        self.clock = clock
        self.send = send
        self.status_provider = status_provider or (lambda: {})
        self.control = dict(control or {})
        self.session = Session(name=session_name, umo=f"webchat:FriendMessage:{session_name}")
        self._print_lock = threading.RLock()
        self._stop = threading.Event()
        self.tty = sys.stdout.isatty() if tty is None else tty
        self.turns = 0
        self._readline: Any = None
        if self.tty:
            try:
                import readline  # noqa: F401  (importing it enables line editing)

                self._readline = readline
            except ImportError:  # pragma: no cover - platform dependent
                self._readline = None
        platform.open(self.session.umo)
        previous = platform.on_deliver
        platform.on_deliver = self._on_delivered
        self._previous_on_deliver = previous

    # ---------------------------------------------------------------- printing

    def _write(self, text: str) -> None:
        """Write one line under the print lock, redrawing the prompt after."""
        with self._print_lock:
            if self.tty:
                sys.stdout.write(CLEAR_LINE)
            sys.stdout.write(text + "\n")
            if self.tty:
                sys.stdout.write(self._prompt_line())
            sys.stdout.flush()

    def _prompt_line(self) -> str:
        """Return the prompt plus whatever the user has typed so far."""
        typed = ""
        if self._readline is not None:
            try:
                typed = self._readline.get_line_buffer()
            except Exception:  # noqa: BLE001 - cosmetic only
                typed = ""
        return f"{self.status_line()}\n{PROMPT}{typed}"

    def _on_delivered(self, message: Delivered) -> None:
        """Show a message that arrived on its own (called from the host thread)."""
        if message.kind == "user":
            return  # the operator just typed it; echoing it twice is noise
        style = BOLD if self.tty else ""
        reset = RESET if self.tty else ""
        label = "主动" if message.kind == "proactive" else "回复"
        clock = f"{message.at:%m-%d %H:%M}"
        self._write(f"{style}◆ 小九九({label}) {clock}{reset}\n{message.text}")
        if self._previous_on_deliver is not None:
            self._previous_on_deliver(message)

    def status_line(self) -> str:
        """Return the one-line status bar."""
        variables = dict(self.status_provider() or {})
        moment = self.clock.now()
        clock_state = self.clock.state()
        rate = "停" if clock_state.frozen or clock_state.scale == 0 else f"×{clock_state.scale:g}"
        mood = variables.get("mood_valence")
        mood_text = f"{mood:+.2f}" if isinstance(mood, (int, float)) else "—"
        drive = "/".join(
            f"{variables.get(key, 0):.2f}" if isinstance(variables.get(key), (int, float)) else "—"
            for key in ("impulse", "restraint", "pressure")
        )
        return (
            f"{DIM if self.tty else ''}"
            f"[{moment:%m-%d %H:%M} {rate} | 心境 {mood_text} | I/R/P {drive} | "
            f"未决 {variables.get('unresolved', '—')} | 候选 {variables.get('candidates_active', '—')} | "
            f"未结 {variables.get('unfinished_open', '—')}]"
            f"{RESET if self.tty else ''}"
        )

    # ------------------------------------------------------------------- loop

    def banner(self, *, llm: Mapping[str, Any], runtime_url: str) -> None:
        """Print the opening banner: who is talking, and to what."""
        model = llm.get("model") or "(未配置)"
        endpoint = llm.get("base_url") or "(未配置)"
        self._write(
            f"{BOLD if self.tty else ''}小九九 · 外接框架聊天窗口{RESET if self.tty else ''}\n"
            f"  Runtime   : {runtime_url}\n"
            f"  主 LLM    : {model} @ {endpoint}\n"
            f"  会话      : {self.session.umo}\n"
            f"  输入 /help 看命令，/quit 退出。"
        )

    def run(self) -> int:
        """Read, act, print -- until the operator leaves."""
        while not self._stop.is_set():
            try:
                line = input(f"{self.status_line()}\n{PROMPT}")
            except (EOFError, KeyboardInterrupt):
                self._write("")
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                if self._command(text):
                    break
                continue
            self._say(text)
        return 0

    def _say(self, text: str) -> None:
        """Send one user message and show the reply."""
        self.turns += 1
        try:
            reply = self.send(text, self.session.umo)
        except Exception as exc:  # noqa: BLE001 - a failed turn must not end the chat
            self._write(f"{YELLOW if self.tty else ''}[这一轮失败了] {type(exc).__name__}: {exc}{RESET if self.tty else ''}")
            return
        if not reply:
            self._write(f"{YELLOW if self.tty else ''}[主 LLM 没有返回内容]{RESET if self.tty else ''}")

    # -------------------------------------------------------------- commands

    def _command(self, text: str) -> bool:
        """Handle one slash command. Returns True when the loop should stop."""
        name, _, argument = text[1:].partition(" ")
        name = name.strip().lower()
        argument = argument.strip()

        if name in {"quit", "exit", "q"}:
            return True
        if name in {"help", "h", "?"}:
            self._write(HELP_TEXT)
            return False
        if name == "time":
            state = self.clock.state()
            self._write(
                f"虚拟时间 {state.virtual_now:%Y-%m-%d %H:%M:%S}\n"
                f"真实时间 {state.wall_now:%Y-%m-%d %H:%M:%S}   偏移 {state.offset_seconds:+.1f}s\n"
                f"流速 ×{state.scale:g}   冻结 {'是' if state.frozen else '否'}"
            )
            return False
        if name == "advance":
            if not argument:
                self._write("用法：/advance 8h")
                return False
            try:
                self.clock.advance(parse_duration(argument))
            except ValueError as exc:
                self._write(f"看不懂这个时长：{exc}")
                return False
            self._write(f"时间推进到 {self.clock.now():%Y-%m-%d %H:%M:%S}")
            return False
        if name == "scale":
            try:
                self.clock.set_scale(float(argument))
            except ValueError as exc:
                self._write(f"倍率不对：{exc}")
                return False
            self._write(f"时间流速 ×{self.clock.scale:g}")
            return False
        if name == "freeze":
            self.clock.freeze()
            self._write(f"时间已钉在 {self.clock.now():%Y-%m-%d %H:%M:%S}")
            return False
        if name == "unfreeze":
            self.clock.unfreeze()
            self._write(f"时间恢复流动（×{self.clock.scale:g}）")
            return False
        if name == "vars":
            self._write(_format_variables(self.status_provider() or {}))
            return False
        if name in {"session", "sessions"}:
            if not argument:
                self._write("当前会话 " + self.session.umo + "；已打开：" + ", ".join(self.platform.sessions()))
                return False
            self.session = Session(name=argument, umo=f"webchat:FriendMessage:{argument}")
            self.platform.open(self.session.umo)
            self._write(f"切到会话 {self.session.umo}")
            return False
        if name == "say":
            if argument:
                self._say(argument)
            return False
        handler = self.control.get(name)
        if handler is not None:
            self._write(handler(argument))
            return False
        if name == "set":
            try:
                self.clock.set(parse_when(argument))
            except ValueError as exc:
                self._write(f"看不懂这个时间：{exc}")
                return False
            self._write(f"时间跳到 {self.clock.now():%Y-%m-%d %H:%M:%S}")
            return False
        self._write(f"没有这个命令：/{name}（/help 看列表）")
        return False


def _format_variables(variables: Mapping[str, Any]) -> str:
    """Render the variable snapshot for ``/vars``."""
    if not variables:
        return "还没有心跳数据（等一拍，或按 §4 检查 harness）"
    order = (
        ("mood_valence", "心境·愉悦"),
        ("mood_arousal", "心境·唤醒"),
        ("mood_stability", "心境·稳定"),
        ("impulse", "I 接近冲动"),
        ("restraint", "R 克制"),
        ("pressure", "P 压力"),
        ("allow_proactive", "允许主动"),
        ("contact_count_today", "今日触达"),
        ("unresolved", "未决事件"),
        ("unfinished_open", "未结之事"),
        ("candidates_active", "候选意图"),
        ("boundaries_effective", "生效边界"),
        ("outbox_pending", "待投递"),
        ("outbox_delivered", "已投递"),
        ("attempts_open", "进行中尝试"),
        ("next_wake_at", "下次唤醒"),
        ("next_wake_in_s", "还有(秒)"),
        ("next_wake_reasons", "理由"),
        ("quiet_hours", "免打扰"),
    )
    lines = []
    for key, label in order:
        if key in variables:
            lines.append(f"  {label:<12} {variables[key]}")
    return "\n".join(lines) if lines else "（变量为空）"
