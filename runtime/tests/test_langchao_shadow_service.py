from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest

from companion_runtime.langchao_engine import LangchaoParameters
from companion_runtime.langchao_shadow import FakeLangchaoShadowRepository
from companion_runtime.langchao_shadow_service import (
    LangchaoShadowAuthorityError,
    LangchaoShadowCASConflictError,
    LangchaoShadowService,
)
from test_langchao_runtime_adapter import NOW, build, facts, source


class Aggregate:
    def __init__(self):
        self.events = []
        self.revisions = {"goal": {}, "reward": {}, "candidate": {}, "outcome": {}}
        self.active = {"goal": {}, "reward": {}, "candidate": {}, "outcome": {}}
        self.bindings = {}
        self.active_state = None
        self.pointer = 0
        self.rounds = {}
        self.audit_count = 0
        self.fail_on = None
        self.cas_loss = None
        self.claims = 0
        self.transaction_entries = 0

    @contextmanager
    def transaction(self):
        self.transaction_entries += 1
        snapshot = copy.deepcopy(self.__dict__)
        try:
            yield
        except BaseException:
            self.__dict__.clear()
            self.__dict__.update(snapshot)
            raise

    def _put(self, kind, identity, revision, dto):
        payload = dto.to_dict()
        key = (identity, revision)
        old = self.revisions[kind].get(key)
        if old is not None and old != payload:
            raise RuntimeError("revision conflict")
        if old is None:
            self.revisions[kind][key] = payload
            self.events.append(f"put:{kind}:{revision}")
        if self.fail_on == f"put:{kind}:{revision}":
            raise RuntimeError("injected failure")

    def _get(self, kind, identity):
        value = self.active[kind].get(identity)
        return None if value is None else {"revision": value[0], "pointer_version": value[1]}

    def _activate(self, kind, identity, revision, expected):
        current = self.active[kind].get(identity)
        actual = 0 if current is None else current[1]
        if self.cas_loss == (kind, identity):
            self.cas_loss = None
            # Simulate another writer; tests choose whether it published target.
            winner_revision = getattr(self, "cas_winner_revision", revision + 99)
            self.active[kind][identity] = (winner_revision, actual + 1)
            return False
        if actual != expected:
            return False
        self.active[kind][identity] = (revision, actual + 1)
        self.events.append(f"activate:{kind}:{revision}")
        return True


class Contracts:
    def __init__(self, aggregate): self.a = aggregate
    def put_goal_revision(self, dto): self.a._put("goal", dto.goal_id, dto.revision, dto)
    def put_reward_revision(self, dto): self.a._put("reward", dto.reward_contract_id, dto.revision, dto)
    def put_candidate_revision(self, dto): self.a._put("candidate", dto.candidate_id, dto.semantic_revision, dto)
    def get_active_goal(self, *, goal_id): return self.a._get("goal", goal_id)
    def get_active_reward(self, *, reward_contract_id): return self.a._get("reward", reward_contract_id)
    def get_active_candidate(self, *, candidate_id): return self.a._get("candidate", candidate_id)
    def activate_goal(self, *, goal_id, revision, expected_pointer_version):
        return self.a._activate("goal", goal_id, revision, expected_pointer_version)
    def activate_reward(self, *, reward_contract_id, revision, expected_pointer_version):
        return self.a._activate("reward", reward_contract_id, revision, expected_pointer_version)
    def activate_candidate(self, *, candidate_id, revision, expected_pointer_version):
        return self.a._activate("candidate", candidate_id, revision, expected_pointer_version)


class Outcomes:
    def __init__(self, aggregate): self.a = aggregate
    def put_outcome_revision(self, dto, *, revision, reward_contract_id, reward_contract_revision):
        assert dto.settlement_type.value == "expected"
        assert reward_contract_revision == 1
        self.a._put("outcome", dto.token_id, revision, dto)
    def get_active_outcome(self, *, token_id): return self.a._get("outcome", token_id)
    def activate_outcome(self, *, token_id, revision, expected_pointer_version):
        return self.a._activate("outcome", token_id, revision, expected_pointer_version)
    def bind_reward_outcomes(self, *, reward_contract_id, reward_contract_revision, outcome_revisions):
        key = (reward_contract_id, reward_contract_revision)
        value = tuple(outcome_revisions)
        old = self.a.bindings.get(key)
        if old is not None and old != value: raise RuntimeError("binding conflict")
        if old is None:
            self.a.bindings[key] = value
            self.a.events.append("bind:expected")


