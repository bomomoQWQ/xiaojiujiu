from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

import companion_runtime.langchao_shadow_replay as replay_module
from companion_runtime.langchao_shadow_replay import (
    ReplaySafetyError,
    _write_json_atomic,
    compare_persisted,
    dsn_host,
    parse_as_of,
    replay_scope,
    require_allowed_host,
    schema_for_scope,
    verify_isolated_marker,
)

NOW = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class AuditConnection:
    def __init__(self, audits=(), shadows=()):
        self.audits = list(audits)
        self.shadows = list(shadows)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        if "runtime_v2_decision_audits" in sql:
            return Cursor(self.audits)
        if "langchao_shadow_runs" in sql:
            return Cursor(self.shadows)
        raise AssertionError(sql)


def audit(decision_id: str, chosen: str | None, reason: str):
    return {
        "decision_id": decision_id,
        "updated_at": NOW,
        "audit": {
            "run": {"chosen": chosen},
            "events": [{"stage": "reconciled", "details": {"reason": reason}}],
        },
    }


def shadow(decision_id: str, baseline, candidate, baseline_reason=None, shadow_reason=None):
    return {
        "idempotency_key": f"runtime-v2:{decision_id}",
        "candidate_id": candidate,
        "defer_reason": shadow_reason,
        "recorded_at": NOW,
        "comparison": {
            "baseline_candidate_id": baseline,
            "shadow_candidate_id": candidate,
            "baseline_defer_reason": baseline_reason,
            "shadow_defer_reason": shadow_reason,
        },
    }


def test_host_gate_accepts_exact_allowlist_and_rejects_other_hosts():
    dsn = "postgresql://user:secret@b2-shadow-db:5432/copy"
    assert dsn_host(dsn) == "b2-shadow-db"
    assert require_allowed_host(dsn, ["B2-SHADOW-DB."]) == "b2-shadow-db"
    with pytest.raises(ReplaySafetyError, match="not allowlisted"):
        require_allowed_host(dsn, ["production-db"])
    with pytest.raises(ReplaySafetyError, match="allowlist is empty"):
        require_allowed_host(dsn, [])


def test_keyword_dsn_requires_explicit_host():
    assert dsn_host("host='b2-copy' dbname=x user=u") == "b2-copy"
    with pytest.raises(ReplaySafetyError, match="explicit host"):
        dsn_host("dbname=x user=u")


def test_marker_is_only_read_and_hash_verified(tmp_path):
    marker = tmp_path / "isolated.marker"
    marker.write_bytes(b"B2 production-copy; no dispatch\n")
    digest = hashlib.sha256(marker.read_bytes()).hexdigest()
    assert verify_isolated_marker(marker, digest)["sha256"] == digest
    with pytest.raises(ReplaySafetyError, match="mismatch"):
        verify_isolated_marker(marker, "0" * 64)


def test_schema_derivation_matches_fleet_shape_and_is_stable():
    scope = "default:FriendMessage:20001"
    assert schema_for_scope(scope) == "cr_default_friendmessage_20001"
    assert schema_for_scope(scope) == schema_for_scope(scope)


def test_as_of_must_be_timezone_aware():
    assert parse_as_of("2026-10-02T09:00:00Z") == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_as_of("2026-10-02T09:00:00")


def test_comparison_uses_audits_as_denominator_and_reports_missing_shadow_unknown():
    connection = AuditConnection(
        audits=[audit("d1", "c1", "sent"), audit("d2", "c2", "sent")],
        shadows=[shadow("d1", "c1", "c1")],
    )
    result = compare_persisted(connection, scope="s", schema="cr_s", as_of=NOW)
    assert result.candidate_rows == 2
    assert result.covered_rows == 1
    assert result.coverage == 0.5
    assert result.agreements == 1
    assert result.agreement == 1.0
    assert result.unknown == 1
    assert result.executed_pure_assess is False


