"""Unfinished matters: things that need future attention but are not settled.

Lifecycle::

    open -> waiting -> due -> resolved
    (cancelled | muted | expired | invalidated)

Unfinished matters are one of the few *endogenous* wake-up anchors: a matter that
reaches ``due`` becomes a legitimate reason for the Runtime to wake itself up and
consider speaking, without any external calendar or reminder.

Detection is rule-based (zero models) and intentionally narrow; the semantic
layer may add matters through the proposal API.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from .config import RuntimeConfig
from .projections import UnfinishedProjection
from .typing import EventType, RawEvent, UnfinishedMatter, UnfinishedStatus, new_id
from .utility import isoformat, parse_datetime, utcnow

LOGGER = logging.getLogger("companion_runtime.unfinished")

_DAY_OFFSETS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"后天"), 2),
    (re.compile(r"明天|明日|tomorrow"), 1),
    (re.compile(r"今天|今晚|today"), 0),
)

_DAY_PARTS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"凌晨"), 5),
    (re.compile(r"上午|早上|早晨|morning"), 10),
    (re.compile(r"中午|noon"), 12),
    (re.compile(r"下午|afternoon"), 16),
    (re.compile(r"傍晚"), 18),
    (re.compile(r"晚上|今晚|夜里|evening|night"), 21),
)

#: Topics that create an obligation to follow up.
FOLLOW_UP_PATTERNS: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"(面试|面谈|interview)"), "等待面试结果", 0.78),
    (re.compile(r"(考试|笔试|测验|exam)"), "等待考试结果", 0.74),
    (re.compile(r"(体检|检查结果|报告)"), "等待检查结果", 0.72),
    (re.compile(r"(结果|通知|答复|回复我|告诉你)"), "等待用户告知结果", 0.60),
    (re.compile(r"(出差|旅行|起飞|落地|到(家|了))"), "关心行程是否顺利", 0.58),
    (re.compile(r"(明天|后天).{0,6}(给我|告诉你|说)"), "等待用户的后续消息", 0.55),
)

RESOLUTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(结果|通过|过[啦了]|成功了|失败|没过|挂了|录取|offer|拿到)", re.IGNORECASE
        ),
        "result_reported",
    ),
    (re.compile(r"(我回来了|到家了|落地了|回来了)", re.IGNORECASE), "arrived"),
    (re.compile(r"(搞定了|解决了|完成了|做完了|没事了)", re.IGNORECASE), "settled"),
    (re.compile(r"(i (passed|got it|made it)|results? (are )?(out|in))", re.IGNORECASE), "result_reported"),
)


@dataclass(slots=True)
class UnfinishedProposal:
    """A proposed unfinished matter, detected or proposed by a model."""

    title: str
    source_event_ids: list[str] = field(default_factory=list)
    waiting_until: datetime | None = None
    priority: float = 0.5
    resolution_conditions: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable rendering."""
        return {
            "title": self.title,
            "source_event_ids": list(self.source_event_ids),
            "waiting_until": isoformat(self.waiting_until),
            "priority": self.priority,
            "resolution_conditions": list(self.resolution_conditions),
            "topics": list(self.topics),
        }


def _next_day_reference(event: RawEvent) -> datetime:
    """Return the local-midnight anchor used for relative day expressions."""
    local = event.timestamp.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _expected_completion(event: RawEvent, text: str) -> datetime | None:
    """Extract an expected completion time from a relative expression.

    Args:
        event: Source event supplying the day anchor.
        text: User text to scan.

    Returns:
        A timezone-aware expected time, or ``None`` when no expression is found.
    """
    day_offset: int | None = None
    for pattern, offset in _DAY_OFFSETS:
        if pattern.search(text):
            day_offset = offset
            break
    hour: int | None = None
    for pattern, value in _DAY_PARTS:
        if pattern.search(text):
            hour = value
            break
    if day_offset is None and hour is None:
        return None
    anchor = _next_day_reference(event)
    offset = day_offset if day_offset is not None else 0
    start_hour = hour if hour is not None else 9
    candidate = anchor + timedelta(days=offset)
    candidate = candidate.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if day_offset is None and candidate <= event.timestamp:
        candidate += timedelta(days=1)
    # Events usually finish a couple of hours after the stated start.
    return candidate + timedelta(hours=2)


def detect(
    event: RawEvent,
    *,
    config: RuntimeConfig,
    existing: Sequence[UnfinishedMatter] = (),
) -> list[UnfinishedProposal]:
    """Detect follow-up obligations created by one event.

    Args:
        event: Candidate event.
        config: Runtime configuration.
        existing: Currently live matters, used to avoid duplicates.

    Returns:
        Proposed matters (possibly empty).
    """
    if event.event_type != EventType.USER_MESSAGE.value:
        return []
    text = event.content or ""
    if not text.strip():
        return []

    proposals: list[UnfinishedProposal] = []
    for pattern, title, priority in FOLLOW_UP_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        topics = [match.group(0)]
        if any(_same_subject(title, existing_matter.title) for existing_matter in existing):
            continue
        proposals.append(
            UnfinishedProposal(
                title=title,
                source_event_ids=[event.event_id],
                waiting_until=_expected_completion(event, text),
                priority=priority * (0.8 + 0.4 * priority),
                resolution_conditions=["用户主动告知后续结果"],
                topics=topics,
            )
        )
        break
    return proposals


