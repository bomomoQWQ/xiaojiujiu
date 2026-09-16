"""The deep-refresh ledger: every attempt leaves a row, including the quiet ones.

Why this table exists: the refresh is the only path that settles an event the coarse
classifier deferred, so after a week the question is not "did it run" but "why did
running it change nothing". The outcome already carried ``reason``, ``degraded``,
``operations``, ``applied``, ``violations`` and ``settled_events``; none of it was
persisted except a timestamp in ``runtime_state.meta``, so a refresh that silently
applied nothing was indistinguishable from one that was never attempted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from companion_runtime.config import RuntimeConfig
from companion_runtime.providers import (
    DeepRefreshRequest,
    DeepRefreshSuggestions,
    DisabledProvider,
)
from companion_runtime.runtime import Runtime


@pytest.fixture()
def runtime(tmp_path: Path) -> Runtime:
    """A Runtime on a scratch database, with the observability writer on."""
    config = RuntimeConfig()
    config.conversation_id = "default:FriendMessage:20001"
    config.storage.database_path = str(tmp_path / "companion.sqlite3")
    config.storage.raw_log_path = str(tmp_path / "raw_events.jsonl")
    config.observability.enabled = True
    return Runtime(config)


def _runs(runtime: Runtime) -> list[dict[str, object]]:
    """Read the ledger straight from the projection."""
    return runtime.projections.observability.list_refresh_runs(limit=50)


def test_a_disabled_refresh_is_still_recorded(runtime: Runtime) -> None:
    """``disabled`` is a verdict, not an absence: it must leave a row.

    A Runtime with the refresh switched off and one whose refreshes all declined
    look identical in the data unless the declined attempts are written down.
    """
    runtime.config.semantic.deep_refresh_enabled = False
    outcome = runtime.deep_refresh(force=True)
    assert outcome.reason == "disabled"
    runs = _runs(runtime)
    assert len(runs) == 1
    assert runs[0]["ran"] == 0
    assert runs[0]["reason"] == "disabled"


def test_a_refresh_without_a_provider_records_the_provider_name(runtime: Runtime) -> None:
    """The unavailable-provider case names who was missing, for the report."""
    runtime.semantic_provider = DisabledProvider()
    outcome = runtime.deep_refresh(force=True)
    assert outcome.reason == "provider_unavailable"
    runs = _runs(runtime)
    assert runs[-1]["reason"] == "provider_unavailable"
    assert runs[-1]["provider"] == "disabled"


def test_the_trigger_reason_is_kept_next_to_the_outcome(runtime: Runtime) -> None:
    """The row answers "why spend" and "what came of it" together.

    Reconstructing the trigger from a separate log line is exactly the manual
    archaeology this table replaces.
    """

    class _Provider:
        name = "fake"

        def available(self) -> bool:
            return True

        def deep_refresh(self, request: DeepRefreshRequest, **_: object):
            return DeepRefreshSuggestions(provider="fake", degraded=False)

    runtime.semantic_provider = _Provider()  # type: ignore[assignment]
    outcome = runtime.deep_refresh(force=True)
    assert outcome.reason == "empty_suggestions"
    runs = _runs(runtime)
    row = runs[-1]
    assert row["ran"] == 0
    assert row["reason"] == "empty_suggestions"
    assert row["trigger"] == "forced", "a forced run must not look like a scheduled one"
    payload = row["payload_json"]
    assert isinstance(payload, dict)
    assert payload["trigger"]["reason"]
    assert payload["degraded"] is False
    assert payload["operations"] == 0
    # ``ran`` means "a proposal reached the reducer", so a provider that answered
    # with nothing is recorded as ran=0 with the reason saying why -- that pair is
    # the distinction the report needs ("nothing needed doing" versus "the model
    # said nothing"), which one boolean could not carry.


def test_a_failed_refresh_records_its_violations(runtime: Runtime) -> None:
    """Grounding violations are the reason a "successful" run changed nothing."""

    class _Provider:
        name = "fake"

        def available(self) -> bool:
            return True

        def deep_refresh(self, request: DeepRefreshRequest, **_: object):
            return DeepRefreshSuggestions(
                provider="fake",
                degraded=False,
                reinterpretations=[{"content": "no sources given"}],
            )

    runtime.semantic_provider = _Provider()  # type: ignore[assignment]
    outcome = runtime.deep_refresh(force=True)
    assert outcome.operations == 0
    payload = _runs(runtime)[-1]["payload_json"]
    assert isinstance(payload, dict)
    assert payload["violations"], "an ungrounded suggestion must be visible afterwards"
    assert payload["violations"][0]["reason"] == "missing_sources"


def test_recording_can_be_switched_off(runtime: Runtime) -> None:
    """Observability stays optional; the refresh itself must not care."""
    runtime.config.observability.enabled = False
    runtime.deep_refresh(force=True)
    assert _runs(runtime) == []
