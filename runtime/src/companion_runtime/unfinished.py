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
from .utility import isoformat, parse_datetime, topic_tokens, utcnow

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

#: Topics that create an obligation to follow up. The order matters: the rules are
#: alternative readings of one sentence and the first *applicable* match decides, so
#: the specific subjects come first. "明天要出差，落地告诉你" mentions both a trip and a
#: promise to report, and the trip reading is the more informative one - it names the
#: subject instead of filing a generic "wait for the user to tell me something".
FOLLOW_UP_PATTERNS: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"(面试|面谈|interview)"), "等待面试结果", 0.78),
    (re.compile(r"(考试|笔试|测验|exam)"), "等待考试结果", 0.74),
    (re.compile(r"(体检|检查结果|报告)"), "等待检查结果", 0.72),
    (re.compile(r"(出差|旅行|起飞|落地|到(?!家了|了))"), "关心行程是否顺利", 0.58),
    (re.compile(r"(结果|通知|答复|回复我|告诉你)"), "等待用户告知结果", 0.60),
    (re.compile(r"(明天|后天).{0,6}(给我|告诉你|说)"), "等待用户的后续消息", 0.55),
)

#: Words that turn a travel mention into a promise to report back. Without one of
#: these, a trip mention is *not* an obligation: "我到家了" reports that something
#: finished, so reading it as "care whether the trip goes well" would invent an
#: obligation out of its own resolution - and would do so on every arrival.
FOLLOW_UP_MARKERS: tuple[str, ...] = (
    "告诉你",
    "给你说",
    "跟你说",
    "说一声",
    "告诉我",
    "通知你",
    "汇报",
    "发消息",
    "联系你",
)

#: The travel rule above additionally requires one of :data:`FOLLOW_UP_MARKERS`.
#: Every other rule is an obligation on its own terms (an exam has a result, a
#: report is expected), so the requirement is expressed per pattern.
PATTERNS_REQUIRING_A_PROMISE = frozenset({"关心行程是否顺利"})

