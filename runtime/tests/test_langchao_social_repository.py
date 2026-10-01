"""Repository/authority tests for 「浪潮」 social-emotional memory v1."""

from __future__ import annotations

import dataclasses
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from companion_runtime.langchao_social_repository import (
    BuildRun,
    CommitDisposition,
    LangchaoSocialCASConflictError,
    LangchaoSocialConflictError,
    LangchaoSocialRepository,
    LangchaoSocialSourceError,
    SourceResolution,
    SourceResolutionStatus,
)
from companion_runtime.langchao_social_schema import LANGCHAO_SOCIAL_SCHEMA_STATEMENTS
from companion_runtime.langchao_social_types import (
    SocialItemProposal,
    SocialItemType,
    SocialProposal,
    SocialRole,
    SourceKind,
    SourceRef,
    sha256_json,
)

NOW = datetime(2026, 10, 3, 8, tzinfo=timezone.utc)
SCOPE = "user:social"


class Cursor:
    def __init__(self, row=None, rows=None, rowcount=1):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        self.calls.append((compact, params))
        response = self.responses.pop(0) if self.responses else None
        if isinstance(response, Cursor):
            return response
        return Cursor(row=response)

    @contextmanager
    def transaction(self):
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


class Resolver:
    def __init__(self, result=None):
        self.result = result
        self.refs = []

    def resolve_source(self, source):
        self.refs.append(source)
        return self.result or resolution(source)


def source(*, scope=SCOPE, revision=1, digest=None, source_id="event:1"):
    return SourceRef(
        scope_key=scope, source_kind=SourceKind.EVENT, source_id=source_id,
        source_revision=revision, source_sha256=digest or sha256_json({"event": source_id}),
        observed_at=NOW,
    )


def resolution(ref, *, status=SourceResolutionStatus.ACTIVE, revision=None, digest=None):
    return SourceResolution(
        scope_key=ref.scope_key, source_kind=ref.source_kind, source_id=ref.source_id,
        source_revision=ref.source_revision if revision is None else revision,
        source_sha256=ref.source_sha256 if digest is None else digest,
        status=status, observed_at=ref.observed_at,
    )


def proposal(*, item_type=SocialItemType.SHARED_MATTER, sources=None,
             proposal_id="proposal:1", item_key="item:1"):
    refs = tuple(sources or (source(),))
    item = SocialItemProposal(
        item_key=item_key, scope_key=SCOPE, item_type=item_type,
        role=SocialRole.SHARED, summary="共同事实", sources=refs,
    )
    return SocialProposal(
        proposal_id=proposal_id, scope_key=SCOPE, revision=1,
        created_at=NOW, items=(item,),
    )


def stored(value):
    return {"payload": value.to_dict(), "payload_sha256": sha256_json(value.to_dict())}


def repository(conn, resolver=None):
    return LangchaoSocialRepository(conn, scope_key=SCOPE, source_resolver=resolver or Resolver())


def test_build_run_is_idempotent_and_different_output_is_hard_conflict():
    run = BuildRun(
        build_run_id="build:1", revision=1, builder_version="rules:1",
        input_sha256="a" * 64, output_sha256="b" * 64, status="completed",
        started_at=NOW, completed_at=NOW,
    )
    existing = {"build_run_id": "build:1", "revision": 1,
                "output_sha256": "b" * 64, "status": "completed"}
    conn = Connection([existing])
    assert repository(conn).record_build_run(run) is existing
    assert not any("INSERT INTO" in sql for sql, _ in conn.calls)

    conflict = Connection([{**existing, "output_sha256": "c" * 64}])
    with pytest.raises(LangchaoSocialConflictError, match="different output"):
        repository(conflict).record_build_run(run)
    assert conflict.rollbacks == 1


def test_submit_is_immutable_hash_idempotent_and_provenance_bound():
    value = proposal()
    digest = sha256_json(value.to_dict())
    existing = {"payload_matches": True, "build_run_id": "build:1", "build_run_revision": 1}
    conn = Connection([{"output_sha256": value.proposal_sha256}, existing])
    assert repository(conn).submit_proposal(value, build_run_id="build:1") is existing
    assert not any("INSERT INTO langchao_social_proposals" in sql for sql, _ in conn.calls)

    conflict = Connection([{"output_sha256": value.proposal_sha256},
                           {"payload_matches": False, "payload_sha256": digest}])
    with pytest.raises(LangchaoSocialConflictError, match="different hash"):
        repository(conflict).submit_proposal(value, build_run_id="build:1")
    assert conflict.rollbacks == 1


