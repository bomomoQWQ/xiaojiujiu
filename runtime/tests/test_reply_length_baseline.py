"""Reply *length* is judged against the user's own habit (design §29).

Design §29 is one sentence with three clauses:

    回复速度、回复长度、对话持续长度，都应该相对用户自己的历史基线，而不是绝对阈值。

Speed got a per-user baseline (``reply_delay_baseline``); length did not, and was compared
against the hardcoded bars ``<= 4`` and ``>= 20`` instead:

* ``evidence_weight`` gave any reply of four characters or fewer
  ``min(implicit_weight, slow_reply_weight)`` - permanently weaker evidence;
* ``_target_rewards`` added ``-0.05`` to a four-character reply, i.e. read it as a step
  towards *negative* evidence.

So a user whose habit is three characters was judged cold for having a terse style, no
matter how consistently they answered, continued the topic and asked back. Measured before
the fix: identical behaviour scored 0.072 per observation at three characters versus 0.180
at thirty, and the learned ``positive_probability`` reached 0.619 versus 0.700
(``docs/BUSINESS_LOGIC_AUDIT.md`` §2, ``scripts/business_probes.py``).

These tests pin the *property* rather than the numbers: **two users with different but
consistent habits must be read the same way**, and a reply that is merely shorter than
usual may weaken a bonus but must never invert a reply's sign.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from companion_runtime.db import Database
from companion_runtime.projections import UserModelProjection
from companion_runtime.user_model import (
    REPLY_LENGTH_BASELINE_MIN_SAMPLES,
    UserInteractionModel,
    BehaviourReaction,
    compute_weight,
)

from conftest import BASE_TIME, build_config

ACTION = {"type": "contact", "proactive": True}
CONTEXT: dict = {}
TERSE = 3
MEDIUM = 8
VERBOSE = 30


def make_model(config=None) -> tuple[UserInteractionModel, Database]:
    """Build a model over an in-memory database."""
    db = Database(":memory:")
    db.migrate()
    return UserInteractionModel(UserModelProjection(db), config or build_config()), db


def learn(
    model: UserInteractionModel,
    db: Database,
    *,
    length: int,
    count: int,
    delay_seconds: float = 300.0,
    continued_topic: bool = True,
    asked_back: bool = True,
    turns: int = 0,
) -> None:
    """Fold ``count`` replies of one fixed length into the model."""
    stamp = BASE_TIME
    for _ in range(count):
        stamp += timedelta(hours=6)
        with db.transaction() as conn:
            model.observe(
                conn,
                action=ACTION,
                context=CONTEXT,
                reaction=BehaviourReaction(
                    replied=True,
                    reply_length=length,
                    reply_delay_seconds=delay_seconds,
                    continued_topic=continued_topic,
                    asked_back=asked_back,
                    turns=turns,
                ),
                now=stamp,
                observed_at=stamp,
            )


def weight_of(model: UserInteractionModel, *, length: int) -> float:
    """Return the evidence weight this model would give a reply of ``length``."""
    reaction = BehaviourReaction(
        replied=True, reply_length=length, reply_delay_seconds=300.0, continued_topic=True
    )
    return compute_weight(
        reaction,
        config=model._config.user_model,
        observed_at=BASE_TIME,
        now=BASE_TIME,
        short_reply_relative=model.short_reply_relative(reaction),
    ).total


def test_two_consistent_habits_are_read_the_same_way() -> None:
    """The property design §29 exists for: style must not decide the verdict.

    Both users always answer, continue the topic and ask back; only their habitual length
    differs. Their own baselines are learned separately, so after enough replies the two
    models must agree about their own user - which is exactly what the old absolute bar
    made impossible (0.072 vs 0.180 per observation).
    """
    terse, terse_db = make_model()
    verbose, verbose_db = make_model()
    try:
        learn(terse, terse_db, length=TERSE, count=12)
        learn(verbose, verbose_db, length=VERBOSE, count=12)

        assert weight_of(terse, length=TERSE) == pytest.approx(
            weight_of(verbose, length=VERBOSE)
        ), "a consistent terse user must not look like weaker evidence"

        terse_prediction = terse.predict(action=ACTION, context=CONTEXT)
        verbose_prediction = verbose.predict(action=ACTION, context=CONTEXT)
        assert terse_prediction.positive_probability == pytest.approx(
            verbose_prediction.positive_probability, abs=0.01
        )
        assert terse_prediction.reply_probability == pytest.approx(
            verbose_prediction.reply_probability, abs=0.01
        )
    finally:
        terse_db.close()
        verbose_db.close()


def test_the_baseline_is_per_user_not_global() -> None:
    """A length is only "short" or "long" relative to the person who wrote it.

    The same eight characters are long for a three-character user and short for a
    thirty-character one, and the models must disagree about it - otherwise this is an
    absolute bar wearing a baseline's clothes.
    """
    terse, terse_db = make_model()
    verbose, verbose_db = make_model()
    try:
        learn(terse, terse_db, length=TERSE, count=6)
        learn(verbose, verbose_db, length=VERBOSE, count=6)

        assert terse.relative_length_signal(MEDIUM) > 0, "8 chars is long for a 3-char user"
        assert verbose.relative_length_signal(MEDIUM) < 0, "8 chars is short for a 30-char user"
    finally:
        terse_db.close()
        verbose_db.close()


def test_a_shorter_than_habitual_reply_is_weaker_evidence() -> None:
    """Once the habit is known, a noticeably shorter reply counts for less.

    The signal is not "shorter at all" but "one log-space standard deviation shorter", so
    an ordinary one-word difference does not flip the weighting.
    """
    model, db = make_model()
    try:
        learn(model, db, length=VERBOSE, count=6)

        assert model.short_reply_relative(
            BehaviourReaction(replied=True, reply_length=TERSE)
        ) is True, "3 characters against a 30-character habit is short"
        assert model.short_reply_relative(
            BehaviourReaction(replied=True, reply_length=VERBOSE)
        ) is False, "their own habit is not short"
        assert weight_of(model, length=TERSE) < weight_of(model, length=VERBOSE)
    finally:
        db.close()


def test_a_shorter_reply_weakens_a_bonus_but_never_inverts_the_sign() -> None:
    """A terse reply must not be able to turn a reply into negative evidence.

    The old code subtracted a flat ``-0.05`` for ``<= 4`` characters, which on a reply with
    no other bonus produced a *negative* verdict about an interaction the user actually
    had. The relative term is capped at the bonus already earned, like the delay term.
    """
    model, db = make_model()
    try:
        learn(model, db, length=VERBOSE, count=6)
        # Replied, but nothing else positive: the two "no" branches cost -0.05 each.
        plain = BehaviourReaction(replied=True, reply_length=TERSE, reply_delay_seconds=300.0)

        targets = model._target_rewards(plain)

        assert targets["positive_probability"] >= 0.40, (
            "a shorter-than-usual reply may cancel a bonus, never invert the sign"
        )
    finally:
        db.close()


def test_before_the_baseline_is_trusted_length_contributes_nothing() -> None:
    """Cold start must not punish a style it has never seen (design §31).

    There is no defensible *absolute* reference length - unlike a reply delay, where
    "8 hours" is a meaningful prior - so the honest fallback is silence rather than a
    guess. This is also what makes the fix safe for an existing database: nothing about a
    user's verdict changes until their own habit has been measured.
    """
    model, db = make_model()
    try:
        assert model.reply_length_baseline_samples == 0
        assert model.length_reference()[2] is False, "an unmeasured habit is not trusted"

        reaction = BehaviourReaction(replied=True, reply_length=TERSE, reply_delay_seconds=300.0)
        assert model.short_reply_relative(reaction) is False
        assert model.relative_length_signal(TERSE) == 0.0
        assert model._relative_length_delta(reaction, 0.5) == 0.0

        # Two samples is still not a habit.
        learn(model, db, length=TERSE, count=REPLY_LENGTH_BASELINE_MIN_SAMPLES - 1)
        assert model.length_reference()[2] is False
        assert model.relative_length_signal(TERSE) == 0.0

        learn(model, db, length=TERSE, count=1)
        assert model.length_reference()[2] is True
        assert model.reply_length_baseline_samples >= REPLY_LENGTH_BASELINE_MIN_SAMPLES
    finally:
        db.close()


def test_the_baseline_survives_a_reload() -> None:
    """It is persisted beside the delay baseline, with no schema change.

    A habit that is forgotten on every restart would never be trusted in real use, where
    the process is restarted far more often than a user sends three replies.
    """
    db = Database(":memory:")
    db.migrate()
    config = build_config()
    try:
        first = UserInteractionModel(UserModelProjection(db), config)
        learn(first, db, length=VERBOSE, count=5)
        samples = first.reply_length_baseline_samples
        mean = first.reply_length_baseline_chars
        assert samples == 5 and mean is not None

        reloaded = UserInteractionModel(UserModelProjection(db), config)

        assert reloaded.reply_length_baseline_samples == samples
        assert reloaded.reply_length_baseline_chars == pytest.approx(mean)
        assert reloaded.length_reference()[2] is True
    finally:
        db.close()


def test_a_malformed_stored_baseline_is_discarded_not_fatal() -> None:
    """A corrupt row reads as cold start; it must not take the model down.

    The persisted block is JSON that a human or an older version may have written by hand.
    """
    db = Database(":memory:")
    db.migrate()
    projection = UserModelProjection(db)
    config = build_config()
    try:
        with db.transaction() as conn:
            projection.upsert_params(
                conn,
                params={"reply_length_baseline": {"samples": 5, "log_mean": "not a number"}},
                precision={},
                observations=3,
                effective_count=3.0,
            )
        model = UserInteractionModel(projection, config)
        assert model.reply_length_baseline_samples == 0
        assert model.length_reference()[2] is False
    finally:
        db.close()


# --------------------------------------------------------------------------------------
# §29's third clause: 对话持续长度
# --------------------------------------------------------------------------------------


def test_cold_start_keeps_the_bar_that_was_there_before() -> None:
    """An unmeasured habit must not change anyone's verdict.

    ``CONVERSATION_FALLBACK_TURNS`` is 3, which is exactly what the old
    ``min(3, turns) / 3`` implied - so this half of the fix is invisible until a user's own
    habit has been measured, and it cannot regress an existing database.
    """
    model, db = make_model()
    try:
        for turns, expected in ((1, 1 / 3), (2, 2 / 3), (3, 1.0), (10, 1.0)):
            assert model.conversation_bonus(
                BehaviourReaction(replied=True, turns=turns)
            ) == pytest.approx(expected), f"turns={turns}"
    finally:
        db.close()


def test_a_long_conversation_is_credited_relative_to_the_habit() -> None:
    """The saturation point follows the user, not the number three.

    This is the measured defect: ``min(3, turns)`` made a three-turn and a thirty-turn
    conversation score *identically* for every user, so the model could not tell "they
    stayed and talked for ages" from "they said three things" - and gave no extra credit to
    someone whose habit is one turn.
    """
    talkative, talkative_db = make_model()
    brief, brief_db = make_model()
    try:
        learn(talkative, talkative_db, length=VERBOSE, count=6, turns=10)
        learn(brief, brief_db, length=VERBOSE, count=6, turns=1)

        # For the talkative user, ten turns is the habit and three is short.
        assert talkative.conversation_bonus(
            BehaviourReaction(replied=True, turns=10)
        ) == pytest.approx(1.0, abs=0.02)
        assert talkative.conversation_bonus(BehaviourReaction(replied=True, turns=3)) < 0.9

        # For the brief user, three turns already exceeds the habit and is fully credited.
        assert brief.conversation_bonus(
            BehaviourReaction(replied=True, turns=3)
        ) == pytest.approx(1.0)
    finally:
        talkative_db.close()
        brief_db.close()


def test_the_target_no_longer_saturates_at_three_for_a_talkative_user() -> None:
    """The user-visible consequence, asserted on the target the model learns from."""
    model, db = make_model()
    try:
        learn(model, db, length=VERBOSE, count=6, turns=30)

        three = model._target_rewards(
            BehaviourReaction(replied=True, reply_length=VERBOSE, turns=3)
        )["continue_probability"]
        thirty = model._target_rewards(
            BehaviourReaction(replied=True, reply_length=VERBOSE, turns=30)
        )["continue_probability"]

        assert thirty > three, (
            "a thirty-turn conversation must score above a three-turn one for a user "
            "who habitually talks for thirty turns"
        )
    finally:
        db.close()


def test_the_turns_baseline_survives_a_reload() -> None:
    """Same persistence path as the other two baselines, no schema change."""
    db = Database(":memory:")
    db.migrate()
    config = build_config()
    try:
        first = UserInteractionModel(UserModelProjection(db), config)
        learn(first, db, length=VERBOSE, count=5, turns=8)
        assert first.reply_turns_baseline_samples == 5

        reloaded = UserInteractionModel(UserModelProjection(db), config)

        assert reloaded.reply_turns_baseline_samples == 5
        assert reloaded.reply_turns_baseline_turns == pytest.approx(
            first.reply_turns_baseline_turns
        )
        assert reloaded.turns_reference()[1] is True
    finally:
        db.close()


def test_an_unknown_conversation_length_earns_no_credit() -> None:
    """``turns=0`` means "not known", not "a zero-turn conversation"."""
    model, db = make_model()
    try:
        assert model.conversation_bonus(BehaviourReaction(replied=True, turns=0)) == 0.0

        learn(model, db, length=VERBOSE, count=5, turns=0)
        assert model.reply_turns_baseline_samples == 0, "an unknown length teaches nothing"
    finally:
        db.close()


def test_all_three_baselines_are_visible_to_the_operator() -> None:
    """§29's three signals must be inspectable, not just stored.

    ``numeric_view`` is the design's 数值视图 (§30) - the operator's answer to "why did it
    read that reply as cold?". An untrusted baseline contributes *nothing*, so whether each
    one is trusted is exactly the part that has to be visible. This assertion is also the
    one that caught the two new views being written but never wired into the view.
    """
    model, db = make_model()
    try:
        view = model.numeric_view()

        for key in ("reply_delay_baseline", "reply_length_baseline", "reply_turns_baseline"):
            assert key in view, f"{key} is not exposed"
            assert view[key]["samples"] == 0
            assert view[key]["trusted"] is False
        assert view["reply_length_baseline"]["min_samples"] == REPLY_LENGTH_BASELINE_MIN_SAMPLES

        learn(model, db, length=VERBOSE, count=5, turns=4)
        refreshed = model.numeric_view()
        assert refreshed["reply_length_baseline"]["trusted"] is True
        assert refreshed["reply_length_baseline"]["mean_chars"] == pytest.approx(VERBOSE)
        assert refreshed["reply_turns_baseline"]["trusted"] is True
        assert refreshed["reply_turns_baseline"]["mean_turns"] == pytest.approx(4.0)
    finally:
        db.close()
