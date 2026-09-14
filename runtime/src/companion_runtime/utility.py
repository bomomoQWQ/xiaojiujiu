"""Math, time and text helpers shared by every cognitive module.

The module is deliberately dependency-free: the Runtime must run on a weak VPS
with nothing but the standard library plus FastAPI.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import logging

LOGGER = logging.getLogger("companion_runtime")

# --------------------------------------------------------------------------------------
# Numeric helpers
# --------------------------------------------------------------------------------------


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp ``value`` into ``[low, high]``.

    Args:
        value: Input number.
        low: Lower bound.
        high: Upper bound.

    Returns:
        The clamped value.
    """
    if value < low:
        return low
    if value > high:
        return high
    return value


def sigmoid(value: float) -> float:
    """Numerically stable logistic function."""
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def logit(probability: float, eps: float = 1e-6) -> float:
    """Inverse of :func:`sigmoid`, guarding the open interval ``(0, 1)``."""
    p = clamp(probability, eps, 1.0 - eps)
    return math.log(p / (1.0 - p))


def softplus(value: float, beta: float = 1.0) -> float:
    """Smooth rectifier ``S_beta(x) = ln(1 + e^(beta x)) / beta``.

    This is the ``S_beta`` used by the pressure dynamics. It is never negative
    and grows linearly for large positive inputs.
    """
    if beta <= 0.0:
        raise ValueError("beta must be positive")
    scaled = beta * value
    if scaled > 40.0:
        return value
    if scaled < -40.0:
        return math.exp(scaled) / beta
    return math.log1p(math.exp(scaled)) / beta


def softmax(scores: Sequence[float], temperature: float = 1.0) -> list[float]:
    """Return a numerically stable softmax over ``scores``.

    Args:
        scores: Raw logits.
        temperature: Positive temperature; smaller is greedier.

    Returns:
        A probability vector summing to 1. A uniform vector is returned for an
        empty input or a non-positive temperature.
    """
    if not scores:
        return []
    if temperature <= 0.0:
        temperature = 1e-6
    scaled = [s / temperature for s in scores]
    top = max(scaled)
    exps = [math.exp(s - top) for s in scaled]
    total = sum(exps)
    if total <= 0.0 or not math.isfinite(total):
        return [1.0 / len(scores)] * len(scores)
    return [e / total for e in exps]


def exponential_decay(rate: float, elapsed_seconds: float, half_life: float | None = None) -> float:
    """Return the retention factor ``exp(-rate * dt)``.

    Args:
        rate: Decay rate per second. Ignored when ``half_life`` is given.
        elapsed_seconds: Elapsed time in seconds (negative values are treated as 0).
        half_life: Optional half-life in seconds, overriding ``rate``.

    Returns:
        A factor in ``(0, 1]``.
    """
    dt = max(0.0, elapsed_seconds)
    if half_life is not None and half_life > 0.0:
        return math.pow(0.5, dt / half_life)
    return math.exp(-max(0.0, rate) * dt)


def approach(current: float, target: float, tau_seconds: float, dt_seconds: float) -> float:
    """First-order inertia step ``dX/dt = (target - X) / tau``.

    The update is analytic over ``dt`` so a single lazy tick can jump hours
    without accumulating integration error.

    Args:
        current: Current value.
        target: Target value.
        tau_seconds: Time constant in seconds; non-positive means instant.
        dt_seconds: Elapsed time in seconds.

    Returns:
        The updated value.
    """
    if tau_seconds <= 0.0:
        return target
    alpha = 1.0 - math.exp(-max(0.0, dt_seconds) / tau_seconds)
    return current + (target - current) * alpha


def weighted_mean(items: Iterable[tuple[float, float]]) -> float:
    """Return a weighted mean of ``(value, weight)`` pairs, 0.0 when empty."""
    numerator = 0.0
    denominator = 0.0
    for value, weight in items:
        if weight <= 0.0:
            continue
        numerator += value * weight
        denominator += weight
    if denominator <= 0.0:
        return 0.0
    return numerator / denominator


