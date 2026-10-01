"""Contracts for 「浪潮」 v15 engine authority and SQL dispatch claims."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from companion_runtime.langchao_authority import (
    AuthorityEngine,
    AuthorityMode,
    authority_may_dispatch,
)
from companion_runtime.langchao_authority_repository import (
    AuthorityConflictError,
    AuthorityInFlightError,
    DispatchNotAuthorizedError,
    LangchaoAuthorityRepository,
)
from companion_runtime.langchao_authority_schema import LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS
from companion_runtime.user_model_v2_migrations import migration_records
from companion_runtime.user_model_v2_schema import MIGRATIONS

NOW = datetime(2026, 3, 2, tzinfo=timezone.utc)
SCOPE = "scope:a"


class Cursor:
    def __init__(self, *, row=None, rows=None, rowcount=1):
        self.row = row
        self.rows = list(rows or ())
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.transactions = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        response = self.responses.pop(0) if self.responses else {}
        if isinstance(response, Cursor):
            return response
        if isinstance(response, list):
            return Cursor(rows=response)
        return Cursor(row=response)

    @contextmanager
    def transaction(self):
        self.transactions += 1
        try:
            yield
        except Exception:
            self.rollbacks += 1
            raise


def live_row(engine="runtime_v2"):
    return {
        "authority_id": "authority:1",
        "revision": 3,
        "engine_key": engine,
        "mode": "live",
        "may_dispatch": True,
    }


def test_authority_dispatch_derivation_is_closed():
    assert authority_may_dispatch(AuthorityEngine.RUNTIME_V2, AuthorityMode.LIVE)
    assert authority_may_dispatch(AuthorityEngine.LANGCHAO, AuthorityMode.LIVE)
    for engine in AuthorityEngine:
        for mode in AuthorityMode:
            if mode is not AuthorityMode.LIVE or engine is AuthorityEngine.NONE:
                assert not authority_may_dispatch(engine, mode)


def test_v15_migration_is_appended_after_state_schema_with_stable_checksum():
    assert MIGRATIONS[14] == (15, LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS)
    records = migration_records()
    assert tuple(record.version for record in records[:15]) == tuple(range(1, 16))
    assert len(records[14].checksum) == 64
    assert records[14].checksum == migration_records()[14].checksum
    flattened = [statement for _version, statements in MIGRATIONS for statement in statements]
    authority_index = flattened.index(LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS[0])
    candidate_index = next(
        index for index, statement in enumerate(flattened)
        if "CREATE TABLE IF NOT EXISTS langchao_candidate_revisions" in statement
    )
    assert candidate_index < authority_index  # claim candidate FK target exists first


def test_schema_enforces_one_active_pointer_and_live_claim_fk():
    ddl = " ".join("\n".join(LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS).split()).upper()
    assert "LANGCHAO_AUTHORITY_REVISIONS" in ddl
    assert "PRIMARY KEY (SCOPE_KEY, AUTHORITY_ID, REVISION)" in ddl
    assert "MAY_DISPATCH BOOLEAN GENERATED ALWAYS AS" in ddl
    assert "MODE = 'LIVE' AND ENGINE_KEY <> 'NONE'" in ddl
    assert "SCOPE_KEY TEXT PRIMARY KEY" in ddl  # at most one active/live authority per scope
    assert "POINTER_VERSION BIGINT" in ddl
    assert "LANGCHAO_DISPATCH_CLAIMS" in ddl
    assert "CHECK (MAY_DISPATCH)" in ddl
    assert "FOREIGN KEY (SCOPE_KEY, AUTHORITY_ID, AUTHORITY_REVISION, ENGINE_KEY, MAY_DISPATCH)" in ddl
    assert "REFERENCES LANGCHAO_AUTHORITY_REVISIONS (SCOPE_KEY, AUTHORITY_ID, REVISION, ENGINE_KEY, MAY_DISPATCH)" in ddl
    assert "FOREIGN KEY (SCOPE_KEY, CANDIDATE_ID, CANDIDATE_REVISION)" in ddl
    assert "UNIQUE (SCOPE_KEY, ATTEMPT_ID)" in ddl
    assert "UNIQUE (SCOPE_KEY, IDEMPOTENCY_KEY)" in ddl
    assert ddl.count("BEFORE UPDATE OR DELETE") == 2
    # No premature coupling to existing attempt/outbox tables.
    assert "REFERENCES ATTEMPT" not in ddl
    assert "REFERENCES OUTBOX" not in ddl


def test_shadow_or_disabled_claim_is_sql_impossible_by_witness():
    ddl = " ".join("\n".join(LANGCHAO_AUTHORITY_SCHEMA_V15_STATEMENTS).split()).upper()
    # Claim requires literal TRUE, while the referenced generated witness can only be
    # TRUE for live + non-none. Therefore no shadow/disabled authority FK can match.
    assert "MAY_DISPATCH BOOLEAN NOT NULL DEFAULT TRUE CHECK (MAY_DISPATCH)" in ddl
    assert "(MODE = 'LIVE' AND ENGINE_KEY <> 'NONE') STORED" in ddl
    assert "ENGINE_KEY TEXT NOT NULL CHECK (ENGINE_KEY IN ('RUNTIME_V2', 'LANGCHAO'))" in ddl


def test_bootstrap_is_runtime_live_or_none_disabled_only():
    # lock; no active; insert revision; active insert RETURNING; final active read
    conn = Connection([{}, None, {"authority_id": "authority:runtime_v2"}, {"scope_key": SCOPE}, {"mode": "live"}])
    row = LangchaoAuthorityRepository(conn, scope_key=SCOPE).bootstrap(created_at=NOW)
    assert row == {"mode": "live"}
    revision_params = conn.calls[2][1]
    assert revision_params[3:5] == ("runtime_v2", "live")
    assert "pg_advisory_xact_lock" in conn.calls[0][0]

    disabled = Connection([{}, None, {"authority_id": "authority:none"}, {"scope_key": SCOPE}, {"mode": "disabled"}])
    LangchaoAuthorityRepository(disabled, scope_key=SCOPE).bootstrap(
        engine_key=AuthorityEngine.NONE, created_at=NOW
    )
    assert disabled.calls[2][1][3:5] == ("none", "disabled")
    with pytest.raises(ValueError, match="bootstrap"):
        LangchaoAuthorityRepository(Connection(), scope_key=SCOPE).bootstrap(engine_key="langchao")


def test_switch_uses_advisory_lock_and_cas_and_freezes_inflight_audit():
    current = {"authority_id": "old", "revision": 2, "pointer_version": 7}
    conn = Connection([{}, current, None, {"authority_id": "new"}, Cursor(rowcount=1), {"pointer_version": 8}])
    result = LangchaoAuthorityRepository(conn, scope_key=SCOPE).switch_authority(
        authority_id="new",
        engine_key="langchao",
        mode="live",
        expected_pointer_version=7,
        in_flight_count=2,
        transfer_refs=("attempt:1",),
        abort_refs=("attempt:2",),
        reason="cut over",
        created_at=NOW,
    )
    assert result == {"pointer_version": 8}
    assert "pg_advisory_xact_lock" in conn.calls[0][0]
    assert "FOR UPDATE" in conn.calls[1][0]
    assert conn.calls[4][1] == (SCOPE, "new", 1, 8, 7)
    encoded = conn.calls[3][1][6]
    assert '"transfer_refs":["attempt:1"]' in encoded
    assert '"abort_refs":["attempt:2"]' in encoded


def test_switch_cas_and_inflight_fail_before_publication_and_roll_back():
    with pytest.raises(AuthorityInFlightError):
        LangchaoAuthorityRepository(Connection(), scope_key=SCOPE).switch_authority(
            engine_key="langchao", mode="live", expected_pointer_version=1,
            in_flight_count=2, transfer_refs=("one",), reason="unsafe", created_at=NOW,
        )

    conn = Connection([{}, {"authority_id": "old", "revision": 1, "pointer_version": 4}])
    with pytest.raises(AuthorityConflictError, match="expected 3, found 4"):
        LangchaoAuthorityRepository(conn, scope_key=SCOPE).switch_authority(
            engine_key="langchao", mode="live", expected_pointer_version=3,
            in_flight_count=0, reason="stale", created_at=NOW,
        )
    assert len(conn.calls) == 2  # no immutable revision inserted
    assert conn.rollbacks == 1

    failed_cas = Connection([
        {}, {"authority_id": "old", "revision": 1, "pointer_version": 4},
        None, {"authority_id": "new"}, Cursor(rowcount=0),
    ])
    with pytest.raises(AuthorityConflictError):
        LangchaoAuthorityRepository(failed_cas, scope_key=SCOPE).switch_authority(
            authority_id="new", engine_key="langchao", mode="live",
            expected_pointer_version=4, in_flight_count=0, reason="race", created_at=NOW,
        )
    assert failed_cas.rollbacks == 1  # inserted revision is transactionally rolled back


def test_dispatch_claim_requires_active_live_exact_authority_and_candidate_revision():
    inserted = {"dispatch_id": "dispatch:1"}
    conn = Connection([{}, live_row("langchao"), [], inserted])
    row = LangchaoAuthorityRepository(conn, scope_key=SCOPE).create_dispatch_claim(
        dispatch_id="dispatch:1", candidate_id="candidate:7", candidate_revision=5,
        attempt_id="attempt:1", idempotency_key="key:1", created_at=NOW,
    )
    assert row is inserted
    assert "FOR UPDATE OF a" in conn.calls[1][0]
    params = conn.calls[3][1]
    assert params[:9] == (SCOPE, "dispatch:1", "authority:1", 3, "langchao", "candidate:7", 5, "attempt:1", "key:1")

    denied = Connection([{}, {**live_row(), "mode": "shadow", "may_dispatch": False}])
    with pytest.raises(DispatchNotAuthorizedError):
        LangchaoAuthorityRepository(denied, scope_key=SCOPE).create_dispatch_claim(
            dispatch_id="d", candidate_id="c", candidate_revision=1,
            attempt_id="a", idempotency_key="k", created_at=NOW,
        )
    assert denied.rollbacks == 1


def test_dispatch_claim_idempotency_returns_same_hash_and_rejects_conflict():
    existing = {"dispatch_id": "dispatch:1", "claim_matches": True}
    conn = Connection([{}, live_row(), [existing]])
    result = LangchaoAuthorityRepository(conn, scope_key=SCOPE).create_dispatch_claim(
        dispatch_id="dispatch:1", candidate_id="candidate:1", candidate_revision=1,
        attempt_id="attempt:1", idempotency_key="key:1", created_at=NOW,
    )
    assert result is existing
    assert len(conn.calls) == 3

    conflict = Connection([{}, live_row(), [{"claim_matches": False}]])
    with pytest.raises(AuthorityConflictError, match="different content"):
        LangchaoAuthorityRepository(conflict, scope_key=SCOPE).create_dispatch_claim(
            dispatch_id="dispatch:1", candidate_id="candidate:1", candidate_revision=2,
            attempt_id="attempt:1", idempotency_key="key:1", created_at=NOW,
        )
    assert conflict.rollbacks == 1


def test_live_dispatch_claim_requires_exact_engine_and_is_idempotent():
    inserted = {"claim_id": "claim:1"}
    conn = Connection([{}, live_row("runtime_v2"), [], inserted])
    repo = LangchaoAuthorityRepository(conn, scope_key=SCOPE)
    row = repo.create_live_dispatch_claim(
        claim_id="claim:1", round_id="round:1", candidate_id="candidate:1",
        candidate_version="v1", attempt_id="attempt:1", render_outbox_id="outbox:1",
        idempotency_key="key:1", expected_engine="runtime_v2", created_at=NOW,
    )
    assert row is inserted
    assert "live_dispatch_claims" in conn.calls[-1][0]
    assert conn.calls[-1][1][:12] == (
        SCOPE, "claim:1", "authority:1", 3, "runtime_v2", "round:1",
        "candidate:1", "v1", "attempt:1", "outbox:1", "key:1",
        conn.calls[-1][1][11],
    )

    retry = {"claim_id": "claim:1", "claim_matches": True}
    same = Connection([{}, live_row("runtime_v2"), [retry]])
    assert LangchaoAuthorityRepository(same, scope_key=SCOPE).create_live_dispatch_claim(
        claim_id="claim:1", round_id="round:1", candidate_id="candidate:1",
        candidate_version="v1", attempt_id="attempt:1", render_outbox_id="outbox:1",
        idempotency_key="key:1", expected_engine="runtime_v2", created_at=NOW,
    ) is retry

    wrong_engine = Connection([{}, live_row("langchao")])
    with pytest.raises(DispatchNotAuthorizedError, match="runtime_v2/live"):
        LangchaoAuthorityRepository(wrong_engine, scope_key=SCOPE).create_live_dispatch_claim(
            claim_id="claim:1", round_id="round:1", candidate_id="candidate:1",
            candidate_version="v1", attempt_id="attempt:1", render_outbox_id="outbox:1",
            idempotency_key="key:1", expected_engine="runtime_v2", created_at=NOW,
        )


def test_repository_scope_is_present_in_every_dispatch_query_and_hash():
    conn_a = Connection([{}, live_row(), [], {"dispatch_id": "same"}])
    conn_b = Connection([{}, live_row(), [], {"dispatch_id": "same"}])
    for scope, conn in (("scope:a", conn_a), ("scope:b", conn_b)):
        LangchaoAuthorityRepository(conn, scope_key=scope).create_dispatch_claim(
            dispatch_id="same", candidate_id="candidate:1", candidate_revision=1,
            attempt_id="same", idempotency_key="same", created_at=NOW,
        )
        assert all(scope in params for sql, params in conn.calls if params and "pg_advisory" not in sql)
        assert scope in conn.calls[0][1][0]  # scoped advisory-lock namespace
    # Scope participates in canonical claim content, so cross-scope hashes differ.
    assert conn_a.calls[-1][1][-2] != conn_b.calls[-1][1][-2]
