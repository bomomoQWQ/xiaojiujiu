from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from companion_runtime.legacy_bridge_v2 import ConcreteLegacyRuntimeV2Bridge
from companion_runtime.runtime_repository_v2 import PostgresV2RuntimeRepository
from companion_runtime.typing import CandidateIntent
from companion_runtime.user_model_v2_types import Target

NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)


class Candidates:
    def __init__(self, item): self.item = item
    def list_active(self, limit): return [self.item]
    def get(self, candidate_id): return self.item if candidate_id == self.item.candidate_id else None


class Boundaries:
    def active(self, now): return []


def test_legacy_bridge_translates_only_mechanical_reply_and_candidate_pool():
    item = CandidateIntent(
        candidate_id="c1", type="follow_up", intent="问结果", goal="ask-result",
        sources=["unfinished:interview"], internal_need=.4, unfinished_relevance=.5,
    )
    runtime = SimpleNamespace(
        projections=SimpleNamespace(candidates=Candidates(item), boundaries=Boundaries()),
        config=SimpleNamespace(candidate=SimpleNamespace(max_active=12), conversation_id="default"),
        _event_ids_behind=lambda source: ["evt-source"],
    )
    bridge = ConcreteLegacyRuntimeV2Bridge(runtime)
    outcome = SimpleNamespace(
        event=SimpleNamespace(event_id="evt-reply", timestamp=NOW),
        duplicate=False, attributed_attempt_id="att-1", unfinished_resolved=[],
        # These legacy-model-shaped values must not become v2 explicit labels.
        observation_id="legacy-observation", appraisal_source="rule",
    )
    translated = bridge.after_user_event(event={}, legacy_outcome=outcome)
    assert [(o.target, o.candidate_exposure_ids) for o in translated.observations] == [
        (Target.REPLY, ("8c1a9eec-bdc3-562e-aaae-a3afe00045a7",))
    ]
    assert all(o.target not in {Target.ACCEPTANCE, Target.NEGATIVE} for o in translated.observations)
    candidate = bridge.candidates(scope_key="scope", now=NOW)[0]
    assert candidate.candidate_id == "c1"
    assert candidate.repeat_subject.concern_id == "interview"
    assert candidate.source_event_ids == ("evt-source",)


class Cursor:
    def __init__(self, rows=()): self.rows = rows
    def fetchall(self): return list(self.rows)
    def fetchone(self): return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self): self.calls = []; self.results = []
    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return Cursor(self.results.pop(0) if self.results else ())


class PredictionService:
    def predict(self, **kwargs):
        from companion_runtime.user_model_v2_types import SupportStatus, TargetPredictionV2
        def p(target):
            return TargetPredictionV2(
                prediction_id=target.value, scope_key="scope", target=target,
                point=.5, lower=.4, upper=.6, interval_level=.9,
                interval_kind="laplace", support=SupportStatus.INFORMATIVE,
                predicted_at=NOW, created_at=NOW, updated_at=NOW,
            )
        return SimpleNamespace(
            envelope_id="env-1", predictions=tuple(p(t) for t in Target),
            parameter_snapshot_ids=tuple((t, "params-1") for t in Target),
        )


class ServiceRepository:
    def get_prepared_exposure(self, **kwargs): return SimpleNamespace(key=kwargs["idempotency_key"])


def repository(connection):
    return PostgresV2RuntimeRepository(
        connection, prediction_service=PredictionService(),
        service_repository=ServiceRepository(), scope_key="scope",
        context_provider=lambda candidate, now: {
            "busy_probability": 0.0, "recent_contact_count": 0,
            "hours_since_contact": 2.0, "user_active_now": False,
            "ever_boundary": False, "novelty": .5, "explicit_permission": False,
        },
    )


def test_runtime_repository_reads_v2_history_and_writes_idempotently():
    connection = FakeConnection()
    connection.results = [[{
        "exposure_id": "e1", "acknowledged_at": NOW,
        "concern_id": "interview", "action_goal_id": "ask",
    }]]
    repo = repository(connection)
    history = repo.acknowledged_exposures(scope_key="scope", now=NOW)
    assert history[0].concern_id == "interview"
    from companion_runtime.repeat_v2 import UserMatterEventKind, UserMatterEventV2
    repo.append_user_matter_events(scope_key="scope", events=(UserMatterEventV2(
        event_id="u1", occurred_at_utc=NOW, kind=UserMatterEventKind.PROGRESS,
        concern_id="interview",
    ),))
    repo.save_decision_audit(decision_id="d1", audit={"events": []})
    assert any("ON CONFLICT (scope_key, event_id) DO NOTHING" in sql for sql, _ in connection.calls)
    assert any("runtime_v2_decision_audits" in sql for sql, _ in connection.calls)


