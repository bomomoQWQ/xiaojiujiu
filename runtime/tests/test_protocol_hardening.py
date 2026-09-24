"""Focused hardening tests: the staleness boundary and wire timestamps.

Two contracts are pinned here, both of which were previously enforced one version
too late (or not at all):

1. **The staleness boundary.** A version gap that *reaches* a sensitivity's
   budget is already stale, so it must be re-coordinated (REBASE) instead of
   applied. The single exception that proves the rule is the ``critical`` budget
   of 0: a gap of 0 is currency, not drift, and must still apply. The same
   boundary drives :func:`protocol.should_discard_explanation`, so the two can
   never disagree about whether a result is fresh.
2. **Wire timestamps.** A timestamp that carries no explicit UTC offset is
   refused rather than read as UTC. The v0 API answers 422; the v1 API keeps its
   fail-open contract (HTTP 200, the record still ingested, its outcome and
   metadata stating that the Runtime substituted its own clock). The shipped
   adapter's aware ``...Z`` stamps are preserved to the second.

These live in their own module because they were added as a focused hardening
pass over ``protocol.py``, ``utility.py``, ``api.py`` and ``api_v1.py``; the
broader behaviour of those layers is covered by ``test_protocol.py``,
``test_api.py`` and ``test_api_v1.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime import protocol as protocol_module
from companion_runtime.api import create_app
from companion_runtime.runtime import Runtime
from companion_runtime.typing import EventType, ProtocolAction, TaskKind
from companion_runtime.utility import (
    NaiveTimestampError,
    parse_aware_datetime,
    parse_datetime,
    utcnow,
)

from conftest import BASE_TIME

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(runtime: Runtime):
    """A TestClient bound to a fresh Runtime (v0 and v1 routers alike)."""
    app = create_app(runtime, runtime.config)
    with TestClient(app) as test_client:
        yield test_client


#: Sensitivity class -> task type, so the boundary table can be exercised per class.
SENSITIVITY_TASKS = {
    "low": TaskKind.SHALLOW_TAG.value,
    "medium": TaskKind.EMOTION_EVAL.value,
    "high": TaskKind.CANDIDATE_GEN.value,
    "critical": TaskKind.PROACTIVE_DRAFT.value,
}

#: The shipped adapter's stamp format: UTC, millisecond precision, ``Z`` suffix.
ADAPTER_TIMESTAMP = "2026-03-01T09:00:00.000Z"


def proposal(task_type: str, *, gap: int, current_version: int = 100) -> protocol_module.Proposal:
    """Build a proposal sitting ``gap`` versions behind ``current_version``."""
    return protocol_module.Proposal(
        task_id=f"tsk_{task_type}_{gap}",
        task_type=task_type,
        based_on_version=current_version - gap,
        payload={"summary": "任何内容"},
    )


def classify_at(task_type: str, *, gap: int, current_version: int = 100):
    """Classify a proposal that is exactly ``gap`` versions behind."""
    return protocol_module.classify(
        proposal(task_type, gap=gap, current_version=current_version),
        current_version=current_version,
        source_events=[],
    )


# --------------------------------------------------------------------------------------
# 1. the staleness boundary
# --------------------------------------------------------------------------------------


def test_the_documented_budgets_are_unchanged() -> None:
    """The numbers the README documents are the numbers the protocol uses."""
    assert dict(protocol_module.STALENESS_BUDGET) == {
        "low": 50,
        "medium": 6,
        "high": 2,
        "critical": 0,
    }
    assert protocol_module.sensitivity_of(TaskKind.DEEP_REFRESH.value) == "low"


def test_a_gap_equal_to_the_budget_is_already_stale() -> None:
    """The boundary is inclusive on the stale side: gap == budget -> REBASE.

    A budget is the number of *tolerable* gaps, so "within the budget" means
    ``0 .. budget - 1``. Off by one here is not cosmetic: it lets a result apply
    against a state the protocol has already decided it is too old for.
    """
    for sensitivity, task_type in SENSITIVITY_TASKS.items():
        budget = protocol_module.STALENESS_BUDGET[sensitivity]
        tolerance = protocol_module.staleness_threshold(sensitivity)

        assert tolerance == max(1, budget), sensitivity
        below = classify_at(task_type, gap=tolerance - 1)
        assert below.action == ProtocolAction.APPLY.value, f"{sensitivity} inside budget"
        assert below.version_gap == tolerance - 1

        at = classify_at(task_type, gap=tolerance)
        assert at.action == ProtocolAction.REBASE.value, f"{sensitivity} at the boundary"
        assert at.reason == f"stale_by_{tolerance}_versions"
        assert at.rebase_notes, "a rebase must explain what has to be recomputed"

        beyond = classify_at(task_type, gap=tolerance + 1)
        assert beyond.action == ProtocolAction.REBASE.value, f"{sensitivity} past it"


def test_a_current_version_still_applies_at_any_sensitivity() -> None:
    """Gap 0 is currency, not drift -- including for the critical budget of 0.

    ``critical`` must tolerate no drift, and a naive ``gap > budget`` reading of
    that would rebase a draft dispatched against the *current* version. That is
    the one case the budget-0 row exists to describe, so it is pinned directly.
    """
    assert protocol_module.STALENESS_BUDGET["critical"] == 0
    assert protocol_module.staleness_threshold("critical") == 1
    for task_type in SENSITIVITY_TASKS.values():
        classification = classify_at(task_type, gap=0)
        assert classification.action == ProtocolAction.APPLY.value, task_type
        assert classification.reason == "fresh"
        assert classification.version_gap == 0
    # ...and the very next version does re-coordinate a critical result.
    assert classify_at(TaskKind.PROACTIVE_DRAFT.value, gap=1).action == (
        ProtocolAction.REBASE.value
    )


def test_an_unknown_sensitivity_uses_the_medium_boundary() -> None:
    """The fallback class and the fallback threshold agree with each other."""
    assert protocol_module.sensitivity_of("no-such-task") == "medium"
    assert protocol_module.staleness_threshold("no-such-task") == (
        protocol_module.staleness_threshold("medium")
    )


def test_the_explanation_discard_rule_shares_the_high_boundary() -> None:
    """One result can never be "fresh enough to apply" and "too stale to keep".

    ``classify`` re-coordinates where :func:`should_discard_explanation` drops,
    but both must switch at the same gap: a cached explanation that the protocol
    has already called stale must not keep being served.
    """
    task_type = TaskKind.EMOTION_EXPLAIN.value
    assert protocol_module.sensitivity_of(task_type) == "high"
    assert protocol_module.staleness_threshold("high") == 2

    for gap in range(0, 5):
        candidate = proposal(task_type, gap=gap)
        classification = classify_at(task_type, gap=gap)
        discarded = protocol_module.should_discard_explanation(
            candidate, current_version=100
        )
        stale = classification.action != ProtocolAction.APPLY.value
        assert discarded is stale, f"gap {gap}: classify says {classification.action}"

    assert protocol_module.should_discard_explanation(
        proposal(task_type, gap=1), current_version=100
    ) is False
    assert protocol_module.should_discard_explanation(
        proposal(task_type, gap=2), current_version=100
    ) is True


def test_the_boundary_is_the_one_the_reducer_acts_on(runtime: Runtime) -> None:
    """The classifier's decision is what the reducer records and applies.

    A boundary test that only called ``classify`` would keep passing if the
    reducer kept its own arithmetic, so the effect is asserted through the entry
    point that actually mutates state.
    """
    current = runtime.version()
    inside = protocol_module.Proposal(
        task_id="tsk_inside_budget",
        task_type=TaskKind.CANDIDATE_GEN.value,
        based_on_version=current - 1,
        payload={"operations": []},
    )
    assert runtime.reducer.process_proposal(inside).action == ProtocolAction.APPLY.value

    stale = protocol_module.Proposal(
        task_id="tsk_at_the_boundary",
        task_type=TaskKind.CANDIDATE_GEN.value,
        based_on_version=runtime.version() - 2,
        payload={"operations": []},
    )
    result = runtime.reducer.process_proposal(stale)
    assert result.action == ProtocolAction.REBASE.value
    assert result.reason == "stale_by_2_versions"
    assert result.applied is True, "REBASE still applies, it just recomputes"


# --------------------------------------------------------------------------------------
# 2. strict timestamp parsing
# --------------------------------------------------------------------------------------


def test_parse_aware_datetime_accepts_every_explicit_offset_form() -> None:
    """Aware stamps -- including the shipped adapter's -- parse to UTC."""
    expected = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    assert parse_aware_datetime("2026-03-01T09:00:00Z") == expected
    assert parse_aware_datetime("2026-03-01T09:00:00z") == expected
    assert parse_aware_datetime(ADAPTER_TIMESTAMP) == expected
    assert parse_aware_datetime("2026-03-01T09:00:00+00:00") == expected
    assert parse_aware_datetime("2026-03-01T17:00:00+08:00") == expected
    assert parse_aware_datetime("2026-03-01T17:00:00+0800") == expected
    assert parse_aware_datetime(expected) == expected
    assert parse_aware_datetime(None) is None
    assert parse_aware_datetime("") is None
    assert parse_aware_datetime("   ") is None


