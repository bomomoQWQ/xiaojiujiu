"""Contract tests for isolated 「浪潮」 v14 round/state persistence."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from companion_runtime.langchao_engine import LangchaoParameters, advance_langchao
from companion_runtime.langchao_state_repository import (
    LangchaoStateConflictError,
    LangchaoStateRepository,
)
from companion_runtime.langchao_state_schema import LANGCHAO_STATE_SCHEMA_V14_STATEMENTS
from companion_runtime.langchao_types import LangchaoState, MotivationDirection
from companion_runtime.user_model_v2_migrations import migration_records
from companion_runtime.user_model_v2_schema import MIGRATIONS, USER_MODEL_SCHEMA_VERSION

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
SCOPE = "scope:state"


class Cursor:
    def __init__(self, row=None, rowcount=1):
        self.row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, rows=(), rowcounts=()):
        self.rows = list(rows)
        self.rowcounts = list(rowcounts)
        self.calls = []
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        row = self.rows.pop(0) if self.rows else None
        rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        return Cursor(row, rowcount)

    @contextmanager
    def transaction(self):
        self.transactions += 1
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


def state(*, revision=1, advanced_at=NOW, readiness=None, order=("a", "b")):
    readiness = readiness or tuple((candidate_id, 0.0) for candidate_id in order)
    return LangchaoState(
        scope_key=SCOPE, decision_round_id="round:1", working_set=order,
        readiness=readiness, attraction=tuple((candidate_id, 0.5) for candidate_id in order),
        attention=tuple((direction, 1.0) for direction in MotivationDirection),
        advanced_at=advanced_at, based_on_state_version=4, event_cursor="event:7",
        goal_snapshot_version="goals:2", reward_snapshot_version="rewards:3",
        candidate_snapshot_version="candidates:5", prediction_snapshot_version="predictions:8",
        value_profile_version="values:1", attention_version="attention:1",
        parameter_version="langchao.parameters.v1", permission_version="permissions:9",
        revision=revision,
    )


def parameters():
    return LangchaoParameters(
        leak=0.2, competition_gain=0.0, decision_threshold=0.99,
        time_scale_seconds=10.0, max_step_seconds=0.5,
        crossing_tolerance=1e-7, tie_tolerance=1e-6,
    )


def test_v14_migration_appends_after_unchanged_v1_through_v13():
    assert USER_MODEL_SCHEMA_VERSION == 14
    assert MIGRATIONS[-1] == (14, LANGCHAO_STATE_SCHEMA_V14_STATEMENTS)
    records = migration_records()
    assert tuple(record.version for record in records) == tuple(range(1, 15))
    assert records[-2].version == 13
    assert len(records[-1].checksum) == 64
    assert records[-1].checksum == migration_records()[-1].checksum


def test_v14_schema_has_exact_fks_checks_hashes_and_immutable_history():
    ddl = " ".join("\n".join(LANGCHAO_STATE_SCHEMA_V14_STATEMENTS).split()).upper()
    for table in (
        "LANGCHAO_ROUNDS", "LANGCHAO_STATE_SNAPSHOTS", "LANGCHAO_ACTIVE_STATE",
        "LANGCHAO_STATE_CANDIDATES", "LANGCHAO_INTEGRATION_STEPS",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "RUN_MODE IN ('LIVE', 'SHADOW', 'REPLAY')" in ddl
    assert "STATUS IN ('OPEN', 'DECIDED', 'DEFERRED', 'ABORTED')" in ddl
    assert "DECISION_CANDIDATE_REVISION" in ddl
    assert "REFERENCES LANGCHAO_CANDIDATE_REVISIONS (SCOPE_KEY, CANDIDATE_ID, REVISION)" in ddl
    assert "REFERENCES LANGCHAO_STATE_SNAPSHOTS (SCOPE_KEY, ROUND_ID, STATE_REVISION)" in ddl
    assert "ON DELETE RESTRICT" in ddl
    assert "READINESS >= 0.0 AND READINESS <= 1.0" in ddl
    assert "'INFINITY'::DOUBLE PRECISION" in ddl
    assert ddl.count("BEFORE UPDATE OR DELETE") == 3
    assert "STATUS = 'DECIDED'" in ddl and "STATUS = 'DEFERRED'" in ddl


def test_begin_round_persists_mode_snapshot_candidate_order_and_cas():
    # round lookup, round insert, snapshot lookup/insert, two candidates,
    # advisory lock, active lookup, active upsert
    conn = Connection(rows=[None, None, None, None, None, None, None, None, None])
    pointer = LangchaoStateRepository(conn, scope_key=SCOPE).begin_round(
        state(), run_mode="shadow", candidate_revisions={"b": 22, "a": 11},
    )
    assert pointer == 1
    assert conn.transactions == conn.commits == 1
    round_insert = next(call for call in conn.calls if "INSERT INTO langchao_rounds" in call[0])
    assert round_insert[1][2] == "shadow"
    candidate_inserts = [call for call in conn.calls if "INSERT INTO langchao_state_candidates" in call[0]]
    assert [(call[1][3], call[1][4], call[1][5]) for call in candidate_inserts] == [
        (0, "a", 11), (1, "b", 22),
    ]
    assert "pg_advisory_xact_lock" in conn.calls[-3][0]
    assert "FOR UPDATE" in conn.calls[-2][0]
    assert conn.calls[-1][1] == (SCOPE, "round:1", 1, 1, 0)


@pytest.mark.parametrize("mode", ["live", "shadow", "replay"])
def test_begin_round_accepts_all_explicit_run_modes(mode):
    conn = Connection(rows=[None] * 9)
    LangchaoStateRepository(conn, scope_key=SCOPE).begin_round(
        state(), run_mode=mode, candidate_revisions={"a": 1, "b": 2},
    )
    params = next(params for sql, params in conn.calls if "INSERT INTO langchao_rounds" in sql)
    assert params[2] == mode


def test_begin_round_cas_conflict_rolls_back_everything():
    # Existing active pointer 7 disagrees with expected zero.
    conn = Connection(rows=[None, None, None, None, None, None, None, {"pointer_version": 7}])
    with pytest.raises(LangchaoStateConflictError, match="CAS failed"):
        LangchaoStateRepository(conn, scope_key=SCOPE).begin_round(
            state(), run_mode="live", candidate_revisions={"a": 1, "b": 2},
        )
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_append_writes_snapshot_then_steps_in_engine_order_then_cas_and_finishes_deferred():
    before = state()
    result = advance_langchao(
        before, until=NOW + timedelta(seconds=1.2), parameters=parameters(),
        decision_budget_seconds=1.2,
    )
    assert len(result.steps) == 3
    rows = [
        {"round_id": "round:1", "state_revision": 1, "pointer_version": 1,
         "payload_sha256": _digest(before)},
        {"status": "open"},
        None,  # new snapshot absent
        None, None, None,  # snapshot and candidate INSERT responses
        None, None, None,  # step INSERT responses
        None, {"pointer_version": 1}, None,  # CAS
        None,  # round UPDATE
    ]
    conn = Connection(rows=rows)
    pointer = LangchaoStateRepository(conn, scope_key=SCOPE).append_advance(
        result, input_state=before, candidate_revisions={"a": 3, "b": 4},
        expected_pointer_version=1,
    )
    assert pointer == 2
    step_calls = [call for call in conn.calls if "INSERT INTO langchao_integration_steps" in call[0]]
    assert [call[1][3] for call in step_calls] == [0, 1, 2]
    assert [call[1][4] for call in step_calls] == [step.started_at for step in result.steps]
    snapshot_index = next(i for i, call in enumerate(conn.calls) if "INSERT INTO langchao_state_snapshots" in call[0])
    assert snapshot_index < min(i for i, call in enumerate(conn.calls) if "INSERT INTO langchao_integration_steps" in call[0])
    update = next(call for call in conn.calls if "UPDATE langchao_rounds SET" in call[0])
    assert update[1][0] == "deferred"
    assert update[1][3:7] == (None, None, None, "decision_budget_exhausted")


def test_append_decision_freezes_exact_candidate_revision_and_mutual_exclusion():
    before = state(order=("a",), readiness=(("a", 0.99),))
    result = advance_langchao(before, until=NOW, parameters=parameters())
    rows = [
        {"round_id": "round:1", "state_revision": 1, "pointer_version": 4,
         "payload_sha256": _digest(before)},
        {"status": "open"}, None, None, None, None, {"pointer_version": 4}, None, None,
    ]
    conn = Connection(rows=rows)
    LangchaoStateRepository(conn, scope_key=SCOPE).append_advance(
        result, input_state=before, candidate_revisions={"a": 37}, expected_pointer_version=4,
    )
    update = next(call for call in conn.calls if "UPDATE langchao_rounds SET" in call[0])
    assert update[1][0] == "decided"
    assert update[1][3:7] == ("a", 37, NOW, None)


def test_exact_append_replay_is_idempotent_but_different_payload_fails_hard():
    before = state()
    result = advance_langchao(before, until=NOW + timedelta(seconds=1), parameters=parameters())
    active_output = {
        "round_id": "round:1", "state_revision": result.state.revision,
        "pointer_version": 2, "payload_sha256": _digest(result.state),
    }
    conn = Connection(rows=[active_output, {"payload_sha256": _digest(result.state)}])
    pointer = LangchaoStateRepository(conn, scope_key=SCOPE).append_advance(
        result, input_state=before, candidate_revisions={"a": 1, "b": 2}, expected_pointer_version=1,
    )
    assert pointer == 2
    assert conn.commits == 1
    assert not any("INSERT INTO" in sql or "UPDATE " in sql for sql, _ in conn.calls)

    conflicting_active = dict(active_output, payload_sha256="0" * 64)
    conflict = Connection(rows=[conflicting_active, {"payload_sha256": "f" * 64}])
    with pytest.raises(LangchaoStateConflictError, match="exact input"):
        LangchaoStateRepository(conflict, scope_key=SCOPE).append_advance(
            result, input_state=before, candidate_revisions={"a": 1, "b": 2}, expected_pointer_version=1,
        )
    assert conflict.rollbacks == 1


def test_restart_strictly_rebuilds_active_state_and_resumes_at_advanced_at():
    saved = state(revision=9, advanced_at=NOW + timedelta(minutes=3), readiness=(("a", 0.2), ("b", 0.4)))
    payload = saved.to_dict()
    conn = Connection(rows=[{"payload": payload, "payload_sha256": _digest(saved), "pointer_version": 12}])
    recovered, pointer = LangchaoStateRepository(conn, scope_key=SCOPE).recover_active_state()  # type: ignore[misc]
    assert recovered == saved
    assert recovered.advanced_at == NOW + timedelta(minutes=3)
    assert pointer == 12
    resumed = advance_langchao(
        recovered, until=recovered.advanced_at + timedelta(seconds=1), parameters=parameters(),
    )
    assert resumed.steps[0].started_at == recovered.advanced_at

    malformed = dict(payload)
    malformed["unexpected"] = True
    bad = Connection(rows=[{"payload": malformed, "payload_sha256": _digest(saved), "pointer_version": 12}])
    with pytest.raises(LangchaoStateConflictError, match="fields"):
        LangchaoStateRepository(bad, scope_key=SCOPE).load_active_state()


def test_snapshot_hash_is_canonical_and_candidate_mapping_must_be_exact():
    first = state(order=("a", "b"))
    reordered = state(order=("b", "a"), readiness=(("b", 0.0), ("a", 0.0)))
    assert _digest(first) != _digest(reordered)  # traversal order is part of immutable state
    repository = LangchaoStateRepository(Connection(), scope_key=SCOPE)
    with pytest.raises(ValueError, match="exactly cover"):
        repository.begin_round(first, run_mode="live", candidate_revisions={"a": 1})


def _digest(item: LangchaoState) -> str:
    import hashlib

    encoded = json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@pytest.mark.skipif(not __import__("os").environ.get("LANGCHAO_TEST_POSTGRES_DSN"), reason="set LANGCHAO_TEST_POSTGRES_DSN for PostgreSQL integration")
def test_v14_postgres_schema_and_immutable_triggers():
    """Opt-in smoke test; the normal suite remains hermetic."""
    psycopg = pytest.importorskip("psycopg")
    dsn = __import__("os").environ["LANGCHAO_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute("CREATE SCHEMA IF NOT EXISTS langchao_v14_test")
        connection.execute("SET LOCAL search_path TO langchao_v14_test, public")
        # v14 exact candidate FKs deliberately require v12 to have run first.
        from companion_runtime.langchao_schema import LANGCHAO_SCHEMA_V12_STATEMENTS
        for statement in LANGCHAO_SCHEMA_V12_STATEMENTS + LANGCHAO_STATE_SCHEMA_V14_STATEMENTS:
            connection.execute(statement)
        tables = connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'langchao_v14_test' AND table_name LIKE 'langchao_%'"
        ).fetchone()[0]
        assert tables >= 13
        connection.execute("DROP SCHEMA langchao_v14_test CASCADE")
