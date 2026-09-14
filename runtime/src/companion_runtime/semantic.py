"""Coarse semantic settlement: the persistent-cognition entry point.

Architecture patch v0.2 splits the system into two time scales:

* the **acting layer** - the host main LLM understands the current turn and
  performs it immediately;
* the **persistent cognition layer** - the Runtime decides what this turn leaves
  behind across hours and days.

That split means the Runtime no longer needs a precise emotional reading of every
message. It needs something cheaper and more honest: a *coarse settlement* when
the evidence is unambiguous, and an explicit ``unresolved`` marker when it is not.

This module implements that contract with no model at all:

* :func:`classify_event` returns a :class:`CoarseSettlement` only for events that
  carry an explicit lexical anchor and no hedging marker;
* anything ambiguous returns ``None``, which the Runtime records as
  ``semantic_status = unresolved`` and revisits later.

Deliberate design note: bare ``算了`` / ``随便`` / ``也没什么`` must **not** be
classified. Patch v0.2 names ``"算了，也没什么。"`` as the canonical example of an
event the Runtime should refuse to guess about, so ambiguity markers act as a
veto over the anchor table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Sequence

from .emotion import EmotionEvaluation
from .typing import EmotionDirection
from .utility import clamp, isoformat

__all__ = [
    "AmbiguityVeto",
    "CoarseSettlement",
    "IntensityBand",
    "SemanticStatus",
    "UnresolvedRecord",
    "band_to_intensity",
    "classify_event",
    "potential_relevance",
    "settlement_to_evaluation",
]


class SemanticStatus(str, Enum):
    """Whether a raw event has been given a durable semantic reading yet."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class IntensityBand(str, Enum):
    """Coarse strength of a persistent emotional after-effect.

    Bands exist so that a rule or a cheap model can express "clearly positive and
    fairly strong" without inventing a calibrated float it cannot justify.
    """

    NEGLIGIBLE = "negligible"
    LOW = "low"
    MEDIUM = "medium"
    MEDIUM_HIGH = "medium_high"
    HIGH = "high"


#: Representative float for each band, used when the settlement must be handed to
#: the numeric emotion dynamics. The values are midpoints, not measurements.
_BAND_INTENSITY: dict[str, float] = {
    IntensityBand.NEGLIGIBLE.value: 0.05,
    IntensityBand.LOW.value: 0.20,
    IntensityBand.MEDIUM.value: 0.40,
    IntensityBand.MEDIUM_HIGH.value: 0.62,
    IntensityBand.HIGH.value: 0.85,
}


def band_to_intensity(band: str | IntensityBand) -> float:
    """Return a representative intensity for a coarse band.

    Args:
        band: Band name or enum member.

    Returns:
        A float in ``[0, 1]``; unknown bands degrade to ``MEDIUM``.
    """
    key = band.value if isinstance(band, IntensityBand) else str(band)
    return _BAND_INTENSITY.get(key, _BAND_INTENSITY[IntensityBand.MEDIUM.value])


@dataclass(slots=True)
class CoarseSettlement:
    """A durable, coarse reading of one raw event.

    Attributes:
        direction: ``+``, ``-``, ``0`` or ``+-``.
        intensity: An :class:`IntensityBand` value.
        confidence: Confidence in this settlement, in ``[0, 1]``.
        source: Machine-readable provenance, e.g. ``explicit_positive_feedback``.
        evidence: The matched surface form, kept for auditability.
        semantic_label: Always ``None`` for coarse settlements; naming an emotion
            is the job of the deep refresh, never of the rule layer.
    """

    direction: str
    intensity: str
    confidence: float
    source: str
    evidence: str = ""
    semantic_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "direction": self.direction,
            "intensity": self.intensity,
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "evidence": self.evidence,
            "semantic_label": self.semantic_label,
        }


@dataclass(slots=True)
class UnresolvedRecord:
    """One event the Runtime declined to interpret yet.

    Patch v0.2 makes deliberate delay a feature: the acting layer may already have
    understood the turn, while the persistent layer keeps the raw evidence and
    waits for a better moment or a later, clearer signal.
    """

    event_id: str
    potential_relevance: str
    created_at: datetime
    reason: str = ""
    text_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "event_id": self.event_id,
            "semantic_status": SemanticStatus.UNRESOLVED.value,
            "potential_relevance": self.potential_relevance,
            "created_at": isoformat(self.created_at),
            "reason": self.reason,
            "text_preview": self.text_preview,
        }


@dataclass(frozen=True, slots=True)
class AmbiguityVeto:
    """A hedge marker that forbids a confident settlement on its own."""

    needle: str
    reason: str