def test_commit_rebases_revision_or_hash_staleness_but_discards_deleted_source():
    value = proposal()
    ref = value.items[0].sources[0]
    stale_resolver = Resolver(resolution(ref, revision=2, digest="f" * 64))
    stale = Connection([None, stored(value), None])
    result = repository(stale, stale_resolver).commit_proposal(
        proposal_id=value.proposal_id, revision=1, expected_projection_pointer_version=0,
    )
    assert result.disposition is CommitDisposition.REBASE
    assert result.stale_sources[0].source_revision == 2
    assert not any("INSERT INTO langchao_social_items" in sql for sql, _ in stale.calls)

    deleted_resolver = Resolver(resolution(ref, status=SourceResolutionStatus.TOMBSTONED))
    deleted = Connection([None, stored(value), None])
    result = repository(deleted, deleted_resolver).commit_proposal(
        proposal_id=value.proposal_id, revision=1, expected_projection_pointer_version=0,
    )
    assert result.disposition is CommitDisposition.DISCARDED
    assert "unavailable" in result.reason


def test_same_source_coordinate_with_conflicting_hashes_is_hard_conflict():
    value = proposal()
    payload = value.to_dict()
    conflicting = dict(payload["items"][0]["sources"][0], source_sha256="f" * 64)
    payload["items"][0]["sources"].append(conflicting)
    conn = Connection([None, {"payload": payload, "payload_sha256": sha256_json(payload)}, None])
    with pytest.raises(LangchaoSocialConflictError, match="conflicting hashes"):
        repository(conn).commit_proposal(
            proposal_id=value.proposal_id, revision=1,
            expected_projection_pointer_version=0,
        )
    assert conn.rollbacks == 1


def test_resolver_must_match_scope_kind_and_id_exactly():
    value = proposal()
    ref = value.items[0].sources[0]
    wrong = dataclasses.replace(resolution(ref), scope_key="other")
    conn = Connection([None, stored(value), None])
    with pytest.raises(LangchaoSocialSourceError, match="scope/kind/id"):
        repository(conn, Resolver(wrong)).commit_proposal(
            proposal_id=value.proposal_id, revision=1, expected_projection_pointer_version=0,
        )
    assert conn.rollbacks == 1


def test_commit_cas_conflict_rolls_back_before_materialisation():
    value = proposal()
    conn = Connection([None, stored(value), None, None, {"pointer_version": 4}])
    with pytest.raises(LangchaoSocialCASConflictError, match="expected 3, actual 4"):
        repository(conn).commit_proposal(
            proposal_id=value.proposal_id, revision=1,
            expected_projection_pointer_version=3,
        )
    assert conn.rollbacks == 1 and conn.commits == 0
    assert not any("INSERT INTO langchao_social_items" in sql for sql, _ in conn.calls)


def test_goal_draft_commit_remains_proposed_and_never_active_or_adopted():
    value = proposal(item_type=SocialItemType.GOAL_DRAFT)
    ref = value.items[0].sources[0]
    responses = [
        None, stored(value), None, None, {"pointer_version": 0}, None,
        {"source_sha256": ref.source_sha256, "source_status": "active"},
        {"revision": 1}, None, None, None, Cursor(rowcount=1), Cursor(rowcount=1),
    ]
    conn = Connection(responses)
    result = repository(conn).commit_proposal(
        proposal_id=value.proposal_id, revision=1,
        expected_projection_pointer_version=0, committed_at=NOW,
    )
    assert result.disposition is CommitDisposition.COMMITTED
    insert = next((sql, params) for sql, params in conn.calls
                  if "INSERT INTO langchao_social_items" in sql)
    assert insert[1][7] == "proposed"
    assert "adopted" not in insert[0].lower()


def _commit_responses(value, pointer):
    ref = value.items[0].sources[0]
    return [
        None, stored(value), None, None, {"pointer_version": pointer}, None,
        {"source_sha256": ref.source_sha256, "source_status": "active"},
        {"revision": 1}, None, None, None, Cursor(rowcount=1), Cursor(rowcount=1),
    ]


def test_sequential_commits_of_different_items_use_singleton_pointer_not_head_versions():
    first = proposal()
    second = proposal(proposal_id="proposal:2", item_key="item:2",
                      sources=(source(source_id="event:2"),))
    conn = Connection(_commit_responses(first, 0) + _commit_responses(second, 1))
    repo = repository(conn)
    assert repo.commit_proposal(
        proposal_id=first.proposal_id, revision=1,
        expected_projection_pointer_version=0, committed_at=NOW,
    ).pointer_version == 1
    assert repo.commit_proposal(
        proposal_id=second.proposal_id, revision=1,
        expected_projection_pointer_version=1, committed_at=NOW,
    ).pointer_version == 2
    assert sum("SELECT pointer_version FROM langchao_social_projection_state" in sql
               for sql, _ in conn.calls) == 2
    assert not any("SELECT pointer_version FROM langchao_social_projection_heads" in sql
                   for sql, _ in conn.calls)


