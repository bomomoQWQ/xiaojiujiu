"""User interaction model: simplified hierarchical Bayesian prediction.

The model answers a single question: *if the character does this, in this
situation, how will this particular user probably react?* It is not a "liking
score".

For each candidate behaviour it predicts four quantities plus an uncertainty:

* ``reply_probability``     - will the user respond at all
* ``positive_probability``  - given a reply, is it positive
* ``continue_probability``  - given a reply, does the interaction continue
* ``boundary_risk``         - probability this touches a boundary
* ``uncertainty``           - how much the estimate can be trusted

Implementation notes (intentionally simplified, structurally faithful):

* four logistic models, each ``P = sigmoid(theta^T x)`` over shared features;
* hierarchical shrinkage: a per-behaviour offset ``delta_behaviour`` learned on
  top of the global vector, so sparse behaviour classes fall back to general
  knowledge;
* evidence weighting ``w = w_source * w_attribution * w_semantic * w_recency``;
* Gaussian prior plus diagonal Laplace-style precision so the model can report a
  conservative lower quantile (used for boundary-risky candidates);
* slow drift ``Theta_t ~ N(Theta_{t-1}, Q dt)`` implemented as exponential
  forgetting of precision, so stale knowledge loses confidence instead of
  disappearing. Drift now runs on two paths: :meth:`UserInteractionModel.tick_drift`
  ages the parameters by the number of seconds that actually elapsed (the Runtime
  calls it from its time passage), and the older per-observation nudge in
  :meth:`UserInteractionModel._apply_drift` is unchanged, so a Runtime that never
  ticks still ages its beliefs instead of freezing them;
* reply speed is judged *relative to this user's own habit*: an exponential moving
  average of ``log1p(reply_delay_seconds)``, persisted alongside the other
  parameters, is the baseline and each observed delay becomes a z-score against it.
  Until :data:`REPLY_DELAY_BASELINE_MIN_SAMPLES` delays have been seen the baseline
  is not trusted and the absolute ``default_reply_delay_seconds`` is used instead.
  Reply *length* and turn count are still compared against absolute thresholds.

Crucially, an observation is *not* an attribution: "6 hours without a reply" is
recorded as ``reply_delay = 21600`` and gets a tiny weight, never as
``feedback = negative``. A *late* reply is weaker positive evidence, not negative
evidence: the relative-delay term can only take back the bonus the other signals
earned, it can never push the positive target below neutral.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .config import RuntimeConfig
from .projections import UserModelProjection
from .typing import InteractionObservation, new_id
from .utility import clamp, exponential_decay, sigmoid, utcnow

LOGGER = logging.getLogger("companion_runtime.user_model")

#: Shared feature vector layout. Keep the order stable: it is persisted.
FEATURE_NAMES: tuple[str, ...] = (
    "bias",
    "proactive",
    "follow_up",
    "emotional_expression",
    "question",
    "topic_shift",
    "busy",
    "recent_contact_ratio",
    "hours_since_contact",
    "collision",
    "after_boundary",
    "novelty",
    "explicit_permission",
)

TARGET_NAMES: tuple[str, ...] = (
    "reply_probability",
    "positive_probability",
    "continue_probability",
    "boundary_risk",
)

#: Behaviour classes used for the hierarchical offset.
BEHAVIOUR_CLASSES: tuple[str, ...] = (
    "proactive_contact",
    "follow_up",
    "curious_question",
    "emotional_expression",
    "repair",
    "reply",
)

#: Reasonable starting beliefs; symmetric where the Runtime should stay agnostic.
#: The ``boundary_risk`` cold start is deliberately *neutral* rather than
#: suspicious: risk rises only once a boundary is actually known
#: (``after_boundary``) or the user is provably busy.
DEFAULT_THETA: dict[str, tuple[float, ...]] = {
    "reply_probability": (0.30, -0.35, 0.25, -0.10, 0.20, 0.05, -0.60, -1.10, 0.25, -0.15, -0.30, 0.05, 0.80),
    "positive_probability": (0.35, 0.05, 0.10, -0.10, 0.05, -0.05, -0.45, -0.50, 0.05, -0.20, -0.55, 0.05, 0.60),
    "continue_probability": (0.20, 0.05, 0.20, -0.05, 0.15, -0.05, -0.40, -0.55, 0.10, -0.20, -0.35, 0.05, 0.45),
    "boundary_risk": (-2.20, 0.55, 0.15, 0.15, 0.25, -0.05, 0.30, 0.75, 0.15, 0.35, 1.60, 0.20, -0.90),
}

#: Behaviour-class name mapping from candidate types.
TYPE_TO_BEHAVIOUR: Mapping[str, str] = {
    "contact": "proactive_contact",
    "follow_up": "follow_up",
    "check_in": "proactive_contact",
    "question": "curious_question",
    "curious_question": "curious_question",
    "share": "emotional_expression",
    "emotional_expression": "emotional_expression",
    "repair": "repair",
    "apology": "repair",
    "reply": "reply",
}

#: Candidate types whose behaviour is a probe - design §22.1's "是否追问". A property of
#: what the character chose to do, never of how the sentence happens to be punctuated.
QUESTION_TYPES: frozenset[str] = frozenset(
    {"follow_up", "check_in", "question", "curious_question"}
)

#: Candidate types that expose feeling - design §22.1's "情绪暴露程度". Both names of the
#: same behaviour class are listed: :data:`TYPE_TO_BEHAVIOUR` maps ``share`` and
#: ``emotional_expression`` onto one class, so treating only the first as exposure made
#: the feature disagree with the behaviour class it is meant to describe.
EMOTIONAL_EXPRESSION_TYPES: frozenset[str] = frozenset({"share", "emotional_expression"})

#: Candidate types that move the subject - design §22.1's "话题".
TOPIC_SHIFT_TYPES: frozenset[str] = frozenset({"curious_question"})

#: The members of :data:`FEATURE_NAMES` that come from ``A`` alone (design §22.1); the
#: rest of the vector comes from ``C``/``Z``. ``follow_up`` is listed because
#: :func:`extract_features` derives it from ``type``: it is a property of the behaviour,
#: so no caller may supply it either.
ACTION_FEATURE_NAMES: tuple[str, ...] = (
    "proactive",
    "follow_up",
    "emotional_expression",
    "question",
    "topic_shift",
)

#: Keys :func:`describe_supplied_action` always takes from the canonical encoder, so a
#: host cannot describe a behaviour inconsistently with the type it reports.
_CANONICAL_ACTION_KEYS: frozenset[str] = frozenset(ACTION_FEATURE_NAMES) | {"type"}


def describe_action(*, type: str, proactive: bool) -> dict[str, Any]:
    """Build the canonical action record ``A`` for one behaviour (design §22.1).

    Every path that produces an ``A`` goes through here. Design §25 features a candidate
    as ``x = phi(A, C, Z)`` and §27 reuses that same ``X_i`` in the posterior, so a
    behaviour described one way when it is predicted and another way when its outcome is
    observed teaches the model about a feature vector it never scored: the ``theta`` for
    that feature is pulled towards a value learned from inputs the prediction never used.

    That is not hypothetical. The prediction path passed ``emotional_expression``,
    ``question`` and ``topic_shift``; every observation path passed only ``type`` and
    ``proactive``, and two of them derived ``question`` from an ASCII ``"?"`` in the
    intent text. No candidate template contains one, so the observed feature was ``0.0``
    for every real behaviour while the predicted feature was ``1.0`` for a follow-up -
    including the design document's own example, ``{"type": "follow_up", "intent":
    "询问用户今天的面试结果"}`` (design §39).

    Args:
        type: The candidate's type. An unknown type is described conservatively (no
            exposure, no probe, no topic shift) rather than optimistically.
        proactive: Whether the character initiated this, i.e.
            :func:`~companion_runtime.candidate.is_candidate_proactive`. It is passed in
            rather than derived here because that predicate is the hard-boundary gate's
            authority and has to stay in one place.

    Returns:
        A mapping carrying the action-derived members of :data:`FEATURE_NAMES`.
        ``length`` (design §22.1's "消息长度") is deliberately absent: it is not a
        feature, and a key that :func:`extract_features` silently ignores is worse than
        no key at all.
    """
    kind = str(type or "contact")
    return {
        "type": kind,
        "proactive": bool(proactive),
        "emotional_expression": kind in EMOTIONAL_EXPRESSION_TYPES,
        "question": kind in QUESTION_TYPES,
        "topic_shift": kind in TOPIC_SHIFT_TYPES,
    }


def describe_supplied_action(action: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the canonical ``A`` for a behaviour a *caller* described.

    The public observation endpoint lets a host report what the character did, and the
    host is not the authority on how a behaviour maps onto features - design §25's ``phi``
    is. Passing the payload through verbatim reopened the same divergence at the API
    boundary: ``{"type": "share", "proactive": true}`` carried no
    ``emotional_expression`` and was therefore learned as if the character had shown
    nothing, while the same candidate scored ``1.0`` when the Runtime predicted it.

    Unknown keys are preserved so the record stays extensible (design §79's version
    fields, or anything a future host wants to record); the action-derived features are
    recomputed from ``type``/``proactive`` and cannot be overridden.

    Args:
        action: The caller's description, or ``None``. A non-mapping is treated as
            absent rather than raising: the endpoint must not answer a malformed body
            with a 500, and "there was no description" is the honest reading.

    Returns:
        A canonicalised action record.
    """
    supplied = dict(action) if isinstance(action, Mapping) else {}
    canonical = describe_action(
        type=str(supplied.get("type") or "contact"),
        proactive=bool(supplied.get("proactive", True)),
    )
    extras = {
        key: value for key, value in supplied.items() if key not in _CANONICAL_ACTION_KEYS
    }
    return extras | canonical


# --------------------------------------------------------------------------------------
# Relative reply-delay scoring (§29)
#
# These are module constants rather than ``UserModelConfig`` fields because the two
# defects being fixed here must not touch ``config.py``; they are reported so the
# config owner can promote them to real knobs with the same defaults.
# --------------------------------------------------------------------------------------

#: Minimum number of observed reply delays before the per-user baseline is used for
#: scoring. With fewer samples the baseline is still being learned (one sample would
#: simply echo the observation back and teach nothing), so scoring falls back to the
#: absolute ``config.user_model.default_reply_delay_seconds`` reference instead.
REPLY_DELAY_BASELINE_MIN_SAMPLES = 3

#: Weight of the newest sample in the exponential moving average of
#: ``log1p(reply_delay_seconds)``. 0.25 makes the baseline roughly the mean of the
#: last four replies: fast enough to follow a real change of habit within a day or
#: two of chatting, slow enough that one unusual reply does not redefine "normal".
REPLY_DELAY_BASELINE_ALPHA = 0.25

#: Floor for the baseline's log-space standard deviation. A metronomic user has a
#: spread near zero and would otherwise register an enormous z-score for a difference
#: of a few seconds. ``0.34`` means "anything inside a factor of ~1.4 counts as the
#: same speed".
REPLY_DELAY_MIN_LOG_STDEV = 0.34

#: Spread assumed while the fallback (absolute) reference is in use. ``0.7`` is
#: roughly "a factor of two either way", i.e. the fallback reacts to order-of-magnitude
#: differences only - it is a prior, not a measurement.
REPLY_DELAY_FALLBACK_LOG_STDEV = 0.7

#: z-score magnitude at which the relative-delay signal saturates at +-1. Two log-space
#: standard deviations ("this reply is far outside their normal rhythm") is the most
#: the delay term is allowed to say.
REPLY_DELAY_Z_SCALE = 2.0

#: Largest absolute contribution of the relative-delay term to the
#: ``positive_probability`` target. 0.10 is the same order as the reply-length term
#: already in :meth:`UserInteractionModel._target_rewards`, so speed informs the
#: update without drowning the other observation channels.
REPLY_DELAY_TARGET_WEIGHT = 0.10


@dataclass(slots=True)
class Prediction:
    """Predicted user reaction to a candidate behaviour."""

    reply_probability: float = 0.5
    positive_probability: float = 0.5
    continue_probability: float = 0.5
    boundary_risk: float = 0.1
    uncertainty: float = 0.5
    observation_count: int = 0
    effective_count: float = 0.0
    behaviour_class: str = "proactive_contact"
    cold_start: bool = True
    features: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "reply_probability": round(self.reply_probability, 6),
            "positive_probability": round(self.positive_probability, 6),
            "continue_probability": round(self.continue_probability, 6),
            "boundary_risk": round(self.boundary_risk, 6),
            "uncertainty": round(self.uncertainty, 6),
            "observation_count": self.observation_count,
            "effective_count": round(self.effective_count, 3),
            "behaviour_class": self.behaviour_class,
            "cold_start": self.cold_start,
            "features": {k: round(v, 4) for k, v in self.features.items()},
        }