@dataclass(frozen=True, slots=True)
class Anchor:
    """A lexical anchor that can carry a confident coarse settlement."""

    needles: tuple[str, ...]
    direction: str
    intensity: str
    confidence: float
    source: str


#: Hedging and vagueness markers. Their presence does not by itself decide the
#: direction, but it does veto any settlement that is not backed by an explicit
#: anchor, because the text is not committed to a single reading.
#:
#: ``算了`` and ``也没什么`` are here on purpose: patch v0.2 uses
#: ``"算了，也没什么。"`` as the canonical event that must stay unresolved.
AMBIGUITY_MARKERS: tuple[AmbiguityVeto, ...] = (
    AmbiguityVeto("算了", "hedged_withdrawal"),
    AmbiguityVeto("也没什么", "minimising"),
    AmbiguityVeto("没什么", "minimising"),
    AmbiguityVeto("随便", "indifferent"),
    AmbiguityVeto("都行", "indifferent"),
    AmbiguityVeto("无所谓", "indifferent"),
    AmbiguityVeto("可能", "uncertain"),
    AmbiguityVeto("也许", "uncertain"),
    AmbiguityVeto("大概", "uncertain"),
    AmbiguityVeto("不知道", "uncertain"),
    AmbiguityVeto("不清楚", "uncertain"),
    AmbiguityVeto("还好", "mild"),
    AmbiguityVeto("一般", "mild"),
    AmbiguityVeto("再说吧", "deferred"),
    AmbiguityVeto("看情况", "deferred"),
)

#: Explicit anchors, ordered from most to least specific within a category. A
#: match settles the event; no match leaves it unresolved.
ANCHORS: tuple[Anchor, ...] = (
    # --- explicit appreciation / positive feedback
    Anchor(
        ("谢谢你", "谢谢", "感谢", "多谢", "thx", "thank you"),
        EmotionDirection.POSITIVE.value,
        IntensityBand.MEDIUM_HIGH.value,
        0.85,
        "explicit_positive_feedback",
    ),
    Anchor(
        ("被你安慰到", "安慰到了", "心里好受多了", "好多了"),
        EmotionDirection.POSITIVE.value,
        IntensityBand.MEDIUM_HIGH.value,
        0.80,
        "explicit_positive_feedback",
    ),
    Anchor(
        ("我喜欢你", "想你", "想你了", "爱你", "离不开你"),
        EmotionDirection.POSITIVE.value,
        IntensityBand.HIGH.value,
        0.85,
        "explicit_affection",
    ),
    Anchor(
        ("太好了", "太棒了", "真开心", "好开心", "我成功了", "我过了", "过了！", "升职", "录取"),
        EmotionDirection.POSITIVE.value,
        IntensityBand.HIGH.value,
        0.80,
        "explicit_good_news",
    ),
    Anchor(
        ("过啦", "通过了", "考上", "成功了", "拿到了", "拿到 offer", "搞定了"),
        EmotionDirection.POSITIVE.value,
        IntensityBand.HIGH.value,
        0.78,
        "explicit_good_news",
    ),
    # --- explicit need for space, stated as a choice rather than a rejection.
    # This is a settled reading: the user is not being ambiguous, they are telling
    # the character what they want. It yields a distance signal, not a mood swing.
    Anchor(
        ("想自己待着", "想自己待一会", "想自己待会儿", "想一个人待着", "想一个人静静", "需要一点空间"),
        EmotionDirection.NEGATIVE.value,
        IntensityBand.LOW.value,
        0.72,
        "explicit_need_for_space",
    ),
    # --- explicit repair / apology
    Anchor(
        ("对不起", "抱歉", "是我不好", "我错了", "原谅我"),
        EmotionDirection.POSITIVE.value,
        IntensityBand.MEDIUM.value,
        0.75,
        "explicit_repair",
    ),
    # --- major negative life events (facts, not moods)
    Anchor(
        ("去世", "走了", "过世", "葬礼"),
        EmotionDirection.NEGATIVE.value,
        IntensityBand.HIGH.value,
        0.85,
        "major_loss",
    ),
    Anchor(
        ("被辞", "被裁", "失业", "分手", "离婚", "确诊", "住院", "手术"),
        EmotionDirection.NEGATIVE.value,
        IntensityBand.HIGH.value,
        0.85,
        "major_setback",
    ),
    # --- explicit conflict directed at the character
    Anchor(
        ("你根本不懂", "你太过分", "我讨厌你", "我受够你了", "你怎么这样"),
        EmotionDirection.NEGATIVE.value,
        IntensityBand.HIGH.value,
        0.80,
        "explicit_conflict",
    ),
    # --- explicit affect disclosure
    Anchor(
        ("很难过", "很难受", "很失望", "很生气", "撑不住了", "受不了了", "想哭"),
        EmotionDirection.NEGATIVE.value,
        IntensityBand.MEDIUM_HIGH.value,
        0.80,
        "explicit_distress",
    ),
    Anchor(
        ("很开心", "很高兴", "好开心", "很幸福"),
        EmotionDirection.POSITIVE.value,
        IntensityBand.MEDIUM_HIGH.value,
        0.80,
        "explicit_joy",
    ),
    # --- explicit refusal of a specific behaviour (distinct from boundaries)
    Anchor(
        ("我不想聊这个", "别再问了", "我不想说", "我不想谈"),
        EmotionDirection.NEGATIVE.value,
        IntensityBand.MEDIUM.value,
        0.75,
        "explicit_refusal",
    ),
)

#: A refusal that names no object is still a refusal only when it is blunt enough;
#: these are kept separate so the ambiguity veto stays meaningful.
BLUNT_REFUSALS: tuple[str, ...] = ("不行", "不可以", "我拒绝", "不要这样")

#: Markers that make an event worth revisiting later even when it is unresolved.
HIGH_RELEVANCE_HINTS: tuple[str, ...] = (
    "关系",
    "喜欢",
    "讨厌",
    "离开",
    "分手",
    "以后",
    "永远",
    "一直",
    "为什么",
    "是不是",
    "你觉得",
)

MEDIUM_RELEVANCE_HINTS: tuple[str, ...] = (
    "今天",
    "明天",
    "面试",
    "工作",
    "考试",
    "答应",
    "约",
    "等",
    "忙",
)

_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+")

#: Fillers removed before anchor matching. They carry no sentiment, but they do
#: break naive substring matching: ``"我今天很难过"`` would otherwise miss the
#: ``"很难过"`` anchor even though the feeling is stated explicitly.
#:
#: Only sentiment-neutral words belong here. Removing anything evaluative would
#: turn a hedged sentence into a confident one, which is exactly the failure this
#: module exists to prevent.
FILLER_TOKENS: tuple[str, ...] = (
    "我今天",
    "我现在",
    "我刚刚",
    "我刚才",
    "今天",
    "现在",
    "刚刚",
    "刚才",
    "其实",
    "真的",
    "确实",
    "感觉",
    "觉得",
)


def normalize_for_anchors(text: str) -> str:
    """Strip sentiment-neutral fillers so anchors match the feeling itself.

    Args:
        text: Raw event text.

    Returns:
        The text with :data:`FILLER_TOKENS` removed.
    """
    normalized = text
    for filler in FILLER_TOKENS:
        normalized = normalized.replace(filler, "")
    return normalized


def _matched(text: str, needles: Iterable[str]) -> str:
    """Return the first needle present in ``text``, or an empty string."""
    for needle in needles:
        if needle and needle in text:
            return needle
    return ""


def _matched_normalized(text: str, needles: Iterable[str]) -> str:
    """Match needles against the filler-stripped form of ``text``."""
    normalized = normalize_for_anchors(text)
    return _matched(normalized, needles)


def ambiguity_veto(text: str) -> AmbiguityVeto | None:
    """Return the first hedging marker present in ``text``, if any.

    Args:
        text: Raw event text.

    Returns:
        The matching :class:`AmbiguityVeto`, or ``None`` when the text is direct.
    """
    for veto in AMBIGUITY_MARKERS:
        if veto.needle in text:
            return veto
    return None


