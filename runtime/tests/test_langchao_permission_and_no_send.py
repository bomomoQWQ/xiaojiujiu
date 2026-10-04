from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.langchao_no_send import DELIVERY_STAGE, NoSendReason, NoSendResult
from companion_runtime.langchao_no_send_repository import (
    LangchaoNoSendConflictError,
    LangchaoNoSendRepository,
)
from companion_runtime.langchao_permission import (
    PermissionEvent,
    PermissionVerdict,
    project_current_permission,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SCOPE = "fixture:user:permission"


def test_every_no_send_reason_is_one_the_ledger_accepts():
    """The enum and the table's CHECK constraint must not drift apart.

    The table accepts a closed set of reasons. Adding ``cold_start_budget_exhausted`` to the
    enum without widening the constraint meant every cold-start refusal was rejected on
    insert: the refusal could not be recorded and the round failed instead of being
    accounted for. It happened 27 times in production before anyone read the logs.
    """
    import re

    from companion_runtime.langchao_no_send_schema import (
        LANGCHAO_NO_SEND_SCHEMA_V24_STATEMENTS,
    )

    table = LANGCHAO_NO_SEND_SCHEMA_V24_STATEMENTS[0]
    clause = re.search(r"reason IN \((.*?)\)\)", table, re.S)
    assert clause is not None, "the ledger's allowed reasons must be discoverable"
    allowed = set(re.findall(r"'([a-z_]+)'", clause.group(1)))
    assert allowed, "the constraint must list its reasons"

    undeclared = {reason.value for reason in NoSendReason} - allowed
    assert not undeclared, (
        f"these reasons would be rejected on insert: {sorted(undeclared)}; "
        f"the constraint allows {sorted(allowed)}"
    )


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


class _NoSendConnection:
    """Minimal store for the commit/no-send mutual exclusion rule."""

    def __init__(self, *, committed: bool):
        self.committed = committed
        self.rows: dict[str, str] = {}

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT 1 FROM langchao_live_commits"):
            return _NoSendCursor(row={"?": 1} if self.committed else None)
        if normalized.startswith("INSERT INTO langchao_no_send_results"):
            round_id = params[1]
            if round_id in self.rows:
                return _NoSendCursor(row=None)
            self.rows[round_id] = params[2]
            return _NoSendCursor(row={"round_id": round_id})
        if normalized.startswith("SELECT reason,stage,round_id"):
            return _NoSendCursor(row=None)
        raise AssertionError(normalized)


class _NoSendCursor:
    def __init__(self, *, row=None):
        self.row = row

    def fetchone(self):
        return self.row


def test_a_committed_round_may_still_record_its_failed_delivery():
    """Censoring a failed acknowledgement settles a commit; it is not a rival account.

    Refusing it would leave every failed send permanently unsettled, so the rule keys on
    the shared delivery stage rather than blocking any no-send for a committed round.
    """
    repository = LangchaoNoSendRepository(_NoSendConnection(committed=True), scope_key="scope")
    settled = NoSendResult(
        reason=NoSendReason.DISPATCH_FAILED, stage=DELIVERY_STAGE, round_id="round:1"
    )
    assert repository.save(settled) is settled


def test_a_committed_round_cannot_also_claim_it_never_dispatched():
    repository = LangchaoNoSendRepository(_NoSendConnection(committed=True), scope_key="scope")
    with pytest.raises(LangchaoNoSendConflictError):
        repository.save(NoSendResult(
            reason=NoSendReason.NO_ELIGIBLE_CANDIDATE, stage="selection", round_id="round:1"
        ))


def test_an_uncommitted_round_records_any_stage_normally():
    repository = LangchaoNoSendRepository(_NoSendConnection(committed=False), scope_key="scope")
    result = NoSendResult(
        reason=NoSendReason.NO_ELIGIBLE_CANDIDATE, stage="selection", round_id="round:2"
    )
    assert repository.save(result) is result
