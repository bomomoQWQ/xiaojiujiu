"""Emotion system: event appraisal, background mood and emotional impact events.

Three layers, matching the architecture:

1. **Event appraisal** (:func:`appraise_event`) - a cheap, rule-based
   classification of *what kind of thing happened to the character*. It must not
   output final emotion values; it outputs direction, impact, activation,
   uncertainty, relation signal and responsibility.
2. **Dynamics** (:func:`tick_emotions`, :func:`apply_new_emotion_events`) - plain
   code combining values, current mood, the user model and existing emotion
   events into mood movement and decaying impact events.
3. **Explanation** (:class:`EmotionExplainer`) - translates structured state into
   first-person psychological language. Templates by default, with an optional
   semantic provider; it may never modify state.

``semantic_label = None`` is a legitimate state: the Runtime can know that
something was a moderately negative, relation-relevant impact before it knows
whether it was disappointment, anxiety or shame.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Sequence

from .config import EmotionConfig, RuntimeConfig
from .projections import EmotionProjection
from .typing import (
    EmotionDirection,
    EmotionEvaluation,
    EmotionEvent,
    EventType,
    RawEvent,
    RuntimeState,
    new_id,
)
from .utility import clamp, exponential_decay

LOGGER = logging.getLogger("companion_runtime.emotion")


@dataclass(slots=True)
class Signal:
    """A lexicon entry used by the rule-based appraiser."""

    needle: str
    direction: str
    impact: float
    activation: float
    relation_signal: str = "neutral"
    responsibility: str = "unclear"
    label: str | None = None


#: Small bilingual lexicon. Order matters: the first match wins, so the more
#: specific / more intense phrases come first.
SIGNAL_LEXICON: tuple[Signal, ...] = (
    # --- explicit boundaries (handled by the boundary machine, but they also sting)
    Signal("不要主动", EmotionDirection.NEGATIVE.value, 0.55, 0.35, "distance", "other", "被拒绝"),
    Signal("别主动", EmotionDirection.NEGATIVE.value, 0.55, 0.35, "distance", "other", "被拒绝"),
    Signal("别一直问", EmotionDirection.NEGATIVE.value, 0.62, 0.45, "distance", "self", "被责备"),
    Signal("don't message me", EmotionDirection.NEGATIVE.value, 0.55, 0.35, "distance", "other", "rejection"),
    Signal("stop asking", EmotionDirection.NEGATIVE.value, 0.62, 0.45, "distance", "self", "rebuke"),
    # --- strong negative life events
    Signal("家里出事", EmotionDirection.NEGATIVE.value, 0.85, 0.80, "crisis", "third_party", "担忧"),
    Signal("很难受", EmotionDirection.NEGATIVE.value, 0.70, 0.60, "sorrow", "third_party", "心疼"),
    Signal("出事了", EmotionDirection.NEGATIVE.value, 0.78, 0.72, "crisis", "third_party", "担忧"),
    Signal("去世", EmotionDirection.NEGATIVE.value, 0.90, 0.70, "loss", "third_party", "哀伤"),
    Signal("生病", EmotionDirection.NEGATIVE.value, 0.65, 0.50, "worry", "third_party", "担心"),
    Signal("难过", EmotionDirection.NEGATIVE.value, 0.62, 0.55, "sorrow", "third_party", "心疼"),
    Signal("崩溃", EmotionDirection.NEGATIVE.value, 0.80, 0.75, "sorrow", "third_party", "心疼"),
    Signal("好累", EmotionDirection.NEGATIVE.value, 0.48, 0.38, "fatigue", "third_party", "心疼"),
    Signal("好烦", EmotionDirection.NEGATIVE.value, 0.45, 0.45, "fatigue", "third_party", "共情"),
    Signal("工作很多", EmotionDirection.NEGATIVE.value, 0.25, 0.20, "busy", "third_party", None),
    Signal("很忙", EmotionDirection.NEGATIVE.value, 0.22, 0.18, "busy", "third_party", None),
    Signal("没时间", EmotionDirection.NEGATIVE.value, 0.35, 0.25, "busy", "third_party", "失落"),
    Signal("不来了", EmotionDirection.NEGATIVE.value, 0.42, 0.30, "distance", "third_party", "失落"),
    Signal("自己待着", EmotionDirection.NEGATIVE.value, 0.30, 0.22, "distance", "third_party", "失落"),
    Signal("没发现", EmotionDirection.NEGATIVE.value, 0.55, 0.45, "guilt", "self", "愧疚"),
    Signal("对不起", EmotionDirection.NEGATIVE.value, 0.40, 0.45, "apology", "third_party", None),
    Signal("算了", EmotionDirection.NEGATIVE.value, 0.30, 0.25, "uncertain", "third_party", "不确定"),
    Signal("随便", EmotionDirection.NEGATIVE.value, 0.22, 0.20, "uncertain", "third_party", "不确定"),
    Signal("sorry", EmotionDirection.NEGATIVE.value, 0.40, 0.45, "apology", "third_party", None),
    Signal("太累了", EmotionDirection.NEGATIVE.value, 0.50, 0.40, "fatigue", "third_party", "心疼"),
    # --- positive signals
    Signal("面试过啦", EmotionDirection.POSITIVE.value, 0.80, 0.75, "good_news", "third_party", "高兴"),
    Signal("过了", EmotionDirection.POSITIVE.value, 0.55, 0.50, "good_news", "third_party", "高兴"),
    Signal("成功了", EmotionDirection.POSITIVE.value, 0.72, 0.70, "good_news", "third_party", "高兴"),
    Signal("好消息", EmotionDirection.POSITIVE.value, 0.65, 0.60, "good_news", "third_party", "高兴"),
    Signal("考上", EmotionDirection.POSITIVE.value, 0.72, 0.70, "good_news", "third_party", "高兴"),
    Signal("谢谢", EmotionDirection.POSITIVE.value, 0.45, 0.40, "appreciation", "third_party", "被感激"),
    Signal("谢谢你", EmotionDirection.POSITIVE.value, 0.55, 0.50, "appreciation", "third_party", "被感激"),
    Signal("喜欢你", EmotionDirection.POSITIVE.value, 0.75, 0.70, "closeness", "third_party", "喜悦"),
    Signal("想你", EmotionDirection.POSITIVE.value, 0.68, 0.65, "closeness", "third_party", "喜悦"),
    Signal("多主动", EmotionDirection.POSITIVE.value, 0.60, 0.50, "closeness", "third_party", "被接纳"),
    Signal("在吗", EmotionDirection.POSITIVE.value, 0.30, 0.45, "contact", "third_party", None),
    Signal("哈哈", EmotionDirection.POSITIVE.value, 0.38, 0.45, "amusement", "third_party", "愉快"),
    Signal("开心", EmotionDirection.POSITIVE.value, 0.52, 0.50, "good_news", "third_party", "高兴"),
    Signal("thank you", EmotionDirection.POSITIVE.value, 0.45, 0.40, "appreciation", "third_party", None),
    Signal("miss you", EmotionDirection.POSITIVE.value, 0.68, 0.65, "closeness", "third_party", None),
)


def _match_signal(text: str) -> Signal | None:
    """Return the first lexicon entry present in ``text``.

    Args:
        text: User-visible content to scan.

    Returns:
        The matching :class:`Signal`, or ``None``.
    """
    lowered = (text or "").lower()
    if not lowered:
        return None
    for signal in SIGNAL_LEXICON:
        if signal.needle.lower() in lowered:
            return signal
    return None


def appraise_event(
    event: RawEvent,
    *,
    state: RuntimeState,
    config: EmotionConfig,
    user_busy_probability: float = 0.0,
) -> EmotionEvaluation:
    """Classify what kind of thing an event was for the character.

    This is deliberately rule-based so the Runtime runs with zero models
    (degradation Level 0). A semantic provider may replace it later; the output
    contract is identical.

    Args:
        event: The raw event to appraise.
        state: Current runtime state (values modulate sensitivity).
        config: Emotion configuration.
        user_busy_probability: Current belief that the user is busy, used to
            dampen negative attributions.

    Returns:
        An :class:`EmotionEvaluation` with no final emotion values.
    """
    values = state.values
    text = event.content or ""
    metadata = event.metadata or {}

    if event.event_type == EventType.TOOL_RESULT.value:
        success = bool(metadata.get("success", True))
        return EmotionEvaluation(
            direction=EmotionDirection.POSITIVE.value if success else EmotionDirection.NEGATIVE.value,
            impact=0.20 if success else 0.35,
            activation=0.15,
            uncertainty=0.20,
            relation_signal="neutral",
            responsibility="tool",
            confidence=0.9,
            source="rule",
        )

    if event.event_type in {EventType.BOUNDARY_DECLARED.value, EventType.BOUNDARY_REVOKED.value}:
        negative = event.event_type == EventType.BOUNDARY_DECLARED.value
        return EmotionEvaluation(
            direction=EmotionDirection.NEGATIVE.value if negative else EmotionDirection.POSITIVE.value,
            impact=0.40 if negative else 0.25,
            activation=0.30,
            uncertainty=0.15,
            relation_signal="distance" if negative else "closeness",
            responsibility="user",
            confidence=0.85,
            source="rule",
        )

    if event.event_type == EventType.ASSISTANT_MESSAGE.value:
        # The character's own words are not evidence about the world. They only
        # matter through the user's later reaction, which arrives separately.
        return EmotionEvaluation(
            direction=EmotionDirection.NEUTRAL.value,
            impact=0.0,
            activation=0.0,
            uncertainty=0.5,
            relation_signal="self",
            responsibility="self",
            confidence=0.5,
            source="rule",
        )

    signal = _match_signal(text)
    if signal is None:
        # Unknown content: stay agnostic rather than inventing an interpretation.
        return EmotionEvaluation(
            direction=EmotionDirection.NEUTRAL.value,
            impact=0.05,
            activation=0.10,
            uncertainty=0.60,
            relation_signal="neutral",
            responsibility="unclear",
            confidence=0.30,
            source="rule",
        )

    impact = signal.impact
    activation = signal.activation
    uncertainty = 0.35

    # Values modulate sensitivity, never the facts themselves.
    sensitivity = 1.0
    if signal.direction == EmotionDirection.NEGATIVE.value:
        sensitivity *= 0.6 + 0.8 * values.relationship_maintenance
        sensitivity *= 0.7 + 0.6 * values.stability_commitment
        sensitivity *= 1.0 - 0.25 * values.autonomy
    else:
        sensitivity *= 0.6 + 0.8 * values.user_care
        sensitivity *= 0.7 + 0.5 * values.emotional_expression

    # A busy user explains the signal away: the fact stands, the weight drops.
    attribution_damping = 1.0 - 0.55 * clamp(user_busy_probability) if signal.direction == EmotionDirection.NEGATIVE.value else 1.0
    impact = clamp(impact * sensitivity * attribution_damping * config.event_reactivity)
    activation = clamp(activation * sensitivity * config.event_reactivity)

    if signal.relation_signal in {"distance", "uncertain", "busy"}:
        uncertainty = clamp(uncertainty + 0.25 * user_busy_probability + 0.1)

    return EmotionEvaluation(
        direction=signal.direction,
        impact=impact,
        activation=activation,
        uncertainty=uncertainty,
        relation_signal=signal.relation_signal,
        responsibility=signal.responsibility,
        confidence=0.75 if text else 0.3,
        source="rule",
    )


@dataclass(slots=True)
class EmotionTickResult:
    """Outcome of decaying the active emotion events."""

    decayed_ids: list[str]
    mood_excess_valence: float
    mood_excess_arousal: float


def tick_emotions(
    *,
    active: Sequence[EmotionEvent],
    state: RuntimeState,
    config: EmotionConfig,
    dt_seconds: float,
) -> list[EmotionEvent]:
    """Decay active emotion events in place by ``dt_seconds``.

    Events below the retirement threshold after decay are dropped from the
    returned list (the caller deactivates them in the database).

    Args:
        active: Currently active emotion events.
        state: Runtime state (unused for scaling but kept for future use).
        config: Emotion configuration.
        dt_seconds: Elapsed seconds since the last tick.

    Returns:
        The still-active events with updated intensities.
    """
    if dt_seconds <= 0.0:
        return list(active)
    survivors: list[EmotionEvent] = []
    for event in active:
        factor = exponential_decay(event.decay_rate, dt_seconds)
        event.intensity = event.intensity * factor
        event.activation = event.activation * factor
        if event.intensity >= config.emotion_retire_threshold:
            survivors.append(event)
    return survivors


def apply_new_emotion_events(
    *,
    evaluations: Sequence[tuple[RawEvent, EmotionEvaluation]],
    active: Sequence[EmotionEvent],
    state: RuntimeState,
    config: EmotionConfig,
) -> tuple[list[EmotionEvent], list[EmotionEvent]]:
    """Turn appraisals into mood motion and new impact events.

    Mood is pulled toward the sign of each active impact, weighted by its
    intensity relative to the current mood, and then relaxes back toward a
    neutral baseline. Arousal moves with activation. Intensity of each event is
    pulled slightly toward the mood it produced, so repeated events settle
    instead of diverging.

    Args:
        evaluations: ``(source event, appraisal)`` pairs to fold in.
        active: Existing active impact events.
        state: Runtime state (mutated in place).
        config: Emotion configuration.

    Returns:
        ``(mood_changed, new_events)``.
    """
    created: list[EmotionEvent] = []
    mood_changed = False
    values = state.values

    for source, evaluation in evaluations:
        if evaluation.impact <= config.min_event_impact:
            # Too small to be worth an impact event; the appraisal itself is still
            # available to callers and to the working situation.
            continue
        signed = evaluation.impact
        if evaluation.direction == EmotionDirection.NEGATIVE.value:
            signed = -signed
        elif evaluation.direction == EmotionDirection.NEUTRAL.value:
            signed = 0.0

        # Long-memory residue: relation-relevant events with high stability
        # orientation decay more slowly.
        decay_rate = config.emotion_decay_rate
        if evaluation.relation_signal in {"distance", "uncertain", "guilt", "loss"}:
            decay_rate *= 1.0 - 0.35 * values.stability_commitment
        decay_rate = max(0.005, decay_rate)

        event = EmotionEvent(
            emotion_event_id=new_id("emotion"),
            source_event_id=source.event_id,
            direction=evaluation.direction,
            intensity=evaluation.impact,
            activation=evaluation.activation,
            target="user" if source.actor != "runtime" else "self",
            semantic_label=None,
            created_at=source.timestamp,
            decay_rate=decay_rate,
        )
        created.append(event)

        delta_v = signed * config.valence_pull_gain * (1.0 - 0.5 * abs(state.mood_valence))
        delta_a = evaluation.activation * config.arousal_pull_gain
        state.mood_valence = clamp(state.mood_valence + delta_v, -1.0, 1.0)
        state.mood_arousal = clamp(state.mood_arousal + delta_a - 0.02, 0.0, 1.0)
        state.mood_stability = clamp(
            state.mood_stability - 0.5 * config.stability_pull_gain * evaluation.impact,
            0.0,
            1.0,
        )
        mood_changed = True

    # Existing events keep shaping mood so that mood reflects the whole stack.
    if active and not created:
        for event in active:
            signed = event.signed_intensity
            state.mood_valence = clamp(
                state.mood_valence + signed * config.valence_pull_gain * 0.25, -1.0, 1.0
            )
    return (created if mood_changed else []), created


def mood_relax(state: RuntimeState, config: EmotionConfig, dt_seconds: float) -> None:
    """Relax background mood toward baseline and update stability.

    Args:
        state: Runtime state (mutated in place).
        config: Emotion configuration.
        dt_seconds: Elapsed seconds.
    """
    if dt_seconds <= 0.0:
        return
    pull = config.mood_recovery_rate * min(dt_seconds, 86400.0)
    recovery = min(0.9, pull)
    state.mood_valence = clamp(state.mood_valence * (1.0 - recovery), -1.0, 1.0)
    state.mood_arousal = clamp(state.mood_arousal * (1.0 - recovery * 1.2), 0.0, 1.0)
    state.mood_stability = clamp(state.mood_stability + (0.70 - state.mood_stability) * recovery * 0.5, 0.0, 1.0)


# --------------------------------------------------------------------------------------
# Explanation layer
# --------------------------------------------------------------------------------------


class EmotionSemanticProvider(Protocol):
    """Optional port for a local small model that writes the心理 explanation."""

    def explain(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return first-person psychological language for ``payload``."""
        ...