def classify_event(
    text: str,
    *,
    event_type: str = "user_message",
    actor: str = "user",
) -> CoarseSettlement | None:
    """Return a confident coarse settlement, or ``None`` when it must stay unresolved.

    The contract is deliberately one-sided: a wrong settlement silently corrupts
    the character's long-term state, while an ``unresolved`` event costs only the
    opportunity to settle early and can always be revisited. So this function
    answers ``None`` whenever the evidence is not explicit.

    Args:
        text: Raw event text.
        event_type: Raw event type, used to skip non-conversational records.
        actor: Who produced the event.

    Returns:
        A :class:`CoarseSettlement` for explicit evidence, otherwise ``None``.
    """
    body = (text or "").strip()
    if not body:
        return None
    if event_type not in {"user_message", "assistant_message"}:
        return None
    if actor == "system":
        return None

    # An explicit anchor is allowed to survive a hedge only when it is a strong,
    # unambiguous statement of fact or feeling. Everything else defers.
    veto = ambiguity_veto(body)
    strong = _matched(
        body,
        (
            "去世",
            "过世",
            "被辞",
            "被裁",
            "失业",
            "分手",
            "离婚",
            "确诊",
            "手术",
            "谢谢你",
            "对不起",
            "抱歉",
            "我喜欢你",
            "我很开心",
            "我很高兴",
            "我很难过",
            "我很失望",
        ),
    )
    if veto is not None and not strong:
        return None

    for anchor in ANCHORS:
        hit = _matched(body, anchor.needles) or _matched_normalized(body, anchor.needles)
        if hit:
            return CoarseSettlement(
                direction=anchor.direction,
                intensity=anchor.intensity,
                confidence=anchor.confidence,
                source=anchor.source,
                evidence=hit,
            )

    hit = _matched(body, BLUNT_REFUSALS)
    if hit and veto is None:
        return CoarseSettlement(
            direction=EmotionDirection.NEGATIVE.value,
            intensity=IntensityBand.MEDIUM.value,
            confidence=0.70,
            source="explicit_refusal",
            evidence=hit,
        )

    # No explicit evidence: the Runtime records the event and waits.
    return None


def potential_relevance(
    text: str,
    *,
    hours_since_contact: float = 0.0,
    has_open_matters: bool = False,
) -> str:
    """Return a cheap guess at whether an unresolved event will matter later.

    This only prioritises the deep-refresh queue; it never changes state.

    Args:
        text: Raw event text.
        hours_since_contact: Hours since the last exchange.
        has_open_matters: Whether unfinished matters are currently open.

    Returns:
        ``"low"``, ``"medium"`` or ``"high"``.
    """
    body = (text or "").strip()
    if len(body) <= 3:
        return "low"
    if _matched(body, HIGH_RELEVANCE_HINTS):
        return "high"
    if has_open_matters and hours_since_contact >= 6.0:
        return "medium"
    if _matched(body, MEDIUM_RELEVANCE_HINTS):
        return "medium"
    if len(body) >= 24:
        return "medium"
    return "low"


def settlement_to_evaluation(settlement: CoarseSettlement) -> EmotionEvaluation:
    """Convert a coarse settlement into the numeric emotion contract.

    The conversion is intentionally lossy in one direction only: a settlement
    never claims more precision than its band. Uncertainty is derived from the
    settlement confidence so that a low-confidence reading damps its own effect
    downstream.

    Args:
        settlement: The coarse settlement to convert.

    Returns:
        An :class:`EmotionEvaluation` tagged with ``source="coarse_rule"``.
    """
    confidence = clamp(float(settlement.confidence))
    return EmotionEvaluation(
        direction=str(settlement.direction),
        impact=band_to_intensity(settlement.intensity),
        activation=clamp(band_to_intensity(settlement.intensity) * 0.8),
        uncertainty=clamp(1.0 - confidence),
        relation_signal=_relation_signal_for(settlement.source),
        responsibility="unclear",
        confidence=confidence,
        source="coarse_rule",
    )


def _relation_signal_for(source: str) -> str:
    """Map a settlement source to the relation-signal vocabulary."""
    mapping = {
        "explicit_positive_feedback": "appreciation",
        "explicit_affection": "closeness",
        "explicit_good_news": "good_news",
        "explicit_repair": "repair",
        "major_loss": "loss",
        "major_setback": "bad_news",
        "explicit_conflict": "distance",
        "explicit_distress": "sorrow",
        "explicit_joy": "good_news",
        "explicit_refusal": "distance",
        "explicit_need_for_space": "distance",
    }
    return mapping.get(source, "neutral")


def resolve_backlog(
    unresolved: Sequence[UnresolvedRecord],
    *,
    now: datetime,
    max_age_hours: float = 72.0,
    limit: int = 20,
) -> tuple[list[UnresolvedRecord], list[UnresolvedRecord]]:
    """Split an unresolved backlog into "still worth refreshing" and "stale".

    Args:
        unresolved: Records currently marked unresolved.
        now: Reference time.
        max_age_hours: Age after which a record stops justifying a deep refresh.
        limit: Maximum number of live records to return, newest and most relevant
            first.

    Returns:
        ``(live, stale)`` - the records worth sending to a deep refresh, and the
        records that should simply age out.
    """
    weight = {"high": 0, "medium": 1, "low": 2}
    live: list[UnresolvedRecord] = []
    stale: list[UnresolvedRecord] = []
    for record in unresolved:
        age_hours = (now - record.created_at).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            stale.append(record)
        else:
            live.append(record)
    live.sort(key=lambda item: (weight.get(item.potential_relevance, 3), -item.created_at.timestamp()))
    return live[:limit], stale
