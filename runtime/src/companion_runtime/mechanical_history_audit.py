"""Read-only semantic audit gate for 「浪潮」 mechanical-history plans.

The planner validates SQLite shape and native values.  This module performs the
cross-row checks which must pass before a plan may enter the PostgreSQL applier.
It consumes only an immutable :class:`MechanicalMigrationPlan` and never opens or
writes PostgreSQL or SQLite.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Final, Iterable, Mapping

from .mechanical_history_import import MechanicalMigrationPlan, TABLE_SPECS
from .mechanical_history_import_apply import PRIMARY_KEYS

AUDIT_FORMAT: Final[str] = "langchao/mechanical-history-audit-v1"
BIGINT_MIN: Final[int] = -(1 << 63)
BIGINT_MAX: Final[int] = (1 << 63) - 1
OPTIONAL_PROJECTIONS: Final[frozenset[str]] = frozenset(
    spec.name for spec in TABLE_SPECS if spec.rebuildable
)
JSON_TOP_LEVEL: Final[dict[tuple[str, str], type]] = {
    ("raw_events", "metadata_json"): dict,
    ("raw_events", "source_event_ids"): list,
    ("interpretation_versions", "source_event_ids"): list,
    ("memories", "structured_json"): dict,
    ("memories", "topics_json"): list,
    ("memories", "source_event_ids"): list,
    ("memory_candidates", "source_event_ids"): list,
    ("memory_candidates", "topics_json"): list,
    ("memory_candidates", "structured_json"): dict,
    ("unfinished_matters", "source_event_ids"): list,
    ("unfinished_matters", "resolution_conditions"): list,
}
SOURCE_EVENT_ID_COLUMNS: Final[tuple[tuple[str, str], ...]] = tuple(
    key for key in JSON_TOP_LEVEL if key[1] == "source_event_ids"
)
# Keys with actual domain meaning beyond a table's surrogate primary key.
NATURAL_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "raw_events": ("event_id",),
    "event_semantics": ("event_id",),
    "interpretation_versions": ("target_kind", "target_id", "interpretation_version"),
    "memories": ("memory_id",),
    "memory_candidates": ("candidate_id",),
    "unfinished_matters": ("unfinished_id",),
    "boundaries": ("boundary_id",),
}
_SPECS = {spec.name: spec for spec in TABLE_SPECS}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identity_hash(values: Iterable[Any]) -> str:
    return _sha(sorted(_sha(value) for value in values))


@dataclass(frozen=True, slots=True)
class AuditFinding:
    code: str
    table: str | None
    count: int
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class MechanicalHistoryAudit:
    format: str
    scope_key_sha256: str
    plan_sha256: str
    as_of: str
    allow_historical_pruning: bool
    strict_soft: bool
    table_counts: tuple[tuple[str, int], ...]
    unfinished_status_counts: tuple[tuple[str, int], ...]
    boundary_state_counts: tuple[tuple[str, int], ...]
    boundary_source_event_counts: tuple[tuple[str, int], ...]
    effective_active_boundary: tuple[tuple[str, bool], ...]
    source_event_references: tuple[tuple[str, int], ...]
    hard_errors: tuple[AuditFinding, ...]
    soft_warnings: tuple[AuditFinding, ...]
    audit_sha256: str

    @property
    def ok(self) -> bool:
        return not self.hard_errors

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


class _Collector:
    def __init__(self, *, strict_soft: bool):
        self.strict_soft = strict_soft
        self.hard: list[AuditFinding] = []
        self.soft: list[AuditFinding] = []

    def add(self, code: str, table: str | None, evidence: Iterable[Any], *, soft: bool = False) -> None:
        items = list(evidence)
        if not items:
            return
        finding = AuditFinding(code, table, len(items), _identity_hash(items))
        (self.hard if self.strict_soft or not soft else self.soft).append(finding)


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("not canonical JSON text")
    return json.loads(value)


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("not timestamp text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone-naive timestamp")
    return parsed.astimezone(timezone.utc)


def _normalise_as_of(value: datetime | str | None) -> tuple[datetime, str]:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        parsed = _parse_time(value)
        assert parsed is not None
    elif isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        parsed = value.astimezone(timezone.utc)
    else:
        raise ValueError("as_of must be a timezone-aware datetime or timestamp")
    return parsed, parsed.isoformat()


def audit_mechanical_plan(
    plan: MechanicalMigrationPlan,
    *,
    allow_historical_pruning: bool = False,
    strict_soft: bool = False,
    as_of: datetime | str | None = None,
) -> MechanicalHistoryAudit:
    """Return a stable, redacted audit of one already-planned SQLite snapshot."""
    now, as_of_text = _normalise_as_of(as_of)
    findings = _Collector(strict_soft=strict_soft)
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    row_hashes: dict[str, list[str]] = defaultdict(list)
    for row in plan.rows:
        rows[row.table].append(row.as_dict())
        row_hashes[row.table].append(row.row_sha256)

    findings.add("planner_quarantine", None, (item.source_row_sha256 for item in plan.quarantine))
    optional = [digest for table in OPTIONAL_PROJECTIONS for digest in row_hashes[table]]
    if plan.include_rebuildable:
        optional.append(_sha("include_rebuildable"))
    findings.add("optional_projection_in_plan", None, optional)

    # Destination-shape checks are deliberately repeated here: this gate remains useful
    # when a plan was deserialised or manufactured outside the planner.
    for table, table_rows in sorted(rows.items()):
        spec = _SPECS.get(table)
        if spec is None:
            findings.add("unknown_table", table, row_hashes[table])
            continue
        kinds = {column: kind for column, kind, _nullable in spec.columns}
        required_ids = {
            column for column, _kind, nullable in spec.columns
            if not nullable and (column == PRIMARY_KEYS.get(table) or column.endswith("_id"))
        }
        for index, value_map in enumerate(table_rows):
            evidence = row_hashes[table][index]
            for column in required_ids:
                value = value_map.get(column)
                if not isinstance(value, str) or not value.strip():
                    findings.add("empty_required_id", table, [(evidence, column)])
            for column, kind in kinds.items():
                value = value_map.get(column)
                if kind == "bigint" and value is not None and (
                    type(value) is not int or value < BIGINT_MIN or value > BIGINT_MAX
                ):
                    findings.add("bigint_out_of_range", table, [(evidence, column)])
            for (json_table, column), expected in JSON_TOP_LEVEL.items():
                if json_table != table:
                    continue
                try:
                    decoded = _parse_json(value_map.get(column))
                except (TypeError, ValueError, json.JSONDecodeError):
                    findings.add("invalid_json", table, [(evidence, column)])
                    continue
                if type(decoded) is not expected:
                    findings.add("json_top_level_type", table, [(evidence, column)])
                if column == "source_event_ids" and isinstance(decoded, list):
                    bad = [(evidence, column, position) for position, item in enumerate(decoded)
                           if not isinstance(item, str)]
                    findings.add("source_event_id_not_string", table, bad)

    raw_ids = {row.get("event_id") for row in rows["raw_events"] if isinstance(row.get("event_id"), str)}
    semantic_missing = [row_hashes["event_semantics"][i] for i, row in enumerate(rows["event_semantics"])
                        if row.get("event_id") not in raw_ids]
    findings.add("event_semantics_missing_raw_event", "event_semantics", semantic_missing)

    memory_ids = {row.get("memory_id") for row in rows["memories"] if isinstance(row.get("memory_id"), str)}
    candidate_missing = [row_hashes["memory_candidates"][i]
                         for i, row in enumerate(rows["memory_candidates"])
                         if row.get("consolidated_memory_id") is not None
                         and row.get("consolidated_memory_id") not in memory_ids]
    findings.add("candidate_missing_memory", "memory_candidates", candidate_missing)

    interpretations = rows["interpretation_versions"]
    interpretation_ids = {row.get("interpretation_id") for row in interpretations
                          if isinstance(row.get("interpretation_id"), str)}
    missing_supersedes, self_supersedes = [], []
    graph: dict[str, str] = {}
    hash_by_interpretation: dict[str, str] = {}
    for index, row in enumerate(interpretations):
        identity, parent = row.get("interpretation_id"), row.get("supersedes_id")
        evidence = row_hashes["interpretation_versions"][index]
        if isinstance(identity, str):
            hash_by_interpretation[identity] = evidence
        if parent is not None and parent not in interpretation_ids:
            missing_supersedes.append(evidence)
        elif parent == identity and parent is not None:
            self_supersedes.append(evidence)
        elif isinstance(identity, str) and isinstance(parent, str):
            graph[identity] = parent
    findings.add("interpretation_supersedes_missing", "interpretation_versions", missing_supersedes)
    findings.add("interpretation_supersedes_self", "interpretation_versions", self_supersedes)
    cyclic: set[str] = set()
    for start in sorted(graph):
        trail: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in graph and current not in positions:
            positions[current] = len(trail)
            trail.append(current)
            current = graph[current]
        if current in positions:
            cyclic.update(trail[positions[current]:])
    findings.add("interpretation_supersedes_cycle", "interpretation_versions",
                 (hash_by_interpretation[item] for item in sorted(cyclic)))

    total_refs = missing_refs = 0
    missing_ref_evidence: list[Any] = []
    for table, column in SOURCE_EVENT_ID_COLUMNS:
        for index, row in enumerate(rows[table]):
            try:
                references = _parse_json(row.get(column))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(references, list):
                continue
            for position, event_id in enumerate(references):
                if not isinstance(event_id, str):
                    continue
                total_refs += 1
                if event_id not in raw_ids:
                    missing_refs += 1
                    missing_ref_evidence.append((row_hashes[table][index], position, _sha(event_id)))
    findings.add("source_event_reference_missing", None, missing_ref_evidence,
                 soft=allow_historical_pruning)

    boundary_states: Counter[str] = Counter()
    active_boundaries: list[dict[str, Any]] = []
    boundary_missing = []
    for index, row in enumerate(rows["boundaries"]):
        source = row.get("source_event_id")
        if source is not None and source not in raw_ids:
            boundary_missing.append(row_hashes["boundaries"][index])
        try:
            starts, expires, revoked = (_parse_time(row.get(name)) for name in
                                         ("starts_at", "expires_at", "revoked_at"))
        except ValueError:
            findings.add("boundary_invalid_time", "boundaries", [row_hashes["boundaries"][index]])
            continue
        if revoked is not None:
            state = "revoked"
        elif expires is not None and expires <= now:
            state = "expired"
        elif starts is not None and starts > now:
            state = "scheduled"
        else:
            state = "active"
            active_boundaries.append(row)
        boundary_states[state] += 1
    findings.add("boundary_source_event_missing", "boundaries", boundary_missing)

    memory_inconsistent = []
    for index, row in enumerate(rows["memories"]):
        if (row.get("status") == "archived") != (row.get("archived_at") is not None):
            memory_inconsistent.append(row_hashes["memories"][index])
    findings.add("memory_archived_state_time_inconsistent", "memories", memory_inconsistent)

    for table, key_columns in NATURAL_KEYS.items():
        grouped: dict[str, list[str]] = defaultdict(list)
        for index, row in enumerate(rows[table]):
            grouped[_sha([row.get(column) for column in key_columns])].append(row_hashes[table][index])
        duplicate_rows = [digest for group in grouped.values() if len(group) > 1 for digest in group]
        findings.add("duplicate_natural_key", table, duplicate_rows)

    hard = tuple(sorted(findings.hard, key=lambda item: (item.code, item.table or "", item.evidence_sha256)))
    soft = tuple(sorted(findings.soft, key=lambda item: (item.code, item.table or "", item.evidence_sha256)))
    table_counts = tuple((name, len(rows[name])) for name in sorted(rows))
    unfinished_counts = tuple(sorted(Counter(str(row.get("status")) for row in rows["unfinished_matters"]).items()))
    boundary_counts = tuple(sorted(boundary_states.items()))
    boundary_source_counts = (
        ("missing", len(boundary_missing)),
        ("present", sum(1 for row in rows["boundaries"]
                        if row.get("source_event_id") is not None
                        and row.get("source_event_id") in raw_ids)),
        ("without_source", sum(1 for row in rows["boundaries"]
                               if row.get("source_event_id") is None)),
    )
    effective = (
        ("allow_proactive", all(row.get("allow_proactive") is True for row in active_boundaries)),
        ("allow_reply", all(row.get("allow_reply") is True for row in active_boundaries)),
    )
    references = (("missing", missing_refs), ("present", total_refs - missing_refs), ("total", total_refs))
    body = {
        "format": AUDIT_FORMAT,
        "scope_key_sha256": _sha(plan.scope_key),
        "plan_sha256": plan.plan_sha256,
        "as_of": as_of_text,
        "allow_historical_pruning": allow_historical_pruning,
        "strict_soft": strict_soft,
        "table_counts": table_counts,
        "unfinished_status_counts": unfinished_counts,
        "boundary_state_counts": boundary_counts,
        "boundary_source_event_counts": boundary_source_counts,
        "effective_active_boundary": effective,
        "source_event_references": references,
        "hard_errors": [asdict(item) for item in hard],
        "soft_warnings": [asdict(item) for item in soft],
    }
    return MechanicalHistoryAudit(
        format=AUDIT_FORMAT,
        scope_key_sha256=body["scope_key_sha256"],
        plan_sha256=plan.plan_sha256,
        as_of=as_of_text,
        allow_historical_pruning=allow_historical_pruning,
        strict_soft=strict_soft,
        table_counts=table_counts,
        unfinished_status_counts=unfinished_counts,
        boundary_state_counts=boundary_counts,
        boundary_source_event_counts=boundary_source_counts,
        effective_active_boundary=effective,
        source_event_references=references,
        hard_errors=hard,
        soft_warnings=soft,
        audit_sha256=_sha(body),
    )


__all__ = [
    "AUDIT_FORMAT", "BIGINT_MAX", "BIGINT_MIN", "AuditFinding",
    "MechanicalHistoryAudit", "audit_mechanical_plan",
]
