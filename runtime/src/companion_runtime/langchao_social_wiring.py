"""Production wiring for scope-bound social facts built from legacy projections.

The adapter is deliberately mechanical: it copies authoritative projection rows into
rule inputs, asks a proposal provider for a *proposal only*, and leaves all persistence
and source revalidation to :class:`LangchaoSocialRepository`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote

from .langchao_social import RuleInputKind, SocialRuleInput, build_social_proposal
from .langchao_social_repository import (
    BuildRun, CommitDisposition, LangchaoSocialRepository, SourceResolution,
    SourceResolutionStatus,
)
from .langchao_social_types import (
    LANGCHAO_SOCIAL_BUILDER_VERSION, SocialRole, SourceKind, SourceRef, sha256_json,
)
from .utility import ensure_aware


@dataclass(frozen=True, slots=True)
class SocialFactsSnapshot:
    scope_key: str
    observed_at: datetime
    inputs: tuple[SocialRuleInput, ...]
    input_sha256: str


def _utc(value: datetime | None, fallback: datetime) -> datetime:
    stamp = ensure_aware(value) or fallback
    return stamp.astimezone(timezone.utc)


def _revision(value: datetime | None, fallback: datetime) -> int:
    stamp = _utc(value, fallback)
    return max(1, int(stamp.timestamp() * 1_000_000))


def _payload(kind: SourceKind, row: Any) -> dict[str, Any]:
    if kind is SourceKind.MEMORY:
        return {
            "memory_id": row.memory_id, "kind": row.kind, "summary": row.summary,
            "structured": row.structured, "topics": row.topics,
            "importance": float(row.importance), "confidence": float(row.confidence),
            "status": row.status, "source_event_ids": row.source_event_ids,
        }
    if kind is SourceKind.BOUNDARY:
        return {
            "boundary_id": row.boundary_id, "type": row.type, "scope": row.scope,
            "allow_reply": bool(row.allow_reply), "allow_proactive": bool(row.allow_proactive),
            "starts_at": row.starts_at.isoformat() if row.starts_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "source_event_id": row.source_event_id, "note": row.note, "subject": row.subject,
        }
    return {
        "unfinished_id": row.unfinished_id, "title": row.title, "status": row.status,
        "waiting_until": row.waiting_until.isoformat() if row.waiting_until else None,
        "priority": float(row.priority), "mute_until": row.mute_until.isoformat() if row.mute_until else None,
        "expire_at": row.expire_at.isoformat() if row.expire_at else None,
        "source_event_ids": row.source_event_ids, "resolution_conditions": row.resolution_conditions,
    }


def encode_exact_ref(kind: str, *, scope_key: str, object_id: str, revision: int, digest: str) -> str:
    """Encode an exact, unambiguous scope/id/revision/hash reference."""
    return f"{kind}|{quote(scope_key, safe='')}|{quote(object_id, safe='')}|{revision}|{digest}"


def decode_exact_ref(value: str) -> tuple[str, str, str, int, str]:
    parts = value.split("|")
    if len(parts) != 5:
        raise ValueError("exact ref must contain kind/scope/id/revision/hash")
    kind, scope, object_id, raw_revision, digest = parts
    revision = int(raw_revision)
    if revision < 1 or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid exact ref revision/hash")
    return kind, unquote(scope), unquote(object_id), revision, digest


class LegacyProjectionSourceResolver:
    """Resolve exact social sources against the live legacy projections of one scope."""
    def __init__(self, runtime: Any, *, scope_key: str) -> None:
        self.runtime = runtime
        self.scope_key = scope_key

    def _current(self, source: SourceRef) -> tuple[Any | None, datetime | None, SourceResolutionStatus]:
        if source.source_kind is SourceKind.MEMORY:
            projection = self.runtime.projections.memory
            getter = getattr(projection, "get_memory", None) or getattr(projection, "get", None)
            if not callable(getter):
                raise TypeError("memory projection must provide get_memory() or get()")
            row = getter(source.source_id)
            if row is None:
                return None, None, SourceResolutionStatus.MISSING
            status = SourceResolutionStatus.ACTIVE if row.status in {"active", "low_activation"} else SourceResolutionStatus.TOMBSTONED
            return row, getattr(row, "updated_at", None) or getattr(row, "created_at", None), status
        if source.source_kind is SourceKind.BOUNDARY:
            row = self.runtime.projections.boundaries.get(source.source_id)
            if row is None:
                return None, None, SourceResolutionStatus.MISSING
            active = row.is_active(datetime.now(timezone.utc))
            return row, getattr(row, "starts_at", None), SourceResolutionStatus.ACTIVE if active else SourceResolutionStatus.TOMBSTONED
        if source.source_kind is SourceKind.RUNTIME_FACT:
            row = self.runtime.projections.unfinished.get(source.source_id)
            if row is None:
                return None, None, SourceResolutionStatus.MISSING
            status = SourceResolutionStatus.ACTIVE if row.status in {"open", "waiting", "due"} else SourceResolutionStatus.TOMBSTONED
            return row, getattr(row, "updated_at", None) or getattr(row, "created_at", None), status
        return None, None, SourceResolutionStatus.MISSING

    def resolve_source(self, source: SourceRef) -> SourceResolution:
        if source.scope_key != self.scope_key:
            return SourceResolution(scope_key=self.scope_key, source_kind=source.source_kind,
                                    source_id=source.source_id, source_revision=None,
                                    source_sha256=None, status=SourceResolutionStatus.MISSING,
                                    reason="cross-scope source")
        row, stamp, status = self._current(source)
        if row is None:
            return SourceResolution(scope_key=self.scope_key, source_kind=source.source_kind,
                                    source_id=source.source_id, source_revision=None,
                                    source_sha256=None, status=status)
        observed = _utc(stamp, source.observed_at)
        return SourceResolution(
            scope_key=self.scope_key, source_kind=source.source_kind, source_id=source.source_id,
            source_revision=_revision(stamp, source.observed_at),
            source_sha256=sha256_json(_payload(source.source_kind, row)), status=status,
            observed_at=observed,
        )


ProposalProvider = Callable[..., Any]


class LangchaoSocialSnapshotService:
    """Build/commit a bounded projection snapshot and supply exact candidate refs."""
    def __init__(self, runtime: Any, *, scope_key: str, repository: LangchaoSocialRepository,
                 proposal_provider: ProposalProvider = build_social_proposal) -> None:
        self.runtime = runtime
        self.scope_key = scope_key
        self.repository = repository
        self.proposal_provider = proposal_provider
        self._source_refs: dict[str, str] = {}
        self._social_refs: dict[str, str] = {}

    def collect(self, *, now: datetime) -> SocialFactsSnapshot:
        inputs: list[SocialRuleInput] = []
        token_refs: dict[str, str] = {}
        def add(token: str, kind: SourceKind, object_id: str, stamp: datetime | None,
                summary: str, rule: RuleInputKind, role: SocialRole) -> None:
            observed = _utc(stamp, now)
            row = ({SourceKind.MEMORY: self.runtime.projections.memory.get_memory,
                    SourceKind.BOUNDARY: self.runtime.projections.boundaries.get,
                    SourceKind.RUNTIME_FACT: self.runtime.projections.unfinished.get}[kind])(object_id)
            payload = _payload(kind, row)
            ref = SourceRef(scope_key=self.scope_key, source_kind=kind, source_id=object_id,
                            source_revision=_revision(stamp, now), source_sha256=sha256_json(payload),
                            observed_at=observed)
            inputs.append(SocialRuleInput(kind=rule, source=ref, summary=summary,
                                          role=role, semantic_key=token))
            token_refs[token] = encode_exact_ref("memory" if kind is SourceKind.MEMORY else "source",
                                                 scope_key=self.scope_key, object_id=object_id,
                                                 revision=ref.source_revision, digest=ref.source_sha256)
        for row in self.runtime.projections.unfinished.list_open():
            add(f"unfinished:{row.unfinished_id}", SourceKind.RUNTIME_FACT, row.unfinished_id,
                getattr(row, "updated_at", None) or getattr(row, "created_at", None), row.title,
                RuleInputKind.UNFINISHED, SocialRole.SHARED)
        for row in self.runtime.projections.memory.list_memories(status=("active", "low_activation"), limit=100):
            add(f"memory:{row.memory_id}", SourceKind.MEMORY, row.memory_id,
                getattr(row, "updated_at", None) or getattr(row, "created_at", None), row.summary,
                RuleInputKind.ACTUAL_EVENT, SocialRole.SHARED)
        for row in self.runtime.projections.boundaries.active(now):
            add(f"boundary:{row.boundary_id}", SourceKind.BOUNDARY, row.boundary_id,
                getattr(row, "starts_at", None), row.note or row.subject or row.type,
                RuleInputKind.ACTIVE_BOUNDARY, SocialRole.USER)
        material = [
            dict(kind=x.kind.value, semantic_key=x.semantic_key, summary=x.summary,
                 role=x.role.value, actual=x.actual, active=x.active,
                 source=x.source.to_dict())
            for x in inputs
        ]
        self._source_refs = token_refs
        created_at = max((x.source.observed_at for x in inputs), default=now)
        return SocialFactsSnapshot(self.scope_key, created_at, tuple(inputs), sha256_json(material))

    def refresh(self, *, now: datetime) -> Any | None:
        snapshot = self.collect(now=now)
        if not snapshot.inputs:
            self._social_refs = {}
            return None
        proposal = self.proposal_provider(scope_key=self.scope_key,
                                          created_at=snapshot.observed_at,
                                          inputs=snapshot.inputs, semantic_available=False)
        run_id = f"social-build:{snapshot.input_sha256}"
        run = BuildRun(build_run_id=run_id, revision=1,
                       builder_version=LANGCHAO_SOCIAL_BUILDER_VERSION,
                       input_sha256=snapshot.input_sha256, output_sha256=proposal.proposal_sha256,
                       status="completed", started_at=now, completed_at=now)
        self.repository.record_build_run(run)
        self.repository.submit_proposal(proposal, build_run_id=run_id)
        result = self.repository.commit_proposal(
            proposal_id=proposal.proposal_id, revision=proposal.revision,
            expected_projection_pointer_version=self.repository.get_projection_pointer_version(),
            committed_at=now,
        )
        if result.disposition not in {CommitDisposition.COMMITTED, CommitDisposition.IDEMPOTENT}:
            self._social_refs = {}
            return result
        revisions = dict(result.item_revisions)
        refs: dict[str, str] = {}
        for item in proposal.items:
            revision = revisions.get(item.item_key)
            if revision is None:
                continue
            digest = sha256_json({**item.to_dict(), "committed_status": "active"})
            exact = encode_exact_ref("social", scope_key=self.scope_key, object_id=item.item_key,
                                     revision=revision, digest=digest)
            for source in item.sources:
                refs[f"{source.source_kind.value}:{source.source_id}"] = exact
        self._social_refs = refs
        return result

    def candidate_refs(self, sources: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for token in sources:
            if token.startswith("memory:") and token in self._source_refs:
                result["memory_ref"] = self._source_refs[token]
            kind_id = token.split(":", 1)
            if len(kind_id) == 2:
                key = ({"memory": "memory", "unfinished": "runtime_fact", "boundary": "boundary"}
                       .get(kind_id[0], kind_id[0])) + ":" + kind_id[1]
                if key in self._social_refs:
                    result["social_ref"] = self._social_refs[key]
        return result

    def validate_candidate_action(self, action: Mapping[str, Any]) -> bool:
        for name in ("memory_ref", "social_ref"):
            value = action.get(name)
            if value is None:
                continue
            try:
                kind, scope, object_id, revision, digest = decode_exact_ref(str(value))
            except (TypeError, ValueError):
                return False
            if scope != self.scope_key or kind != name.removesuffix("_ref"):
                return False
            if kind == "memory":
                source = SourceRef(scope_key=scope, source_kind=SourceKind.MEMORY,
                                   source_id=object_id, source_revision=revision,
                                   source_sha256=digest, observed_at=datetime.now(timezone.utc))
                resolved = self.repository.source_resolver.resolve_source(source)
                if (resolved.status is not SourceResolutionStatus.ACTIVE
                        or resolved.source_revision != revision or resolved.source_sha256 != digest):
                    return False
            elif not self.repository.is_active_item_ref(item_key=object_id, revision=revision,
                                                        digest=digest):
                return False
        return True


def build_langchao_social_service(*, connection: Any, scope_key: str, runtime: Any,
                                   proposal_provider: ProposalProvider = build_social_proposal) -> LangchaoSocialSnapshotService:
    resolver = LegacyProjectionSourceResolver(runtime, scope_key=scope_key)
    repository = LangchaoSocialRepository(connection, scope_key=scope_key, source_resolver=resolver)
    return LangchaoSocialSnapshotService(runtime, scope_key=scope_key, repository=repository,
                                         proposal_provider=proposal_provider)


__all__ = ["LangchaoSocialSnapshotService", "LegacyProjectionSourceResolver", "SocialFactsSnapshot",
           "build_langchao_social_service", "decode_exact_ref", "encode_exact_ref"]