def test_parse_aware_datetime_refuses_a_naive_value() -> None:
    """No offset means unknown offset: refuse it instead of assuming UTC."""
    for naive in (
        "2026-03-01T09:00:00",
        "2026-03-01 09:00:00",
        "2026-03-01T09:00:00.123456",
        datetime(2026, 3, 1, 9, 0),
    ):
        with pytest.raises(NaiveTimestampError) as caught:
            parse_aware_datetime(naive, field="occurred_at")
        assert "occurred_at" in str(caught.value), "the error must name the field"
        assert "UTC offset" in str(caught.value)
    # A subclass of ValueError, so existing 422 translation keeps working.
    assert issubclass(NaiveTimestampError, ValueError)


def test_parse_aware_datetime_rejects_unparsable_and_non_string_values() -> None:
    """Garbage and wrong JSON types are client errors, not crashes."""
    for bad in ("not a date", 12345, 1.5, ["2026-03-01T09:00:00Z"]):
        with pytest.raises(ValueError) as caught:
            parse_aware_datetime(bad)
        assert not isinstance(caught.value, NaiveTimestampError), (
            "only a well-formed value without an offset is a naive one"
        )


def test_parse_datetime_keeps_reading_naive_values_as_utc() -> None:
    """The internal helper is deliberately unchanged: stored values are UTC.

    The refusal above is a wire-ingestion policy, not a change to how the Runtime
    reads back the naive strings SQLite hands it.
    """
    parsed = parse_datetime("2026-03-01T09:00:00")
    assert parsed == datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------