class States:
    def __init__(self, aggregate): self.a = aggregate
    def load_active_state(self):
        return None if self.a.active_state is None else (self.a.active_state, self.a.pointer)
    def get_round_status(self, *, round_id): return self.a.rounds.get(round_id)
    def begin_round(self, state, *, run_mode, candidate_revisions, expected_pointer_version, authority_revision):
        assert run_mode == "shadow" and set(candidate_revisions) == set(state.working_set)
        if self.a.pointer != expected_pointer_version: raise RuntimeError("state CAS")
        self.a.pointer += 1
        self.a.active_state = state
        self.a.rounds[state.decision_round_id] = "open"
        self.a.events.append("begin:shadow")
        return self.a.pointer
    def abort_open_round(self, *, round_id, ended_at, expected_pointer_version):
        assert self.a.pointer == expected_pointer_version and self.a.rounds[round_id] == "open"
        self.a.rounds[round_id] = "aborted"
        self.a.events.append("abort:open")
        return True


class Shadow(FakeLangchaoShadowRepository):
    def __init__(self, aggregate):
        super().__init__()
        self.a = aggregate
    def put_shadow_run(self, run):
        super().put_shadow_run(run)
        self.a.audit_count += 1
        self.a.events.append("audit")
    def put_state_revision(self, **kwargs):
        super().put_state_revision(**kwargs)
        self.last_input_state = kwargs["input_state"]
        self.last_expected_pointer = kwargs["expected_pointer_version"]
        result = kwargs["result"]
        self.a.pointer += 1
        self.a.active_state = result.state
        self.a.rounds[result.state.decision_round_id] = (
            "decided" if result.decision_candidate_id else "deferred" if result.defer_reason else "open"
        )
        self.a.events.append("advance")


class Authority:
    def __init__(self, engine="runtime_v2", mode="live", may_dispatch=True):
        self.row = {"engine_key": engine, "mode": mode, "may_dispatch": may_dispatch, "revision": 7}
    def get_active(self): return self.row


class ForbiddenAuthority(Authority):
    def create_dispatch_claim(self, **kwargs):
        raise AssertionError("service must never create a claim")


def parameters():
    return LangchaoParameters(leak=.2, competition_gain=0, decision_threshold=.99,
        time_scale_seconds=10, max_step_seconds=.5, crossing_tolerance=1e-7, tie_tolerance=1e-6)


def harness(*, authority=None):
    aggregate = Aggregate()
    shadow = Shadow(aggregate)
    service = LangchaoShadowService(contract_repository=Contracts(aggregate),
        outcome_repository=Outcomes(aggregate), state_repository=States(aggregate),
        shadow_repository=shadow, authority_reader=authority or ForbiddenAuthority(),
        transaction_factory=aggregate.transaction)
    return aggregate, shadow, service


def execute(service, built, *, key="idem:1", run_id="run:1", baseline="legacy:c"):
    return service.run(built, now=NOW + timedelta(seconds=1), parameters=parameters(),
        decision_budget=1, run_id=run_id, idempotency_key=key,
        baseline_candidate_id=baseline, baseline_defer_reason="legacy_defer")


def test_order_contract_stability_expected_tokens_zero_send_and_baseline_comparison():
    built = build((source("a"),))
    aggregate, _, service = harness()
    result = execute(service, built)
    assert aggregate.events[:8] == [
        "put:goal:1", "activate:goal:1", "put:reward:1", "activate:reward:1",
        "put:goal:2", "activate:goal:2", "put:candidate:1", "activate:candidate:1",
    ]
    assert aggregate.events.index("bind:expected") < aggregate.events.index("begin:shadow")
    assert result.comparison.baseline_candidate_id == "legacy:c"
    assert result.sent_count == result.reward_count == result.training_count == result.quota_count == 0
    assert result.outbox_id is None and aggregate.claims == 0
    item = built.contracts[0]
    assert item.initial_goal.revision == 1 and item.goal.revision == 2
    assert item.initial_goal.goal_id == item.goal.goal_id
    assert set(aggregate.bindings[(item.reward.reward_contract_id, 1)]) == {
        (token.token_id, 1) for token in item.reward.outcome_tokens
    }


def test_new_round_appends_from_exact_built_snapshot_and_begin_pointer():
    built = build((source("a"),))
    aggregate, shadow, service = harness()

    execute(service, built)

    assert shadow.last_input_state is built.state
    assert shadow.last_expected_pointer == 1
    assert aggregate.transaction_entries == 1