def test_legacy_atomic_commit_rolls_back_when_snapshot_insert_fails():
    item = CandidateIntent(
        candidate_id="c1", type="share", intent="说句话", goal="contact",
        sources=[], internal_need=.4,
    )
    durable = {"claims": [], "attempts": [], "outbox": {}}

    class Transaction:
        def __enter__(self):
            self.snapshot = (list(durable["claims"]), list(durable["attempts"]), dict(durable["outbox"]))
            return object()
        def __exit__(self, exc_type, exc, tb):
            if exc_type is not None:
                durable["claims"][:] = self.snapshot[0]
                durable["attempts"][:] = self.snapshot[1]
                durable["outbox"].clear(); durable["outbox"].update(self.snapshot[2])
            return False

    class Outbox:
        def get(self, outbox_id): return durable["outbox"].get(outbox_id)
        def enqueue(self, connection, row): durable["outbox"][row.outbox_id] = row

    row = SimpleNamespace(outbox_id="o1", payload={"attempt_id": "a1"})
    runtime = SimpleNamespace(
        projections=SimpleNamespace(
            candidates=Candidates(item), outbox=Outbox(),
            runtime=SimpleNamespace(ensure=lambda: SimpleNamespace()),
        ),
        db=SimpleNamespace(transaction=lambda: Transaction()),
        write_session=lambda: Transaction(),
        _commit_attempt=lambda connection, chosen, state, now, attempt_id=None, outbox_id=None: (
            durable["attempts"].append(attempt_id) or durable["outbox"].update(
                {outbox_id: SimpleNamespace(outbox_id=outbox_id, payload={"attempt_id": attempt_id})}
            ) or (attempt_id, outbox_id)
        ),
    )
    class DispatchCoordinator:
        def create_live_dispatch_claim(self, **kwargs):
            durable["claims"].append(kwargs["claim_id"])
            return kwargs

    bridge = ConcreteLegacyRuntimeV2Bridge(
        runtime, scope_key="scope", dispatch_coordinator=DispatchCoordinator()
    )
    bridge._legacy_candidates["c1"] = item
    candidate = bridge._candidate(item)
    import pytest
    with pytest.raises(RuntimeError, match="snapshot insert failed"):
        bridge.commit_candidate_with_snapshot(
            decision_id="d1", candidate=candidate, now=NOW,
            persist_snapshot=lambda receipt: (_ for _ in ()).throw(
                RuntimeError("snapshot insert failed")
            ),
        )
    assert durable == {"claims": [], "attempts": [], "outbox": {}}


def test_langchao_in_transaction_bridge_does_not_open_transaction():
    from contextlib import contextmanager

    calls = {"transactions": 0, "write_sessions": 0}
    durable = {"claims": [], "attempts": [], "outbox": {}}

    class Candidates:
        def get(self, candidate_id): return item
    class Outbox:
        def get(self, outbox_id): return durable["outbox"].get(outbox_id)
        def enqueue(self, connection, row): durable["outbox"][row.outbox_id] = row
    class Coordinator:
        def create_live_dispatch_claim(self, **kwargs): durable["claims"].append("live")
        def create_dispatch_claim(self, **kwargs): durable["claims"].append("semantic")

    @contextmanager
    def forbidden_transaction():
        calls["transactions"] += 1
        raise AssertionError("inner transaction opened")
        yield
    @contextmanager
    def forbidden_write_session():
        calls["write_sessions"] += 1
        raise AssertionError("inner write session opened")
        yield

    item = CandidateIntent(
        candidate_id="c1", type="share", intent="说句话", goal="contact",
        sources=[], internal_need=.4,
    )
    runtime = SimpleNamespace(
        projections=SimpleNamespace(
            candidates=Candidates(), outbox=Outbox(),
            runtime=SimpleNamespace(ensure=lambda: SimpleNamespace()),
        ),
        db=SimpleNamespace(transaction=forbidden_transaction),
        write_session=forbidden_write_session,
        _commit_attempt=lambda connection, chosen, state, now, attempt_id=None, outbox_id=None: (
            durable["attempts"].append(attempt_id) or durable["outbox"].update(
                {outbox_id: SimpleNamespace(outbox_id=outbox_id, payload={})}
            ) or (attempt_id, outbox_id)
        ),
    )
    bridge = ConcreteLegacyRuntimeV2Bridge(
        runtime, scope_key="scope", dispatch_coordinator=Coordinator()
    )
    bridge._legacy_candidates["c1"] = item
    candidate = bridge._candidate(item)
    bridge.commit_langchao_candidate_in_transaction(
        object(), round_id="round", langchao_candidate_id="lang:c1",
        candidate_revision=1, candidate_version="v1", source_candidate=candidate,
        reward_contract_id="reward", reward_revision=1,
        now=NOW, persist_snapshot=lambda receipt: durable.setdefault("snapshot", receipt),
    )
    assert calls == {"transactions": 0, "write_sessions": 0}
    assert durable["claims"] == ["live", "semantic"]
    assert len(durable["attempts"]) == 1 and "snapshot" in durable


def test_runtime_repository_prediction_uses_v2_prediction_service():
    from companion_runtime.motivation_v2 import CandidatePolicyV2, UserUtilityCoefficientsV2
    from companion_runtime.repeat_v2 import RepeatSubjectV2
    from companion_runtime.runtime_v2 import CandidateV2
    repo = repository(FakeConnection())
    result = repo.prediction_for(scope_key="scope", candidate=CandidateV2(
        candidate_id="c1", action={"type": "share", "proactive": True},
        internal_utility=0, coefficients=UserUtilityCoefficientsV2(
            v_reply=1, v_continue=1, c_negative=1),
        repeat_subject=RepeatSubjectV2(), policy=CandidatePolicyV2(),
    ), now=NOW)
    assert result.snapshot_id == "env-1"
    assert result.reply.target is Target.REPLY
    assert result.negative.target is Target.NEGATIVE
