"""Typed, clamped view over the raw AstrBot plugin config mapping.

The WebUI may hand config values over as strings, missing keys, or values the
operator typed by hand. Everything is coerced and clamped here once, so the rest
of the adapter can treat :class:`Settings` as trusted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .coerce import as_bool, as_float, as_int, as_str, clamp

#: Environment variable that overrides ``runtime_token``, so a shared secret
#: never has to be written into the plugin config file.
TOKEN_ENV_VAR = "COMPANION_RUNTIME_TOKEN"

#: Must match the Runtime's own default (``RuntimeConfig.port``). A mismatch is
#: invisible until an operator wonders why nothing ever arrives.
DEFAULT_BASE_URL = "http://127.0.0.1:8787"

#: How long plugin termination waits for leased actions that are already in
#: flight before it cancels them. Delivery is irreversible, so cancelling an
#: authorized send mid-flight is what makes a duplicate possible; this window is
#: the adapter's only defence and stays short so unloading a plugin stays
#: responsive. Not a config key: it is a lifecycle policy, not a preference.
SHUTDOWN_GRACE_S = 5.0

#: ``on_llm_request`` runs inside AstrBot's LLM request path. Its context fetch
#: is capped at this hard upper bound regardless of what is configured.
HARD_MAX_CONTEXT_TIMEOUT_S = 2.0

#: Prefetch runs in the background, so it may take a little longer than the
#: in-request fetch, but it still must not pile up.
HARD_MAX_PREFETCH_TIMEOUT_S = 5.0

#: How the adapter decides which messages it observes.
OBSERVE_MODE_WAKE = "wake"
OBSERVE_MODE_ALL = "all"
OBSERVE_MODES = (OBSERVE_MODE_WAKE, OBSERVE_MODE_ALL)

DEFAULT_SESSION: str = "default"


@dataclass(frozen=True)
class Settings:
    """Normalized adapter settings."""

    enabled: bool = True
    base_url: str = DEFAULT_BASE_URL
    token: str = field(default="", repr=False)
    """Shared secret for the Runtime HTTP API. Never logged, never bundled."""
    adapter_id: str = DEFAULT_SESSION
    """Identifies this AstrBot deployment; multiple instances need distinct ids."""

    request_timeout_s: float = 1.5
    """Timeout for ordinary Runtime calls (event reports, leases, reports)."""
    context_timeout_s: float = 0.4
    """Strict short deadline for the in-request context fetch."""
    context_cache_ttl_s: float = 30.0
    context_cache_max_sessions: int = 64
    context_prefetch: bool = True
    """Warm the context cache in the background when a message is observed."""

    observe_mode: str = OBSERVE_MODE_WAKE
    report_assistant_messages: bool = True
    inject_enabled: bool = True
    inject_max_chars: int = 2000

    outbox_enabled: bool = True
    outbox_poll_interval_s: float = 1.0
    outbox_batch: int = 2
    outbox_lease_ttl_s: float = 30.0
    outbox_max_concurrency: int = 1
    render_timeout_s: float = 60.0
    send_timeout_s: float = 20.0

    queue_max_items: int = 256
    queue_max_attempts: int = 6
    queue_max_age_s: float = 600.0
    queue_base_backoff_s: float = 0.5
    queue_max_backoff_s: float = 30.0
    queue_send_timeout_s: float = 10.0

    debug: bool = False
    issues: tuple[str, ...] = ()
    """Human readable problems found while normalizing the config."""

    @property
    def usable(self) -> bool:
        """Whether the Runtime can be contacted at all."""
        return self.enabled and bool(self.base_url)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> Settings:
        """Build settings from a raw plugin config mapping.

        Args:
            raw: The plugin config mapping (``AstrBotConfig`` is a ``dict``), or
                ``None`` when the plugin has no config yet.

        Returns:
            A :class:`Settings` instance. Invalid values never raise; they are
            replaced with defaults and recorded in :attr:`issues`.
        """
        data: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}
        issues: list[str] = []

        base_url = as_str(data.get("runtime_base_url"), DEFAULT_BASE_URL).strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            issues.append(
                f"runtime_base_url must start with http:// or https:// (got {base_url!r}); "
                "Runtime calls are disabled",
            )
            base_url = ""

        token = as_str(data.get("runtime_token")).strip()
        if not token:
            token = as_str(os.environ.get(TOKEN_ENV_VAR)).strip()

        adapter_id = as_str(data.get("adapter_id"), DEFAULT_SESSION).strip() or DEFAULT_SESSION

        observe_mode = as_str(data.get("observe_mode"), OBSERVE_MODE_WAKE).strip().lower()
        if observe_mode not in OBSERVE_MODES:
            issues.append(
                f"unknown observe_mode {observe_mode!r}; using {OBSERVE_MODE_WAKE!r}",
            )
            observe_mode = OBSERVE_MODE_WAKE

        def seconds(key: str, default_ms: float, low_ms: float, high_ms: float) -> float:
            """Read a millisecond config value and clamp it into a sane range."""
            raw_ms = as_float(data.get(key), default_ms)
            clamped_ms = clamp(raw_ms, low_ms, high_ms)
            if clamped_ms != raw_ms:
                issues.append(
                    f"{key}={raw_ms:g}ms clamped into [{low_ms:g}, {high_ms:g}]ms",
                )
            return clamped_ms / 1000.0

        def count(key: str, default: int, low: int, high: int) -> int:
            """Read an integer config value and clamp it into a sane range."""
            raw_count = as_int(data.get(key), default)
            clamped_count = int(clamp(raw_count, low, high))
            if clamped_count != raw_count:
                issues.append(f"{key}={raw_count} clamped into [{low}, {high}]")
            return clamped_count

        context_timeout_s = seconds(
            "context_timeout_ms",
            400.0,
            50.0,
            HARD_MAX_CONTEXT_TIMEOUT_S * 1000.0,
        )
        if context_timeout_s >= HARD_MAX_CONTEXT_TIMEOUT_S:
            issues.append(
                "context_timeout_ms sits at the hard cap; the in-request fetch may "
                "noticeably delay first token",
            )

        # A ceiling below the floor would make the backoff meaningless.
        queue_base_backoff_s = seconds("queue_base_backoff_ms", 500.0, 50.0, 60000.0)
        queue_max_backoff_s = max(
            queue_base_backoff_s,
            seconds("queue_max_backoff_ms", 30000.0, 500.0, 600000.0),
        )

        return cls(
            enabled=as_bool(data.get("enabled"), True),
            base_url=base_url,
            token=token,
            adapter_id=adapter_id,
            request_timeout_s=seconds("request_timeout_ms", 1500.0, 200.0, 30000.0),
            context_timeout_s=context_timeout_s,
            context_cache_ttl_s=seconds("context_cache_ttl_ms", 30000.0, 0.0, 3600000.0),
            context_cache_max_sessions=count("context_cache_max_sessions", 64, 1, 1024),
            context_prefetch=as_bool(data.get("context_prefetch"), True),
            observe_mode=observe_mode,
            report_assistant_messages=as_bool(data.get("report_assistant_messages"), True),
            inject_enabled=as_bool(data.get("inject_enabled"), True),
            inject_max_chars=count("inject_max_chars", 2000, 0, 100000),
            outbox_enabled=as_bool(data.get("outbox_enabled"), True),
            outbox_poll_interval_s=seconds("outbox_poll_interval_ms", 1000.0, 200.0, 60000.0),
            outbox_batch=count("outbox_max_actions_per_poll", 2, 1, 16),
            outbox_lease_ttl_s=seconds("outbox_lease_ttl_ms", 30000.0, 1000.0, 3600000.0),
            outbox_max_concurrency=count("outbox_max_concurrency", 1, 1, 8),
            render_timeout_s=seconds("render_timeout_ms", 60000.0, 1000.0, 600000.0),
            send_timeout_s=seconds("send_timeout_ms", 20000.0, 1000.0, 300000.0),
            queue_max_items=count("queue_max_items", 256, 8, 8192),
            queue_max_attempts=count("queue_max_attempts", 6, 1, 50),
            queue_max_age_s=seconds("queue_max_age_ms", 600000.0, 5000.0, 86400000.0),
            queue_base_backoff_s=queue_base_backoff_s,
            queue_max_backoff_s=queue_max_backoff_s,
            queue_send_timeout_s=seconds("queue_send_timeout_ms", 10000.0, 500.0, 120000.0),
            debug=as_bool(data.get("debug"), False),
            issues=tuple(issues),
        )