def test_exact_service_replay_adds_no_revision_state_or_audit():
    built = build((source("a"),))
    aggregate, shadow, service = harness()
    first = execute(service, built)
    counts = (copy.deepcopy(aggregate.revisions), aggregate.pointer, aggregate.audit_count, list(aggregate.events))
    second = execute(service, built)
    assert second == first
    assert (aggregate.revisions, aggregate.pointer, aggregate.audit_count, aggregate.events) == counts
    assert len(shadow.runs) == 1


def test_transaction_rolls_back_all_contract_work_on_failure():
    built = build((source("a"),))
    aggregate, _, service = harness()
    aggregate.fail_on = "put:candidate:1"
    with pytest.raises(RuntimeError, match="injected"):
        execute(service, built)
    assert not any(aggregate.revisions.values())
    assert aggregate.active_state is None and aggregate.audit_count == 0 and aggregate.events == []


def test_authority_allows_only_runtime_live_or_langchao_shadow_and_never_claims():
    built = build((source("a"),))
    execute(harness(authority=Authority("langchao", "shadow", False))[2], built)
    for authority in (Authority("langchao", "shadow", True), Authority("langchao", "live", True), Authority("runtime_v2", "shadow", False)):
        with pytest.raises(LangchaoShadowAuthorityError):
            execute(harness(authority=authority)[2], built)


def test_open_round_resumes_compatible_state_but_changed_working_set_aborts_open():
    first = build((source("a"),))
    aggregate, _, service = harness()
    execute(service, first)
    aggregate.rounds[aggregate.active_state.decision_round_id] = "open"
    prior_pointer = aggregate.pointer
    execute(service, first, key="idem:2", run_id="run:2")
    assert "abort:open" not in aggregate.events
    assert aggregate.pointer == prior_pointer + 1

    changed = build((source("b"),), (facts("b", "followup.v1"),))
    aggregate.rounds[aggregate.active_state.decision_round_id] = "open"
    execute(service, changed, key="idem:3", run_id="run:3")
    abort_index = max(i for i, event in enumerate(aggregate.events) if event == "abort:open")
    assert aggregate.events[abort_index + 1] == "begin:shadow"


def test_compatible_closed_round_fails_instead_of_appending_again():
    built = build((source("a"),))
    aggregate, _, service = harness()
    execute(service, built)
    aggregate.rounds[aggregate.active_state.decision_round_id] = "deferred"
    with pytest.raises(Exception, match="already closed"):
        execute(service, built, key="idem:2", run_id="run:2")


def test_changed_working_set_does_not_abort_terminal_round():
    first = build((source("a"),))
    aggregate, _, service = harness()
    execute(service, first)
    aggregate.rounds[aggregate.active_state.decision_round_id] = "deferred"
    before = aggregate.events.count("abort:open")
    execute(service, build((source("b"),)), key="idem:2", run_id="run:2")
    assert aggregate.events.count("abort:open") == before


def test_contract_cas_rebuild_once_reuses_exact_winner_or_fails_without_retry():
    built = build((source("a"),))
    item = built.contracts[0]
    aggregate, _, service = harness()
    aggregate.cas_loss = ("goal", item.goal.goal_id)
    aggregate.cas_winner_revision = 1
    execute(service, built)  # exact winner is accepted

    aggregate, _, service = harness()
    aggregate.cas_loss = ("goal", item.goal.goal_id)
    aggregate.cas_winner_revision = 99
    with pytest.raises(LangchaoShadowCASConflictError):
        execute(service, built)
    assert aggregate.events == []  # outer transaction rolled back; no blind retry


def test_adapter_contract_payload_is_stable_for_identical_build():
    one = build((source("a"),))
    two = build((source("a"),))
    assert [item.initial_goal.to_dict() for item in one.contracts] == [item.initial_goal.to_dict() for item in two.contracts]
    assert [item.goal.to_dict() for item in one.contracts] == [item.goal.to_dict() for item in two.contracts]
    assert [item.reward.to_dict() for item in one.contracts] == [item.reward.to_dict() for item in two.contracts]
    assert [item.candidate.to_dict() for item in one.contracts] == [item.candidate.to_dict() for item in two.contracts]


def test_service_source_has_no_cli_scheduler_send_claim_or_wave_surface():
    import companion_runtime.langchao_shadow_service as module
    from pathlib import Path
    text = Path(module.__file__).read_text(encoding="utf-8")
    assert "create_dispatch_claim(" not in text
    assert "send(" not in text and "Wave" not in text and "wave" not in text