#: Resolution rules, each scoped to the subject it can actually settle.
#:
#: ``topics`` is what makes resolution subject-aware. An empty tuple means "this
#: statement settles whatever is open" - a completed exam result settles the exam
#: matter. A non-empty tuple means the matter must be *about* one of those
#: subjects. Without that scoping, "到家了" resolved every open matter, including
#: an interview the user had not heard back from.
RESOLUTION_PATTERNS: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (
        re.compile(
            r"(结果|通过|过[啦了]|成功了|失败|没过|挂了|录取|offer|拿到)", re.IGNORECASE
        ),
        "result_reported",
        (),
    ),
    (
        re.compile(r"(我回来了|到家了|落地了|回来了)", re.IGNORECASE),
        "arrived",
        ("行程", "出差", "旅行", "落地", "到家", "回来", "起飞", "顺利"),
    ),
    (
        re.compile(r"(搞定了|解决了|完成了|做完了|没事了)", re.IGNORECASE),
        "settled",
        ("事", "任务", "问题", "工作"),
    ),
    (
        re.compile(
            r"(i (passed|got it|made it)|results? (are )?(out|in))", re.IGNORECASE
        ),
        "result_reported",
        (),
    ),
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
    resolved_topics: Sequence[str] = (),
) -> list[UnfinishedProposal]:
    """Detect follow-up obligations created by one event.

    Args:
        event: Candidate event.
        config: Runtime configuration.
        existing: Matters whose *subjects* are already spoken for, used to avoid
            duplicates. Pass :func:`subject_guards` rather than the open matters:
            what makes a mention a duplicate is that the subject is taken, not that
            the obligation is still open.
        resolved_topics: Subjects this same event just settled. A completion
            statement must not re-open an obligation about the thing it finished,
            so these subjects are skipped even when the text looks like a promise.

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
        # A travel mention is an obligation only when the user promises to report
        # back. "我到家了" is the completion of a trip, not a new promise about it.
        if title in PATTERNS_REQUIRING_A_PROMISE and not any(
            marker in text for marker in FOLLOW_UP_MARKERS
        ):
            break
        if resolved_topics and _mentions_any(match.group(0), resolved_topics):
            break
        # The patterns are alternative readings of the *same* sentence, not
        # independent obligations: "明天下午面试，结束告诉你结果。" matches both
        # the interview pattern and the generic follow-up pattern. So the first
        # match decides, and a match that is already covered ends the search -
        # otherwise the same sentence would open a second, vaguer matter.
        if any(_same_subject(title, existing_matter.title) for existing_matter in existing):
            break
        proposals.append(
            UnfinishedProposal(
                title=title,
                source_event_ids=[event.event_id],
                waiting_until=_expected_completion(event, text),
                priority=priority * (0.8 + 0.4 * priority),
                resolution_conditions=["用户主动告知后续结果"],
                topics=[match.group(0)],
            )
        )
        break
    return proposals


#: Words that appear in every follow-up title and therefore carry no subject.
#: They are stripped before two titles are compared, because the titles are built
#: from a template ("等待{subjects}结果") and only the subject is distinctive.
_TITLE_TEMPLATE_WORDS: tuple[str, ...] = (
    "等待",
    "用户",
    "结果",
    "告知",
    "关心",
    "后续",
    "是否",
    "顺利",
    "消息",
)


def _subject_core(title: str) -> str:
    """Return the distinctive part of a follow-up title.

    Titles are assembled from a template, so comparing them directly (or by
    single characters) produces false matches: "等待面试结果" and "等待考试结果"
    share the character 试 and the whole tail 试结果, yet they are different
    subjects. Removing the template words leaves exactly the subject.

    Args:
        title: A matter title.

    Returns:
        The title with template words removed; empty when nothing distinctive
        remains, which makes the caller fall back to a strict comparison.
    """
    core = title or ""
    for word in _TITLE_TEMPLATE_WORDS:
        core = core.replace(word, "")
    return core.strip()


def _same_subject(candidate_title: str, existing_title: str) -> bool:
    """Return whether two matter titles refer to the same subject.

    Splitting on the template leaves the subject alone ("面试" vs "考试"), which
    is the granularity that matters: the point is to avoid opening a second matter
    about the *same* interview, not to avoid opening one about a different exam.
    When neither title has a distinctive core, the comparison falls back to a
    bigram overlap, which is stricter than single characters.
    """
    left = _subject_core(candidate_title)
    right = _subject_core(existing_title)
    if left and right:
        if left == right or left in right or right in left:
            return True
        # Model-written titles (the deep refresh) contain none of the template
        # words, so the test above is *exact equality* for them and every
        # re-wording opens another copy of the same obligation. Measured on the
        # test deployment: three open matters about one milk-tea invitation,
        # worded "无糖奶茶" / "奶茶/去糖奶茶" / "奶茶邀约尚未落地".
        return _nearly_identical(left, right)
    if not left and not right:
        return candidate_title == existing_title
    # One side is pure template; only treat it as the same subject when the other
    # side is empty of meaning too.
    return False


#: Token overlap at which two stripped prose cores are candidates for "same
#: obligation". Calibrated on the measured pairs below (they are also the test
#: cases); 0.6 is not enough on *this* basis -- the "奶茶" -> "咖啡" pair scores
#: 0.70 and would have to be caught by the second bar alone.
SUBJECT_BIGRAM_THRESHOLD = 0.77

#: ...and how much of the core may actually differ. Overlap alone over-merges: two
#: *different* topics can share a long boilerplate tail, which lifts the overlap
#: just as high. Measured on the stripped cores:
#:
#:     same topic, re-worded   奶茶/去糖奶茶 vs 无糖奶茶      0.800  22.2%
#:     same topic, one clause  「当个事办」…轻量确认 + 不必…   0.808  19.2%
#:     different topic         奶茶/去糖奶茶 vs 咖啡          0.700  33.3%
#:     different topic         「我试试什么（）」vs「换个头像」 0.737  29.4%
#:
#: So the differing share is the discriminator, and both bars are set to the
#: middle of their measured gap (leaving roughly three points on each side).
SUBJECT_DIFF_RATIO_LIMIT = 0.26


def _nearly_identical(left: str, right: str) -> bool:
    """Return whether two prose titles are one sentence with a small edit.

    Args:
        left: First title (already stripped of template words).
        right: Second title (already stripped of template words).

    Returns:
        ``True`` when the two share most of their tokens and differ only slightly.
    """
    first = topic_tokens(left)
    second = topic_tokens(right)
    if not first or not second:
        return False
    if len(first & second) / len(first | second) < SUBJECT_BIGRAM_THRESHOLD:
        return False
    changed = len(first - second) + len(second - first)
    return changed / max(len(first), len(second)) <= SUBJECT_DIFF_RATIO_LIMIT


# --------------------------------------------------------------------------------------
# Subject reservation
# --------------------------------------------------------------------------------------

#: Statuses in which a matter still owns its subject outright: nothing has discharged
#: the obligation, so a mention of the subject is that obligation continuing.
LIVE_MATTER_STATUSES: frozenset[str] = frozenset(
    {
        UnfinishedStatus.OPEN.value,
        UnfinishedStatus.WAITING.value,
        UnfinishedStatus.DUE.value,
        UnfinishedStatus.MUTED.value,
    }
)

#: Statuses in which the *user* closed the matter, as opposed to it having lapsed on
#: its own (``expired``) or been withdrawn by the Runtime (``invalidated``). Only these
#: leave a subject behind that is worth reserving: somebody said something about it.
SETTLED_MATTER_STATUSES: frozenset[str] = frozenset(
    {UnfinishedStatus.RESOLVED.value, UnfinishedStatus.CANCELLED.value}
)

#: How long a settled matter keeps its subject reserved, in seconds.
#:
#: Three days is the smallest window that covers a realistic "I'll tell you how it went
#: / don't bring it up again" tail: the report itself, the acknowledgement, and the
#: change of heart that follows. It is deliberately finite - a reservation that never
#: expired would make the subject unreachable forever, and "下周三还有个面试" is a
#: genuinely new promise about the same subject.
SETTLED_SUBJECT_RESERVATION_SECONDS = 3 * 24 * 3600.0


def already_spoken_for(
    title: str,
    sources: Sequence[str],
    matters: Sequence[UnfinishedMatter],
) -> bool:
    """Return whether a proposal restates something a matter already holds.

    Two different ways a proposal can be a restatement, and the deep refresh needs
    both because it re-reads the *same* unresolved events on every run:

    * **the same source event already produced a matter.** This is the one that
      actually bites. A refresh that re-reads an event it has already understood will
      derive the same obligation again, worded differently enough that a title
      comparison misses it.
    * **the subject is taken** (:func:`_same_subject`), which catches the same
      obligation arriving from a *different* event - the wording-independent case.

    Measured on the test deployment before this guard existed: ten open matters
    traced to two events, five each, created over twelve hours, and not one of them
    was ever the same matter twice. Nothing resolved them either, so the open set
    only grew - and every one of them is injected into the context block.

    Args:
        title: The proposed matter's title.
        sources: Events the proposal is grounded in.
        matters: Matters that still own a subject; pass
            :func:`subject_guards` rather than the open set.

    Returns:
        ``True`` when the proposal should be dropped as already covered.
    """
    wanted = {str(item) for item in sources if item}
    for matter in matters:
        if wanted and wanted & set(matter.source_event_ids or ()):
            return True
        if _same_subject(title, matter.title):
            return True
    return False


def subject_guards(
    matters: Sequence[UnfinishedMatter],
    *,
    now: datetime,
    reservation_seconds: float = SETTLED_SUBJECT_RESERVATION_SECONDS,
) -> list[UnfinishedMatter]:
    """Return the matters whose subjects are already spoken for.

    This answers "is this subject taken?", which is the question the obligation
    detector needs - *not* "is this obligation currently open?". A matter the user has
    just settled still occupies its subject, and that is the whole point: the sentence
    that reports a result and the sentence that forbids the topic both mention the
    interview, and neither is a *new* promise about it. Reading only the open set is
    what let "面试过了！" close the interview matter and, in the same breath, open a
    fresh "等待面试结果" from that very sentence.

    The reservation is finite on purpose: after :data:`SETTLED_SUBJECT_RESERVATION_SECONDS`
    a mention may open a new matter again, so a genuinely later interview is still
    remembered.

    Args:
        matters: Matters of any status.
        now: Current time.
        reservation_seconds: How long a settled matter keeps its subject.

    Returns:
        The live matters, plus every matter that settled recently enough to still hold
        its subject. Conservative on missing evidence: a ``updated_at`` that is absent,
        unparsable or ahead of ``now`` counts as recent, because treating the subject
        as released is the more damaging guess.
    """
    guards: list[UnfinishedMatter] = []
    for matter in matters:
        if matter.status in LIVE_MATTER_STATUSES:
            guards.append(matter)
        elif matter.status in SETTLED_MATTER_STATUSES and _settled_recently(
            matter, now=now, reservation_seconds=reservation_seconds
        ):
            guards.append(matter)
    return guards


def _settled_recently(
    matter: UnfinishedMatter, *, now: datetime, reservation_seconds: float
) -> bool:
    """Return whether a settled matter still holds its subject.

    A stamp *ahead* of ``now`` counts as recent too. That is not a hypothetical: the
    whole day can be replayed under a simulated clock, and an event ingested with its
    original timestamp lands in a matter whose ``updated_at`` is stamped from a clock
    that has already moved past it. Reading that as "long released" would re-open
    every subject the replay touched.
    """
    updated = matter.updated_at
    if updated is None:
        return True
    try:
        age = (now - updated).total_seconds()
    except TypeError:
        # A naive stamp cannot be compared with an aware ``now``; that is no evidence
        # that the subject has been released either.
        return True
    return age <= reservation_seconds


def detect_resolution(
    event: RawEvent, *, live: Sequence[UnfinishedMatter]
) -> list[tuple[str, str]]:
    """Return ``(unfinished_id, reason)`` pairs resolved by an event.

    Resolution is *subject-aware*. A completion statement settles the matters it is
    actually about, and never the whole open set: "到家了" closes a matter about a
    trip and leaves "等待面试结果" open, because the user has said nothing about the
    interview. The price of the narrower rule is a missed early settlement, which
    the deep refresh can still pick up; the price of the broad rule was silently
    closing obligations the user never addressed.

    Args:
        event: Candidate event.
        live: Live matters to test.

    Returns:
        Matters that the user has just settled.
    """
    if event.event_type != EventType.USER_MESSAGE.value or not live:
        return []
    text = event.content or ""
    resolved: list[tuple[str, str]] = []
    for pattern, reason, topics in RESOLUTION_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        # The matched phrase is the *evidence*, so it is consumed before the subject
        # is looked for: otherwise "我到家了" would be read as being about "到家",
        # which the sibling rule already lists as a subject, and it would close a
        # matter about a trip by quoting the very words that ended one.
        remainder = text[: match.start()] + text[match.end() :]
        scope = _resolution_scope(topics, remainder=remainder, live=live)
        if scope is None:
            continue
        for matter in live:
            if not _settles(matter, remainder=remainder, subjects=scope):
                continue
            pair = (matter.unfinished_id, reason)
            if pair not in resolved:
                resolved.append(pair)
    return resolved


#: Words that carry no subject in a short completion statement, and are therefore
#: ignored when the subject of "结果出来了" is worked out.
_SUBJECT_STOPWORDS: frozenset[str] = frozenset(
    {
        "我", "你", "他", "她", "它", "我们", "你们", "他们",
        "的", "了", "啦", "呀", "吧", "吗", "呢", "啊", "嘛", "哦", "诶",
        "已经", "终于", "结果", "通知", "答复", "回复", "告诉", "说", "一声",
        "出来", "果出", "回来", "过来", "到了", "有了", "拿到", "过啦", "过了",
        "搞定", "解决", "完成", "做完", "没事", "我回", "到家", "落地",
        "ok", "done", "already", "the", "it", "is", "are",
    }
)

#: Subjects that belong to *a* resolution rule. A subject named in a message that
#: belongs to one rule must not be allowed to authorise a different one, which is
#: what `面试到家了` would otherwise do to the arrival rule.
_ALL_RESOLUTION_SUBJECTS: frozenset[str] = frozenset(
    token for _pattern, _reason, topics in RESOLUTION_PATTERNS for token in topics
)

#: Scope token that cannot occur in any matter title or message. It expresses "this
#: rule applies but no subject was named", which must settle nothing.
_NO_SUBJECT = "\x00no_subject"


def _subject_tokens(text: str) -> list[str]:
    """Return the subjects ``text`` names.

    CJK bigrams are used rather than single characters: single characters are far too
    common to mean a shared subject (every sentence has 我 in it), while a bigram
    survives punctuation boundaries that would split a longer phrase apart.
    """
    return [token for token in sorted(topic_tokens(text)) if _is_subject_token(token)]


def _is_subject_token(token: str) -> bool:
    """Return whether a token can stand for a subject in a completion statement.

    Single CJK characters and short Latin words are function words ("我", "的",
    "out") rather than subjects, and every subject a rule owns is excluded here so
    that naming one subject cannot authorise a different rule.
    """
    if token in _SUBJECT_STOPWORDS or token in _ALL_RESOLUTION_SUBJECTS:
        return False
    if token.isascii():
        return len(token) > 3
    return len(token) > 1


def _resolution_scope(
    topics: Sequence[str], *, remainder: str, live: Sequence[UnfinishedMatter]
) -> tuple[str, ...] | None:
    """Return the subjects a resolution rule may settle for this message.

    The subject is read from the part of the message *around* the resolving phrase,
    which is what stops a completion statement from supplying its own subject. A rule
    that declares no subjects of its own ("a result came out", which settles whatever
    is open) is scoped by the subject the message names.

    Args:
        topics: The rule's declared subjects; empty means "the rule owns none".
        remainder: The message with the resolving phrase removed.
        live: Live matters, used only to answer "does this rule own anything here".

    Returns:
        The subjects to match on, or ``None`` when the rule does not apply to this
        message at all - a rule that owns subjects must not settle on the strength of
        a message about a subject it knows nothing about ("面试到家了" is not an
        arrival).
    """
    declared = tuple(topics)
    named = tuple(_subject_tokens(remainder))
    if not declared:
        # A rule with no subjects of its own (a reported *result*) is scoped by the
        # subject the message names. When it names none, nothing is settled: an
        # unrestricted fallback here would let "结果出来了" close every open matter,
        # which is the exact over-reach the subject scoping exists to prevent.
        return named or (_NO_SUBJECT,)
    if _mentions_any(remainder, declared):
        return declared
    if any(_mentions_any(matter.title, declared) for matter in live):
        # The rule owns something live, so it applies; the subject it may settle is
        # narrowed to what the message actually named, if anything.
        return named or declared
    return None


def resolved_topics(event: RawEvent, pairs: Sequence[tuple[str, str]]) -> list[str]:
    """Return the subjects an event settled, for the obligation detector.

    The subjects are read from the resolving rule rather than from the text, so the
    sentence that finishes a subject cannot immediately re-open an obligation about
    the very thing it finished.
    """
    if not pairs:
        return []
    reasons = {reason for _identifier, reason in pairs}
    subjects: list[str] = []
    for pattern, reason, topics in RESOLUTION_PATTERNS:
        if reason not in reasons or not pattern.search(event.content or ""):
            continue
        subjects.extend(topics)
    return subjects


def _settles(
    matter: UnfinishedMatter, *, remainder: str, subjects: Sequence[str] | None
) -> bool:
    """Return whether a resolution statement applies to one matter.

    Args:
        matter: The live matter being tested.
        remainder: The message with the resolving phrase removed. The subject is
            looked for here rather than in the whole message, so the resolving words
            themselves cannot supply the subject they resolve.
        subjects: Subjects this rule may settle; ``None`` means "any subject".
    """
    if subjects is None:
        return True
    if not subjects:
        # The rule owns subjects and the message names a different one.
        return False
    return _mentions_any(remainder, subjects) or _mentions_any(matter.title, subjects)


def _mentions_any(text: str, tokens: Sequence[str]) -> bool:
    """Return whether ``text`` mentions any of ``tokens``."""
    return any(token and token in (text or "") for token in tokens)


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
