"""Turns the Runtime's current context into a temporary prompt part.

The hard requirement on this module: the fetch that happens inside AstrBot's
``on_llm_request`` hook must fit inside a *strict short deadline*, and failing to
make that deadline must be invisible to the user. So the bridge is cache first:

* a fresh snapshot is used with zero awaits,
* a stale snapshot is still better than nothing and is used as a fallback,
* the network fetch is bounded by ``asyncio.wait_for`` *and* by the transport's
  own timeout,
* the cache is warmed in the background when a message is observed.

Hidden Runtime context must never enter permanent conversation history, so the
caller pairs this text with ``TextPart.mark_as_temp()``.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .protocol import ContextRequest, ContextSnapshot, RuntimeTransport, truncate_error
from .retry_queue import NULL_LOG
from .settings import HARD_MAX_PREFETCH_TIMEOUT_S, Settings

#: Upper bound on concurrent background prefetches.
MAX_INFLIGHT_PREFETCHES = 4

#: Opening tag of the injected block. The block states where the text came from
#: so a model that quotes its own context stays unambiguous.
CONTEXT_TAG = "companion_runtime_context"


@dataclass
class _CacheEntry:
    """One cached snapshot plus the monotonic time it was stored."""

    snapshot: ContextSnapshot
    fetched_at: float


@dataclass
class BridgeStats:
    """Counters describing how well the deadline is being met."""

    requests: int = 0
    cache_hits: int = 0
    fetches: int = 0
    stale_fallbacks: int = 0
    timeouts: int = 0
    errors: int = 0
    empty: int = 0
    prefetches: int = 0
    prefetch_skipped: int = 0
    injections: int = 0


class ContextBridge:
    """Cache and deadline manager for Runtime context injection."""

    def __init__(
        self,
        *,
        transport: RuntimeTransport,
        settings: Settings,
        clock: Any = time.monotonic,
        log: Any = NULL_LOG,
    ) -> None:
        """Create the bridge.

        Args:
            transport: Runtime HTTP client.
            settings: Normalized adapter settings.
            clock: Monotonic clock source (injectable for tests).
            log: Logger-like object.
        """
        self._transport = transport
        self._settings = settings
        self._clock = clock
        self._log = log
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._inflight: set[asyncio.Task[None]] = set()
        self.stats = BridgeStats()

    def cached_text(self, session: str) -> str | None:
        """Return injection-ready text from a *fresh* cached snapshot only."""
        entry = self._cache.get(session)
        if entry is None:
            return None
        self._cache.move_to_end(session)
        if not self._is_fresh(entry):
            return None
        return self._render(entry.snapshot)

    async def text_for_llm_request(self, request: ContextRequest) -> str | None:
        """Return injection-ready context for an LLM request.

        Args:
            request: Context request describing the session being served.

        Returns:
            The wrapped context block, or ``None`` when nothing usable could be
            obtained in time. Never raises: every failure degrades to ``None``
            so the host LLM request proceeds unchanged.
        """
        self.stats.requests += 1
        entry = self._cache.get(request.session)
        if entry is not None and self._is_fresh(entry):
            self._cache.move_to_end(request.session)
            self.stats.cache_hits += 1
            return self._render(entry.snapshot)

        timeout_s = self._settings.context_timeout_s
        snapshot: ContextSnapshot | None = None
        # Deadline misses and errors are normal operating noise unless the
        # operator asked for detail via ``debug``.
        log_detail = self._log.warning if self._settings.debug else self._log.debug
        try:
            snapshot = await asyncio.wait_for(
                self._transport.fetch_context(request, timeout_s=timeout_s),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            self.stats.timeouts += 1
            log_detail(
                "context fetch for %s exceeded the %.0fms deadline",
                request.session,
                timeout_s * 1000.0,
            )
        except Exception as exc:
            self.stats.errors += 1
            log_detail(
                "context fetch for %s failed: %s",
                request.session,
                truncate_error(exc),
            )

        if snapshot is not None and not snapshot.is_empty():
            self.stats.fetches += 1
            self._store(request.session, snapshot)
            return self._render(snapshot)

        if entry is not None:
            # Stale is better than nothing: the Runtime context is explanatory,
            # never authoritative over the user's current words.
            self.stats.stale_fallbacks += 1
            return self._render(entry.snapshot)

        self.stats.empty += 1
        return None

    def prefetch(self, request: ContextRequest) -> bool:
        """Warm the cache in the background; never blocks and never raises.

        Args:
            request: Context request for the session just observed.

        Returns:
            ``True`` when a background fetch was scheduled.
        """
        if not self._settings.context_prefetch:
            return False
        entry = self._cache.get(request.session)
        if entry is not None and self._is_fresh(entry):
            return False
        if len(self._inflight) >= MAX_INFLIGHT_PREFETCHES:
            self.stats.prefetch_skipped += 1
            return False
        try:
            task = asyncio.create_task(
                self._prefetch(request),
                name=f"companion-runtime-prefetch:{request.session}",
            )
        except RuntimeError:
            # No running loop (e.g. called from a sync context): skip silently.
            self.stats.prefetch_skipped += 1
            return False
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return True

    def invalidate(self, session: str = "") -> None:
        """Drop cached context for one session, or for every session."""
        if session:
            self._cache.pop(session, None)
        else:
            self._cache.clear()

    async def aclose(self) -> None:
        """Cancel in-flight prefetches. Safe to call repeatedly."""
        tasks = list(self._inflight)
        self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _prefetch(self, request: ContextRequest) -> None:
        """Fetch and cache a snapshot outside the request path."""
        timeout_s = min(
            max(self._settings.request_timeout_s, self._settings.context_timeout_s),
            HARD_MAX_PREFETCH_TIMEOUT_S,
        )
        try:
            snapshot = await asyncio.wait_for(
                self._transport.fetch_context(request, timeout_s=timeout_s),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.debug(
                "context prefetch for %s failed: %s",
                request.session,
                truncate_error(exc),
            )
            return
        if snapshot is None or snapshot.is_empty():
            return
        self._store(request.session, snapshot)
        self.stats.prefetches += 1

    def _is_fresh(self, entry: _CacheEntry) -> bool:
        """Whether a cached snapshot is still within its TTL."""
        ttl = self._settings.context_cache_ttl_s
        if ttl <= 0:
            return False
        return (self._clock() - entry.fetched_at) < ttl

    def _store(self, session: str, snapshot: ContextSnapshot) -> None:
        """Cache a snapshot, evicting the least recently used session."""
        self._cache[session] = _CacheEntry(snapshot=snapshot, fetched_at=self._clock())
        self._cache.move_to_end(session)
        while len(self._cache) > max(1, self._settings.context_cache_max_sessions):
            self._cache.popitem(last=False)

    def _render(self, snapshot: ContextSnapshot) -> str | None:
        """Wrap a snapshot for prompt injection, respecting the length cap."""
        inner = snapshot.render().strip()
        if not inner:
            return None
        limit = self._settings.inject_max_chars
        if limit > 0 and len(inner) > limit:
            inner = inner[:limit].rstrip() + "\n…"
        version = snapshot.version.replace('"', "").replace("<", "").replace(">", "")
        version_attr = f' version="{version}"' if version else ""
        self.stats.injections += 1
        return f"<{CONTEXT_TAG}{version_attr}>\n{inner}\n</{CONTEXT_TAG}>"