@dataclass(slots=True)
class EvidenceWeight:
    """Decomposition of the weight given to one observation."""

    source: float = 0.3
    attribution: float = 0.5
    semantic: float = 0.5
    recency: float = 1.0
    total: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-serialisable rendering."""
        return {
            key: round(float(value), 6)
            for key, value in dataclasses.asdict(self).items()
        }


@dataclass(slots=True)
class BehaviourReaction:
    """Purely observed outcome of one behaviour, before any attribution."""

    replied: bool = False
    reply_delay_seconds: float | None = None
    reply_length: int = 0
    continued_topic: bool = False
    asked_back: bool = False
    turns: int = 0
    explicit_negative: bool = False
    explicit_positive: bool = False
    busy_probability: float = 0.0
    boundary_touched: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable rendering."""
        return {
            "replied": self.replied,
            "reply_delay_seconds": self.reply_delay_seconds,
            "reply_length": self.reply_length,
            "continued_topic": self.continued_topic,
            "asked_back": self.asked_back,
            "turns": self.turns,
            "explicit_negative": self.explicit_negative,
            "explicit_positive": self.explicit_positive,
            "busy_probability": self.busy_probability,
            "boundary_touched": self.boundary_touched,
        }


def source_weight(reaction: BehaviourReaction, config: Any) -> float:
    """Return the evidence quality implied by the *source* of the observation.

    Explicit user statements dominate; a non-reply is a very weak signal.

    Args:
        reaction: Observed reaction.
        config: User-model configuration.

    Returns:
        A weight in ``(0, 1]``.
    """
    if reaction.explicit_positive:
        return config.explicit_positive_weight
    if reaction.explicit_negative:
        return config.explicit_negative_weight
    if not reaction.replied:
        return config.no_reply_weight
    if (reaction.reply_length or 0) <= 4:
        return min(config.implicit_weight, config.slow_reply_weight)
    return config.implicit_weight


def attribution_weight(reaction: BehaviourReaction, config: Any) -> float:
    """Return how much this observation can be attributed to the behaviour.

    If the user is very likely busy, a slow or missing reply says almost nothing
    about the behaviour, so the attribution weight collapses toward a floor:
    ``w_attribution = clamp(1 - P(busy), floor, 1)``.

    Args:
        reaction: Observed reaction.
        config: User-model configuration.

    Returns:
        A weight in ``[busy_attribution_floor, 1]``.
    """
    busy = clamp(reaction.busy_probability)
    floor = config.busy_attribution_floor
    return clamp(1.0 - busy, floor, 1.0)


def recency_weight(observed_at: datetime | None, now: datetime, half_life_days: float = 21.0) -> float:
    """Return a recency factor so old evidence loses influence smoothly."""
    if observed_at is None:
        return 1.0
    elapsed = max(0.0, (now - observed_at).total_seconds())
    return clamp(0.35 + 0.65 * exponential_decay(0.0, elapsed, half_life=half_life_days * 86400.0))


def compute_weight(
    reaction: BehaviourReaction,
    *,
    config: Any,
    observed_at: datetime | None,
    now: datetime,
    semantic_confidence: float = 0.6,
) -> EvidenceWeight:
    """Combine the four evidence weights into one.

    Args:
        reaction: Observed reaction.
        config: User-model configuration.
        observed_at: When the observation was made.
        now: Reference time.
        semantic_confidence: Confidence of the semantic reading of the reaction.

    Returns:
        An :class:`EvidenceWeight` whose ``total`` is ``w_source * w_attribution *
        w_semantic * w_recency``.
    """
    weight = EvidenceWeight(
        source=source_weight(reaction, config),
        attribution=attribution_weight(reaction, config),
        semantic=clamp(semantic_confidence),
        recency=recency_weight(observed_at, now),
    )
    weight.total = clamp(weight.source * weight.attribution * weight.semantic * weight.recency, 0.0, 1.0)
    return weight


def _clamp_seconds(value: float | None, default: float) -> float:
    """Return a usable delay in seconds."""
    if value is None or value < 0:
        return default
    return float(value)


def extract_features(
    *,
    action: Mapping[str, Any],
    context: Mapping[str, Any],
    config: Any,
) -> dict[str, float]:
    """Build the feature vector ``x = phi(A, C, Z)`` for a candidate behaviour.

    Args:
        action: Description of what the character would do.
        context: Situation description.
        config: User-model configuration.

    Returns:
        A mapping from :data:`FEATURE_NAMES` to floats.
    """
    proactive = 1.0 if action.get("proactive") else 0.0
    follow_up = 1.0 if action.get("type") in {"follow_up", "check_in"} else 0.0
    expression = 1.0 if action.get("emotional_expression") else 0.0
    question = 1.0 if action.get("question") else 0.0
    topic_shift = 1.0 if action.get("topic_shift") else 0.0
    busy = clamp(float(context.get("busy_probability", 0.0)))
    tolerance = max(1, int(getattr(config, "repeat_contact_tolerance", 2)))
    recent = clamp(float(context.get("recent_contact_count", 0)) / tolerance)
    hours_since = clamp(float(context.get("hours_since_contact", 0.0)) / 24.0)
    collision = 1.0 if context.get("user_active_now") else 0.0
    after_boundary = 1.0 if context.get("ever_boundary") else 0.0
    novelty = clamp(float(context.get("novelty", 0.5)))
    permission = 1.0 if context.get("explicit_permission") else 0.0
    return {
        "bias": 1.0,
        "proactive": proactive,
        "follow_up": follow_up,
        "emotional_expression": expression,
        "question": question,
        "topic_shift": topic_shift,
        "busy": busy,
        "recent_contact_ratio": recent,
        "hours_since_contact": hours_since,
        "collision": collision,
        "after_boundary": after_boundary,
        "novelty": novelty,
        "explicit_permission": permission,
    }