# 3. the v0 API refuses a naive timestamp
# --------------------------------------------------------------------------------------


def test_v0_event_with_a_naive_timestamp_is_a_422(client: TestClient, runtime: Runtime) -> None:
    """The value is refused, and nothing is appended on the way to the refusal."""
    response = client.post(
        "/events",
        json={
            "event_type": EventType.USER_MESSAGE.value,
            "content": "在吗",
            "timestamp": "2026-03-01T09:00:00",
        },
    )
    assert response.status_code == 422, response.text
    assert "UTC offset" in response.json()["detail"]
    assert "timestamp" in response.json()["detail"]
    assert runtime.events.count() == 0, "a refused request must not write history"


def test_v0_event_with_an_explicit_offset_is_recorded_at_that_instant(
    client: TestClient, runtime: Runtime
) -> None:
    """Aware stamps are preserved, not shifted: the same instant, three spellings."""
    for index, stamp in enumerate((ADAPTER_TIMESTAMP, "2026-03-01T09:00:00+00:00")):
        response = client.post(
            "/events",
            json={
                "event_type": EventType.ASSISTANT_MESSAGE.value,
                "content": f"reply {index}",
                "timestamp": stamp,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["event"]["timestamp"] == BASE_TIME.isoformat()

    # The same instant expressed in +08:00 must land on the same UTC moment.
    shifted = client.post(
        "/events",
        json={
            "event_type": EventType.ASSISTANT_MESSAGE.value,
            "content": "reply 2",
            "timestamp": "2026-03-01T17:00:00+08:00",
        },
    )
    assert shifted.status_code == 200, shifted.text
    assert shifted.json()["event"]["timestamp"] == BASE_TIME.isoformat()


def test_v0_timestamps_stay_optional_and_still_default_to_the_server_clock(
    client: TestClient, runtime: Runtime
) -> None:
    """An omitted field is not an error: the Runtime uses its own clock."""
    response = client.post(
        "/events",
        json={"event_type": EventType.ASSISTANT_MESSAGE.value, "content": "no stamp"},
    )
    assert response.status_code == 200, response.text
    recorded = datetime.fromisoformat(response.json()["event"]["timestamp"])
    assert abs((recorded - utcnow()).total_seconds()) < 60


def test_v0_rejects_a_naive_now_and_numeric_timestamps(client: TestClient) -> None:
    """Every timestamp field is covered, and a wrong type is a 422 rather than a 500."""
    naive_tick = client.post("/tick", json={"now": "2026-03-01T10:00:00"})
    assert naive_tick.status_code == 422, naive_tick.text
    assert "now" in naive_tick.json()["detail"]

    numeric_tick = client.post("/tick", json={"now": 1772000000})
    assert numeric_tick.status_code == 422, numeric_tick.text

    naive_query = client.get("/schedule", params={"hazard_wake_at": "2026-03-20T10:00:00"})
    assert naive_query.status_code == 422, naive_query.text
    assert "hazard_wake_at" in naive_query.json()["detail"]

    aware_tick = client.post("/tick", json={"now": BASE_TIME.isoformat()})
    assert aware_tick.status_code == 200, aware_tick.text


def test_v0_does_not_reject_a_proposal_that_carries_no_created_at(
    client: TestClient, runtime: Runtime
) -> None:
    """The strictness is about the value of a present field, not about its absence."""
    event = runtime.events.append(
        EventType.USER_MESSAGE,
        actor="user",
        content="家里出事了，我很难受",
        timestamp=BASE_TIME,
    )
    response = client.post(
        "/proposals",
        json={
            "task_id": "tsk_no_stamp",
            "task_type": TaskKind.EMOTION_EVAL.value,
            "based_on_version": runtime.version(),
            "source_event_ids": [event.event_id],
            "payload": {"direction": "-", "impact": 0.5, "activation": 0.4},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["action"] == ProtocolAction.APPLY.value

    naive_created_at = client.post(
        "/proposals",
        json={
            "task_id": "tsk_naive_stamp",
            "task_type": TaskKind.EMOTION_EVAL.value,
            "based_on_version": runtime.version(),
            "created_at": "2026-03-01T09:00:00",
            "payload": {"direction": "-", "impact": 0.5},
        },
    )
    assert naive_created_at.status_code == 422, naive_created_at.text
    assert "created_at" in naive_created_at.json()["detail"]


# --------------------------------------------------------------------------------------
# 4. the v1 API refuses it, fail-open
# --------------------------------------------------------------------------------------


def post_events(client: TestClient, events: list[dict]) -> dict:
    """Post one v1 event envelope and return the decoded body (asserting 200)."""
    response = client.post(
        "/v1/events",
        json={"protocol_version": "1", "adapter_id": "test-adapter", "events": events},
    )
    assert response.status_code == 200, response.text
    return response.json()


def event_record(event_id: str, kind: str, *, occurred_at: object) -> dict:
    """Build one ``EventRecord.to_wire()`` mapping with a chosen stamp."""
    return {
        "event_id": event_id,
        "kind": kind,
        "session": "webchat:FriendMessage:user-1",
        "text": "在吗",
        "occurred_at": occurred_at,
        "platform": "webchat",
    }


def test_v1_preserves_the_shipped_adapters_aware_stamp(
    client: TestClient, runtime: Runtime
) -> None:
    """The adapter's own format lands on the exact instant it named."""
    body = post_events(
        client, [event_record("evt_aware", "assistant_message", occurred_at=ADAPTER_TIMESTAMP)]
    )
    assert body["accepted"] == 1
    assert body["rejected"] == 0
    assert "timestamp_rejected" not in body["outcomes"][0]

    stored = runtime.events.get("evt_aware")
    assert stored is not None
    assert stored.timestamp == BASE_TIME
    assert "timestamp_rejected" not in stored.metadata["adapter"]


def test_v1_refuses_a_naive_stamp_without_reading_it_as_utc(
    client: TestClient, runtime: Runtime
) -> None:
    """Fail-open, but honest: the record survives, the guess does not.

    ``"2026-03-01T09:00:00"`` is exactly :data:`BASE_TIME` read as UTC, so the
    "silently interpreted" bug has a distinctive signature. It must be absent:
    the event is stamped with the Runtime's own clock instead, and the
    substitution is stated in the outcome and kept in the metadata.
    """
    body = post_events(
        client,
        [event_record("evt_naive", "assistant_message", occurred_at="2026-03-01T09:00:00")],
    )
    assert body["accepted"] == 1, "a bad stamp must not cost the adapter the batch"
    assert body["rejected"] == 0
    assert body["outcomes"][0]["timestamp_rejected"] == "naive_timestamp"

    stored = runtime.events.get("evt_naive")
    assert stored is not None, "the fact is still recorded"
    assert abs((stored.timestamp - BASE_TIME).total_seconds()) > 86_400, (
        "the naive value must not have been read as UTC"
    )
    assert abs((stored.timestamp - utcnow()).total_seconds()) < 60, (
        "the Runtime must substitute its own clock"
    )
    adapter = stored.metadata["adapter"]
    assert adapter["timestamp_rejected"] == "naive_timestamp"
    assert adapter["occurred_at_raw"] == "2026-03-01T09:00:00", (
        "the raw value the adapter sent stays available as evidence"
    )


def test_v1_user_message_reports_the_refused_stamp_in_its_outcome(
    client: TestClient, runtime: Runtime
) -> None:
    """The foreground path is covered too: a refusal never drops a user message."""
    body = post_events(
        client, [event_record("evt_naive_user", "user_message", occurred_at="2026-03-01T09:00:00")]
    )
    assert body["accepted"] == 1
    outcome = body["outcomes"][0]
    assert outcome["event"]["event_id"] == "evt_naive_user"
    assert outcome["timestamp_rejected"] == "naive_timestamp"
    assert runtime.events.exists("evt_naive_user") is True
    stored = runtime.events.get("evt_naive_user")
    assert stored.metadata["adapter"]["timestamp_rejected"] == "naive_timestamp"
    assert stored.timestamp != BASE_TIME, "the naive value must not have been read as UTC"


def test_v1_reports_an_unparsable_stamp_separately(
    client: TestClient, runtime: Runtime
) -> None:
    """A wrong format and a missing offset are different adapter bugs."""
    body = post_events(
        client,
        [
            event_record("evt_broken", "assistant_message", occurred_at="yesterday"),
            event_record("evt_missing_stamp", "assistant_message", occurred_at=""),
        ],
    )
    assert body["accepted"] == 2
    assert body["outcomes"][0]["timestamp_rejected"] == "unparsable_timestamp"
    # An absent stamp is not a refusal: it is simply not a claim about time.
    assert "timestamp_rejected" not in body["outcomes"][1]


def test_v1_stays_fail_open_across_a_mixed_batch(
    client: TestClient, runtime: Runtime
) -> None:
    """One unusable sibling never voids the batch, and nothing is lost."""
    body = post_events(
        client,
        [
            event_record("evt_batch_naive", "assistant_message", occurred_at="2026-03-01T09:00:00"),
            event_record("evt_batch_broken", "assistant_message", occurred_at="not a date"),
            event_record("evt_batch_aware", "assistant_message", occurred_at=ADAPTER_TIMESTAMP),
        ],
    )
    assert body["accepted"] == 3
    assert body["rejected"] == 0
    assert body["protocol_version"] == "1"

    assert [item.get("timestamp_rejected") for item in body["outcomes"]] == [
        "naive_timestamp",
        "unparsable_timestamp",
        None,
    ]
    assert runtime.events.count(EventType.ASSISTANT_MESSAGE.value) == 3
    # The healthy record is untouched by its siblings.
    assert runtime.events.get("evt_batch_aware").timestamp == BASE_TIME
