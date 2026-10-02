"""Durable repository for the privacy deletion v1 coordinator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .privacy_deletion_schema import PRIVACY_DELETION_CONTRACT_VERSION


class DeletionStrategy(str, Enum):
    TOMBSTONE = "tombstone"
    CRYPTO_ERASURE = "crypto_erasure"
    PAYLOAD_REDACTION = "payload_redaction"


WORK_KINDS = (
    "source_protection",
    "social_invalidation",
    "goal_candidate_invalidation",
    "memory_interpretation_invalidation",
    "live_outbox_stop",
    "learning_artifact_invalidation",
    "outcome_evidence_minimization",
)


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletionRequest:
    scope_key: str
    request_id: str
    requested_by: str
    selector_kind: str
    selector: Mapping[str, Any]
    strategy: DeletionStrategy
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.scope_key.strip() or not self.request_id.strip():
            raise ValueError("scope_key and request_id are required")
        if self.requested_by not in {"user", "authorized_operator"}:
            raise ValueError("requested_by is not authorized")
        if self.selector_kind not in {"source", "conversation", "scope_exit"}:
            raise ValueError("unsupported deletion selector_kind")
        if self.selector_kind == "scope_exit" and self.selector:
            raise ValueError("scope_exit selector must be empty")
        if self.selector_kind != "scope_exit" and not self.selector:
            raise ValueError("selector is required")

    @property
    def selector_json(self) -> str:
        return canonical_json(dict(self.selector))

    @property
    def selector_digest(self) -> str:
        return digest(self.selector_json)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimedWork:
    scope_key: str
    request_id: str
    work_kind: str
    selector_kind: str
    selector: Mapping[str, Any]
    strategy: DeletionStrategy


class PrivacyDeletionConflict(RuntimeError):
    pass


class PrivacyDeletionRepository:
    def __init__(self, connection: Any, *, scope_key: str) -> None:
        if not scope_key.strip():
            raise ValueError("scope_key is required")
        self.connection = connection
        self.scope_key = scope_key

    def create_request(self, request: DeletionRequest) -> bool:
        if request.scope_key != self.scope_key:
            raise ValueError("deletion request belongs to another scope")
        with self.connection.transaction():
            row = self.connection.execute(
                """SELECT selector_digest,strategy,requested_by FROM privacy_deletion_requests_v1
                   WHERE scope_key=%s AND request_id=%s""",
                (self.scope_key, request.request_id),
            ).fetchone()
            if row is not None:
                values = _row(row, ("selector_digest", "strategy", "requested_by"))
                if values != (request.selector_digest, request.strategy.value, request.requested_by):
                    raise PrivacyDeletionConflict("request_id was reused with different deletion intent")
                return False
            self.connection.execute(
                """INSERT INTO privacy_deletion_requests_v1
                   (scope_key,request_id,requested_by,selector_kind,selector_digest,selector_json,
                    strategy,status,contract_version,requested_at)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,'pending',%s,%s)""",
                (self.scope_key, request.request_id, request.requested_by,
                 request.selector_kind, request.selector_digest, request.selector_json,
                 request.strategy.value, PRIVACY_DELETION_CONTRACT_VERSION, request.requested_at),
            )
            for ordinal, work_kind in enumerate(WORK_KINDS):
                self.connection.execute(
                    """INSERT INTO privacy_deletion_work_v1
                       (scope_key,request_id,work_kind,ordinal,status)
                       VALUES (%s,%s,%s,%s,'pending')""",
                    (self.scope_key, request.request_id, work_kind, ordinal),
                )
            self._audit(request.request_id, "requested", request.requested_at)
            return True

    def claim_next(self, *, request_id: str, worker_id: str, now: datetime,
                   lease_expires_at: datetime) -> ClaimedWork | None:
        with self.connection.transaction():
            row = self.connection.execute(
                """SELECT w.work_kind,r.selector_kind,r.selector_json,r.strategy
                   FROM privacy_deletion_work_v1 w
                   JOIN privacy_deletion_requests_v1 r USING (scope_key,request_id)
                   WHERE w.scope_key=%s AND w.request_id=%s
                     AND (w.status IN ('pending','failed')
                          OR (w.status='running' AND w.lease_expires_at <= %s))
                   ORDER BY w.ordinal FOR UPDATE OF w SKIP LOCKED LIMIT 1""",
                (self.scope_key, request_id, now),
            ).fetchone()
            if row is None:
                return None
            work_kind, selector_kind, selector_raw, strategy = _row(
                row, ("work_kind", "selector_kind", "selector_json", "strategy")
            )
            self.connection.execute(
                """UPDATE privacy_deletion_work_v1 SET status='running',attempt_count=attempt_count+1,
                   lease_owner=%s,lease_expires_at=%s,started_at=COALESCE(started_at,%s),last_error_code=NULL
                   WHERE scope_key=%s AND request_id=%s AND work_kind=%s""",
                (worker_id, lease_expires_at, now, self.scope_key, request_id, work_kind),
            )
            self.connection.execute(
                """UPDATE privacy_deletion_requests_v1 SET status='running',last_error_code=NULL
                   WHERE scope_key=%s AND request_id=%s AND status <> 'completed'""",
                (self.scope_key, request_id),
            )
            selector = json.loads(selector_raw) if isinstance(selector_raw, str) else selector_raw
            return ClaimedWork(scope_key=self.scope_key, request_id=request_id,
                               work_kind=str(work_kind), selector_kind=str(selector_kind),
                               selector=dict(selector), strategy=DeletionStrategy(str(strategy)))

    def complete_work(self, work: ClaimedWork, *, worker_id: str, now: datetime,
                      counts: Mapping[str, int]) -> None:
        clean_counts = {str(k): int(v) for k, v in counts.items() if int(v) >= 0}
        with self.connection.transaction():
            cursor = self.connection.execute(
                """UPDATE privacy_deletion_work_v1
                   SET status='completed',lease_owner=NULL,lease_expires_at=NULL,completed_at=%s,
                       result_counts=%s::jsonb,last_error_code=NULL
                   WHERE scope_key=%s AND request_id=%s AND work_kind=%s
                     AND status='running' AND lease_owner=%s RETURNING work_kind""",
                (now, canonical_json(clean_counts), self.scope_key, work.request_id,
                 work.work_kind, worker_id),
            )
            if cursor.fetchone() is None:
                raise PrivacyDeletionConflict("deletion work lease was lost")
            self._audit(work.request_id, "work_completed", now,
                        work_kind=work.work_kind, counts=clean_counts)

    def fail_work(self, work: ClaimedWork, *, worker_id: str, now: datetime,
                  error_code: str) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """UPDATE privacy_deletion_work_v1 SET status='failed',lease_owner=NULL,
                   lease_expires_at=NULL,last_error_code=%s
                   WHERE scope_key=%s AND request_id=%s AND work_kind=%s
                     AND status='running' AND lease_owner=%s""",
                (error_code, self.scope_key, work.request_id, work.work_kind, worker_id),
            )
            self.connection.execute(
                """UPDATE privacy_deletion_requests_v1 SET status='failed',last_error_code=%s
                   WHERE scope_key=%s AND request_id=%s""",
                (error_code, self.scope_key, work.request_id),
            )
            self._audit(work.request_id, "work_failed", now,
                        work_kind=work.work_kind, error_code=error_code)

    def finalize(self, *, request_id: str, now: datetime) -> bool:
        with self.connection.transaction():
            pending = self.connection.execute(
                """SELECT 1 FROM privacy_deletion_work_v1
                   WHERE scope_key=%s AND request_id=%s AND status <> 'completed' LIMIT 1""",
                (self.scope_key, request_id),
            ).fetchone()
            if pending is not None:
                return False
            cursor = self.connection.execute(
                """UPDATE privacy_deletion_requests_v1 SET status='completed',completed_at=%s,last_error_code=NULL
                   WHERE scope_key=%s AND request_id=%s AND status <> 'completed' RETURNING request_id""",
                (now, self.scope_key, request_id),
            )
            if cursor.fetchone() is not None:
                self._audit(request_id, "completed", now)
            return True

    def status(self, *, request_id: str) -> Mapping[str, Any] | None:
        row = self.connection.execute(
            """SELECT request_id,selector_kind,strategy,status,requested_at,completed_at,last_error_code
               FROM privacy_deletion_requests_v1 WHERE scope_key=%s AND request_id=%s""",
            (self.scope_key, request_id),
        ).fetchone()
        if row is None:
            return None
        keys = ("request_id", "selector_kind", "strategy", "status", "requested_at",
                "completed_at", "last_error_code")
        return dict(zip(keys, _row(row, keys), strict=True))

    def _audit(self, request_id: str, event_type: str, occurred_at: datetime, *,
               work_kind: str | None = None, counts: Mapping[str, int] | None = None,
               error_code: str | None = None) -> None:
        self.connection.execute(
            """INSERT INTO privacy_deletion_audit_v1
               (scope_key,request_id,event_seq,event_type,work_kind,occurred_at,counts,error_code)
               SELECT %s,%s,COALESCE(MAX(event_seq),0)+1,%s,%s,%s,%s::jsonb,%s
               FROM privacy_deletion_audit_v1 WHERE scope_key=%s AND request_id=%s""",
            (self.scope_key, request_id, event_type, work_kind, occurred_at,
             canonical_json(dict(counts or {})), error_code, self.scope_key, request_id),
        )


def _row(row: Any, keys: tuple[str, ...]) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        return tuple(row[key] for key in keys)
    return tuple(row[index] for index in range(len(keys)))


__all__ = [
    "ClaimedWork", "DeletionRequest", "DeletionStrategy", "PrivacyDeletionConflict",
    "PrivacyDeletionRepository", "WORK_KINDS", "digest",
]
