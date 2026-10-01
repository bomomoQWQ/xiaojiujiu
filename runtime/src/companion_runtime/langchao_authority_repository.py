"""Transactional repository for 「浪潮」 engine authority and dispatch claims."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .langchao_authority import AuthorityEngine, AuthorityMode, authority_may_dispatch


class AuthorityConflictError(RuntimeError):
    """A CAS, immutable revision, or idempotency identity conflicted."""


class AuthorityInFlightError(RuntimeError):
    """An authority switch did not account for work already in flight."""


class DispatchNotAuthorizedError(RuntimeError):
    """The active scoped authority is not permitted to dispatch."""


def _row(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def _canonical(value: Mapping[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _positive(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


class LangchaoAuthorityRepository:
    """Repository bound to one isolation scope.

    It does not invoke either engine and does not enqueue or send messages.  It only
    publishes authority and issues the SQL-backed capability claim a future sender
    integration can require.
    """

    def __init__(self, connection: Any, *, scope_key: str) -> None:
        self.connection = connection
        self.scope_key = _text(scope_key, "scope_key")

    def _transaction(self) -> Any:
        transaction = getattr(self.connection, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    def _lock_scope(self) -> None:
        self.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"langchao:authority:{self.scope_key}",),
        )

    def get_active(self) -> Any | None:
        return self.connection.execute(
            """SELECT r.*, a.pointer_version
               FROM langchao_authority_active AS a
               JOIN langchao_authority_revisions AS r
                 ON r.scope_key = a.scope_key
                AND r.authority_id = a.authority_id
                AND r.revision = a.revision
               WHERE a.scope_key = %s""",
            (self.scope_key,),
        ).fetchone()

    def bootstrap(
        self,
        *,
        engine_key: AuthorityEngine | str = AuthorityEngine.RUNTIME_V2,
        reason: str = "bootstrap existing runtime authority",
        created_at: datetime | None = None,
    ) -> Any:
        """Create the first authority: runtime_v2/live, or none/disabled."""

        engine = AuthorityEngine(engine_key)
        if engine not in (AuthorityEngine.RUNTIME_V2, AuthorityEngine.NONE):
            raise ValueError("bootstrap engine_key must be runtime_v2 or none")
        mode = AuthorityMode.LIVE if engine is AuthorityEngine.RUNTIME_V2 else AuthorityMode.DISABLED
        with self._transaction():
            self._lock_scope()
            active = self.get_active()
            if active is not None:
                return active
            authority_id = f"authority:{engine.value}"
            self._insert_revision(
                authority_id=authority_id,
                revision=1,
                engine_key=engine,
                mode=mode,
                reason=reason,
                payload={"operation": "bootstrap"},
                created_at=created_at,
            )
            cursor = self.connection.execute(
                """INSERT INTO langchao_authority_active
                   (scope_key, authority_id, revision, pointer_version)
                   VALUES (%s, %s, 1, 1)
                   ON CONFLICT (scope_key) DO NOTHING
                   RETURNING scope_key""",
                (self.scope_key, authority_id),
            )
            if cursor.fetchone() is None:
                raise AuthorityConflictError("authority bootstrap lost its first-writer CAS")
            return self.get_active()

    def switch_authority(
        self,
        *,
        engine_key: AuthorityEngine | str,
        mode: AuthorityMode | str,
        expected_pointer_version: int,
        in_flight_count: int,
        reason: str,
        transfer_refs: Sequence[str] = (),
        abort_refs: Sequence[str] = (),
        authority_id: str | None = None,
        created_at: datetime | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        """Append and CAS-publish a revision while freezing in-flight disposition."""

        engine = AuthorityEngine(engine_key)
        authority_mode = AuthorityMode(mode)
        if engine is AuthorityEngine.NONE and authority_mode is not AuthorityMode.DISABLED:
            raise ValueError("engine none is only valid in disabled mode")
        if not isinstance(expected_pointer_version, int) or isinstance(expected_pointer_version, bool) or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version must be a non-negative integer")
        if not isinstance(in_flight_count, int) or isinstance(in_flight_count, bool) or in_flight_count < 0:
            raise ValueError("in_flight_count must be a non-negative integer")
        transfers = tuple(_text(item, "transfer_ref") for item in transfer_refs)
        aborts = tuple(_text(item, "abort_ref") for item in abort_refs)
        if len(set(transfers + aborts)) != len(transfers) + len(aborts):
            raise ValueError("transfer_refs and abort_refs must be unique and disjoint")
        if in_flight_count and len(transfers) + len(aborts) != in_flight_count:
            raise AuthorityInFlightError("every in-flight item requires an explicit transfer or abort ref")
        if not in_flight_count and (transfers or aborts):
            raise ValueError("transfer/abort refs require in_flight_count > 0")
        reason = _text(reason, "reason")
        target_id = _text(authority_id or f"authority:{uuid4()}", "authority_id")
        audit = dict(payload or {})
        audit["in_flight"] = {
            "count": in_flight_count,
            "transfer_refs": list(transfers),
            "abort_refs": list(aborts),
        }
        with self._transaction():
            self._lock_scope()
            current = self.connection.execute(
                """SELECT authority_id, revision, pointer_version
                   FROM langchao_authority_active
                   WHERE scope_key = %s FOR UPDATE""",
                (self.scope_key,),
            ).fetchone()
            actual = 0 if current is None else int(_row(current, "pointer_version", 2))
            if actual != expected_pointer_version:
                raise AuthorityConflictError(f"authority pointer CAS failed: expected {expected_pointer_version}, found {actual}")
            latest = self.connection.execute(
                """SELECT revision FROM langchao_authority_revisions
                   WHERE scope_key = %s AND authority_id = %s
                   ORDER BY revision DESC LIMIT 1""",
                (self.scope_key, target_id),
            ).fetchone()
            revision = (0 if latest is None else int(_row(latest, "revision", 0))) + 1
            self._insert_revision(
                authority_id=target_id,
                revision=revision,
                engine_key=engine,
                mode=authority_mode,
                reason=reason,
                payload=audit,
                created_at=created_at,
            )
            cursor = self.connection.execute(
                """INSERT INTO langchao_authority_active
                   (scope_key, authority_id, revision, pointer_version)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (scope_key) DO UPDATE
                   SET authority_id = EXCLUDED.authority_id,
                       revision = EXCLUDED.revision,
                       pointer_version = EXCLUDED.pointer_version,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE langchao_authority_active.pointer_version = %s""",
                (self.scope_key, target_id, revision, actual + 1, expected_pointer_version),
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise AuthorityConflictError("authority pointer CAS failed")
            return self.get_active()

    def _insert_revision(
        self,
        *,
        authority_id: str,
        revision: int,
        engine_key: AuthorityEngine,
        mode: AuthorityMode,
        reason: str,
        payload: Mapping[str, Any],
        created_at: datetime | None,
    ) -> Any:
        stamp = created_at or datetime.now(timezone.utc)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        document = {
            "authority_id": authority_id,
            "engine_key": engine_key.value,
            "mode": mode.value,
            "reason": _text(reason, "reason"),
            "payload": dict(payload),
            "revision": _positive(revision, "revision"),
            "scope_key": self.scope_key,
        }
        encoded, digest = _canonical(document)
        return self.connection.execute(
            """INSERT INTO langchao_authority_revisions
               (scope_key, authority_id, revision, engine_key, mode, reason,
                payload, payload_sha256, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
               RETURNING *""",
            (self.scope_key, authority_id, revision, engine_key.value, mode.value,
             reason, encoded, digest, stamp),
        ).fetchone()

    def create_live_dispatch_claim(
        self,
        *,
        claim_id: str,
        round_id: str,
        candidate_id: str,
        candidate_version: str,
        attempt_id: str,
        render_outbox_id: str,
        idempotency_key: str,
        expected_engine: AuthorityEngine | str,
        created_at: datetime | None = None,
    ) -> Any:
        """Issue the production attempt/outbox witness under active live authority.

        The caller must invoke this on the same connection and transaction that inserts
        the attempt and render outbox.  Identity conflicts are explicit; an exact retry
        returns the existing immutable claim.
        """
        claim_id = _text(claim_id, "claim_id")
        round_id = _text(round_id, "round_id")
        candidate_id = _text(candidate_id, "candidate_id")
        candidate_version = _text(candidate_version, "candidate_version")
        attempt_id = _text(attempt_id, "attempt_id")
        render_outbox_id = _text(render_outbox_id, "render_outbox_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        engine = AuthorityEngine(expected_engine)
        if engine is AuthorityEngine.NONE:
            raise ValueError("expected_engine must be a live engine")
        stamp = created_at or datetime.now(timezone.utc)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        with self._transaction():
            self._lock_scope()
            authority = self.connection.execute(
                """SELECT r.authority_id, r.revision, r.engine_key, r.mode, r.may_dispatch
                   FROM langchao_authority_active AS a
                   JOIN langchao_authority_revisions AS r
                     ON r.scope_key = a.scope_key
                    AND r.authority_id = a.authority_id
                    AND r.revision = a.revision
                   WHERE a.scope_key = %s FOR UPDATE OF a""",
                (self.scope_key,),
            ).fetchone()
            if (authority is None
                    or str(_row(authority, "engine_key", 2)) != engine.value
                    or str(_row(authority, "mode", 3)) != AuthorityMode.LIVE.value
                    or not bool(_row(authority, "may_dispatch", 4))):
                raise DispatchNotAuthorizedError(
                    f"scope has no active {engine.value}/live dispatch authority"
                )
            authority_id = str(_row(authority, "authority_id", 0))
            authority_revision = int(_row(authority, "revision", 1))
            document = {
                "attempt_id": attempt_id,
                "authority_id": authority_id,
                "authority_revision": authority_revision,
                "candidate_id": candidate_id,
                "candidate_version": candidate_version,
                "claim_id": claim_id,
                "engine_key": engine.value,
                "idempotency_key": idempotency_key,
                "render_outbox_id": render_outbox_id,
                "round_id": round_id,
                "scope_key": self.scope_key,
            }
            _encoded, digest = _canonical(document)
            existing = self.connection.execute(
                """SELECT *, claim_sha256 = %s AS claim_matches
                   FROM live_dispatch_claims
                   WHERE scope_key = %s
                     AND (claim_id = %s OR attempt_id = %s OR render_outbox_id = %s
                          OR idempotency_key = %s OR (engine_key = %s AND round_id = %s))
                   FOR UPDATE""",
                (digest, self.scope_key, claim_id, attempt_id, render_outbox_id,
                 idempotency_key, engine.value, round_id),
            ).fetchall()
            if existing:
                if len(existing) == 1 and bool(_row(existing[0], "claim_matches", -1)):
                    return existing[0]
                raise AuthorityConflictError("live dispatch claim identity already has different content")
            return self.connection.execute(
                """INSERT INTO live_dispatch_claims
                   (scope_key, claim_id, authority_id, authority_revision, engine_key,
                    may_dispatch, round_id, candidate_id, candidate_version, attempt_id,
                    render_outbox_id, idempotency_key, claim_sha256, created_at)
                   VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (self.scope_key, claim_id, authority_id, authority_revision, engine.value,
                 round_id, candidate_id, candidate_version, attempt_id, render_outbox_id,
                 idempotency_key, digest, stamp),
            ).fetchone()

    def create_dispatch_claim(
        self,
        *,
        dispatch_id: str,
        candidate_id: str,
        candidate_revision: int,
        attempt_id: str,
        idempotency_key: str,
        created_at: datetime | None = None,
    ) -> Any:
        """Issue one claim against the exact currently active live authority/candidate."""

        dispatch_id = _text(dispatch_id, "dispatch_id")
        candidate_id = _text(candidate_id, "candidate_id")
        candidate_revision = _positive(candidate_revision, "candidate_revision")
        attempt_id = _text(attempt_id, "attempt_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        stamp = created_at or datetime.now(timezone.utc)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        with self._transaction():
            self._lock_scope()
            authority = self.connection.execute(
                """SELECT r.authority_id, r.revision, r.engine_key, r.mode, r.may_dispatch
                   FROM langchao_authority_active AS a
                   JOIN langchao_authority_revisions AS r
                     ON r.scope_key = a.scope_key
                    AND r.authority_id = a.authority_id
                    AND r.revision = a.revision
                   WHERE a.scope_key = %s FOR UPDATE OF a""",
                (self.scope_key,),
            ).fetchone()
            if authority is None or not bool(_row(authority, "may_dispatch", 4)) or str(_row(authority, "mode", 3)) != AuthorityMode.LIVE.value:
                raise DispatchNotAuthorizedError("scope has no active live dispatch authority")
            authority_id = str(_row(authority, "authority_id", 0))
            authority_revision = int(_row(authority, "revision", 1))
            engine_key = str(_row(authority, "engine_key", 2))
            document = {
                "attempt_id": attempt_id,
                "authority_id": authority_id,
                "authority_revision": authority_revision,
                "candidate_id": candidate_id,
                "candidate_revision": candidate_revision,
                "dispatch_id": dispatch_id,
                "engine_key": engine_key,
                "idempotency_key": idempotency_key,
                "scope_key": self.scope_key,
            }
            _encoded, digest = _canonical(document)
            existing = self.connection.execute(
                """SELECT *, claim_sha256 = %s AS claim_matches
                   FROM langchao_dispatch_claims
                   WHERE scope_key = %s
                     AND (dispatch_id = %s OR attempt_id = %s OR idempotency_key = %s)
                   FOR UPDATE""",
                (digest, self.scope_key, dispatch_id, attempt_id, idempotency_key),
            ).fetchall()
            if existing:
                if len(existing) == 1 and bool(_row(existing[0], "claim_matches", -1)):
                    return existing[0]
                raise AuthorityConflictError("dispatch claim identity already has different content")
            return self.connection.execute(
                """INSERT INTO langchao_dispatch_claims
                   (scope_key, dispatch_id, authority_id, authority_revision, engine_key,
                    may_dispatch, candidate_id, candidate_revision, attempt_id,
                    idempotency_key, claim_sha256, created_at)
                   VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (self.scope_key, dispatch_id, authority_id, authority_revision, engine_key,
                 candidate_id, candidate_revision, attempt_id, idempotency_key, digest, stamp),
            ).fetchone()


__all__ = [
    "AuthorityConflictError",
    "AuthorityInFlightError",
    "DispatchNotAuthorizedError",
    "LangchaoAuthorityRepository",
]
