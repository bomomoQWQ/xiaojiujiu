"""Transactional repository for 「浪潮」 social-emotional memory v1.

The repository is deliberately authority-bearing: builders and models only submit
immutable proposals.  Commit re-resolves every exact source reference, serialises a
scope-wide projection transition with a PostgreSQL advisory lock, and advances item
heads with compare-and-swap semantics.  Invalidations append history and never delete
facts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .langchao_social_types import (
    LANGCHAO_SOCIAL_CONTRACT_VERSION,
    ProposalStatus,
    SocialItemStatus,
    SocialItemType,
    SocialProposal,
    SourceKind,
    SourceRef,
    canonical_json,
    sha256_json,
)


class LangchaoSocialError(RuntimeError):
    """Base class for social-memory repository failures."""


class LangchaoSocialConflictError(LangchaoSocialError):
    """An immutable coordinate or idempotency key was reused differently."""


class LangchaoSocialSourceError(LangchaoSocialError):
    """A resolver returned an invalid or contradictory source result."""


class LangchaoSocialCASConflictError(LangchaoSocialConflictError):
    """The active projection changed since the caller read it."""


class SourceResolutionStatus(str, Enum):
    ACTIVE = "active"
    TOMBSTONED = "tombstoned"
    INVALIDATED = "invalidated"
    MISSING = "missing"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceResolution:
    """Resolver's authoritative view of one requested source coordinate.

    ``source_revision`` and ``source_sha256`` describe the currently authoritative
    source.  They may differ from the requested reference, in which case commit asks
    the caller to rebuild instead of silently accepting stale model output.
    """

    scope_key: str
    source_kind: SourceKind
    source_id: str
    source_revision: int | None
    source_sha256: str | None
    status: SourceResolutionStatus
    observed_at: datetime | None = None
    locator: str | None = None
    reason: str | None = None


@runtime_checkable
class SourceResolver(Protocol):
    """Authority used by commit to verify every proposal source."""

    def resolve_source(self, source: SourceRef) -> SourceResolution: ...


class CommitDisposition(str, Enum):
    COMMITTED = "committed"
    IDEMPOTENT = "idempotent"
    REBASE = "rebase"
    DISCARDED = "discarded"


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitResult:
    disposition: CommitDisposition
    proposal_id: str
    proposal_revision: int
    pointer_version: int | None = None
    item_revisions: tuple[tuple[str, int], ...] = ()
    stale_sources: tuple[SourceResolution, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildRun:
    build_run_id: str
    revision: int
    builder_version: str
    input_sha256: str
    output_sha256: str
    status: str
    started_at: datetime
    completed_at: datetime
    degradation_reason: str | None = None


def _value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    return row[key] if isinstance(row, Mapping) else row[index]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _event_id(prefix: str, material: Mapping[str, Any]) -> str:
    return f"{prefix}:{sha256_json(material)}"


class LangchaoSocialRepository:
    """Single-scope PostgreSQL repository for social proposal authority."""

    def __init__(self, connection: Any, *, scope_key: str, source_resolver: SourceResolver) -> None:
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key must be a non-empty string")
        if not isinstance(source_resolver, SourceResolver):
            raise TypeError("source_resolver must implement resolve_source(SourceRef)")
        if not callable(getattr(connection, "transaction", None)):
            raise TypeError("connection must provide transactional transaction() support")
        self.connection = connection
        self.scope_key = scope_key
        self.source_resolver = source_resolver

    def _transaction(self) -> Any:
        return self.connection.transaction()

    @staticmethod
    def _positive(name: str, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _pointer(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("expected_projection_pointer_version must be non-negative")

    def record_build_run(self, run: BuildRun) -> Any:
        """Persist a build run idempotently by its deterministic input identity."""
        if not isinstance(run, BuildRun):
            raise TypeError("run must be BuildRun")
        self._positive("run revision", run.revision)
        if run.status not in {"completed", "degraded", "failed"}:
            raise ValueError("unsupported build run status")
        if (run.status == "completed") != (run.degradation_reason is None):
            raise ValueError("only non-completed runs require degradation_reason")
        with self._transaction():
            existing_input = self.connection.execute(
                """SELECT * FROM langchao_social_build_runs
                   WHERE scope_key = %s AND builder_version = %s AND input_sha256 = %s""",
                (self.scope_key, run.builder_version, run.input_sha256),
            ).fetchone()
            if existing_input is not None:
                immutable = (
                    str(_value(existing_input, "build_run_id")) == run.build_run_id
                    and int(_value(existing_input, "revision", 1)) == run.revision
                    and str(_value(existing_input, "output_sha256")) == run.output_sha256
                    and str(_value(existing_input, "status")) == run.status
                )
                if not immutable:
                    raise LangchaoSocialConflictError("build input was replayed with different output")
                return existing_input
            existing_id = self.connection.execute(
                """SELECT * FROM langchao_social_build_runs
                   WHERE scope_key = %s AND build_run_id = %s AND revision = %s""",
                (self.scope_key, run.build_run_id, run.revision),
            ).fetchone()
            if existing_id is not None:
                raise LangchaoSocialConflictError("build run coordinate has different input")
            return self.connection.execute(
                """INSERT INTO langchao_social_build_runs
                   (scope_key, build_run_id, revision, builder_version, input_sha256,
                    output_sha256, status, degradation_reason, started_at, completed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (self.scope_key, run.build_run_id, run.revision, run.builder_version,
                 run.input_sha256, run.output_sha256, run.status, run.degradation_reason,
                 run.started_at, run.completed_at),
            ).fetchone()

    def submit_proposal(
        self, proposal: SocialProposal, *, build_run_id: str, build_run_revision: int = 1
    ) -> Any:
        """Store a proposal immutably; exact hash replay is idempotent."""
        if not isinstance(proposal, SocialProposal):
            raise TypeError("proposal must be SocialProposal")
        if proposal.scope_key != self.scope_key:
            raise ValueError("proposal scope_key does not match repository scope")
        if proposal.status is not ProposalStatus.PROPOSED:
            raise ValueError("only proposed model/builder output may be submitted")
        self._positive("build_run_revision", build_run_revision)
        encoded = canonical_json(proposal.to_dict())
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if digest != sha256_json(proposal.to_dict()):
            raise AssertionError("non-deterministic canonical proposal encoding")
        with self._transaction():
            build = self.connection.execute(
                """SELECT output_sha256 FROM langchao_social_build_runs
                   WHERE scope_key = %s AND build_run_id = %s AND revision = %s""",
                (self.scope_key, build_run_id, build_run_revision),
            ).fetchone()
            if build is None:
                raise LangchaoSocialConflictError("exact build run does not exist")
            if str(_value(build, "output_sha256")) != proposal.proposal_sha256:
                raise LangchaoSocialConflictError("build output hash does not match proposal hash")
            existing = self.connection.execute(
                """SELECT *, payload_sha256 = %s AS payload_matches
                   FROM langchao_social_proposals
                   WHERE scope_key = %s AND proposal_id = %s AND revision = %s""",
                (digest, self.scope_key, proposal.proposal_id, proposal.revision),
            ).fetchone()
            if existing is not None:
                if not bool(_value(existing, "payload_matches", -1)):
                    raise LangchaoSocialConflictError("same proposal coordinate has different hash")
                if (_value(existing, "build_run_id") != build_run_id or
                        int(_value(existing, "build_run_revision")) != build_run_revision):
                    raise LangchaoSocialConflictError("proposal replay changed build provenance")
                return existing
            return self.connection.execute(
                """INSERT INTO langchao_social_proposals
                   (scope_key, proposal_id, revision, build_run_id, build_run_revision,
                    status, payload, payload_sha256, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING *""",
                (self.scope_key, proposal.proposal_id, proposal.revision, build_run_id,
                 build_run_revision, ProposalStatus.PROPOSED.value, encoded, digest,
                 proposal.created_at),
            ).fetchone()

    def _load_proposal(self, proposal_id: str, revision: int) -> tuple[Any, dict[str, Any]]:
        row = self.connection.execute(
            """SELECT * FROM langchao_social_proposals
               WHERE scope_key = %s AND proposal_id = %s AND revision = %s""",
            (self.scope_key, proposal_id, revision),
        ).fetchone()
        if row is None:
            raise LangchaoSocialConflictError("proposal does not exist in repository scope")
        payload = _value(row, "payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise LangchaoSocialConflictError("stored proposal payload is not an object")
        digest = sha256_json(payload)
        if digest != str(_value(row, "payload_sha256")):
            raise LangchaoSocialConflictError("stored proposal hash verification failed")
        return row, payload

    @staticmethod
    def _refs(payload: Mapping[str, Any]) -> tuple[SourceRef, ...]:
        unique: dict[tuple[str, str, str, int], SourceRef] = {}
        for owner in (*payload.get("items", ()), *payload.get("links", ())):
            for raw in owner.get("sources", ()):
                ref = SourceRef(
                    scope_key=raw["scope_key"], source_kind=SourceKind(raw["source_kind"]),
                    source_id=raw["source_id"], source_revision=raw["source_revision"],
                    source_sha256=raw["source_sha256"],
                    observed_at=datetime.fromisoformat(raw["observed_at"]), locator=raw.get("locator"),
                    contract_version=raw.get("contract_version", LANGCHAO_SOCIAL_CONTRACT_VERSION),
                )
                coordinate = (
                    ref.scope_key, ref.source_kind.value, ref.source_id, ref.source_revision,
                )
                previous = unique.get(coordinate)
                if previous is not None and previous.source_sha256 != ref.source_sha256:
                    raise LangchaoSocialConflictError(
                        "same source coordinate appears with conflicting hashes"
                    )
                unique[coordinate] = ref
        return tuple(unique.values())

    def _verify_sources(self, payload: Mapping[str, Any]) -> tuple[tuple[SourceRef, SourceResolution], ...]:
        checked: list[tuple[SourceRef, SourceResolution]] = []
        for ref in self._refs(payload):
            if ref.scope_key != self.scope_key:
                raise LangchaoSocialSourceError("cross-scope source reference")
            result = self.source_resolver.resolve_source(ref)
            if not isinstance(result, SourceResolution):
                raise LangchaoSocialSourceError("resolver must return SourceResolution")
            if (result.scope_key != ref.scope_key or result.source_kind is not ref.source_kind
                    or result.source_id != ref.source_id):
                raise LangchaoSocialSourceError("resolver returned different scope/kind/id")
            if result.status is SourceResolutionStatus.ACTIVE:
                if result.source_revision is None or result.source_sha256 is None:
                    raise LangchaoSocialSourceError("active resolution requires revision and hash")
            checked.append((ref, result))
        return tuple(checked)

    def commit_proposal(
        self,
        *,
        proposal_id: str,
        revision: int,
        expected_projection_pointer_version: int,
        committed_at: datetime | None = None,
    ) -> CommitResult:
        """Validate and atomically materialise one proposal.

        Stale-but-live sources return ``REBASE`` without writes. Missing, tombstoned,
        or invalidated sources return ``DISCARDED``. A pointer mismatch raises a hard
        CAS error and rolls back the entire transaction.
        """
        self._positive("revision", revision)
        self._pointer(expected_projection_pointer_version)
        occurred_at = committed_at or _utc_now()
        with self._transaction():
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"langchao:social:{self.scope_key}",),
            )
            _row, payload = self._load_proposal(proposal_id, revision)
            accepted_id = _event_id("social-commit", {
                "scope_key": self.scope_key, "proposal_id": proposal_id, "revision": revision,
            })
            replay = self.connection.execute(
                """SELECT payload FROM langchao_social_state_events
                   WHERE scope_key = %s AND state_event_id = %s AND revision = 1""",
                (self.scope_key, accepted_id),
            ).fetchone()
            if replay is not None:
                replay_payload = _value(replay, "payload")
                if isinstance(replay_payload, str):
                    replay_payload = json.loads(replay_payload)
                return CommitResult(
                    disposition=CommitDisposition.IDEMPOTENT, proposal_id=proposal_id,
                    proposal_revision=revision,
                    pointer_version=replay_payload.get("pointer_version"),
                    item_revisions=tuple((x["item_key"], x["revision"])
                                         for x in replay_payload.get("items", ())),
                )

            checked = self._verify_sources(payload)
            unavailable = tuple(result for _, result in checked
                                if result.status is not SourceResolutionStatus.ACTIVE)
            if unavailable:
                return CommitResult(
                    disposition=CommitDisposition.DISCARDED, proposal_id=proposal_id,
                    proposal_revision=revision, stale_sources=unavailable,
                    reason="one or more authoritative sources are unavailable",
                )
            stale = tuple(result for ref, result in checked
                          if result.source_revision != ref.source_revision
                          or result.source_sha256 != ref.source_sha256)
            if stale:
                return CommitResult(
                    disposition=CommitDisposition.REBASE, proposal_id=proposal_id,
                    proposal_revision=revision, stale_sources=stale,
                    reason="authoritative source revision/hash changed; rebuild proposal",
                )

            actual = self._lock_projection_state()
            if actual != expected_projection_pointer_version:
                raise LangchaoSocialCASConflictError(
                    f"projection CAS failed: expected {expected_projection_pointer_version}, actual {actual}"
                )
            for ref, _ in checked:
                self._upsert_active_source(ref)

            item_revisions: dict[str, int] = {}
            for item in payload.get("items", ()):
                next_revision = self._next_revision("langchao_social_items", "item_key", item["item_key"])
                status = (SocialItemStatus.PROPOSED.value if item["item_type"] == SocialItemType.GOAL_DRAFT.value
                          else SocialItemStatus.ACTIVE.value)
                supersedes_revision = None
                if item.get("supersedes_item_key"):
                    supersedes_revision = self._active_item_revision(item["supersedes_item_key"])
                    if supersedes_revision is None:
                        raise LangchaoSocialConflictError("superseded item has no active projection")
                item_digest = sha256_json({**item, "committed_status": status})
                self.connection.execute(
                    """INSERT INTO langchao_social_items
                       (scope_key, item_key, revision, proposal_id, proposal_revision,
                        item_type, role, status, summary, attributes, payload_sha256,
                        supersedes_item_key, supersedes_revision)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)""",
                    (self.scope_key, item["item_key"], next_revision, proposal_id, revision,
                     item["item_type"], item["role"], status, item["summary"],
                     canonical_json(item.get("attributes", {})), item_digest,
                     item.get("supersedes_item_key"), supersedes_revision),
                )
                item_revisions[item["item_key"]] = next_revision
                self._insert_source_refs("item", item["item_key"], next_revision, item["sources"])

            for link in payload.get("links", ()):
                next_revision = self._next_revision("langchao_social_links", "link_key", link["link_key"])
                self.connection.execute(
                    """INSERT INTO langchao_social_links
                       (scope_key, link_key, revision, proposal_id, proposal_revision, relation,
                        from_item_key, from_item_revision, to_item_key, to_item_revision,
                        attributes, payload_sha256)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                    (self.scope_key, link["link_key"], next_revision, proposal_id, revision,
                     link["relation"], link["from_item_key"], item_revisions[link["from_item_key"]],
                     link["to_item_key"], item_revisions[link["to_item_key"]],
                     canonical_json(link.get("attributes", {})), sha256_json(link)),
                )
                self._insert_source_refs("link", link["link_key"], next_revision, link["sources"])

            next_pointer = actual + 1
            event_payload = {"proposal_id": proposal_id, "proposal_revision": revision,
                             "pointer_version": next_pointer,
                             "items": [{"item_key": key, "revision": value}
                                       for key, value in item_revisions.items()]}
            self._append_event(accepted_id, "proposal_accepted", "proposal", proposal_id,
                               revision, revision, event_payload, occurred_at)
            item_types = {item["item_key"]: item["item_type"] for item in payload.get("items", ())}
            for item_key, item_revision in item_revisions.items():
                if item_types[item_key] == SocialItemType.GOAL_DRAFT.value:
                    continue
                cursor = self.connection.execute(
                    """INSERT INTO langchao_social_projection_heads
                       (scope_key, projection_key, item_key, item_revision, pointer_version,
                        based_on_state_event_id, based_on_state_event_revision)
                       VALUES (%s, %s, %s, %s, %s, %s, 1)
                       ON CONFLICT (scope_key, projection_key) DO UPDATE SET
                         item_key = EXCLUDED.item_key, item_revision = EXCLUDED.item_revision,
                         pointer_version = EXCLUDED.pointer_version,
                         based_on_state_event_id = EXCLUDED.based_on_state_event_id,
                         based_on_state_event_revision = 1, updated_at = CURRENT_TIMESTAMP""",
                    (self.scope_key, item_key, item_key, item_revision, next_pointer,
                     accepted_id),
                )
                if getattr(cursor, "rowcount", 1) != 1:
                    raise LangchaoSocialCASConflictError("projection head update failed")
            self._advance_projection_state(actual)
            return CommitResult(
                disposition=CommitDisposition.COMMITTED, proposal_id=proposal_id,
                proposal_revision=revision, pointer_version=next_pointer,
                item_revisions=tuple(item_revisions.items()),
            )

    def _lock_projection_state(self) -> int:
        self.connection.execute(
            """INSERT INTO langchao_social_projection_state (scope_key, pointer_version)
               VALUES (%s, 0) ON CONFLICT (scope_key) DO NOTHING""",
            (self.scope_key,),
        )
        row = self.connection.execute(
            """SELECT pointer_version FROM langchao_social_projection_state
               WHERE scope_key = %s FOR UPDATE""",
            (self.scope_key,),
        ).fetchone()
        if row is None:
            raise LangchaoSocialConflictError("projection state is missing after initialization")
        return int(_value(row, "pointer_version"))

    def _advance_projection_state(self, expected: int) -> int:
        cursor = self.connection.execute(
            """UPDATE langchao_social_projection_state
               SET pointer_version = pointer_version + 1, updated_at = CURRENT_TIMESTAMP
               WHERE scope_key = %s AND pointer_version = %s""",
            (self.scope_key, expected),
        )
        if getattr(cursor, "rowcount", 1) != 1:
            raise LangchaoSocialCASConflictError("projection state CAS update failed")
        return expected + 1

    def _next_revision(self, table: str, key_column: str, key: str) -> int:
        row = self.connection.execute(
            f"SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM {table} "
            f"WHERE scope_key = %s AND {key_column} = %s", (self.scope_key, key),
        ).fetchone()
        return int(_value(row, "revision") or 1)

    def _active_item_revision(self, item_key: str) -> int | None:
        row = self.connection.execute(
            """SELECT item_revision FROM langchao_social_projection_heads
               WHERE scope_key = %s AND projection_key = %s""", (self.scope_key, item_key),
        ).fetchone()
        return None if row is None else int(_value(row, "item_revision"))

    def _upsert_active_source(self, ref: SourceRef) -> None:
        self.connection.execute(
            """INSERT INTO langchao_social_sources
               (scope_key, source_kind, source_id, source_revision, source_sha256,
                observed_at, locator, source_status, contract_version)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s)
               ON CONFLICT (scope_key, source_kind, source_id, source_revision) DO NOTHING""",
            (self.scope_key, ref.source_kind.value, ref.source_id, ref.source_revision,
             ref.source_sha256, ref.observed_at, ref.locator, ref.contract_version),
        )
        row = self.connection.execute(
            """SELECT source_sha256, source_status FROM langchao_social_sources
               WHERE scope_key = %s AND source_kind = %s AND source_id = %s AND source_revision = %s""",
            (self.scope_key, ref.source_kind.value, ref.source_id, ref.source_revision),
        ).fetchone()
        if row is None or _value(row, "source_sha256") != ref.source_sha256:
            raise LangchaoSocialConflictError("source coordinate has different hash")
        if _value(row, "source_status") != "active":
            raise LangchaoSocialSourceError("source became unavailable during commit")

    def _insert_source_refs(self, owner: str, key: str, revision: int,
                            sources: Sequence[Mapping[str, Any]]) -> None:
        table = f"langchao_social_{owner}_sources"
        key_column = f"{owner}_key"
        revision_column = f"{owner}_revision"
        for ordinal, source in enumerate(sources):
            self.connection.execute(
                f"""INSERT INTO {table}
                    (scope_key, {key_column}, {revision_column}, source_kind, source_id,
                     source_revision, source_sha256, ordinal)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.scope_key, key, revision, source["source_kind"], source["source_id"],
                 source["source_revision"], source["source_sha256"], ordinal),
            )

    def _append_event(self, event_id: str, event_type: str, target_kind: str,
                      target_key: str, expected_revision: int | None, result_revision: int,
                      payload: Mapping[str, Any], occurred_at: datetime) -> Any:
        encoded = canonical_json(payload)
        return self.connection.execute(
            """INSERT INTO langchao_social_state_events
               (scope_key, state_event_id, revision, event_type, target_kind, target_key,
                expected_revision, result_revision, payload, payload_sha256, occurred_at)
               VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (scope_key, state_event_id, revision) DO NOTHING RETURNING *""",
            (self.scope_key, event_id, event_type, target_kind, target_key,
             expected_revision, result_revision, encoded,
             hashlib.sha256(encoded.encode("utf-8")).hexdigest(), occurred_at),
        ).fetchone()

    def invalidate_source(self, source: SourceRef, *, reason: str,
                          occurred_at: datetime | None = None) -> tuple[Any, ...]:
        return self._change_source(source, status="invalidated", reason=reason,
                                   occurred_at=occurred_at or _utc_now())

    def tombstone_source(self, source: SourceRef, *, reason: str,
                         occurred_at: datetime | None = None) -> tuple[Any, ...]:
        """Tombstone deletion/unavailability; ordinary memory archive must not call this."""
        return self._change_source(source, status="tombstoned", reason=reason,
                                   occurred_at=occurred_at or _utc_now())

    def _change_source(self, source: SourceRef, *, status: str, reason: str,
                       occurred_at: datetime) -> tuple[Any, ...]:
        if source.scope_key != self.scope_key:
            raise ValueError("source scope_key does not match repository scope")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty")
        if status not in {"tombstoned", "invalidated"}:
            raise ValueError("archive is not a source tombstone/invalidation")
        timestamp_column = "tombstoned_at" if status == "tombstoned" else "invalidated_at"
        with self._transaction():
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"langchao:social:{self.scope_key}",),
            )
            projection_version = self._lock_projection_state()
            # Minimal provenance only: no source body is accepted or persisted here.
            previous = self.connection.execute(
                """SELECT source_sha256, source_status, invalidation_reason
                   FROM langchao_social_sources
                   WHERE scope_key = %s AND source_kind = %s AND source_id = %s
                     AND source_revision = %s FOR UPDATE""",
                (self.scope_key, source.source_kind.value, source.source_id,
                 source.source_revision),
            ).fetchone()
            if previous is not None:
                if _value(previous, "source_sha256") != source.source_sha256:
                    raise LangchaoSocialConflictError(
                        "source tombstone hash conflicts with stored source"
                    )
                previous_status = str(_value(previous, "source_status"))
                if previous_status != "active" and previous_status != status:
                    raise LangchaoSocialConflictError(
                        "terminal source lifecycle cannot be reversed or changed"
                    )
                if (previous_status == status
                        and _value(previous, "invalidation_reason") != reason):
                    raise LangchaoSocialConflictError(
                        "terminal source replay changed invalidation reason"
                    )
            if previous is None:
                self.connection.execute(
                    """INSERT INTO langchao_social_sources
                       (scope_key, source_kind, source_id, source_revision, source_sha256,
                        observed_at, locator, source_status, tombstoned_at, invalidated_at,
                        invalidation_reason, contract_version)
                       VALUES (%s, %s, %s, %s, %s, %s, NULL, %s,
                               CASE WHEN %s = 'tombstoned' THEN %s ELSE NULL END,
                               CASE WHEN %s = 'invalidated' THEN %s ELSE NULL END, %s, %s)""",
                    (self.scope_key, source.source_kind.value, source.source_id,
                     source.source_revision, source.source_sha256, source.observed_at, status,
                     status, occurred_at, status, occurred_at, reason, source.contract_version),
                )
            elif _value(previous, "source_status") == "active":
                self.connection.execute(
                    f"""UPDATE langchao_social_sources SET source_status = %s,
                         {timestamp_column} = %s, invalidation_reason = %s
                         WHERE scope_key = %s AND source_kind = %s AND source_id = %s
                           AND source_revision = %s AND source_status = 'active'
                           AND source_sha256 = %s""",
                    (status, occurred_at, reason, self.scope_key, source.source_kind.value,
                     source.source_id, source.source_revision, source.source_sha256),
                )
            stored = self.connection.execute(
                """SELECT source_sha256, source_status FROM langchao_social_sources
                   WHERE scope_key = %s AND source_kind = %s AND source_id = %s
                     AND source_revision = %s""",
                (self.scope_key, source.source_kind.value, source.source_id,
                 source.source_revision),
            ).fetchone()
            if stored is None or _value(stored, "source_sha256") != source.source_sha256:
                raise LangchaoSocialConflictError("source tombstone hash conflicts with stored source")
            deps = self.connection.execute(
                """SELECT item_key, item_revision FROM langchao_social_item_sources
                   WHERE scope_key = %s AND source_kind = %s AND source_id = %s
                     AND source_revision = %s AND source_sha256 = %s""",
                (self.scope_key, source.source_kind.value, source.source_id,
                 source.source_revision, source.source_sha256),
            ).fetchall()
            events: list[Any] = []
            source_event = _event_id(f"source-{status}", {
                "scope": self.scope_key, "kind": source.source_kind.value,
                "id": source.source_id, "revision": source.source_revision,
                "hash": source.source_sha256, "reason": reason,
            })
            events.append(self._append_event(
                source_event, f"source_{status}", "source", source.source_id,
                source.source_revision, source.source_revision,
                {"source_kind": source.source_kind.value, "source_revision": source.source_revision,
                 "source_sha256": source.source_sha256, "reason": reason,
                 "timestamp_column": timestamp_column}, occurred_at,
            ))
            for dep in deps:
                item_key = str(_value(dep, "item_key"))
                item_revision = int(_value(dep, "item_revision", 1))
                count_row = self.connection.execute(
                    """SELECT COUNT(*) AS source_count
                       FROM langchao_social_item_sources AS refs
                       JOIN langchao_social_sources AS sources
                         ON sources.scope_key = refs.scope_key
                        AND sources.source_kind = refs.source_kind
                        AND sources.source_id = refs.source_id
                        AND sources.source_revision = refs.source_revision
                        AND sources.source_sha256 = refs.source_sha256
                       WHERE refs.scope_key = %s AND refs.item_key = %s
                         AND refs.item_revision = %s AND sources.source_status = 'active'""",
                    (self.scope_key, item_key, item_revision),
                ).fetchone()
                source_count = int(_value(count_row, "source_count") or 0)
                payload = {"source_id": source.source_id, "source_revision": source.source_revision,
                           "reason": reason, "needs_reassessment": source_count > 1,
                           "remaining_independent_sources": max(0, source_count - 1)}
                dep_event = _event_id("dependency-invalidated", {
                    "scope": self.scope_key, "item": item_key, "revision": item_revision,
                    "source": source.source_id, "source_revision": source.source_revision,
                })
                events.append(self._append_event(
                    dep_event, "dependency_invalidated", "item", item_key,
                    item_revision, item_revision, payload, occurred_at,
                ))
                if source_count == 1:
                    self._append_invalidated_item_revision(item_key, item_revision, dep_event)
                    downstream = self.connection.execute(
                        """SELECT link_key, revision AS link_revision
                           FROM langchao_social_links
                           WHERE scope_key = %s AND (
                             (from_item_key = %s AND from_item_revision = %s) OR
                             (to_item_key = %s AND to_item_revision = %s))""",
                        (self.scope_key, item_key, item_revision, item_key, item_revision),
                    ).fetchall()
                    for link in downstream:
                        events.append(self._append_link_invalidation(
                            str(_value(link, "link_key")),
                            int(_value(link, "link_revision", 1)),
                            source, reason, occurred_at,
                        ))

            link_deps = self.connection.execute(
                """SELECT link_key, link_revision FROM langchao_social_link_sources
                   WHERE scope_key = %s AND source_kind = %s AND source_id = %s
                     AND source_revision = %s AND source_sha256 = %s""",
                (self.scope_key, source.source_kind.value, source.source_id,
                 source.source_revision, source.source_sha256),
            ).fetchall()
            for dep in link_deps:
                link_key = str(_value(dep, "link_key"))
                link_revision = int(_value(dep, "link_revision", 1))
                events.append(self._append_link_invalidation(
                    link_key, link_revision, source, reason, occurred_at,
                ))
            self._advance_projection_state(projection_version)
            return tuple(events)

    def _append_link_invalidation(self, link_key: str, link_revision: int,
                                  source: SourceRef, reason: str,
                                  occurred_at: datetime) -> Any:
        event_id = _event_id("link-invalidated", {
            "scope": self.scope_key, "link": link_key, "revision": link_revision,
            "source": source.source_id, "source_revision": source.source_revision,
        })
        return self._append_event(
            event_id, "dependency_invalidated", "link", link_key,
            link_revision, link_revision,
            {"source_id": source.source_id, "source_revision": source.source_revision,
             "reason": reason, "invalidated": True}, occurred_at,
        )

    def _append_invalidated_item_revision(self, item_key: str, revision: int,
                                          event_id: str) -> None:
        row = self.connection.execute(
            """SELECT * FROM langchao_social_items
               WHERE scope_key = %s AND item_key = %s AND revision = %s""",
            (self.scope_key, item_key, revision),
        ).fetchone()
        if row is None:
            raise LangchaoSocialConflictError("dependent item revision is missing")
        next_revision = self._next_revision("langchao_social_items", "item_key", item_key)
        digest = sha256_json({"previous_revision": revision, "status": "invalidated",
                              "event_id": event_id})
        self.connection.execute(
            """INSERT INTO langchao_social_items
               (scope_key, item_key, revision, proposal_id, proposal_revision, item_type,
                role, status, summary, attributes, payload_sha256,
                supersedes_item_key, supersedes_revision)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'invalidated', %s, %s::jsonb,
                       %s, %s, %s)""",
            (self.scope_key, item_key, next_revision, _value(row, "proposal_id"),
             int(_value(row, "proposal_revision")), _value(row, "item_type"),
             _value(row, "role"), _value(row, "summary"),
             canonical_json(_value(row, "attributes") or {}), digest,
             _value(row, "supersedes_item_key"), _value(row, "supersedes_revision")),
        )
        self.connection.execute(
            """UPDATE langchao_social_projection_heads SET item_revision = %s,
                 based_on_state_event_id = %s, based_on_state_event_revision = 1,
                 updated_at = CURRENT_TIMESTAMP
               WHERE scope_key = %s AND projection_key = %s AND item_revision = %s""",
            (next_revision, event_id, self.scope_key, item_key, revision),
        )

    def get_active_projection(self, *, limit: int = 100) -> tuple[Any, ...]:
        """Return a bounded active projection; invalidated/tombstoned rows are excluded."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return tuple(self.connection.execute(
            """SELECT i.*, h.pointer_version, h.projection_key
               FROM langchao_social_projection_heads AS h
               JOIN langchao_social_items AS i
                 ON i.scope_key = h.scope_key AND i.item_key = h.item_key
                AND i.revision = h.item_revision
               WHERE h.scope_key = %s AND i.status = 'active'
               ORDER BY h.updated_at DESC, h.projection_key LIMIT %s""",
            (self.scope_key, limit),
        ).fetchall())


__all__ = [
    "BuildRun", "CommitDisposition", "CommitResult", "LangchaoSocialCASConflictError",
    "LangchaoSocialConflictError", "LangchaoSocialError", "LangchaoSocialRepository",
    "LangchaoSocialSourceError", "SourceResolution", "SourceResolutionStatus",
    "SourceResolver",
]
