from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from companion_runtime.langchao_social_repository import SourceResolutionStatus
from companion_runtime.langchao_social_types import SourceKind, SourceRef
from companion_runtime.langchao_social_wiring import (
    LegacyProjectionSourceResolver, decode_exact_ref, encode_exact_ref,
)
from companion_runtime.legacy_bridge_v2 import ConcreteLegacyRuntimeV2Bridge
from companion_runtime.typing import CandidateIntent

NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)
DIGEST = "a" * 64


class One:
    def __init__(self, row): self.row = row
    def get(self, object_id):
        key = getattr(self.row, "memory_id", getattr(self.row, "unfinished_id", getattr(self.row, "boundary_id", None)))
        return self.row if object_id == key else None


class Candidates:
    def __init__(self, row): self.row = row
    def list_active(self, limit): return [self.row]
    def get(self, candidate_id): return self.row if candidate_id == self.row.candidate_id else None


def memory(status="active"):
    return SimpleNamespace(memory_id="m1", kind="relationship", summary="共同看过海",
                           structured={}, topics=["海"], importance=.8, confidence=.9,
                           status=status, source_event_ids=["e1"], created_at=NOW, updated_at=NOW)


def runtime_with(row):
    return SimpleNamespace(projections=SimpleNamespace(
        memory=One(row), unfinished=One(SimpleNamespace(unfinished_id="u", status="resolved")),
        boundaries=One(SimpleNamespace(boundary_id="b", is_active=lambda now: False)),
    ))


def test_exact_ref_roundtrip_carries_scope_id_revision_hash():
    encoded = encode_exact_ref("social", scope_key="user:1/channel:dm", object_id="item|1",
                               revision=7, digest=DIGEST)
    assert decode_exact_ref(encoded) == ("social", "user:1/channel:dm", "item|1", 7, DIGEST)


def test_scope_bound_resolver_rejects_cross_scope_and_tombstone():
    resolver = LegacyProjectionSourceResolver(runtime_with(memory("archived")), scope_key="scope-a")
    cross = SourceRef(scope_key="scope-b", source_kind=SourceKind.MEMORY, source_id="m1",
                      source_revision=1, source_sha256=DIGEST, observed_at=NOW)
    assert resolver.resolve_source(cross).status is SourceResolutionStatus.MISSING
    local = SourceRef(scope_key="scope-a", source_kind=SourceKind.MEMORY, source_id="m1",
                      source_revision=1, source_sha256=DIGEST, observed_at=NOW)
    assert resolver.resolve_source(local).status is SourceResolutionStatus.TOMBSTONED


def test_candidate_supply_carries_exact_memory_and_social_refs_and_expression_is_reachable():
    item = CandidateIntent(candidate_id="c1", type="share", intent="分享", goal="表达",
                           sources=["memory:m1"], internal_need=.4)
    memory_ref = encode_exact_ref("memory", scope_key="scope", object_id="m1", revision=3,
                                  digest=DIGEST)
    social_ref = encode_exact_ref("social", scope_key="scope", object_id="s1", revision=2,
                                  digest="b" * 64)
    refreshes = []
    social = SimpleNamespace(
        refresh=lambda *, now: refreshes.append(now),
        candidate_refs=lambda sources: {
            "memory_ref": memory_ref, "social_ref": social_ref
        },
    )
    runtime = SimpleNamespace(
        projections=SimpleNamespace(candidates=Candidates(item)),
        config=SimpleNamespace(candidate=SimpleNamespace(max_active=4)),
        _event_ids_behind=lambda source: ["e1"],
    )
    candidate = ConcreteLegacyRuntimeV2Bridge(runtime, social_service=social).candidates(
        scope_key="scope", now=NOW)[0]
    assert refreshes == [NOW]
    assert candidate.action["memory_ref"] == memory_ref
    assert candidate.action["social_ref"] == social_ref
    from companion_runtime.langchao_shadow_wiring import _facts_for
    assessment = SimpleNamespace(candidate=candidate, repeat=SimpleNamespace(
        total_cost=0, policy_version="repeat.v2", hard_limit_reasons=()), blocked=False,
        reasons=(), user_utility=SimpleNamespace(), net_utility=0)
    facts = _facts_for(assessment)
    assert facts is not None and facts.template_key == "expression.v1"
    assert facts.memory_ref == memory_ref and facts.social_ref == social_ref


