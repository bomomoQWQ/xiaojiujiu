from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from companion_runtime.mechanical_history_audit import BIGINT_MAX, audit_mechanical_plan
from companion_runtime.mechanical_history_import import (
    MechanicalMigrationPlan,
    PlannedMechanicalRow,
    SourceTableAudit,
    TABLE_SPECS,
)

AS_OF = "2025-06-01T00:00:00+00:00"


def _default(column: str, kind: str, nullable: bool):
    if nullable:
        return None
    if kind == "text":
        return f"{column}-value"
    if kind == "jsonb":
        return "[]" if column in {"source_event_ids", "topics_json", "resolution_conditions"} else "{}"
    if kind == "timestamptz":
        return "2025-01-01T00:00:00+00:00"
    if kind == "boolean":
        return True
    if kind == "bigint":
        return 1
    if kind == "double":
        return 0.5
    raise AssertionError(kind)


def _row(table: str, digest: str, **overrides) -> PlannedMechanicalRow:
    spec = next(item for item in TABLE_SPECS if item.name == table)
    values = tuple(
        (column, overrides.get(column, _default(column, kind, nullable)))
        for column, kind, nullable in spec.columns
    )
    return PlannedMechanicalRow(table, "private-scope", 0, values, digest * 64)


def _plan(*rows: PlannedMechanicalRow, include_rebuildable: bool = False) -> MechanicalMigrationPlan:
    counts = {spec.name: 0 for spec in TABLE_SPECS}
    for row in rows:
        counts[row.table] += 1
    audits = tuple(
        SourceTableAudit(spec.name, not spec.rebuildable, True, spec.column_names, (), counts[spec.name],
                         counts[spec.name], 0, "0" * 64)
        for spec in TABLE_SPECS if include_rebuildable or not spec.rebuildable
    )
    return MechanicalMigrationPlan(
        "/secret/snapshot.sqlite3", "private-scope", True, "ok", include_rebuildable,
        audits, tuple(rows), (), "f" * 64,
    )


def _codes(audit):
    return {(item.code, item.table) for item in audit.hard_errors}


def test_valid_references_boundary_summary_distributions_and_redaction() -> None:
    plan = _plan(
        _row("raw_events", "1", event_id="event-secret"),
        _row("event_semantics", "2", event_id="event-secret"),
        _row("interpretation_versions", "3", interpretation_id="i1", supersedes_id=None,
             source_event_ids='["event-secret"]'),
        _row("memories", "4", memory_id="m1", source_event_ids='["event-secret"]',
             status="active", archived_at=None),
        _row("memory_candidates", "5", candidate_id="c1", consolidated_memory_id="m1",
             source_event_ids='["event-secret"]'),
        _row("unfinished_matters", "6", unfinished_id="u1", status="waiting",
             source_event_ids='["event-secret"]'),
        _row("boundaries", "7", boundary_id="b1", source_event_id="event-secret",
             starts_at=None, expires_at="2025-07-01T00:00:00+00:00", revoked_at=None,
             allow_reply=True, allow_proactive=False),
    )
    audit = audit_mechanical_plan(plan, as_of=AS_OF)
    assert audit.ok
    assert dict(audit.unfinished_status_counts) == {"waiting": 1}
    assert dict(audit.boundary_state_counts) == {"active": 1}
    assert dict(audit.effective_active_boundary) == {"allow_proactive": False, "allow_reply": True}
    assert dict(audit.source_event_references) == {"missing": 0, "present": 4, "total": 4}
    encoded = json.dumps(audit.to_dict())
    assert "event-secret" not in encoded and "private-scope" not in encoded


def test_references_supersedes_cycle_and_archived_consistency_are_hard() -> None:
    audit = audit_mechanical_plan(_plan(
        _row("event_semantics", "1", event_id="missing"),
        _row("interpretation_versions", "2", interpretation_id="a", supersedes_id="b"),
        _row("interpretation_versions", "3", interpretation_id="b", supersedes_id="a"),
        _row("interpretation_versions", "4", interpretation_id="self", supersedes_id="self"),
        _row("interpretation_versions", "5", interpretation_id="orphan", supersedes_id="gone"),
        _row("memory_candidates", "6", candidate_id="c", consolidated_memory_id="gone"),
        _row("memories", "7", memory_id="m", status="archived", archived_at=None),
        _row("boundaries", "8", boundary_id="b", source_event_id="gone"),
    ), as_of=AS_OF)
    codes = _codes(audit)
    assert ("event_semantics_missing_raw_event", "event_semantics") in codes
    assert ("candidate_missing_memory", "memory_candidates") in codes
    assert ("interpretation_supersedes_cycle", "interpretation_versions") in codes
    assert ("interpretation_supersedes_self", "interpretation_versions") in codes
    assert ("interpretation_supersedes_missing", "interpretation_versions") in codes
    assert ("boundary_source_event_missing", "boundaries") in codes
    assert ("memory_archived_state_time_inconsistent", "memories") in codes


def test_json_id_bigint_natural_key_and_optional_projection_checks() -> None:
    first = _row("raw_events", "1", event_id="", seq=BIGINT_MAX + 1,
                 metadata_json="[]", source_event_ids='[1,"missing"]')
    duplicate = replace(first, row_sha256="2" * 64)
    audit = audit_mechanical_plan(
        _plan(first, duplicate, _row("activated_memories", "3", memory_id="m"),
              include_rebuildable=True), as_of=AS_OF
    )
    codes = _codes(audit)
    assert ("empty_required_id", "raw_events") in codes
    assert ("bigint_out_of_range", "raw_events") in codes
    assert ("json_top_level_type", "raw_events") in codes
    assert ("source_event_id_not_string", "raw_events") in codes
    assert ("duplicate_natural_key", "raw_events") in codes
    assert ("optional_projection_in_plan", None) in codes


def test_historical_pruning_is_reported_soft_and_strict_soft_upgrades() -> None:
    plan = _plan(_row("memories", "1", memory_id="m", source_event_ids='["pruned"]', status="active"))
    allowed = audit_mechanical_plan(plan, allow_historical_pruning=True, as_of=AS_OF)
    assert allowed.ok
    assert [(item.code, item.count) for item in allowed.soft_warnings] == [
        ("source_event_reference_missing", 1)
    ]
    strict = audit_mechanical_plan(plan, allow_historical_pruning=True, strict_soft=True, as_of=AS_OF)
    assert not strict.ok
    assert not strict.soft_warnings
    assert "source_event_reference_missing" in {item.code for item in strict.hard_errors}


def test_hash_and_output_are_stable_for_same_logical_rows() -> None:
    rows = (
        _row("raw_events", "1", event_id="one"),
        _row("raw_events", "2", event_id="two"),
    )
    first = audit_mechanical_plan(_plan(*rows), as_of=AS_OF)
    second = audit_mechanical_plan(_plan(*reversed(rows)), as_of=AS_OF)
    assert first.audit_sha256 == second.audit_sha256
    assert first.to_dict() == second.to_dict()