def test_no_candidate_audits_is_honest_zero_coverage_not_fabricated():
    result = compare_persisted(
        AuditConnection(audits=[], shadows=[shadow("orphan", None, "c1")]),
        scope="s",
        schema="cr_s",
        as_of=NOW,
    )
    assert result.candidate_rows == 0
    assert result.covered_rows == 0
    assert result.coverage == 0.0
    assert result.agreement is None
    assert result.unknown == 0


def test_defer_agreement_requires_candidate_and_reason_match():
    connection = AuditConnection(
        audits=[audit("d1", None, "no_eligible_candidate"), audit("d2", None, "no_eligible_candidate")],
        shadows=[
            shadow("d1", None, None, "no_eligible_candidate", "no_eligible_candidate"),
            shadow("d2", None, None, "no_eligible_candidate", "decision_budget_exhausted"),
        ],
    )
    result = compare_persisted(connection, scope="s", schema="cr_s", as_of=NOW)
    assert result.covered_rows == 2
    assert result.agreements == 1
    assert result.defers == 2


def test_cli_exposes_mandatory_isolation_gate_and_no_onebot_import():
    source = Path(__file__).parents[2] / "scripts" / "replay_langchao_shadow.py"
    text = source.read_text(encoding="utf-8").lower()
    assert '"--require-isolated-marker"' in text
    assert '"--isolated-marker-sha256"' in text
    assert '"--scope"' in text and 'default=11' in text
    assert "onebot" not in "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith('"')
    )


def test_reusable_module_has_no_dispatch_or_commit_dependencies():
    source = Path(__file__).parents[1] / "src" / "companion_runtime" / "langchao_shadow_replay.py"
    text = source.read_text(encoding="utf-8")
    forbidden_imports = ("api_v1", "LegacyRuntimeV2Bridge", "OneBot", "commit_candidate")
    assert all(item not in text for item in forbidden_imports)
    assert '"baseline_committed": False' in text
    assert '"sent": False' in text
    assert '"onebot_called": False' in text


def test_atomic_report_replaces_complete_json_and_leaves_no_temp(tmp_path):
    target = tmp_path / "summary.json"
    target.write_text('{"old":true}\n', encoding="utf-8")
    _write_json_atomic(target, {"new": True, "coverage": 0.0})
    assert target.read_text(encoding="utf-8") == (\
        '{\n  "coverage": 0.0,\n  "new": true\n}\n'
    )
    assert list(tmp_path.glob(".*.tmp-*")) == []


def test_replay_scope_reports_zero_protected_deltas(monkeypatch):
    class Migration:
        current_version = 20
        applied = ()
        already_present = tuple(range(1, 21))

    class Connection:
        def execute(self, sql, params=()):
            assert sql.startswith("SET search_path")

    calls = {"counts": 0, "authority": 0}

    def counts(_connection, *, scope):
        calls["counts"] += 1
        return {"outbox": 4, "attempts": 3, "exposures": 2, "expectations": 1}

    authority = {
        "engine_key": "runtime_v2", "mode": "live", "may_dispatch": True,
        "authority_id": "a", "revision": 1, "pointer_version": 1,
    }

    monkeypatch.setattr(replay_module, "migrate", lambda connection, schema: Migration())
    monkeypatch.setattr(replay_module, "protected_counts", counts)
    monkeypatch.setattr(replay_module, "active_authority", lambda connection, scope: dict(authority))
    monkeypatch.setattr(
        replay_module,
        "compare_persisted",
        lambda connection, scope, schema, as_of: replay_module.ScopeComparison(
            scope=scope, schema=schema, as_of="2026-10-02T09:00:00Z",
            candidate_rows=0, covered_rows=0, coverage=0.0, agreements=0,
            agreement=None, defers=0, unknown=0,
            baseline_candidate_counts={}, shadow_candidate_counts={},
        ),
    )
    report = replay_scope(Connection(), scope="s", schema="cr_s", as_of=NOW)
    assert report["protected_counts"]["delta"] == {
        "outbox": 0, "attempts": 0, "exposures": 0, "expectations": 0,
    }
    assert report["authority"]["unchanged"] is True
    assert report["safety"]["baseline_committed"] is False
    assert calls["counts"] == 2