def test_non_cli_candidate_supply_refreshes_each_round_and_replaces_stale_refs():
    item = CandidateIntent(candidate_id="c1", type="share", intent="分享", goal="表达",
                           sources=["memory:m1"], internal_need=.4)
    stale = encode_exact_ref("memory", scope_key="scope", object_id="m1", revision=1,
                             digest="1" * 64)
    fresh = encode_exact_ref("memory", scope_key="scope", object_id="m1", revision=2,
                             digest="2" * 64)

    class Social:
        def __init__(self):
            self.ref = stale
            self.refreshes = []

        def refresh(self, *, now):
            self.refreshes.append(now)
            self.ref = fresh

        def candidate_refs(self, sources):
            return {"memory_ref": self.ref}

    social = Social()
    runtime = SimpleNamespace(
        projections=SimpleNamespace(candidates=Candidates(item)),
        config=SimpleNamespace(candidate=SimpleNamespace(max_active=4)),
        _event_ids_behind=lambda source: [],
    )
    bridge = ConcreteLegacyRuntimeV2Bridge(runtime, social_service=social)
    candidate = bridge.candidates(scope_key="scope", now=NOW)[0]
    assert social.refreshes == [NOW]
    assert candidate.action["memory_ref"] == fresh
    assert candidate.action["memory_ref"] != stale


def test_failed_refresh_disables_social_refs_without_disabling_other_candidates():
    item = CandidateIntent(candidate_id="c1", type="share", intent="分享", goal="表达",
                           sources=["memory:m1"], internal_need=.4)
    stale = encode_exact_ref("memory", scope_key="scope", object_id="m1", revision=1,
                             digest="1" * 64)
    social = SimpleNamespace(
        refresh=lambda *, now: (_ for _ in ()).throw(RuntimeError("refresh failed")),
        candidate_refs=lambda sources: {"memory_ref": stale},
    )
    runtime = SimpleNamespace(
        projections=SimpleNamespace(candidates=Candidates(item)),
        config=SimpleNamespace(candidate=SimpleNamespace(max_active=4)),
        _event_ids_behind=lambda source: [],
    )
    candidate = ConcreteLegacyRuntimeV2Bridge(runtime, social_service=social).candidates(
        scope_key="scope", now=NOW)[0]
    assert candidate.candidate_id == "c1"
    assert "memory_ref" not in candidate.action
    assert "social_ref" not in candidate.action


def test_invalid_exact_source_is_rejected_before_claim_or_outbox():
    item = CandidateIntent(candidate_id="c1", type="share", intent="分享", goal="表达",
                           sources=["memory:m1"], internal_need=.4)
    refs = {"memory_ref": encode_exact_ref("memory", scope_key="other", object_id="m1",
                                           revision=1, digest=DIGEST)}
    social = SimpleNamespace(refresh=lambda *, now: None,
                             candidate_refs=lambda sources: refs,
                             validate_candidate_action=lambda action: False)
    calls = []
    runtime = SimpleNamespace(projections=SimpleNamespace(candidates=Candidates(item)),
                              config=SimpleNamespace(candidate=SimpleNamespace(max_active=4)),
                              _event_ids_behind=lambda source: [])
    bridge = ConcreteLegacyRuntimeV2Bridge(runtime, scope_key="scope", social_service=social,
                                           dispatch_coordinator=SimpleNamespace(
                                               create_live_dispatch_claim=lambda **kw: calls.append(kw)))
    candidate = bridge.candidates(scope_key="scope", now=NOW)[0]
    import pytest
    with pytest.raises(ValueError, match="stale, tombstoned, or cross-scope"):
        bridge.commit_candidate_with_snapshot(decision_id="d", candidate=candidate, now=NOW,
                                              persist_snapshot=lambda receipt: None)
    assert calls == []
