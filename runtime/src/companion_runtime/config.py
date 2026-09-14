"""Configuration for the Runtime sidecar.

Configuration is plain dataclasses so that every parameter is discoverable,
documented and overridable from three places, in order of increasing priority:

1. dataclass defaults (the calibrated baseline);
2. a TOML or JSON file passed with ``--config``;
3. environment variables prefixed with ``CR_`` (see :func:`load_config`).

No configuration value ever stores an API key: secrets stay in the host
framework's own environment and are read - never persisted - by the host. The
Runtime additionally redacts any credential-shaped key before it can be logged
or returned by an inspection endpoint, see :func:`redact` and
:func:`redact_tree`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from .typing import ValueProfile

LOGGER = logging.getLogger("companion_runtime.config")

_SECRET_PATTERN = re.compile(r"(api[_-]?key|token|secret|password|credential|authorization)", re.I)


def redact(key: str, value: Any) -> Any:
    """Return a masked value for anything that looks like a credential.

    Args:
        key: Configuration key name.
        value: Raw value.

    Returns:
        ``"***redacted***"`` when the key looks secret, otherwise ``value``.
    """
    if _SECRET_PATTERN.search(key or ""):
        return "***redacted***"
    return value


def redact_tree(value: Any, key: str = "") -> Any:
    """Recursively mask secret-shaped keys inside a nested structure.

    The Runtime is designed to *never* store an API key, but the redaction layer
    still guards the inspection endpoints: a key smuggled in through ``extras``
    must not be echoed back to a client or written to a log.

    Args:
        value: Arbitrary value (mapping, list or scalar).
        key: Name under which ``value`` is held, if any.

    Returns:
        A copy of ``value`` with credential-shaped entries masked.
    """
    if key and _SECRET_PATTERN.search(key):
        return "***redacted***"
    if isinstance(value, Mapping):
        return {str(k): redact_tree(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_tree(item, key) for item in value]
    return value


@dataclass(slots=True)
class ServerConfig:
    """HTTP sidecar binding."""

    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "INFO"
    request_timeout_seconds: float = 30.0


@dataclass(slots=True)
class StorageConfig:
    """SQLite and raw-log locations."""

    database_path: str = "./data/runtime.sqlite3"
    raw_log_path: str = "./data/raw_events.jsonl"
    #: Write the raw event log to an append-only JSONL mirror as well as SQLite.
    mirror_raw_events: bool = True
    busy_timeout_ms: int = 5000
    wal: bool = True


@dataclass(slots=True)
class EmotionConfig:
    """Emotion dynamics parameters."""

    valence_pull_gain: float = 0.22
    arousal_pull_gain: float = 0.18
    stability_pull_gain: float = 0.12
    mood_recovery_rate: float = 0.06
    emotion_decay_rate: float = 0.08
    emotion_retire_threshold: float = 0.02
    max_active_emotion_events: int = 24
    event_reactivity: float = 1.0
    #: Impacts at or below this value are not worth an emotion event at all.
    min_event_impact: float = 0.06


@dataclass(slots=True)
class DriveConfig:
    """Approach / restraint / pressure dynamics.

    The calibration target is the "long absence" scenario: with no boundary in
    force, silence must slowly raise impulse above restraint so that endogenous
    contact becomes *possible* after roughly a day, while a recent contact
    (cooldown) suppresses it again.
    """

    tau_impulse_seconds: float = 5400.0
    tau_restraint_seconds: float = 9000.0
    beta: float = 4.0
    kappa_plus: float = 0.000060
    kappa_minus: float = 0.000040
    impulse_release: float = 0.55
    pressure_release: float = 0.70
    restraint_boost: float = 0.06
    cooldown_seconds: float = 2400.0
    max_contacts_per_day: int = 12
    #: Hours of no exchange after which the absence term saturates.
    absence_saturation_hours: float = 36.0
    #: Cap on the pressure integration window per tick, avoiding artefacts after
    #: a very long process sleep.
    max_pressure_step_seconds: float = 21600.0


@dataclass(slots=True)
class SilenceConfig:
    """Baseline utility of staying silent.

    ``base`` is the value of *not* acting when nothing else pushes either way. It is
    deliberately positive and paired with a meaningful ``impulse_gain``: a long
    absence raises both impulse and the comfort of silence, so loneliness alone
    never produces a positive advantage. Only a concrete reason -- a due unfinished
    matter, or a strong internal need -- is meant to cross that line.

    ``impulse_gain`` is **positive**, which diverges from the ``- d * I`` form in the
    architecture document. The sign is a deliberate, test-backed choice: with a
    negative penalty a merely lonely character reaches out on absence alone, which
    breaks scenarios 2b/2d (a restrained character stays quiet; an unrestrained one
    with a standing need speaks). Holding back while wanting to speak is the
    behaviour being modelled, so impulse is priced as a cost of acting rather than as
    a discount on silence. Flip the sign only together with those scenario tests.
    """

    base: float = 0.28
    restraint_gain: float = 0.45
    boundary_gain: float = 0.30
    cooldown_gain: float = 0.25
    impulse_gain: float = 0.42
    pressure_penalty: float = 1.20


@dataclass(slots=True)
class UtilityConfig:
    """Weights of the motivational game."""

    internal_gain: float = 0.65
    user_gain: float = 0.85
    relation_gain: float = 0.45
    boundary_cost_gain: float = 0.75
    interrupt_gain: float = 0.18
    repeat_gain: float = 0.45
    risk_gain: float = 0.10
    uncertainty_penalty: float = 0.12
    #: Extra weight of a *concrete reason* to speak (a due matter, a strong
    #: emotional need). Without a reason, a restrained character stays silent.
    urgency_gain: float = 0.70
    downside_quantile: float = 0.05
    repeat_window_seconds: float = 3600.0
    repeat_contact_tolerance: int = 2
    temperature: float = 0.35
    hazard_base: float = 0.000030
    hazard_beta: float = 4.0
    min_sleep_seconds: float = 60.0
    max_sleep_seconds: float = 1800.0
    utility_epsilon: float = 1e-9
    #: Above this predicted boundary risk a candidate is judged conservatively.
    conservative_risk_threshold: float = 0.30


@dataclass(slots=True)
class BoundaryConfig:
    """Boundary parsing and enforcement."""

    default_temporal_hours: float = 24.0
    scan_recent_events: int = 4


@dataclass(slots=True)
class UnfinishedConfig:
    """Unfinished-matter lifecycle."""

    default_priority: float = 0.55
    default_expiry_hours: float = 168.0
    due_grace_seconds: float = 900.0
    max_active: int = 40


@dataclass(slots=True)
class MemoryConfig:
    """Memory candidates, consolidation and activation."""

    candidate_min_value: float = 0.30
    candidate_max_open: int = 200
    activation_threshold: float = 0.18
    activation_decay_rate: float = 0.00015
    activation_pool_size: int = 8
    recent_recall_penalty: float = 0.35
    random_epsilon: float = 0.03
    consolidation_interval_seconds: float = 3600.0
    episodic_importance: float = 0.45
    stable_knowledge_importance: float = 0.75
    preference_importance: float = 0.70
    relationship_importance: float = 0.65


@dataclass(slots=True)
class UserModelConfig:
    """Simplified Bayesian user interaction model."""

    prior_precision: float = 1.0
    learning_rate: float = 0.35
    forgetting_rate: float = 0.0000025
    min_weight: float = 0.02
    explicit_positive_weight: float = 1.0
    explicit_negative_weight: float = 1.0
    implicit_weight: float = 0.30
    no_reply_weight: float = 0.06
    slow_reply_weight: float = 0.12
    busy_attribution_floor: float = 0.10
    default_reply_delay_seconds: float = 3600.0
    max_observations_in_memory: int = 400
    conservative_z: float = 1.645
    cold_start_prior_mean: float = 0.10
    cold_start_prior_precision: float = 0.60


@dataclass(slots=True)
class CandidateConfig:
    """Candidate intent pool."""

    max_active: int = 12
    default_ttl_seconds: float = 21600.0
    refresh_min_seconds: float = 900.0
    empty_pool_refresh_seconds: float = 300.0
    #: Neutral prior for the permanent "just want to be in contact" candidate. It is
    #: deliberately negative: the bare wish to talk is not by itself a reason to
    #: speak, so the candidate only gains value when impulse and pressure are high
    #: *and* something else (an unfinished matter, a strong need) is pushing.
    contact_baseline_prior: float = -0.25
    contact_bias_impulse: float = 2.6
    contact_bias_pressure: float = 1.4
    contact_bias_restraint: float = 0.9
    unfinished_relevance_weight: float = 0.6
    confidence_floor: float = 0.25


@dataclass(slots=True)
class OutboxConfig:
    """Asynchronous delivery queue.

    ``retry_backoff_seconds`` defaults to **0**: a negatively acknowledged row
    becomes claimable again immediately. This matters because a caller that
    nacks and then retries at once is by far the common case (the host's own
    retry queue already owns pacing), and a hidden service-side delay makes the
    retry look like it was silently dropped. Set it above zero only if the
    Runtime should throttle retries itself.
    """

    lease_seconds: float = 45.0
    max_attempts: int = 3
    max_batch: int = 20
    retry_backoff_seconds: float = 0.0


@dataclass(slots=True)
class ActionConfig:
    """Action attempt state machine."""

    commit_grace_seconds: float = 8.0
    render_timeout_seconds: float = 120.0
    send_expiry_seconds: float = 900.0
    max_committed_attempts: int = 3


@dataclass(slots=True)
class SchedulerConfig:
    """Endogenous wake-up scheduling."""

    min_interval_seconds: float = 60.0
    max_interval_seconds: float = 5400.0
    quiet_hours_start: int | None = None
    quiet_hours_end: int | None = None
    busy_poll_seconds: float = 30.0
    foreground_pause_seconds: float = 60.0


@dataclass(slots=True)
class TaskConfig:
    """Background task batching."""

    merge_window_seconds: float = 15.0
    emotion_explain_change_threshold: float = 0.12
    explain_cache_ttl_seconds: float = 1800.0


@dataclass(slots=True)
class RuntimeConfig:
    """Aggregate configuration for the whole Runtime."""

    runtime_id: str = "companion"
    conversation_id: str = "default"
    values: ValueProfile = field(default_factory=ValueProfile)
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    drive: DriveConfig = field(default_factory=DriveConfig)
    silence: SilenceConfig = field(default_factory=SilenceConfig)
    utility: UtilityConfig = field(default_factory=UtilityConfig)
    boundary: BoundaryConfig = field(default_factory=BoundaryConfig)
    unfinished: UnfinishedConfig = field(default_factory=UnfinishedConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    user_model: UserModelConfig = field(default_factory=UserModelConfig)
    candidate: CandidateConfig = field(default_factory=CandidateConfig)
    outbox: OutboxConfig = field(default_factory=OutboxConfig)
    action: ActionConfig = field(default_factory=ActionConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    task: TaskConfig = field(default_factory=TaskConfig)
    #: Free-form extras; useful for experiments without touching the schema.
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable, secret-redacted view of the config."""
        return redact_tree(dataclasses.asdict(self))

    def dumps(self) -> str:
        """Return a pretty JSON rendering of :meth:`to_dict`."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _apply_mapping(target: Any, data: Mapping[str, Any], path: str = "") -> None:
    """Recursively assign a mapping onto a dataclass instance in place.

    Unknown keys are ignored but logged, so a typo in a config file is visible
    instead of silent.

    Args:
        target: Dataclass instance to mutate.
        data: Mapping of field names to values.
        path: Dotted path used for diagnostics.
    """
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in known:
            LOGGER.warning("Ignoring unknown config key: %s%s", path, key)
            continue
        current = getattr(target, key)
        if is_dataclass(current) and not isinstance(current, type) and isinstance(value, Mapping):
            _apply_mapping(current, value, f"{path}{key}.")
        elif key == "values" and isinstance(value, Mapping):
            setattr(target, key, ValueProfile.from_mapping(value))
        else:
            setattr(target, key, value)


def _coerce_scalar(text: str) -> Any:
    """Coerce an environment-variable string into a bool/int/float/str."""
    lowered = text.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null", ""}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def load_config(
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Build a :class:`RuntimeConfig` from defaults, an optional file and the env.

    Environment overrides use ``CR_`` plus the uppercased dotted path, e.g.
    ``CR_SERVER__PORT=9000`` or ``CR_DRIVE__COOLDOWN_SECONDS=60``. A double
    underscore separates path segments.

    Args:
        path: Optional TOML (``.toml``) or JSON (``.json``) config file.
        env: Environment mapping; defaults to :data:`os.environ`.

    Returns:
        The resolved configuration.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the file extension is unsupported.
    """
    config = RuntimeConfig()
    environment = os.environ if env is None else env

    if path is not None:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"config file not found: {file_path}")
        if file_path.suffix.lower() == ".toml":
            with file_path.open("rb") as handle:
                data = tomllib.load(handle)
        elif file_path.suffix.lower() == ".json":
            data = json.loads(file_path.read_text(encoding="utf-8"))
        else:
            raise ValueError(f"unsupported config extension: {file_path.suffix}")
        _apply_mapping(config, data)

    prefix = "CR_"
    for key, raw in environment.items():
        if not key.startswith(prefix):
            continue
        dotted = key[len(prefix) :].lower()
        segments = [seg for seg in dotted.split("__") if seg]
        if not segments:
            continue
        cursor: Any = config
        for segment in segments[:-1]:
            if not hasattr(cursor, segment):
                cursor = None
                break
            cursor = getattr(cursor, segment)
        if cursor is None or not hasattr(cursor, segments[-1]):
            LOGGER.warning("Ignoring unknown environment override: %s", key)
            continue
        setattr(cursor, segments[-1], _coerce_scalar(raw))

    return config


def resolve_paths(config: RuntimeConfig, base_dir: str | os.PathLike[str] | None = None) -> RuntimeConfig:
    """Resolve relative storage paths against ``base_dir`` (default: cwd).

    Args:
        config: Configuration to adjust in place.
        base_dir: Base directory for relative paths.

    Returns:
        The same configuration object, mutated.
    """
    root = Path(base_dir) if base_dir is not None else Path.cwd()
    for attribute in ("database_path", "raw_log_path"):
        value = getattr(config.storage, attribute)
        # The in-memory sentinel is not a filesystem path and must pass through.
        if not value or value == ":memory:":
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        setattr(config.storage, attribute, str(candidate))
    return config


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger for the sidecar with a compact format."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    logging.getLogger("companion_runtime").setLevel(getattr(logging, level.upper(), logging.INFO))
