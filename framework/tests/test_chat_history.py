"""The chat window's history is durable, per session, and only what was visible.

`framework/README.md` §8 lists "历史跨会话持久化" as a gap: `readline` remembered the
lines the operator typed, for as long as the process lived, and nothing else. Restart the
window and the conversation was gone; switch `/session work` and the two conversations
were indistinguishable. For a project whose claim is long-term continuity, that left the
operator unable to judge the thing being built.

These tests pin four properties, and the fourth is a design invariant rather than a
feature:

1. a turn written by one process is readable by the next;
2. sessions do not leak into one another;
3. a damaged line costs that line, not the history;
4. **only the three visible kinds are ever written.** Design §2.6/§86.10: the Runtime's
   hidden background block is injected per turn and discarded, and must never reach a
   durable file. A recorder that accepted an arbitrary ``kind`` would make that a matter
   of remembering; this one makes it a matter of construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cf.history import (
    KIND_ASSISTANT,
    KIND_PROACTIVE,
    KIND_USER,
    VISIBLE_KINDS,
    ChatHistory,
    Turn,
    summarise,
)

STAMP = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)

#: The literal the host's real adapter wraps its injected block in. The history must never
#: contain it - this string appearing in the file would mean hidden context was persisted.
CONTEXT_TAG = "companion_runtime_context"


def _history(tmp_path: Path, **kwargs) -> ChatHistory:
    return ChatHistory(tmp_path / "chat_history.jsonl", clock=lambda: STAMP, **kwargs)


def test_the_visible_kinds_are_closed(tmp_path: Path) -> None:
    """The allow-list is the guard, so it is asserted rather than assumed."""
    assert set(VISIBLE_KINDS) == {KIND_USER, KIND_ASSISTANT, KIND_PROACTIVE}


def test_a_turn_survives_the_process_that_wrote_it(tmp_path: Path) -> None:
    """The whole point: restarting the window does not erase the conversation."""
    first = _history(tmp_path)
    first.record(kind=KIND_USER, session="s", text="在吗")
    first.record(kind=KIND_ASSISTANT, session="s", text="在的")

    second = ChatHistory(tmp_path / "chat_history.jsonl")
    assert [turn.text for turn in second.recent("s")] == ["在吗", "在的"]
    assert [turn.kind for turn in second.recent("s")] == [KIND_USER, KIND_ASSISTANT]


def test_sessions_do_not_leak_into_one_another(tmp_path: Path) -> None:
    """`/session work` must not show the other conversation in its recap."""
    history = _history(tmp_path)
    history.record(kind=KIND_USER, session="a", text="A 说的话")
    history.record(kind=KIND_USER, session="b", text="B 说的话")

    assert [turn.text for turn in history.recent("a")] == ["A 说的话"]
    assert [turn.text for turn in history.recent("b")] == ["B 说的话"]
    assert history.sessions() == ["a", "b"]
    assert history.user_texts("a") == ["A 说的话"]


def test_only_the_operators_own_lines_are_offered_for_recall(tmp_path: Path) -> None:
    """Up-arrow recall is seeded with what the operator typed, not with the character's."""
    history = _history(tmp_path)
    history.record(kind=KIND_USER, session="s", text="第一句")
    history.record(kind=KIND_ASSISTANT, session="s", text="回答")
    history.record(kind=KIND_PROACTIVE, session="s", text="主动")
    history.record(kind=KIND_USER, session="s", text="第二句")

    assert history.user_texts("s") == ["第一句", "第二句"]


def test_empty_text_is_not_a_turn(tmp_path: Path) -> None:
    """A blank line in the window is not something anyone said."""
    history = _history(tmp_path)
    assert history.record(kind=KIND_USER, session="s", text="   ") is None
    assert history.record(kind=KIND_USER, session="s", text="") is None
    assert history.recent("s") == []
    assert not history.path.exists(), "nothing should have been created"


def test_hidden_context_is_refused_not_written(tmp_path: Path) -> None:
    """Design §2.6/§86.10, enforced at the boundary rather than by convention.

    ``record`` refusing an unknown kind is what keeps the Runtime's per-turn background
    block out of a durable file. If this ever becomes a no-op, the failure mode is silent:
    the file simply starts containing something it must not.
    """
    history = _history(tmp_path)

    with pytest.raises(ValueError) as caught:
        history.record(
            kind="runtime_context",
            session="s",
            text=f"<{CONTEXT_TAG}>\n心境：平静\n</{CONTEXT_TAG}>",
        )
    assert "visible kind" in str(caught.value)

    assert not history.path.exists(), "a refused turn must not create the file"
    raw = history.path.read_text(encoding="utf-8") if history.path.exists() else ""
    assert CONTEXT_TAG not in raw


