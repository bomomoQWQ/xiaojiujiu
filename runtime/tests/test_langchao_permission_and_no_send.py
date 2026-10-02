from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.langchao_no_send import NoSendReason, NoSendResult
from companion_runtime.langchao_permission import (
    PermissionEvent,
    PermissionVerdict,
    project_current_permission,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SCOPE = "fixture:user:permission"


def event(event_id: str, verdict: PermissionVerdict, *, occurred: int, ingested: int,
          revision: int = 1, scope: str = SCOPE) -> PermissionEvent:
    return PermissionEvent(
        event_id=event_id,
        scope_key=scope,
        verdict=verdict,
        occurred_at=NOW + timedelta(seconds=occurred),
        ingested_at=NOW + timedelta(seconds=ingested),
        revision=revision,
    )


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ((event("allow", PermissionVerdict.ALLOW, occurred=1, ingested=1),
          event("deny", PermissionVerdict.DENY, occurred=2, ingested=2)), PermissionVerdict.DENY),
        ((event("deny", PermissionVerdict.DENY, occurred=1, ingested=1),
          event("allow", PermissionVerdict.ALLOW, occurred=2, ingested=2)), PermissionVerdict.ALLOW),
    ],
)
def test_permission_projection_follows_temporal_successor(history, expected):
    projection = project_current_permission(history, scope_key=SCOPE)
    assert projection.verdict is expected
    assert projection.allowed is (expected is PermissionVerdict.ALLOW)


def test_permission_projection_is_independent_of_ingestion_order():
    allow = event("allow:late-ingest", PermissionVerdict.ALLOW, occurred=1, ingested=20)
    deny = event("deny:early-ingest", PermissionVerdict.DENY, occurred=2, ingested=3)
    forward = project_current_permission((allow, deny), scope_key=SCOPE)
    reverse = project_current_permission((deny, allow), scope_key=SCOPE)
    assert forward == reverse
    assert forward.verdict is PermissionVerdict.DENY
    assert forward.event_id == "deny:early-ingest"


def test_equal_occurrence_uses_ingested_at_then_revision_and_version_is_content_bound():
    first = event("permission", PermissionVerdict.DENY, occurred=1, ingested=2, revision=1)
    corrected = event("permission", PermissionVerdict.ALLOW, occurred=1, ingested=2, revision=2)
    result = project_current_permission((corrected, first), scope_key=SCOPE)
    assert result.verdict is PermissionVerdict.ALLOW
    assert result.revision == 2
    assert result.permission_version.startswith("permission:")
    assert result.permission_version == project_current_permission((first, corrected), scope_key=SCOPE).permission_version
    assert result.permission_version != project_current_permission((first,), scope_key=SCOPE).permission_version


def test_projection_ignores_other_scope_and_requires_current_scope_history():
    other = event("other", PermissionVerdict.DENY, occurred=99, ingested=99, scope="other")
    own = event("own", PermissionVerdict.ALLOW, occurred=1, ingested=1)
    assert project_current_permission((other, own), scope_key=SCOPE).event_id == "own"
    with pytest.raises(ValueError, match="no event"):
        project_current_permission((other,), scope_key=SCOPE)


@pytest.mark.parametrize(
    "reason",
    [
        NoSendReason.NO_ELIGIBLE_CANDIDATE,
        NoSendReason.DECISION_BUDGET_EXHAUSTED,
        NoSendReason.COMPETITION_STALEMATE,
        NoSendReason.PERMISSION_DENIED,
        NoSendReason.PERMISSION_REVOKED,
        NoSendReason.INVALIDATED,
        NoSendReason.DISPATCH_FAILED,
        NoSendReason.DELIVERY_UNKNOWN,
    ],
)
def test_unified_no_send_result_round_trips_each_reason(reason):
    result = NoSendResult(reason=reason, stage="fixture", round_id="round:1",
                          permission_version="permission:1")
    assert result.to_dict()["reason"] == reason.value
    assert NoSendResult.from_reason(reason.value, stage="fixture").reason is reason


def test_four_structural_no_send_classes_are_machine_distinct():
    outcomes = {
        NoSendResult(reason=NoSendReason.NO_ELIGIBLE_CANDIDATE, stage="candidate"),
        NoSendResult(reason=NoSendReason.DECISION_BUDGET_EXHAUSTED, stage="competition"),
        NoSendResult(reason=NoSendReason.PERMISSION_REVOKED, stage="permission"),
        NoSendResult(reason=NoSendReason.DISPATCH_FAILED, stage="dispatch"),
    }
    assert {item.reason.value for item in outcomes} == {
        "no_eligible_candidate", "decision_budget_exhausted",
        "permission_revoked", "dispatch_failed",
    }
