"""The durable bound on how often a cold scope may reach out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from companion_runtime.langchao_live_repository import (
    LangchaoColdStartBudget,
    LangchaoLiveRepository,
)
from companion_runtime.langchao_runtime_adapter import (
    COLD_START_DRIVE_OUTCOME,
    COLD_START_EXPLORATION_LIMIT,
    COLD_START_EXPLORATION_MIN_SPACING,
    COLD_START_EXPLORATION_WINDOW,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return ()


class UsageConnection:
    """Answers the one counting query the budget issues, and records its parameters."""

    def __init__(self, *, used=0, last_at=None):
        self.used = used
        self.last_at = last_at
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        assert "jsonb_array_elements" in normalized, normalized
        self.queries.append((normalized, params))
        return Cursor(row={"used": self.used, "last_at": self.last_at})


def budget(connection) -> LangchaoColdStartBudget:
    return LangchaoColdStartBudget(connection, scope_key="scope")


def test_v2_defaults_bound_the_langchao_drive_to_the_same_numbers():
    """One policy, two engines: the numbers must not drift apart."""
    assert (COLD_START_EXPLORATION_LIMIT, COLD_START_EXPLORATION_WINDOW,
            COLD_START_EXPLORATION_MIN_SPACING) == (3, timedelta(hours=24), timedelta(hours=8))


def test_an_unused_budget_permits_the_drive_and_counts_the_right_outcome():
    connection = UsageConnection(used=0, last_at=None)
    assert budget(connection).available(now=NOW) is True
    _sql, params = connection.queries[0]
    assert params[0] == "scope"
    assert params[1] == NOW - COLD_START_EXPLORATION_WINDOW
    assert params[2] == COLD_START_DRIVE_OUTCOME


def test_the_window_limit_stops_further_cold_start_outreach():
    at_limit = UsageConnection(used=COLD_START_EXPLORATION_LIMIT,
                               last_at=NOW - timedelta(hours=9))
    assert budget(at_limit).available(now=NOW) is False
    below = UsageConnection(used=COLD_START_EXPLORATION_LIMIT - 1,
                            last_at=NOW - timedelta(hours=9))
    assert budget(below).available(now=NOW) is True


def test_the_minimum_spacing_stops_a_second_early_reach_out():
    too_soon = UsageConnection(used=1, last_at=NOW - timedelta(hours=2))
    assert budget(too_soon).available(now=NOW) is False
    spaced = UsageConnection(used=1, last_at=NOW - COLD_START_EXPLORATION_MIN_SPACING)
    assert budget(spaced).available(now=NOW) is True


def test_a_zero_limit_disables_cold_start_outreach_entirely():
    """The escape hatch for an operator who wants a purely predictive kernel."""
    connection = UsageConnection(used=0, last_at=None)
    assert budget(connection).available(now=NOW, limit=0) is False
    # Refused before any query, so a disabled drive cannot even be observed as usage.
    assert connection.queries == []


def test_usage_reports_the_count_and_the_last_reach_out():
    last = NOW - timedelta(hours=5)
    used, last_at = budget(UsageConnection(used=2, last_at=last)).usage(
        since=NOW - COLD_START_EXPLORATION_WINDOW
    )
    assert (used, last_at) == (2, last)


def test_live_repository_exposes_the_budget_for_this_scope():
    """The live path must be able to build the bound without knowing the SQL itself."""
    repository = LangchaoLiveRepository(UsageConnection(), scope_key="scope")
    assert repository.scope_key == "scope"