def _same_subject(candidate_title: str, existing_title: str) -> bool:
    """Return whether two matter titles refer to the same subject.

    The follow-up titles are natural language ("等待面试结果" vs "等待用户告知结果"),
    so a shared content token is the right granularity: the point is to avoid
    opening a second matter about the interview, not to compare strings.
    """
    from .utility import tokenize

    generic = {"等待", "用户", "结果", "告知", "关心", "后续"}
    left = {token for token in tokenize(candidate_title)} - generic
    right = {token for token in tokenize(existing_title)} - generic
    if not left or not right:
        return candidate_title == existing_title
    return bool(left & right)


def detect_resolution(
    event: RawEvent, *, live: Sequence[UnfinishedMatter]
) -> list[tuple[str, str]]:
    """Return ``(unfinished_id, reason)`` pairs resolved by an event.

    Args:
        event: Candidate event.
        live: Live matters to test.

    Returns:
        Matters that the user has just settled.
    """
    if event.event_type != EventType.USER_MESSAGE.value or not live:
        return []
    text = event.content or ""
    reasons: list[str] = []
    for pattern, reason in RESOLUTION_PATTERNS:
        if pattern.search(text):
            reasons.append(reason)
    if not reasons:
        return []
    return [(matter.unfinished_id, reasons[0]) for matter in live]


def create(
    projection: UnfinishedProjection,
    connection: sqlite3.Connection,
    proposal: UnfinishedProposal,
    *,
    config: RuntimeConfig,
    now: datetime | None = None,
) -> UnfinishedMatter:
    """Persist a proposed matter.

    Args:
        projection: Unfinished-matter storage.
        connection: Write connection.
        proposal: Matter to create.
        config: Runtime configuration.
        now: Creation time.

    Returns:
        The created matter.
    """
    stamp = now or utcnow()
    status = UnfinishedStatus.WAITING.value if proposal.waiting_until is not None else UnfinishedStatus.OPEN.value
    matter = UnfinishedMatter(
        unfinished_id=new_id("unfinished"),
        title=proposal.title,
        source_event_ids=list(proposal.source_event_ids),
        status=status,
        waiting_until=proposal.waiting_until,
        priority=proposal.priority or config.unfinished.default_priority,
        expire_at=stamp + timedelta(hours=config.unfinished.default_expiry_hours),
        resolution_conditions=list(proposal.resolution_conditions),
        created_at=stamp,
        updated_at=stamp,
    )
    projection.upsert(connection, matter)
    return matter


def tick(
    projection: UnfinishedProjection,
    connection: sqlite3.Connection,
    *,
    config: RuntimeConfig,
    now: datetime,
) -> dict[str, list[str]]:
    """Advance the lifecycle of every live matter in one pass.

    ``waiting`` matters whose ``waiting_until`` has passed become ``due``, and
    ``due`` matters past their expiry become ``expired``.

    Args:
        projection: Unfinished-matter storage.
        connection: Write connection.
        config: Runtime configuration.
        now: Current time.

    Returns:
        Mapping of ``newly_due``, ``expired`` and ``muted`` identifier lists.
    """
    newly_due: list[str] = []
    expired: list[str] = []
    muted: list[str] = []
    for matter in projection.list_open():
        if matter.expire_at is not None and now >= matter.expire_at:
            projection.set_status(connection, matter.unfinished_id, UnfinishedStatus.EXPIRED.value)
            expired.append(matter.unfinished_id)
            continue
        if matter.mute_until is not None and now < matter.mute_until:
            if matter.status != UnfinishedStatus.MUTED.value:
                projection.set_status(connection, matter.unfinished_id, UnfinishedStatus.MUTED.value)
            muted.append(matter.unfinished_id)
            continue
        if matter.waiting_until is not None and now >= matter.waiting_until + timedelta(
            seconds=config.unfinished.due_grace_seconds
        ):
            if matter.status != UnfinishedStatus.DUE.value:
                projection.set_status(connection, matter.unfinished_id, UnfinishedStatus.DUE.value)
                newly_due.append(matter.unfinished_id)
    return {"newly_due": newly_due, "expired": expired, "muted": muted}


def resolve(
    projection: UnfinishedProjection,
    connection: sqlite3.Connection,
    unfinished_id: str,
    *,
    note: str | None = None,
) -> bool:
    """Mark a matter resolved.

    Returns:
        ``True`` when the matter existed and was live.
    """
    matter = projection.get(unfinished_id)
    if matter is None:
        return False
    if matter.status == UnfinishedStatus.RESOLVED.value:
        return False
    projection.set_status(
        connection, unfinished_id, UnfinishedStatus.RESOLVED.value, note=note or "resolved"
    )
    return True


def next_due_at(matters: Sequence[UnfinishedMatter], now: datetime) -> datetime | None:
    """Return the earliest future wake-up time implied by unfinished matters."""
    candidates: list[datetime] = []
    for matter in matters:
        if matter.status in {
            UnfinishedStatus.RESOLVED.value,
            UnfinishedStatus.CANCELLED.value,
            UnfinishedStatus.INVALIDATED.value,
            UnfinishedStatus.EXPIRED.value,
        }:
            continue
        if matter.mute_until is not None and matter.mute_until > now:
            continue
        if matter.waiting_until is not None and matter.waiting_until > now:
            candidates.append(matter.waiting_until)
    return min(candidates) if candidates else None


def priority_of(matters: Sequence[UnfinishedMatter]) -> float:
    """Return the aggregate relevance of live matters in ``[0, 1]``."""
    live = [
        matter
        for matter in matters
        if matter.status
        in {UnfinishedStatus.OPEN.value, UnfinishedStatus.WAITING.value, UnfinishedStatus.DUE.value}
    ]
    if not live:
        return 0.0
    if any(matter.status == UnfinishedStatus.DUE.value for matter in live):
        base = max(matter.priority for matter in live)
        return min(1.0, base * 1.15)
    return max(matter.priority for matter in live) * 0.75
