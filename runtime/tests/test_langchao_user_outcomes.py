from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from companion_runtime.langchao_user_outcomes import LangchaoUserOutcomeSettler
from companion_runtime.langchao_types import MotivationDirection, OutcomeStatus, OutcomeToken, SettlementType
from companion_runtime.user_model_v2_types import LabelStatus, Target, TargetLabelV2

NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)


def expected(key: str, amount: float = 1.0) -> OutcomeToken:
    return OutcomeToken(
        token_id=f"expected:{key}", scope_key="scope", goal_id="goal", episode_id="episode",
        outcome_key=key, settlement_type=SettlementType.EXPECTED,
        status=OutcomeStatus.UNEXECUTED, base_amount=amount,
        direction_weights=((MotivationDirection.APPROACH, 1.0),),
        evidence_version="expected.v1", idempotency_key=f"expected:{key}",
    )


def label(target: Target, status: LabelStatus, value: bool | None, *, revision: int = 2) -> TargetLabelV2:
    return TargetLabelV2(
        label_id=f"label:{target.value}:{revision}", exposure_id="exp", scope_key="scope",
        target=target, status=status, value=value,
        observed_at=NOW + timedelta(minutes=1) if value is not None else None,
        window_started_at=NOW, window_ends_at=NOW + timedelta(hours=1), horizon_seconds=3600,
        created_at=NOW, updated_at=NOW + timedelta(hours=1), source_event_ids=("event",),
    )


class Outcomes:
    def __init__(self):
        self.active = None
        self.writes = []

    def get_active_observation(self, **_kw): return self.active
    def put_outcome_revision(self, token, **kw): self.writes.append((token, kw))
    def activate_observation(self, **kw):
        self.active = {"token_id": kw["token_id"], "revision": kw["revision"],
                       "pointer_version": kw["expected_pointer_version"] + 1,
                       "source_label_revision": kw["source_label_revision"]}
        return True


class Live:
    def __init__(self, scope="scope"):
        self.outcomes = Outcomes(); self.scope = scope; self.revision = 2
        self.commit = SimpleNamespace(
            scope_key=scope, reward_contract_id="reward", reward_revision=1,
            expected_tokens=(expected("reply"), expected("continuation"), expected("negative", -1.0)),
        )
    def get_by_attempt(self, exposure_id): return self.commit if exposure_id == "exp" else None
    def label_revision(self, **_kw): return self.revision


def test_success_reply_does_not_imply_continuation_and_duplicate_is_exactly_once():
    live = Live(); settle = LangchaoUserOutcomeSettler(live)
    reply = label(Target.REPLY, LabelStatus.OBSERVED_POSITIVE, True)
    pending_continue = label(Target.CONTINUE, LabelStatus.PENDING, None)
    assert [x.outcome_key for x in settle.settle_labels((reply, pending_continue))] == ["reply"]
    assert settle.settle_labels((reply,)) == ()
    assert len(live.outcomes.writes) == 1


def test_no_reply_window_and_explicit_negative_have_distinct_facts():
    live = Live(); settle = LangchaoUserOutcomeSettler(live)
    no_reply = label(Target.REPLY, LabelStatus.OBSERVED_NEGATIVE, False)
    actual = settle.settle_labels((no_reply,))[0]
    assert actual.status is OutcomeStatus.NOT_OBSERVED and actual.base_amount == 0

    live = Live(); negative = label(Target.NEGATIVE, LabelStatus.OBSERVED_POSITIVE, True)
    actual = LangchaoUserOutcomeSettler(live).settle_labels((negative,))[0]
    assert actual.status is OutcomeStatus.CONFIRMED and actual.base_amount == -1.0


def test_late_revision_creates_correction_lineage_and_restart_deduplicates():
    live = Live(); settle = LangchaoUserOutcomeSettler(live)
    first = label(Target.REPLY, LabelStatus.OBSERVED_NEGATIVE, False, revision=2)
    settle.settle_labels((first,))
    corrected_id = live.outcomes.active["token_id"]
    live.revision = 3
    late = label(Target.REPLY, LabelStatus.OBSERVED_POSITIVE, True, revision=3)
    correction = LangchaoUserOutcomeSettler(live).settle_labels((late,))[0]
    assert correction.settlement_type is SettlementType.CORRECTION
    assert correction.corrects_token_id == corrected_id
    assert LangchaoUserOutcomeSettler(live).settle_labels((late,)) == ()


def test_cross_scope_or_unknown_attempt_never_settles():
    live = Live(scope="other")
    assert LangchaoUserOutcomeSettler(live).settle_labels((
        label(Target.REPLY, LabelStatus.OBSERVED_POSITIVE, True),
    )) == ()
