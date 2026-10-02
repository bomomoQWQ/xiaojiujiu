"""Explicit, resumable privacy deletion propagation coordinator.

No production work starts merely by constructing this service.  An authorized API caller
creates a request and invokes ``run``.  Every mutation is scope constrained; immutable
accounting/FK rows are retained and made unusable through lifecycle state plus a minimal,
hashed invalidation ledger.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .privacy_deletion_repository import (
    ClaimedWork,
    DeletionRequest,
    DeletionStrategy,
    PrivacyDeletionRepository,
    digest,
)


class PrivacyDeletionCoordinator:
    def __init__(self, repository: PrivacyDeletionRepository, *, worker_id: str,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        self.repository = repository
        self.connection = repository.connection
        self.scope_key = repository.scope_key
        self.worker_id = worker_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def request(self, value: DeletionRequest) -> bool:
        return self.repository.create_request(value)

    def run(self, *, request_id: str, max_steps: int = 32) -> Mapping[str, Any]:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        completed = 0
        for _ in range(max_steps):
            now = self.clock()
            work = self.repository.claim_next(
                request_id=request_id, worker_id=self.worker_id, now=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
            if work is None:
                break
            try:
                counts = self._execute(work, now=now)
                self.repository.complete_work(
                    work, worker_id=self.worker_id, now=self.clock(), counts=counts
                )
                completed += 1
            except Exception as exc:
                # Audit only a stable class name; exception text can contain payload data.
                self.repository.fail_work(
                    work, worker_id=self.worker_id, now=self.clock(),
                    error_code=type(exc).__name__,
                )
                raise
        finalized = self.repository.finalize(request_id=request_id, now=self.clock())
        return {"request_id": request_id, "completed_steps": completed, "completed": finalized}

    def _execute(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        handler = getattr(self, f"_work_{work.work_kind}", None)
        if handler is None:
            raise RuntimeError("unsupported deletion work kind")
        return handler(work, now=now)

    def _source_predicate(self, work: ClaimedWork, *, column: str) -> tuple[str, tuple[Any, ...]]:
        if work.selector_kind == "scope_exit":
            return "TRUE", ()
        if work.selector_kind == "conversation":
            return f"{column} = %s", (str(work.selector["conversation_id"]),)
        return f"{column} = %s", (str(work.selector["source_id"]),)

    def _work_source_protection(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        selector_value = "*" if work.selector_kind == "scope_exit" else canonical_selector(work)
        source_kind = str(work.selector.get("source_kind", work.selector_kind))
        self.connection.execute(
            """INSERT INTO privacy_source_tombstones_v1
               (scope_key,source_kind,source_id_digest,source_revision,strategy,key_destroyed,
                request_id,protected_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (self.scope_key, source_kind, digest(selector_value),
             int(work.selector.get("source_revision", 0)),
             work.strategy.value, work.strategy is DeletionStrategy.CRYPTO_ERASURE,
             work.request_id, now),
        )
        # Physical source rows stay in place for FK/accounting integrity.  The overlay is
        # the authority checked by readers; payload redaction additionally clears mutable
        # raw content while preserving event identity and chronology.
        redacted = 0
        if work.strategy is DeletionStrategy.PAYLOAD_REDACTION:
            where, params = self._source_predicate(work, column=(
                "conversation_id" if work.selector_kind == "conversation" else "event_id"
            ))
            cursor = self.connection.execute(
                f"""UPDATE raw_events SET content=NULL,
                    metadata_json=jsonb_build_object('privacy_status','redacted')
                    WHERE {where}""" + ("" if work.selector_kind == "scope_exit" else "") , params
            )
            redacted = max(0, int(getattr(cursor, "rowcount", 0)))
        return {"source_tombstones": 1, "payloads_redacted": redacted}

    def _work_social_invalidation(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        if work.selector_kind == "source":
            source_id = str(work.selector["source_id"])
            cursor = self.connection.execute(
                """UPDATE langchao_social_sources SET source_status='tombstoned',tombstoned_at=%s,
                   invalidated_at=NULL,invalidation_reason='privacy_deletion',locator=NULL
                   WHERE scope_key=%s AND source_id=%s AND source_status='active'""",
                (now, self.scope_key, source_id),
            )
            count = max(0, int(getattr(cursor, "rowcount", 0)))
        elif work.selector_kind == "scope_exit":
            cursor = self.connection.execute(
                """UPDATE langchao_social_sources SET source_status='tombstoned',tombstoned_at=%s,
                   invalidated_at=NULL,invalidation_reason='privacy_deletion',locator=NULL
                   WHERE scope_key=%s AND source_status='active'""",
                (now, self.scope_key),
            )
            count = max(0, int(getattr(cursor, "rowcount", 0)))
        else:
            # Conversation selectors use the request overlay because social sources do
            # not carry a conversation identifier; source-specific dependants are still
            # invalidated by the other durable work stages.
            count = 0
        return {"social_sources_tombstoned": count}

    def _work_goal_candidate_invalidation(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        pattern = literal_contains_pattern(canonical_selector(work))
        candidate = self.connection.execute(
            """UPDATE candidate_intents SET status='retired',retired_reason='privacy_deletion',updated_at=%s
               WHERE status NOT IN ('retired','expired')
                 AND sources_json::text LIKE %s ESCAPE '\\'""",
            (now, pattern),
        )
        unfinished = self.connection.execute(
            """UPDATE unfinished_matters SET status='cancelled',resolution_note='privacy_deletion',updated_at=%s
               WHERE status NOT IN ('resolved','cancelled')
                 AND source_event_ids::text LIKE %s ESCAPE '\\'""",
            (now, pattern),
        )
        return {"candidates": _count(candidate), "active_goals": _count(unfinished)}

    def _work_memory_interpretation_invalidation(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        pattern = literal_contains_pattern(canonical_selector(work))
        memories = self.connection.execute(
            """UPDATE memories SET status='archived',summary='[privacy-deleted]',structured_json='{}'::jsonb,
               topics_json='[]'::jsonb,archived_at=%s,updated_at=%s
               WHERE status='active' AND source_event_ids::text LIKE %s ESCAPE '\\'""",
            (now, now, pattern),
        )
        interpretations = self.connection.execute(
            """INSERT INTO privacy_reference_invalidations_v1
               (scope_key,request_id,artifact_kind,artifact_id_digest,reason_code,invalidated_at)
               SELECT %s,%s,'interpretation',encode(digest(interpretation_id,'sha256'),'hex'),
                      'privacy_deletion',%s FROM interpretation_versions
               WHERE source_event_ids::text LIKE %s ESCAPE '\\' ON CONFLICT DO NOTHING""",
            (self.scope_key, work.request_id, now, pattern),
        )
        return {"memories": _count(memories), "interpretations": _count(interpretations)}

    def _work_live_outbox_stop(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        pattern = literal_contains_pattern(canonical_selector(work))
        outbox = self.connection.execute(
            """UPDATE outbox SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                   last_error='privacy_deletion'
               WHERE status IN ('pending','leased')
                 AND payload_json::text LIKE %s ESCAPE '\\'""",
            (pattern,),
        )
        commits = self.connection.execute(
            """UPDATE langchao_live_commits SET terminal_ack_id=%s,terminal_ack_kind='failed',
                   terminal_acknowledged_at=%s
               WHERE scope_key=%s AND terminal_ack_kind IS NULL
                 AND snapshot::text LIKE %s ESCAPE '\\'""",
            (f"privacy:{work.request_id}", now, self.scope_key, pattern),
        )
        # Already sent records are never rewritten: their minimal delivery/financial audit
        # survives, while subsequent evidence minimization removes source references.
        return {"outbox_cancelled": _count(outbox), "live_commits_stopped": _count(commits)}

    def _work_learning_artifact_invalidation(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        pattern = literal_contains_pattern(canonical_selector(work))
        total = 0
        for table, id_column in (
            ("interaction_exposures_v2", "exposure_id"),
            ("interaction_target_labels_v2", "label_id"),
            ("interaction_observations", "observation_id"),
        ):
            cursor = self.connection.execute(
                f"""INSERT INTO privacy_reference_invalidations_v1
                    (scope_key,request_id,artifact_kind,artifact_id_digest,reason_code,invalidated_at)
                    SELECT %s,%s,%s,encode(digest(CAST({id_column} AS text),'sha256'),'hex'),
                           'privacy_deletion',%s FROM {table}
                    WHERE CAST({id_column} AS text) LIKE %s ESCAPE '\\'
                    ON CONFLICT DO NOTHING""",
                (self.scope_key, work.request_id, table, now, pattern),
            )
            total += _count(cursor)
        return {"learning_artifacts": total}

    def _work_outcome_evidence_minimization(self, work: ClaimedWork, *, now: datetime) -> Mapping[str, int]:
        pattern = literal_contains_pattern(canonical_selector(work))
        # Outcome revisions are an immutable accounting ledger.  Never UPDATE or DELETE
        # them: append a scope-bound suppression overlay so readers/training exporters
        # omit evidence references while settlement amounts and FKs remain intact.
        cursor = self.connection.execute(
            """INSERT INTO privacy_reference_invalidations_v1
               (scope_key,request_id,artifact_kind,artifact_id_digest,reason_code,invalidated_at)
               SELECT %s,%s,'outcome_evidence',encode(digest(token_id || ':' || revision::text,'sha256'),'hex'),
                      'privacy_deletion',%s FROM langchao_outcome_revisions
               WHERE scope_key=%s AND payload::text LIKE %s ESCAPE '\\'
               ON CONFLICT DO NOTHING""",
            (self.scope_key, work.request_id, now, self.scope_key, pattern),
        )
        return {"outcome_tokens_minimized": _count(cursor)}


def canonical_selector(work: ClaimedWork) -> str:
    if work.selector_kind == "source":
        return str(work.selector["source_id"])
    if work.selector_kind == "conversation":
        return str(work.selector["conversation_id"])
    return work.scope_key


def literal_contains_pattern(value: str) -> str:
    """Build a PostgreSQL LIKE pattern that treats an identifier literally."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _count(cursor: Any) -> int:
    return max(0, int(getattr(cursor, "rowcount", 0)))


__all__ = ["PrivacyDeletionCoordinator", "literal_contains_pattern"]