TEMPLATES_POSITIVE = (
    "心里是松的，也有点高兴。",
    "有一点被回应到的感觉，情绪往上走。",
    "有些轻快，愿意多说几句。",
)
TEMPLATES_NEGATIVE = (
    "有些失落，也有一点不确定。",
    "情绪往下沉了一点，还压着没说。",
    "有点不是滋味，但还不至于表现出来。",
)
TEMPLATES_NEUTRAL = (
    "还算平静，只是在留意接下来的走向。",
    "没什么起伏，保持着观察。",
)


class EmotionExplainer:
    """Translates structured state into first-person psychological context.

    The explainer has no write access to Runtime state. Its output is injected
    into the main LLM prompt for one turn and then discarded.
    """

    def __init__(
        self,
        projection: EmotionProjection,
        config: RuntimeConfig,
        provider: EmotionSemanticProvider | None = None,
    ) -> None:
        """Store the projection, configuration and optional semantic provider."""
        self._projection = projection
        self._config = config
        self._provider = provider

    @staticmethod
    def cache_key(state: RuntimeState, active: Sequence[EmotionEvent]) -> str:
        """Return a coarse cache key that changes only on meaningful movement."""
        top = max((e.intensity for e in active), default=0.0)
        sign = "+" if state.mood_valence >= 0 else "-"
        return "|".join(
            [
                f"v{round(state.mood_valence, 1)}",
                f"a{round(state.mood_arousal, 1)}",
                f"i{round(state.approach_impulse, 1)}",
                f"r{round(state.restraint, 1)}",
                f"p{round(state.pressure, 1)}",
                f"m{top:.1f}",
                sign,
            ]
        )

    @staticmethod
    def should_re_explain(
        previous_key: str | None, current_key: str, threshold_changes: int = 1
    ) -> bool:
        """Return whether the psychological state moved enough to re-explain.

        Args:
            previous_key: Cache key of the last explanation.
            current_key: Cache key for the current state.
            threshold_changes: How many differing key segments trigger a refresh.

        Returns:
            ``True`` when a re-explanation is warranted.
        """
        if not previous_key:
            return True
        previous = previous_key.split("|")
        current = current_key.split("|")
        differing = sum(1 for a, b in zip(previous, current) if a != b)
        return differing >= threshold_changes

    def explain(
        self,
        *,
        state: RuntimeState,
        active: Sequence[EmotionEvent],
        now: datetime,
        force: bool = False,
        rng: random.Random | None = None,
    ) -> dict[str, Any]:
        """Return the current psychological context, using the cache when possible.

        Args:
            state: Current runtime state.
            active: Active emotion events.
            now: Reference time.
            force: Bypass the cache.
            rng: Random source used for template variety.

        Returns:
            A mapping with ``experience``, ``focus``, ``conflict``, ``impulse``,
            ``inhibition`` and ``expression`` keys, plus ``source`` and ``cache_hit``.
        """
        key = self.cache_key(state, active)
        if not force:
            cached = self._projection.cached_explanation(
                key, now, self._config.task.explain_cache_ttl_seconds
            )
            if cached is not None:
                return dict(cached) | {"cache_hit": True, "cache_key": key}

        payload = self._build_input(state, active)
        result = self._render(payload, rng or random.Random(0))
        result["source"] = "semantic" if self._provider is not None else "template"
        result["cache_hit"] = False
        result["cache_key"] = key
        return result

    def explain_and_store(
        self,
        connection: sqlite3.Connection,
        *,
        state: RuntimeState,
        active: Sequence[EmotionEvent],
        now: datetime,
        force: bool = False,
        rng: random.Random | None = None,
    ) -> dict[str, Any]:
        """Like :meth:`explain`, but persists the result in the explanation cache."""
        result = self.explain(state=state, active=active, now=now, force=force, rng=rng)
        if not result.get("cache_hit"):
            stored = dict(result)
            stored.pop("cache_hit", None)
            self._projection.store_explanation(
                connection,
                cache_key=str(result["cache_key"]),
                payload=stored,
                source=str(result.get("source", "template")),
                now=now,
            )
        return result

    def _build_input(self, state: RuntimeState, active: Sequence[EmotionEvent]) -> dict[str, Any]:
        """Assemble the structured input the explainer works from."""
        dominant = max(active, key=lambda e: e.intensity, default=None)
        return {
            "background_mood": {
                "valence": round(state.mood_valence, 3),
                "arousal": round(state.mood_arousal, 3),
                "stability": round(state.mood_stability, 3),
            },
            "active_emotions": [
                {
                    "target": e.target,
                    "direction": e.direction,
                    "intensity": round(e.intensity, 3),
                    "semantic_label": e.semantic_label,
                }
                for e in sorted(active, key=lambda e: e.intensity, reverse=True)[:4]
            ],
            "dominant": None
            if dominant is None
            else {
                "direction": dominant.direction,
                "intensity": round(dominant.intensity, 3),
                "label": dominant.semantic_label,
            },
            "approach_impulse": round(state.approach_impulse, 3),
            "restraint": round(state.restraint, 3),
            "pressure": round(state.pressure, 3),
        }

    def _render(self, payload: dict[str, Any], rng: random.Random) -> dict[str, Any]:
        """Render the explanation, delegating to the provider when available."""
        if self._provider is not None:
            try:
                provided = self._provider.explain(payload)
                required = {"experience", "impulse", "inhibition"}
                if required.issubset(provided.keys()):
                    return {
                        "experience": str(provided.get("experience", "")),
                        "focus": str(provided.get("focus", "")),
                        "conflict": str(provided.get("conflict", "")),
                        "impulse": str(provided.get("impulse", "")),
                        "inhibition": str(provided.get("inhibition", "")),
                        "expression": str(provided.get("expression", "")),
                    }
                LOGGER.warning("Emotion provider returned an incomplete payload; using templates")
            except Exception:  # pragma: no cover - defensive boundary around a model
                LOGGER.exception("Emotion provider failed; falling back to templates")

        return self._render_template(payload, rng)

    def _render_template(self, payload: dict[str, Any], rng: random.Random) -> dict[str, Any]:
        """Compose the explanation from deterministic templates."""
        mood = payload["background_mood"]
        valence = float(mood["valence"])
        impulse = float(payload["approach_impulse"])
        restraint = float(payload["restraint"])
        pressure = float(payload["pressure"])
        dominant = payload.get("dominant")

        if valence > 0.12:
            experience = rng.choice(TEMPLATES_POSITIVE)
        elif valence < -0.12:
            experience = rng.choice(TEMPLATES_NEGATIVE)
        else:
            experience = rng.choice(TEMPLATES_NEUTRAL)

        if dominant is not None:
            label = dominant.get("label")
            if label:
                focus = f"现在最占位置的是「{label}」这种感受。"
            elif dominant["direction"] == EmotionDirection.NEGATIVE.value:
                focus = "有一件事压着，具体叫什么还说不上来。"
            else:
                focus = "有一件事让人心里是暖的。"
        else:
            focus = "没有特别占位置的事，注意力是散的。"

        if impulse > restraint + 0.15:
            conflict = "很想靠近，但知道现在未必合适。"
            impulse_text = "想主动说点什么。"
        elif pressure > 0.45:
            conflict = "压着不说已经有点难受了。"
            impulse_text = "想确认一下对方还在不在。"
        else:
            conflict = "暂时没有明显的拉扯。"
            impulse_text = "没有非做不可的冲动。"

        if restraint > 0.68:
            inhibition = "不希望给对方增加压力，所以会克制。"
            expression = "表达上会偏收着，话不多但留有余地。"
        elif restraint < 0.35:
            inhibition = "顾虑不多，愿意直接说出来。"
            expression = "表达上会更直接一点。"
        else:
            inhibition = "会看情况决定表达多少。"
            expression = "表达上保持平常的分寸。"

        return {
            "experience": experience,
            "focus": focus,
            "conflict": conflict,
            "impulse": impulse_text,
            "inhibition": inhibition,
            "expression": expression,
        }
