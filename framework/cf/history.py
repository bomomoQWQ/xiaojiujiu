"""Durable chat history for the operator's window.

The chat window keeps no record of what was said. ``readline`` remembers the lines the
operator *typed*, for as long as the process lives, and nothing else: restart ``cf chat``
and the conversation is gone, and switching ``/session work`` leaves the two sessions
indistinguishable. For a project whose whole claim is *long-term* continuity, an operator
who cannot see yesterday's conversation cannot judge it.

This module is the record. It is deliberately narrow:

* **Only what the operator could see.** The three accepted kinds are the user's words, the
  reply, and a proactive message - exactly the three things that appear in the window.
  Anything else is refused with a ``ValueError`` rather than quietly written, because the
  one thing that must never end up in a durable file is the Runtime's hidden background
  block (design §2.6 and §86.10: hidden psychological context is injected per turn and
  discarded, never persisted). A recorder that accepted an arbitrary ``kind`` would make
  that a matter of remembering, instead of a matter of construction.
* **Append-only, one JSON object per line.** The same shape as ``trace.jsonl``, for the
  same reason: a chat can be read back with ``tail``, and a crash mid-write costs at most
  the last line.
* **Never fatal.** A malformed line is skipped and counted, not raised: an operator's
  history file is not worth losing the window over, and silently *pretending* it was fine
  would be worse than either.

Nothing here touches the program under test; it is the framework's own bookkeeping.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

LOGGER = logging.getLogger("cf.history")

#: The user's own words, as typed into the window.
KIND_USER = "user"
#: What the main LLM answered.
KIND_ASSISTANT = "assistant"
#: A message nobody asked for - the thing this whole project exists to test.
KIND_PROACTIVE = "proactive"

#: Everything that may be written. See the module docstring: this list is the guard that
#: keeps the Runtime's hidden context out of a durable file, so it is closed by default.
VISIBLE_KINDS: tuple[str, ...] = (KIND_USER, KIND_ASSISTANT, KIND_PROACTIVE)


@dataclass(frozen=True)
class Turn:
    """One visible line of the conversation."""

    at: datetime
    kind: str
    session: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form written to the file."""
        return {
            "at": self.at.astimezone(timezone.utc).isoformat(),
            "kind": self.kind,
            "session": self.session,
            "text": self.text,
        }

    @classmethod
    def from_mapping(cls, payload: Any) -> "Turn | None":
        """Read one stored line back, or ``None`` when it is not usable."""
        if not isinstance(payload, dict):
            return None
        kind = str(payload.get("kind") or "")
        session = str(payload.get("session") or "")
        text = payload.get("text")
        if kind not in VISIBLE_KINDS or not session or not isinstance(text, str):
            return None
        raw_at = payload.get("at")
        try:
            at = datetime.fromisoformat(str(raw_at))
        except (TypeError, ValueError):
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return cls(at=at, kind=kind, session=session, text=text)


class ChatHistory:
    """An append-only record of the visible conversation, per session.

    Args:
        path: Where the JSONL file lives. Parent directories are created on first write.
        clock: Optional ``() -> datetime`` used to stamp turns; injectable so tests are
            deterministic.
    """

    def __init__(self, path: Path | str, *, clock: Any = None) -> None:
        """Store the target path; nothing is created until the first turn."""
        self.path = Path(path)
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._malformed = 0
        self._recorded = 0

    # ------------------------------------------------------------------ writing

    def record(
        self, *, kind: str, session: str, text: str, at: datetime | None = None
    ) -> Turn | None:
        """Append one visible turn. Returns it, or ``None`` when there was nothing to add.

        Raises:
            ValueError: If ``kind`` is not one of :data:`VISIBLE_KINDS`. Refusing loudly is
                the point: the alternative is a durable file containing the Runtime's
                hidden context, which design §2.6 forbids.
        """
        if kind not in VISIBLE_KINDS:
            raise ValueError(
                f"{kind!r} is not a visible kind; only {', '.join(VISIBLE_KINDS)} may be "
                "persisted (design §2.6: hidden Runtime context is never written down)"
            )
        body = (text or "").strip()
        if not body or not session:
            return None
        turn = Turn(at=at or self._clock(), kind=kind, session=session, text=body)
        line = json.dumps(turn.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._recorded += 1
        return turn

    # ------------------------------------------------------------------ reading

    def __iter__(self) -> Iterator[Turn]:
        """Yield every readable turn, oldest first, counting what could not be read."""
        for raw in self._lines():
            try:
                payload = json.loads(raw)
            except ValueError:
                self._malformed += 1
                continue
            turn = Turn.from_mapping(payload)
            if turn is None:
                self._malformed += 1
                continue
            yield turn

    def _lines(self) -> Iterator[str]:
        """Yield the file's non-empty lines; a missing file is an empty history."""
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield line
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover - unreadable disk
            LOGGER.warning("chat history %s could not be read: %s", self.path, exc)
            return

    def recent(self, session: str, *, limit: int = 20) -> list[Turn]:
        """Return the last ``limit`` turns of one session, oldest first."""
        if limit <= 0:
            return []
        kept: deque[Turn] = deque(maxlen=limit)
        for turn in self:
            if turn.session == session:
                kept.append(turn)
        return list(kept)

    def sessions(self) -> list[str]:
        """Return every session that has a recorded turn, in first-seen order."""
        seen: dict[str, None] = {}
        for turn in self:
            seen.setdefault(turn.session, None)
        return list(seen)

    def user_texts(self, session: str, *, limit: int = 500) -> list[str]:
        """Return the operator's own lines for one session, for ``readline`` recall."""
        kept: deque[str] = deque(maxlen=limit)
        for turn in self:
            if turn.session == session and turn.kind == KIND_USER:
                kept.append(turn.text)
        return list(kept)

    # ------------------------------------------------------------------ reporting

    @property
    def malformed(self) -> int:
        """How many lines could not be read during this instance's lifetime."""
        return self._malformed

    @property
    def recorded(self) -> int:
        """How many turns this instance appended."""
        return self._recorded

    def describe(self) -> str:
        """Return a one-line honest summary, including what could not be read."""
        sessions = self.sessions()
        parts = [f"{self.path}", f"{len(sessions)} 个会话"]
        if sessions:
            parts.append(f"最近：{sessions[-1]}")
        if self._malformed:
            parts.append(f"⚠ {self._malformed} 行无法解析（已跳过）")
        return "，".join(parts)


def read_turns(path: Path | str) -> list[Turn]:
    """Read a whole history file. A convenience for tests and tools."""
    return list(ChatHistory(path))


def summarise(turns: Sequence[Turn], *, session: str | None = None) -> list[str]:
    """Render turns as the lines the window would print for a recap."""
    labels = {KIND_USER: "你", KIND_ASSISTANT: "角色", KIND_PROACTIVE: "主动"}
    out: list[str] = []
    for turn in turns:
        who = labels.get(turn.kind, turn.kind)
        stamp = turn.at.astimezone().strftime("%m-%d %H:%M")
        marker = "" if session is None or turn.session == session else f"[{turn.session}] "
        out.append(f"{stamp} {marker}{who}: {_one_line(turn.text)}")
    return out


def _one_line(text: str, *, width: int = 200) -> str:
    """Collapse a multi-line message so a recap stays one line per turn."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"