def test_a_damaged_line_costs_only_that_line(tmp_path: Path) -> None:
    """An operator's history file is not worth losing the window over - but say so."""
    history = _history(tmp_path)
    history.record(kind=KIND_USER, session="s", text="好的一行")
    with history.path.open("a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
        handle.write('{"kind": "user", "session": "s"}\n')  # no text, no timestamp
        handle.write('{"kind": "runtime_context", "session": "s", "text": "x", "at": "2026-09-15T09:00:00+00:00"}\n')

    reread = ChatHistory(tmp_path / "chat_history.jsonl")
    assert [turn.text for turn in reread.recent("s")] == ["好的一行"]
    assert reread.malformed == 3, "unreadable lines are counted, not swallowed"
    assert "无法解析" in reread.describe()


def test_a_stored_turn_round_trips_through_json(tmp_path: Path) -> None:
    """The file is meant to be greppable by a human, so the shape is pinned."""
    history = _history(tmp_path)
    turn = history.record(kind=KIND_PROACTIVE, session="webchat:FriendMessage:default", text="忙完了吗")
    assert turn is not None

    line = history.path.read_text(encoding="utf-8").strip()
    assert line.startswith("{") and "\\n" not in line, "one turn is one line"
    back = Turn.from_mapping(__import__("json").loads(line))
    assert back is not None
    assert back.kind == KIND_PROACTIVE
    assert back.text == "忙完了吗"
    assert back.at == STAMP


def test_from_mapping_rejects_what_it_cannot_trust() -> None:
    """Reading is defensive too: a hand-edited file must not crash the window."""
    assert Turn.from_mapping(None) is None
    assert Turn.from_mapping("nope") is None
    assert Turn.from_mapping({"kind": "runtime_context", "session": "s", "text": "x", "at": "2026-09-15T09:00:00+00:00"}) is None
    assert Turn.from_mapping({"kind": "user", "session": "", "text": "x", "at": "2026-09-15T09:00:00+00:00"}) is None
    assert Turn.from_mapping({"kind": "user", "session": "s", "text": "x", "at": "不是时间"}) is None
    assert Turn.from_mapping({"kind": "user", "session": "s", "text": 7, "at": "2026-09-15T09:00:00+00:00"}) is None


def test_the_recap_is_one_line_per_turn_even_for_a_multiline_message(tmp_path: Path) -> None:
    """A recap that reprints whole messages would bury the window it is recapping."""
    history = _history(tmp_path)
    history.record(kind=KIND_ASSISTANT, session="s", text="第一行\n第二行\n第三行")

    lines = summarise(history.recent("s"))
    assert len(lines) == 1
    assert "第一行 第二行 第三行" in lines[0]


def test_a_missing_history_file_is_an_empty_history(tmp_path: Path) -> None:
    """First run: there is nothing to read, and that is not an error."""
    history = ChatHistory(tmp_path / "never-written.jsonl")
    assert history.recent("s") == []
    assert history.sessions() == []
    assert history.malformed == 0


def test_the_file_is_never_created_until_something_is_said(tmp_path: Path) -> None:
    """Opening a window must not litter the run directory."""
    path = tmp_path / "nested" / "chat_history.jsonl"
    history = ChatHistory(path)
    history.recent("s")
    assert not path.exists()
    history.record(kind=KIND_USER, session="s", text="说了")
    assert path.exists(), "and the parent directory is created on first write"


# --------------------------------------------------------------------------------------
# The window itself: does the TUI actually keep the record?
# --------------------------------------------------------------------------------------


def _window(tmp_path: Path, *, recap: int = 0, reply: str = "在的。"):
    """A non-tty chat window wired to a history file."""
    import io
    from contextlib import redirect_stdout

    from cf.clock import ControllableClock
    from cf.host import Delivered, Platform
    from cf.tui import ChatTUI

    platform = Platform()
    history = ChatHistory(tmp_path / "chat_history.jsonl", clock=lambda: STAMP)
    tui = ChatTUI(
        platform=platform,
        clock=ControllableClock(STAMP, scale=0.0),
        send=lambda text, session, on_delta=None: reply,
        history=history,
        recap=recap,
        tty=False,
    )
    return tui, platform, history


def _capture(call, *args) -> str:
    """Run something and return what it printed."""
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        call(*args)
    return buffer.getvalue()


def test_the_window_writes_the_user_and_the_reply(tmp_path: Path) -> None:
    """The two halves of an ordinary turn are what the operator saw."""
    tui, _platform, history = _window(tmp_path)

    _capture(tui._say, "在吗")

    assert [(t.kind, t.text) for t in history.recent(tui.session.umo)] == [
        (KIND_USER, "在吗"),
        (KIND_ASSISTANT, "在的。"),
    ]


def test_a_reply_is_recorded_once_even_when_it_is_also_delivered(tmp_path: Path) -> None:
    """The streamed reply reaches the screen twice; the file must show it once.

    The delivery callback sees the same text as the sender's return value. Writing both
    would double every answer in the history, which is the kind of drift nobody notices
    until a recap reads wrong.
    """
    tui, platform, history = _window(tmp_path)

    _capture(tui._say, "在吗")
    platform.deliver(tui.session.umo, "在的。", kind="reply", at=STAMP)

    kinds = [t.kind for t in history.recent(tui.session.umo)]
    assert kinds == [KIND_USER, KIND_ASSISTANT], kinds


def test_a_reply_nobody_asked_for_through_the_window_is_still_recorded(tmp_path: Path) -> None:
    """A reply produced elsewhere (another terminal, ``cf say``) only passes the callback."""
    tui, platform, history = _window(tmp_path)

    platform.deliver(tui.session.umo, "远处来的回复", kind="reply", at=STAMP)

    assert [(t.kind, t.text) for t in history.recent(tui.session.umo)] == [
        (KIND_ASSISTANT, "远处来的回复")
    ]


def test_a_proactive_message_is_recorded_under_its_own_session(tmp_path: Path) -> None:
    """Proactivity is the point of the project, and it can arrive for a closed window."""
    tui, platform, history = _window(tmp_path)
    # A session exists once something opened it; the fake platform drops messages to
    # addresses it has never seen, which is what the real host does too.
    platform.open("webchat:FriendMessage:work")

    platform.deliver("webchat:FriendMessage:work", "忙完了吗", kind="proactive", at=STAMP)

    assert [t.text for t in history.recent("webchat:FriendMessage:work")] == ["忙完了吗"]
    assert history.recent(tui.session.umo) == [], "it was not said in this session"


def test_the_operators_own_echo_is_not_recorded_twice(tmp_path: Path) -> None:
    """The platform also reports the user's own line; the window already recorded it."""
    tui, platform, history = _window(tmp_path)

    _capture(tui._say, "在吗")
    platform.deliver(tui.session.umo, "在吗", kind="user", at=STAMP)

    assert [t.kind for t in history.recent(tui.session.umo)] == [KIND_USER, KIND_ASSISTANT]


def test_history_command_shows_the_current_session_only(tmp_path: Path) -> None:
    """/history must answer the question it looks like it answers."""
    tui, _platform, history = _window(tmp_path)
    history.record(kind=KIND_USER, session="webchat:FriendMessage:work", text="别的会话")
    _capture(tui._say, "在吗")

    shown = _capture(tui._command, "/history")

    assert "在吗" in shown
    assert "别的会话" not in shown
    assert "工作" not in shown


def test_history_command_says_so_when_there_is_nothing(tmp_path: Path) -> None:
    """An empty answer must be distinguishable from a broken one."""
    tui, _platform, _history = _window(tmp_path)

    assert "还没有记录" in _capture(tui._command, "/history")


def test_history_command_rejects_a_bad_count(tmp_path: Path) -> None:
    """A typo gets a usage line, not a traceback and not silence."""
    tui, _platform, _history = _window(tmp_path)

    shown = _capture(tui._command, "/history 很多")

    assert "用法" in shown


def test_switching_session_loads_that_sessions_line_history(tmp_path: Path) -> None:
    """Up-arrow recall follows the session, or it is worse than no recall."""
    tui, _platform, history = _window(tmp_path)
    history.record(kind=KIND_USER, session="webchat:FriendMessage:work", text="工作里的那句")

    class FakeReadline:
        def __init__(self) -> None:
            self.lines: list[str] = []
            self.cleared = 0

        def clear_history(self) -> None:
            self.cleared += 1
            self.lines = []

        def add_history(self, text: str) -> None:
            self.lines.append(text)

    fake = FakeReadline()
    tui._readline = fake

    _capture(tui._command, "/session work")

    assert fake.lines == ["工作里的那句"], fake.lines
    assert fake.cleared >= 1


def test_a_missing_history_attachment_changes_nothing(tmp_path: Path) -> None:
    """The TUI must still work with no history at all (``cf say``, tests, older callers)."""
    from cf.clock import ControllableClock
    from cf.host import Platform
    from cf.tui import ChatTUI

    tui = ChatTUI(
        platform=Platform(),
        clock=ControllableClock(STAMP, scale=0.0),
        send=lambda text, session, on_delta=None: "在的。",
        tty=False,
    )

    _capture(tui._say, "在吗")

    assert "没有接历史记录" in _capture(tui._command, "/history")