def jitter(magnitude: float, rng) -> float:
    """Return a symmetric uniform perturbation in ``[-magnitude, magnitude]``."""
    if magnitude <= 0.0:
        return 0.0
    return rng.uniform(-magnitude, magnitude)


# --------------------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------------------

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utcnow() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime | None, default: datetime | None = None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC.

    Args:
        value: Input datetime, possibly naive.
        default: Value returned when ``value`` is ``None``.

    Returns:
        An aware datetime or the default.
    """
    if value is None:
        return default
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 string (or pass through a datetime) into aware UTC.

    A trailing ``Z`` and the common ``+0800`` form are both accepted.
    """
    if value is None or isinstance(value, datetime):
        return ensure_aware(value)
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unsupported datetime format: {value!r}") from None
    return ensure_aware(parsed)


def to_epoch(value: datetime | None) -> float:
    """Return seconds since the Unix epoch for an aware datetime."""
    if value is None:
        return 0.0
    return ensure_aware(value).timestamp()


def from_epoch(seconds: float) -> datetime:
    """Return an aware UTC datetime from seconds since the Unix epoch."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def delta_seconds(later: datetime | None, earlier: datetime | None) -> float:
    """Return ``later - earlier`` in seconds, clamped at zero."""
    if later is None or earlier is None:
        return 0.0
    return max(0.0, (ensure_aware(later) - ensure_aware(earlier)).total_seconds())


def isoformat(value: datetime | None) -> str | None:
    """Return an ISO-8601 string for a datetime, or ``None``."""
    return ensure_aware(value).isoformat() if value is not None else None


def min_datetime(*values: datetime | None) -> datetime | None:
    """Return the earliest non-``None`` datetime."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return min(ensure_aware(v) for v in present)


def hours(value: float) -> timedelta:
    """Return a timedelta of ``value`` hours (kept for readable config math)."""
    return timedelta(hours=value)


def local_now(now: datetime | None = None) -> datetime:
    """Return ``now`` (default: UTC now) converted to the machine local timezone.

    Used for quiet-hours decisions, which are inherently local-clock questions.
    """
    reference = ensure_aware(now) if now is not None else utcnow()
    return reference.astimezone()


# --------------------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------------------


_WHITESPACE = re.compile(r"\s+")


def summarize_text(text: str, limit: int = 120) -> str:
    """Collapse whitespace and truncate a text for use as a summary.

    Args:
        text: Source text.
        limit: Maximum number of characters to keep.

    Returns:
        A single-line summary with an ellipsis when truncated.
    """
    collapsed = _WHITESPACE.sub(" ", (text or "").strip())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def contains_any(text: str, needles: Iterable[str]) -> str | None:
    """Return the first needle present in ``text`` (case-insensitive), else ``None``."""
    lowered = (text or "").lower()
    for needle in needles:
        if needle and needle.lower() in lowered:
            return needle
    return None


def tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens for lexical scoring.

    Latin words are returned whole. CJK text has no spaces, so it is decomposed
    into individual characters - good enough for coarse lexical scoring and for
    "does this text mention this character at all" checks.

    Args:
        text: Source text.

    Returns:
        A list of tokens.
    """
    lowered = (text or "").lower()
    words = re.findall(r"[a-z0-9_]+", lowered)
    cjk = re.findall(r"[\u4e00-\u9fff]", lowered)
    return words + cjk


def topic_tokens(text: str) -> set[str]:
    """Extract topical tokens, using CJK bigrams rather than single characters.

    Single CJK characters are far too common to indicate a shared subject: with
    them, "面试有点紧张" and "询问面试结果" look unrelated. Bigrams make the
    overlap meaningful, which is what the protocol layer needs in order to decide
    whether the user has just answered the question the Runtime was about to ask.

    Args:
        text: Source text.

    Returns:
        A set of lowercase Latin words and CJK bigrams.
    """
    lowered = (text or "").lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens
