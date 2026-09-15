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
  disappearing.

Crucially, an observation is *not* an attribution: "6 hours without a reply" is
recorded as ``reply_delay = 21600`` and gets a tiny weight, never as
``feedback = negative``.
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
        self._observations = int(stored.get("observations") or 0)
        self._effective_count = float(stored.get("effective_count") or 0.0)
        self._summary = stored.get("last_summary_json") or None

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
        self._projection.upsert_params(
            connection,
            params=params,
            precision={name: self._precision[name] for name in TARGET_NAMES},
            observations=self._observations,
            effective_count=self._effective_count,
            summary=self._summary,
        )

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

        targets = self._target_rewards(reaction)
        self._update(connection, action, context, targets, weight.total, learning_rate)
        self._projection.mark_observation_applied(connection, observation.observation_id)
        return observation

    def _target_rewards(self, reaction: BehaviourReaction) -> dict[str, float]:
        """Convert an observed reaction into soft targets per model.

        A non-reply produces *no* positive-probability target at all: absence of a
        reply is not evidence of a negative reaction.
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
        targets["positive_probability"] = clamp(positive)
        targets["continue_probability"] = clamp(
            0.2 + 0.6 * (1.0 if reaction.continued_topic else 0.0) + 0.1 * min(3, reaction.turns) / 3.0
        )
        targets["boundary_risk"] = 1.0 if reaction.boundary_touched else 0.0
        return targets

    def _update(
        self,
        connection: sqlite3.Connection,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
        targets: Mapping[str, float],
        weight: float,
        learning_rate: float | None,
    ) -> None:
        """Apply one weighted online Bayesian update to the parameters."""
        if weight <= 0.0:
            return
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

    def _apply_drift(self) -> None:
        """Model slow preference drift by forgetting precision over evidence.

        ``Theta_t ~ N(Theta_{t-1}, Q dt)`` is approximated by shrinking the
        precision of every parameter toward the prior, so old knowledge keeps its
        mean but loses confidence.
        """
        rate = self._config.user_model.forgetting_rate
        for target in TARGET_NAMES:
            for index in range(len(FEATURE_NAMES)):
                current = self._precision[target][index]
                decayed = current - rate * max(0.0, current - self._config.user_model.prior_precision)
                self._precision[target][index] = max(1e-6, decayed)

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
        """Return the numeric view used by the heartbeat and motivation layers."""
        view: dict[str, Any] = {
            "observations": self._observations,
            "effective_count": round(self._effective_count, 3),
            "class_evidence": {cls: self.behaviour_evidence(cls) for cls in BEHAVIOUR_CLASSES},
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