def behaviour_class_of(action: Mapping[str, Any]) -> str:
    """Map a candidate action onto its behaviour class."""
    kind = str(action.get("type") or "contact")
    return TYPE_TO_BEHAVIOUR.get(kind, "proactive_contact")


def default_parameter_block(prior_precision: float = 1.0) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the seed parameter and precision blocks for a fresh model.

    Args:
        prior_precision: Prior precision for every feature.

    Returns:
        ``(params, precision)``, both JSON-serialisable. This is shared with the
        storage layer so that a summary can be stored even before the model has
        ever been persisted.
    """
    params: dict[str, Any] = {name: list(values) for name, values in DEFAULT_THETA.items()}
    params["delta"] = {}
    # A fresh model has seen no reply delays yet: ``samples`` is 0, which is exactly
    # the state in which scoring falls back to the absolute reference delay.
    params["reply_delay_baseline"] = {
        "log_mean": 0.0,
        "log_variance": 0.0,
        "samples": 0,
    }
    precision = {name: [prior_precision] * len(FEATURE_NAMES) for name in TARGET_NAMES}
    return params, precision


def _vector(features: Mapping[str, float]) -> list[float]:
    """Convert a feature mapping into a vector in :data:`FEATURE_NAMES` order."""
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


class UserInteractionModel:
    """The learned model of how this user reacts to this character."""

    def __init__(self, projection: UserModelProjection, config: RuntimeConfig) -> None:
        """Load the persisted parameters (or seed them from the prior)."""
        self._projection = projection
        self._config = config
        self._theta: dict[str, list[float]] = {}
        self._precision: dict[str, list[float]] = {}
        self._delta: dict[str, dict[str, list[float]]] = {}
        #: How many observations have landed in each behaviour class. This is the
        #: only honest answer to "how much evidence does this class have": the length
        #: of a parameter vector is the number of *features*, which is a constant and
        #: therefore says nothing about evidence at all.
        self._class_counts: dict[str, int] = {}
        self._observations = 0
        self._effective_count = 0.0
        #: Reply-delay baseline, in log space: ``log1p(delay)`` is heavy-tailed (10
        #: minutes and 8 hours are the same "kind" of event to a user's habit), so the
        #: mean and variance are kept as ``log1p`` values and converted back only for
        #: display. ``_delay_samples`` is how many observed delays they are built from.
        self._delay_log_mean = 0.0
        self._delay_log_variance = 0.0
        self._delay_samples = 0
        self._summary: dict[str, Any] | None = None
        self._load()

    # ------------------------------------------------------------------ storage

    @property
    def projection(self) -> UserModelProjection:
        """Return the projection backing this model (read access)."""
        return self._projection

    @property
    def observations(self) -> int:
        """Return how many observations have been folded into the model."""
        return self._observations

    @property
    def effective_count(self) -> float:
        """Return the evidence-weighted sample size."""
        return self._effective_count

    def _load(self) -> None:
        """Load parameters from the projection, seeding defaults when absent."""
        stored = self._projection.get_params()
        if stored is None:
            self._theta = {name: list(values) for name, values in DEFAULT_THETA.items()}
            self._precision = {
                name: [self._config.user_model.prior_precision] * len(FEATURE_NAMES)
                for name in TARGET_NAMES
            }
            self._delta = {}
            self._reset_reply_delay_baseline()
            self._observations = 0
            self._effective_count = 0.0
            return
        params = stored.get("params_json") or {}
        precision = stored.get("precision_json") or {}
        for name in TARGET_NAMES:
            values = params.get(name) or list(DEFAULT_THETA[name])
            if len(values) != len(FEATURE_NAMES):
                LOGGER.warning("Discarding malformed parameter vector for %s", name)
                values = list(DEFAULT_THETA[name])
            self._theta[name] = [float(v) for v in values]
            stored_precision = precision.get(name) or [
                self._config.user_model.prior_precision
            ] * len(FEATURE_NAMES)
            if len(stored_precision) != len(FEATURE_NAMES):
                stored_precision = [self._config.user_model.prior_precision] * len(FEATURE_NAMES)
            self._precision[name] = [float(v) for v in stored_precision]
        self._delta = {
            cls: {
                name: [float(v) for v in vector]
                for name, vector in (params.get("delta") or {}).get(cls, {}).items()
                if len(vector) == len(FEATURE_NAMES)
            }
            for cls in BEHAVIOUR_CLASSES
        }
        self._class_counts = self._load_class_counts(params)
        self._load_reply_delay_baseline(params)
        self._observations = int(stored.get("observations") or 0)
        self._effective_count = float(stored.get("effective_count") or 0.0)
        self._summary = stored.get("last_summary_json") or None

    def _reset_reply_delay_baseline(self) -> None:
        """Forget the reply-delay baseline (fresh model, or a malformed stored row)."""
        self._delay_log_mean = 0.0
        self._delay_log_variance = 0.0
        self._delay_samples = 0

    def _load_reply_delay_baseline(self, params: Mapping[str, Any]) -> None:
        """Restore the persisted reply-delay baseline, when the row has one.

        A row written before this field existed has none, which reads as the
        cold-start state (no samples, absolute fallback) rather than as an error.
        Malformed or non-finite values are discarded, because a corrupted baseline
        would silently mis-score every later reply.
        """
        self._reset_reply_delay_baseline()
        raw = params.get("reply_delay_baseline")
        if not isinstance(raw, Mapping):
            return
        try:
            samples = max(0, int(raw.get("samples") or 0))
            mean = float(raw.get("log_mean") or 0.0)
            variance = max(0.0, float(raw.get("log_variance") or 0.0))
        except (TypeError, ValueError):
            LOGGER.warning("Discarding malformed reply-delay baseline")
            return
        if samples <= 0:
            return
        if not (math.isfinite(mean) and math.isfinite(variance)):
            LOGGER.warning("Discarding non-finite reply-delay baseline")
            return
        self._delay_log_mean = mean
        self._delay_log_variance = variance
        self._delay_samples = samples

    @staticmethod
    def _load_class_counts(params: Mapping[str, Any]) -> dict[str, int]:
        """Return the persisted per-class observation counts.

        A model stored before this field existed has none, so the counts are
        reconstructed from the behaviour offsets: an offset vector only exists once
        at least one observation of that class was applied, which makes 1 the honest
        lower bound and keeps an old database from claiming a class is well known.
        """
        raw = params.get("class_counts")
        counts: dict[str, int] = {}
        if isinstance(raw, Mapping):
            for cls in BEHAVIOUR_CLASSES:
                try:
                    counts[cls] = max(0, int(raw.get(cls) or 0))
                except (TypeError, ValueError):
                    counts[cls] = 0
        else:
            offsets = params.get("delta") or {}
            for cls in BEHAVIOUR_CLASSES:
                present = offsets.get(cls) if isinstance(offsets, Mapping) else None
                counts[cls] = 1 if isinstance(present, Mapping) and present else 0
        return counts

    def behaviour_evidence(self, behaviour_class: str) -> int:
        """Return how many observations the model has for one behaviour class."""
        return int(self._class_counts.get(behaviour_class, 0))

    def _persist(self, connection: sqlite3.Connection) -> None:
        """Persist the current parameters."""
        params: dict[str, Any] = {name: self._theta[name] for name in TARGET_NAMES}
        params["delta"] = {cls: self._delta.get(cls, {}) for cls in BEHAVIOUR_CLASSES if self._delta.get(cls)}
        params["class_counts"] = {
            cls: count for cls, count in self._class_counts.items() if count
        }
        params["reply_delay_baseline"] = {
            "log_mean": self._delay_log_mean,
            "log_variance": self._delay_log_variance,
            "samples": self._delay_samples,
        }
        self._projection.upsert_params(
            connection,
            params=params,
            precision={name: self._precision[name] for name in TARGET_NAMES},
            observations=self._observations,
            effective_count=self._effective_count,
            summary=self._summary,
        )

    # ------------------------------------------------- relative reply-delay baseline

    @property
    def reply_delay_baseline_samples(self) -> int:
        """Return how many observed reply delays the baseline is built from."""
        return int(self._delay_samples)

    @property
    def reply_delay_baseline_seconds(self) -> float | None:
        """Return this user's own typical reply delay in seconds, or ``None``.

        The baseline is an exponential moving average in ``log1p`` space, so this
        converts it back with ``expm1``; it is a *typical* delay (closer to a median
        than to an arithmetic mean), not an average of the raw seconds. ``None``
        means no reply delay has been observed yet - it does not mean zero seconds.
        """
        if self._delay_samples <= 0:
            return None
        return math.expm1(self._delay_log_mean)

    def delay_reference(self) -> tuple[float, float, bool]:
        """Return ``(reference, spread, baseline_trusted)`` for delay scoring.

        ``reference`` is what an observed delay is compared against, expressed in
        ``log1p`` seconds, and ``spread`` is the log-space standard deviation used to
        turn that comparison into a z-score. Until
        :data:`REPLY_DELAY_BASELINE_MIN_SAMPLES` delays have been observed the two
        are the absolute fallback (``default_reply_delay_seconds`` and
        :data:`REPLY_DELAY_FALLBACK_LOG_STDEV`) and ``baseline_trusted`` is ``False``.
        """
        if self._delay_samples >= REPLY_DELAY_BASELINE_MIN_SAMPLES:
            spread = max(REPLY_DELAY_MIN_LOG_STDEV, math.sqrt(max(0.0, self._delay_log_variance)))
            return self._delay_log_mean, spread, True
        default = max(1.0, float(self._config.user_model.default_reply_delay_seconds))
        return math.log1p(default), REPLY_DELAY_FALLBACK_LOG_STDEV, False

    def relative_delay_signal(self, reply_delay_seconds: float) -> float:
        """Return how fast one reply was *for this user*, in ``[-1, 1]``.

        ``+1`` means "far faster than this user normally replies", ``0`` means "about
        their usual speed", ``-1`` means "far slower". The score is a z-score of
        ``log1p(delay)`` against the baseline in :meth:`delay_reference`, divided by
        :data:`REPLY_DELAY_Z_SCALE` and clamped.

        Args:
            reply_delay_seconds: Measured delay; negative values are read as 0.

        Returns:
            The relative-speed signal. This is the exact number
            :meth:`_target_rewards` folds into the positive-probability target, so a
            diagnostic can explain an update without re-deriving it.
        """
        reference, spread, _ = self.delay_reference()
        observed = math.log1p(max(0.0, float(reply_delay_seconds)))
        z = (observed - reference) / max(1e-6, spread)
        return clamp(-z / REPLY_DELAY_Z_SCALE, -1.0, 1.0)

    def reply_delay_baseline_view(self) -> dict[str, Any]:
        """Return the reply-delay baseline and what scoring compares against.

        Additive diagnostic view (also embedded in :meth:`numeric_view`).
        ``mean_seconds`` is ``None`` until a reply delay has been observed;
        ``reference_seconds`` is the value currently used for scoring, which is the
        absolute fallback until the baseline has
        :data:`REPLY_DELAY_BASELINE_MIN_SAMPLES` samples.
        """
        reference, spread, trusted = self.delay_reference()
        return {
            "samples": self._delay_samples,
            "min_samples": REPLY_DELAY_BASELINE_MIN_SAMPLES,
            "mean_seconds": (
                None if self._delay_samples <= 0 else round(math.expm1(self._delay_log_mean), 3)
            ),
            "reference_seconds": round(math.expm1(reference), 3),
            "log_stdev": round(spread, 6),
            "trusted": trusted,
        }

    def _learn_reply_delay(self, reaction: BehaviourReaction) -> None:
        """Fold one observed reply delay into the per-user baseline.

        Only actual replies teach the baseline: a missing reply says nothing about how
        fast this user normally answers, and counting it would let a week of silence
        redefine "normal speed". A negative or absent delay is treated as unknown.
        """
        delay = reaction.reply_delay_seconds
        if not reaction.replied or delay is None or delay < 0.0:
            return
        sample = math.log1p(float(delay))
        if self._delay_samples <= 0:
            self._delay_log_mean = sample
            self._delay_log_variance = 0.0
            self._delay_samples = 1
            return
        deviation = sample - self._delay_log_mean
        self._delay_log_mean += REPLY_DELAY_BASELINE_ALPHA * deviation
        self._delay_log_variance += REPLY_DELAY_BASELINE_ALPHA * (
            deviation * deviation - self._delay_log_variance
        )
        self._delay_samples += 1

    # ---------------------------------------------------------------- prediction

    def _theta_for(self, target: str, behaviour_class: str) -> list[float]:
        """Return the effective parameter vector for a target and behaviour class."""
        base = self._theta[target]
        delta = self._delta.get(behaviour_class, {}).get(target)
        if not delta:
            return base
        return [b + d for b, d in zip(base, delta)]

    def _standard_error(self, target: str, behaviour_class: str, features: Mapping[str, float]) -> float:
        """Return an approximate standard error of the linear predictor.

        Uses the diagonal precision as a Laplace approximation: the variance of
        the linear predictor is ``sum_i x_i^2 / precision_i``.

        The precision of a behaviour class is its *own* evidence count, and the
        class's observations contribute directly to every parameter's precision
        rather than only to the parameters whose feature happened to be non-zero.
        Both parts matter: an earlier version scaled the precision by the length of
        the offset vector - that is, by the number of features, a constant - so a
        behaviour class that had never been observed was reported as confidently as
        one learned from dozens of observations, and the model never became less
        uncertain as the evidence arrived.
        """
        base_precision = self._precision[target]
        # One unit of evidence per observation in this class, plus the offset basis.
        shrink = 1.0 + float(self.behaviour_evidence(behaviour_class))
        variance = 0.0
        for index, value in enumerate(_vector(features)):
            precision = max(1e-6, base_precision[index] * shrink)
            variance += (value * value) / precision
        return math.sqrt(max(0.0, variance))

    def predict(
        self,
        *,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
        with_features: bool = True,
    ) -> Prediction:
        """Predict the user's reaction to a candidate behaviour.

        Args:
            action: What the character is considering doing.
            context: The situation the behaviour would occur in.
            with_features: Include the feature vector in the result.

        Returns:
            A :class:`Prediction`.
        """
        features = extract_features(action=action, context=context, config=self._config.user_model)
        vector = _vector(features)
        behaviour_class = behaviour_class_of(action)

        result = Prediction(
            behaviour_class=behaviour_class,
            observation_count=self._observations,
            effective_count=self._effective_count,
            cold_start=self._effective_count < 5.0,
            features=features if with_features else {},
        )
        errors: list[float] = []
        for target in TARGET_NAMES:
            theta = self._theta_for(target, behaviour_class)
            logit_value = sum(t * x for t, x in zip(theta, vector))
            probability = sigmoid(logit_value)
            errors.append(self._standard_error(target, behaviour_class, features))
            setattr(result, target, clamp(probability, 1e-4, 1 - 1e-4))

        # Uncertainty blends the four standard errors into a single 0..1 figure.
        mean_error = sum(errors) / len(errors)
        result.uncertainty = clamp(1.0 - math.exp(-mean_error * 0.9), 0.0, 1.0)
        return result

    def conservative_bound(self, prediction: Prediction, *, target: str = "reply_probability") -> float:
        """Return a conservative lower quantile of a predicted probability.

        Used for boundary-risky candidates: with little data the bound shrinks
        automatically, which makes the Runtime cautious exactly when it should be.

        Args:
            prediction: A prediction from :meth:`predict`.
            target: Which probability to bound.

        Returns:
            The lower quantile in ``[0, 1]``.
        """
        probability = float(getattr(prediction, target))
        z = self._config.user_model.conservative_z
        # Map uncertainty back to a logit-space standard error.
        error = -math.log(max(1e-6, 1.0 - prediction.uncertainty)) / 0.9
        theta = math.log(max(1e-6, probability) / max(1e-6, 1.0 - probability))
        bounded = sigmoid(theta - z * error * 0.5)
        return clamp(min(bounded, probability))

    # ------------------------------------------------------------------ learning

    def observe(
        self,
        connection: sqlite3.Connection,
        *,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
        reaction: BehaviourReaction,
        now: datetime | None = None,
        observed_at: datetime | None = None,
        semantic_confidence: float = 0.6,
        source_event_ids: Sequence[str] = (),
        attempt_id: str | None = None,
        busy_probability: float | None = None,
        attribution_confidence: float | None = None,
        learning_rate: float | None = None,
    ) -> InteractionObservation:
        """Record and learn from one complete interaction observation.

        The raw observation is stored first (facts before interpretation), then
        folded into the parameters with a weighted Bayesian update.

        Args:
            connection: Write connection.
            action: What the character did.
            context: Situation at the time.
            reaction: Observed user reaction.
            now: Reference time.
            observed_at: When the outcome was observed.
            semantic_confidence: Confidence in the semantic reading.
            source_event_ids: Events backing the observation.
            attempt_id: Action attempt this observation belongs to.
            busy_probability: Belief the user was busy (attribution damping).
            attribution_confidence: Override for the attribution weight.
            learning_rate: Override for the update step size.

        Returns:
            The stored :class:`InteractionObservation`.
        """
        stamp = now or utcnow()
        if busy_probability is not None:
            reaction.busy_probability = clamp(busy_probability)
        weight = compute_weight(
            reaction,
            config=self._config.user_model,
            observed_at=observed_at or stamp,
            now=stamp,
            semantic_confidence=semantic_confidence,
        )
        if attribution_confidence is not None:
            weight.attribution = clamp(attribution_confidence)
            weight.total = clamp(
                weight.source * weight.attribution * weight.semantic * weight.recency, 0.0, 1.0
            )

        observation = InteractionObservation(
            observation_id=new_id("observation"),
            action=dict(action),
            context=dict(context),
            outcome=reaction.to_dict(),
            created_at=stamp,
            attempt_id=attempt_id,
            source_event_ids=list(source_event_ids),
            attribution_confidence=weight.attribution,
            source_weight=weight.source,
            semantic_confidence=weight.semantic,
            weight=weight.total,
        )
        self._projection.record_observation(connection, observation.to_dict() | {"applied": False})

        # The delay is scored against the baseline as it stands *before* this
        # observation and folded in afterwards: scoring it against a baseline that
        # already contains it would compare the reply with itself and detect nothing.
        targets = self._target_rewards(reaction)
        self._learn_reply_delay(reaction)
        if not self._update(connection, action, context, targets, weight.total, learning_rate):
            # ``_update`` skips a zero-weight observation; the baseline still moved,
            # so it is persisted on its own rather than waiting for the next update.
            self._persist(connection)
        self._projection.mark_observation_applied(connection, observation.observation_id)
        return observation

    def _target_rewards(self, reaction: BehaviourReaction) -> dict[str, float]:
        """Convert an observed reaction into soft targets per model.

        A non-reply produces *no* positive-probability target at all: absence of a
        reply is not evidence of a negative reaction.

        When the user did reply, how fast they replied is scored relative to their own
        baseline (:meth:`relative_delay_signal`) instead of an absolute threshold. The
        reply-length and turn-count terms are still absolute.
        """
        targets: dict[str, float] = {"reply_probability": 1.0 if reaction.replied else 0.0}
        target_busy = clamp(reaction.busy_probability)
        if not reaction.replied:
            # Weak negative evidence for reply, shrunk further by busy-ness.
            targets["reply_probability"] = 0.5 * target_busy
            return targets
        positive = 0.5
        if reaction.explicit_positive:
            positive = 0.95
        elif reaction.explicit_negative:
            positive = 0.05
        else:
            positive += 0.12 if reaction.continued_topic else -0.05
            positive += 0.08 if reaction.asked_back else -0.05
            positive += 0.10 if reaction.reply_length >= 20 else (-0.05 if reaction.reply_length <= 4 else 0.0)
            positive += self._relative_delay_delta(reaction, positive)
        targets["positive_probability"] = clamp(positive)
        targets["continue_probability"] = clamp(
            0.2 + 0.6 * (1.0 if reaction.continued_topic else 0.0) + 0.1 * min(3, reaction.turns) / 3.0
        )
        targets["boundary_risk"] = 1.0 if reaction.boundary_touched else 0.0
        return targets

    def _relative_delay_delta(self, reaction: BehaviourReaction, positive: float) -> float:
        """Return the signed adjustment the relative reply delay contributes.

        ``+REPLY_DELAY_TARGET_WEIGHT`` for a reply far faster than this user's own
        baseline, down to ``-REPLY_DELAY_TARGET_WEIGHT`` for one far slower. The
        negative side is capped at the bonus the other signals already earned
        (``positive - 0.5``): arriving late makes the reply *weaker positive* evidence,
        it never turns a reply into negative evidence, which is the same invariant
        that keeps "no reply" from becoming a label.

        Args:
            reaction: The observed reaction.
            positive: The positive-probability target accumulated so far.

        Returns:
            The adjustment to add to ``positive`` (``0.0`` when speed is unknown).
        """
        if not reaction.replied or reaction.reply_delay_seconds is None:
            return 0.0
        delta = REPLY_DELAY_TARGET_WEIGHT * self.relative_delay_signal(
            reaction.reply_delay_seconds
        )
        if delta >= 0.0:
            return delta
        return -min(-delta, max(0.0, positive - 0.5))

    def _update(
        self,
        connection: sqlite3.Connection,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
        targets: Mapping[str, float],
        weight: float,
        learning_rate: float | None,
    ) -> bool:
        """Apply one weighted online Bayesian update to the parameters.

        Returns:
            ``True`` when the update was applied and persisted, ``False`` when a
            zero-weight observation was skipped (nothing changed, nothing written).
        """
        if weight <= 0.0:
            return False
        features = extract_features(action=action, context=context, config=self._config.user_model)
        vector = _vector(features)
        behaviour_class = behaviour_class_of(action)
        base_rate = learning_rate if learning_rate is not None else self._config.user_model.learning_rate

        for target in TARGET_NAMES:
            if target not in targets:
                continue
            theta = self._theta_for(target, behaviour_class)
            prediction = sigmoid(sum(t * x for t, x in zip(theta, vector)))
            error = float(targets[target]) - prediction
            step = base_rate * weight * error
            for index, value in enumerate(vector):
                if value == 0.0:
                    continue
                update = step * value
                self._theta[target][index] += update
                self._precision[target][index] += weight * value * value
                if behaviour_class != "reply":
                    delta = self._delta.setdefault(behaviour_class, {}).setdefault(
                        target, [0.0] * len(FEATURE_NAMES)
                    )
                    # The offset learns alongside the global vector but with a
                    # lower rate, which is what produces shrinkage toward the
                    # more general knowledge.
                    delta[index] += 0.5 * update
            # One observation informs every parameter a little, including the ones
            # whose feature was zero this time. Without this the diagonal precision
            # only ever grew along the directions that happened to be active, so a
            # class could accumulate dozens of observations and still report the
            # uncertainty of one that had none.
            for index in range(len(FEATURE_NAMES)):
                self._precision[target][index] += 0.25 * weight

        self._observations += 1
        self._effective_count += weight
        self._class_counts[behaviour_class] = self.behaviour_evidence(behaviour_class) + 1
        self._apply_drift()
        self._persist(connection)
        return True

    def _apply_drift(self) -> None:
        """Model slow preference drift by forgetting precision over evidence.

        ``Theta_t ~ N(Theta_{t-1}, Q dt)`` is approximated by shrinking the
        precision of every parameter toward the prior, so old knowledge keeps its
        mean but loses confidence.

        This is the *per-observation* nudge: it uses ``forgetting_rate`` as a plain
        fraction, which is how the knob was originally defined, and it is what keeps a
        Runtime that never ticks from freezing its beliefs forever. The decay that is a
        function of elapsed time is :meth:`tick_drift`; the two are independent and
        both pull precision toward the same prior.
        """
        rate = self._config.user_model.forgetting_rate
        for target in TARGET_NAMES:
            for index in range(len(FEATURE_NAMES)):
                current = self._precision[target][index]
                decayed = current - rate * max(0.0, current - self._config.user_model.prior_precision)
                self._precision[target][index] = max(1e-6, decayed)

    def tick_drift(
        self, dt_seconds: float, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        """Age the learned beliefs by ``dt_seconds`` of elapsed time (§28).

        ``Theta_t ~ N(Theta_{t-1}, Q dt)`` is implemented as exponential forgetting of
        the precision *in excess of the prior*: the mean stays where the evidence put
        it, while confidence relaxes toward the cold-start level. Because the retention
        factor is ``exp(-rate * dt)``, the result depends only on the total elapsed
        time - ten one-day ticks leave the same state as one ten-day tick - and the
        call is a strict no-op for ``dt_seconds <= 0``, so an idle or repeated tick
        costs nothing and cannot double-count an interval.

        ``config.user_model.drift_half_life_hours`` is the timescale (a week by
        default): a silence that long halves the confidence accumulated on top of the
        prior. It is deliberately *not* ``forgetting_rate``, which is the
        per-observation nudge applied when a new observation lands and is a plain
        fraction rather than a rate - one knob carrying two units is how the
        time-based path came to be missing in the first place.

        Observation counts and the means themselves are deliberately untouched: they
        are historical facts, not beliefs. What changes is how certain the model is, so
        the effect shows up as a larger ``Prediction.uncertainty`` and a lower
        :meth:`conservative_bound`.

        Args:
            dt_seconds: Elapsed time to integrate. Values ``<= 0`` are ignored.
            connection: Write connection used to persist the decayed precision through
                the user-model projection. Without it the decay is applied in memory
                only and is lost when the model is reloaded, so the Runtime passes the
                connection of the tick it is already inside.

        Returns:
            ``True`` when precision moved (and, with a connection, was persisted).
        """
        if not math.isfinite(dt_seconds) or dt_seconds <= 0.0:
            return False
        half_life_hours = float(self._config.user_model.drift_half_life_hours)
        if half_life_hours <= 0.0:
            return False
        rate = math.log(2.0) / (half_life_hours * 3600.0)
        if rate <= 0.0:
            return False
        retention = exponential_decay(rate, dt_seconds)
        if retention >= 1.0:
            return False
        prior = float(self._config.user_model.prior_precision)
        changed = False
        for target in TARGET_NAMES:
            for index in range(len(FEATURE_NAMES)):
                current = self._precision[target][index]
                excess = current - prior
                if excess <= 0.0:
                    continue
                decayed = prior + excess * retention
                if decayed < current:
                    self._precision[target][index] = max(1e-6, decayed)
                    changed = True
        if changed and connection is not None:
            self._persist(connection)
        return changed

    # ------------------------------------------------------------------- summary

    def semantic_view(self) -> dict[str, Any]:
        """Return the natural-language view used by the strong candidate API.

        Returns:
            A mapping with ``summary`` (a short prose description of what is
            known) and ``confidence``.
        """
        if self._summary is not None:
            return dict(self._summary)
        prediction = self.predict(
            action={"type": "contact", "proactive": True},
            context={"busy_probability": 0.0, "hours_since_contact": 12.0},
        )
        if self._effective_count < 3.0:
            text = (
                "对这个用户的互动偏好还几乎没有证据；"
                "只能依赖通用先验，因此主动行为应当短、轻、易于忽略。"
            )
        else:
            parts: list[str] = []
            if prediction.reply_probability > 0.6:
                parts.append("总体上看，用户对角色主动开口的回应意愿偏高")
            elif prediction.reply_probability < 0.35:
                parts.append("总体上看，用户对角色主动开口的回应意愿偏低")
            else:
                parts.append("用户对角色的主动联系反应中等")
            if prediction.boundary_risk > 0.35:
                parts.append("并且对连续追问这类行为比较敏感")
            if self._effective_count >= 8:
                parts.append("慢回复不应直接视为负反馈，需要结合忙碌程度解释")
            text = "；".join(parts) + "。"
        return {
            "summary": text,
            "confidence": clamp(self._effective_count / (self._effective_count + 4.0)),
            "observations": self._observations,
            "effective_count": round(self._effective_count, 3),
            "parameters": {
                "reply_probability": prediction.reply_probability,
                "positive_probability": prediction.positive_probability,
                "boundary_risk": prediction.boundary_risk,
                "uncertainty": prediction.uncertainty,
            },
        }

    def numeric_view(self) -> dict[str, Any]:
        """Return the numeric view used by the heartbeat and motivation layers.

        Every key the previous version returned is still here and still has the same
        shape; ``reply_delay_baseline`` is additive.
        """
        view: dict[str, Any] = {
            "observations": self._observations,
            "effective_count": round(self._effective_count, 3),
            "class_evidence": {cls: self.behaviour_evidence(cls) for cls in BEHAVIOUR_CLASSES},
            "reply_delay_baseline": self.reply_delay_baseline_view(),
            "behaviour_offsets": {
                behaviour_class: {
                    target: [round(value, 4) for value in vector]
                    for target, vector in targets.items()
                }
                for behaviour_class, targets in self._delta.items()
                if targets
            },
        }
        for target in TARGET_NAMES:
            view[target] = {
                name: round(value, 4) for name, value in zip(FEATURE_NAMES, self._theta[target])
            }
        view["semantic"] = self.semantic_view()
        return view

    def busy_probability(
        self,
        *,
        hours_since_contact: float,
        replied_recently: bool,
        context: Mapping[str, Any] | None = None,
    ) -> float:
        """Return a simple belief that the user is busy right now.

        This is the fast variable ``Z_t`` of the model: it is not a preference,
        it is a current-state estimate used to *dampen attribution*.

        Args:
            hours_since_contact: Hours since the last user message.
            replied_recently: Whether the user replied within the recent window.
            context: Optional explicit signals (``stated_busy``, ``late_hours``).

        Returns:
            A probability in ``[0, 1]``.
        """
        data = dict(context or {})
        belief = 0.25
        if replied_recently:
            belief = 0.15
        elif hours_since_contact >= 8.0:
            belief = 0.60
        elif hours_since_contact >= 4.0:
            belief = 0.45
        if data.get("stated_busy"):
            belief = max(belief, 0.85)
        if data.get("late_hours"):
            belief = max(belief, 0.55)
        return clamp(belief)


def describe_absent_reply(reaction: BehaviourReaction) -> str:
    """Return a neutral description of a missing reply (never an attribution)."""
    delay = _clamp_seconds(reaction.reply_delay_seconds, 0.0)
    return f"no_reply=true, reply_delay={int(delay)}"