def test_invalidation_advances_singleton_and_following_commit_uses_new_version():
    ref = source()
    following = proposal(proposal_id="proposal:2", item_key="item:2",
                         sources=(source(source_id="event:2"),))
    invalidate_responses = [
        None, None, {"pointer_version": 0},
        {"source_sha256": ref.source_sha256, "source_status": "active",
         "invalidation_reason": None},
        None, {"source_sha256": ref.source_sha256, "source_status": "invalidated"},
        Cursor(rows=[]), None, Cursor(rows=[]), Cursor(rowcount=1),
    ]
    conn = Connection(invalidate_responses + _commit_responses(following, 1))
    repo = repository(conn)
    repo.invalidate_source(ref, reason="counterevidence", occurred_at=NOW)
    result = repo.commit_proposal(
        proposal_id=following.proposal_id, revision=1,
        expected_projection_pointer_version=1, committed_at=NOW,
    )
    assert result.pointer_version == 2


def test_commit_exact_replay_uses_state_event_and_performs_no_new_writes():
    value = proposal()
    replay_payload = {"pointer_version": 8, "items": [{"item_key": "item:1", "revision": 3}]}
    conn = Connection([None, stored(value), {"payload": replay_payload}])
    result = repository(conn).commit_proposal(
        proposal_id=value.proposal_id, revision=1,
        expected_projection_pointer_version=7,
    )
    assert result.disposition is CommitDisposition.IDEMPOTENT
    assert result.pointer_version == 8
    assert result.item_revisions == (("item:1", 3),)
    assert not any("INSERT INTO" in sql for sql, _ in conn.calls)


def test_tombstone_persists_only_minimal_identity_hash_reason_and_invalidates_dependencies():
    ref = source()
    item = {
        "proposal_id": "proposal:1", "proposal_revision": 1,
        "item_type": "shared_matter", "role": "shared", "summary": "literal",
        "attributes": {}, "supersedes_item_key": None, "supersedes_revision": None,
    }
    conn = Connection([
        None, None, {"pointer_version": 3}, None, None,
        {"source_sha256": ref.source_sha256, "source_status": "tombstoned"},
        Cursor(rows=[{"item_key": "item:1", "item_revision": 1}]),
        None, {"source_count": 1}, None, item, {"revision": 2}, None, None,
        Cursor(rows=[]), Cursor(rows=[{"link_key": "link:1", "link_revision": 1}]), None,
        Cursor(rowcount=1),
    ])
    repository(conn).tombstone_source(ref, reason="user deletion", occurred_at=NOW)
    source_insert = next((sql, params) for sql, params in conn.calls
                         if "INSERT INTO langchao_social_sources" in sql)
    assert "payload" not in source_insert[0].lower()
    assert "summary" not in source_insert[0].lower()
    assert ref.source_sha256 in source_insert[1] and "user deletion" in source_insert[1]
    assert any("'invalidated'" in sql for sql, _ in conn.calls
               if "INSERT INTO langchao_social_items" in sql)
    assert any(params and params[3] == "link" for sql, params in conn.calls
               if "INSERT INTO langchao_social_state_events" in sql)


def test_multiple_independent_sources_keep_item_and_mark_reassessment():
    ref = source()
    conn = Connection([
        None, None, {"pointer_version": 7}, None, None,
        {"source_sha256": ref.source_sha256, "source_status": "invalidated"},
        Cursor(rows=[{"item_key": "item:1", "item_revision": 1}]),
        None, {"source_count": 2}, None, Cursor(rows=[]), Cursor(rowcount=1),
    ])
    repository(conn).invalidate_source(ref, reason="counterevidence", occurred_at=NOW)
    assert not any("INSERT INTO langchao_social_items" in sql for sql, _ in conn.calls)
    dependency = [params for sql, params in conn.calls
                  if "INSERT INTO langchao_social_state_events" in sql and params[2] == "dependency_invalidated"]
    assert dependency and '"needs_reassessment":true' in dependency[0][7]


def test_active_projection_read_is_bounded():
    conn = Connection([Cursor(rows=[{"item_key": "item:1"}])])
    assert repository(conn).get_active_projection(limit=25) == ({"item_key": "item:1"},)
    assert conn.calls[0][1] == (SCOPE, 25)
    with pytest.raises(ValueError, match="between 1 and 500"):
        repository(Connection()).get_active_projection(limit=501)


@pytest.mark.skipif(not os.getenv("LANGCHAO_TEST_POSTGRES_DSN"),
                    reason="PostgreSQL DSN not configured")
def test_social_repository_postgres_schema_smoke():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(os.environ["LANGCHAO_TEST_POSTGRES_DSN"]) as connection:
        with connection.transaction(force_rollback=True):
            for statement in LANGCHAO_SOCIAL_SCHEMA_STATEMENTS:
                connection.execute(statement)
