"""Regression tests for the confirmed persistent-cognition defects.

Each class below pins one defect. They are written from the failure side - the
interesting assertion is usually that something did *not* happen - because every one
of these bugs produced plausible-looking state rather than an exception:

1. ``SemanticConfig.provider`` was declared, documented, and read by nothing, so a
   configured provider silently never existed;
2. deep-refresh pacing was anchored to the last *tick*, so a heartbeat loop reset the
   clock every round and the minimum interval could never fire;
3. ordinary "走了" ("I'm leaving") settled as a bereavement, and a negated anchor
   ("我没觉得我喜欢你") settled as a declaration;
4. an unfinished matter was resolved by any completion phrase, so "到家了" closed an
   interview the user had not heard back from - and then re-opened a trip matter from
   the same words;
5. memory deduplication merged distinct facts that merely shared a source event, and
   archived memories were still injected into the prompt;
6. user-model uncertainty was derived from the length of a parameter vector, i.e. from
   the number of features, so it never depended on evidence at all;
7. the explanation cache key omitted the dominant event's identity, so a new feeling of
   the same magnitude was served the old feeling's prose.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Mapping

import pytest

from companion_runtime import memory as memory_module
from companion_runtime import unfinished as unfinished_module
from companion_runtime.config import RuntimeConfig, SemanticConfig
from companion_runtime.context import select_memories
from companion_runtime.db import Database
from companion_runtime.deep_refresh import evaluate_triggers
from companion_runtime.emotion import EmotionExplainer
from companion_runtime.eventlog import EventQuery
from companion_runtime.memory import MemoryStore, RetrievalCue
from companion_runtime.projections import (
    EmotionProjection,
    MemoryProjection,
    Projections,
    UserModelProjection,
)
from companion_runtime.providers import (
    DeepRefreshSuggestions,
    DisabledProvider,
    RemoteAPIProvider,
    build_provider,
    resolve_provider_name,
)
from companion_runtime.runtime import LAST_DEEP_REFRESH_META_KEY, Runtime
from companion_runtime.semantic import classify_event
from companion_runtime.typing import (
    Actor,
    ActivatedMemory,
    EmotionEvent,
    EventType,
    Memory,
    MemoryCandidate,
    MemoryKind,
    MemoryStatus,
    RawEvent,
    RuntimeState,
    UnfinishedMatter,
    new_id,
)
from companion_runtime.user_model import (
    BEHAVIOUR_CLASSES,
    BehaviourReaction,
    UserInteractionModel,
)

from conftest import BASE_TIME, build_config


def _event(content: str, *, event_id: str = "evt_1") -> RawEvent:
    """Build a user-message event for unit tests."""
    return RawEvent(
        event_id=event_id,
        event_type=EventType.USER_MESSAGE.value,
        timestamp=BASE_TIME,
        actor=Actor.USER.value,
        conversation_id="c1",
        content=content,
    )


def _runtime(**semantic: Any) -> Runtime:
    """Build an in-memory Runtime on the simulated timeline."""
    config = build_config()
    config.semantic.unresolved_backlog_threshold = 1
    for key, value in semantic.items():
        setattr(config.semantic, key, value)
    return Runtime(config=config, created_at=BASE_TIME)


def _grounded_reinterpretation(event_id: str) -> DeepRefreshSuggestions:
    """Return a bundle that survives grounding and therefore applies.

    A refresh only reaches ``ran=True`` when the provider's suggestions name an
    entity that resolves - an empty or ungrounded bundle ends as
    ``empty_suggestions`` / ``all_suggestions_ungrounded`` with ``ran=False``. Every
    pacing test below therefore feeds this bundle, so ``ran=True`` means "the
    refresh actually happened" and the pacing assertion that follows is meaningful.
    """
    return DeepRefreshSuggestions(
        degraded=False,
        reinterpretations=[{"content": "那是失望。", "sources": [event_id]}],
    )


def _proposal_source_ids(runtime: Runtime) -> set[str]:
    """Return every source identifier claimed by a recorded proposal event.

    Proposal decisions are appended as ``EventType.SYSTEM`` records whose content is
    ``proposal:<task_type>`` (see ``Reducer._record_proposal``); there is no
    dedicated ``EventType.PROPOSAL`` member to query on.
    """
    events = runtime.events.read(
        EventQuery(event_types=[EventType.SYSTEM.value], limit=200, newest_first=True)
    )
    claimed: set[str] = set()
    for event in events:
        if not str(event.content or "").startswith("proposal:"):
            continue
        claimed.update(str(item) for item in (event.source_event_ids or []))
    return claimed


class _CountingProvider:
    """Provider double that returns a fixed bundle and counts its calls."""

    name = "counting"

    def __init__(self, suggestions: Any = None, *, available: bool = True) -> None:
        self.suggestions = suggestions
        self._available = available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def deep_refresh(self, request: Any, *, timeout_s: float | None = None) -> Any:
        self.calls += 1
        return self.suggestions

    def explain_state(self, payload: Mapping[str, Any], *, state_key: str = "") -> Any:
        return None

    def health(self) -> dict[str, Any]:
        return {"provider": self.name, "available": self._available}


# --------------------------------------------------------------------------------------
# 1. SemanticConfig.provider is honoured
# --------------------------------------------------------------------------------------


class TestSemanticConfigProviderIsHonoured:
    """A declared setting must reach the factory, or it is a lie."""

    def test_the_config_field_names_the_provider(self) -> None:
        assert resolve_provider_name(SemanticConfig(provider="remote_api"), env={}) == (
            "remote_api"
        )

    def test_build_provider_reads_the_semantic_section(self) -> None:
        config = RuntimeConfig()
        config.semantic.provider = "remote_api"
        assert isinstance(build_provider(config, env={}), RemoteAPIProvider)

    def test_a_retired_name_in_the_semantic_section_falls_back(self) -> None:
        config = RuntimeConfig()
        config.semantic.provider = "local_cpu"
        assert isinstance(build_provider(config, env={}), DisabledProvider)

    def test_an_explicit_top_level_name_still_wins(self) -> None:
        """The older, more specific key keeps its documented precedence."""
        assert resolve_provider_name({"semantic_provider": "disabled"}, env={}) == "disabled"

    def test_a_malformed_semantic_section_never_raises(self) -> None:
        """``resolve_provider_name`` promises to answer, not to explode."""
        assert resolve_provider_name({"semantic": "not-a-mapping"}, env={}) == "disabled"
        assert resolve_provider_name(None, {}) == "disabled"

    def test_the_name_is_normalised(self) -> None:
        assert resolve_provider_name(SemanticConfig(provider="  Remote_API "), env={}) == (
            "remote_api"
        )

    def test_a_settings_read_that_raises_still_falls_back(self) -> None:
        """A broken setting is a construction failure, not a half-built provider.

        The name is readable, so the factory proceeds to build the remote route -
        and building it touches an endpoint setting that raises. The factory's
        contract is that every unsuccessful construction ends in the one provider
        that always works, so the operator gets a working daemon plus a warning
        instead of a remote client pointed at a value nobody could read.
        """

        class ExplodingConfig:
            """A config whose endpoint lookup raises."""

            extras: dict[str, Any] = {}

            @property
            def semantic_provider(self) -> str:
                return "remote_api"

            @property
            def semantic_base_url(self) -> str:
                raise RuntimeError("endpoint lookup exploded")

        assert isinstance(build_provider(ExplodingConfig(), env={}), DisabledProvider)


# --------------------------------------------------------------------------------------
# 2. Deep-refresh pacing and provenance
# --------------------------------------------------------------------------------------


class TestDeepRefreshPacingIsReal:
    """Pacing must key off a real refresh timestamp, not off the latest tick."""

    def test_the_attempt_timestamp_is_persisted(self) -> None:
        """The recorded time is the refresh's own clock, not the wall clock."""
        runtime = _runtime(deep_refresh_min_interval_seconds=0.0)
        runtime.semantic_provider = _CountingProvider()
        attempt_at = BASE_TIME + timedelta(minutes=5)
        try:
            event = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            runtime.semantic_provider.suggestions = _grounded_reinterpretation(
                event.event.event_id
            )
            outcome = runtime.deep_refresh(now=attempt_at, force=True)
            assert outcome.ran is True, (
                f"the probe refresh must apply ('ran' is only True for a grounded "
                f"bundle); got reason={outcome.reason!r}"
            )
            stored = runtime.state().meta.get(LAST_DEEP_REFRESH_META_KEY)
            assert stored == attempt_at.isoformat(), "the attempt was not recorded durably"
        finally:
            runtime.close()

    def test_ticks_do_not_reset_the_interval(self) -> None:
        """A heartbeat loop must not be able to refresh on every round."""
        runtime = _runtime(deep_refresh_min_interval_seconds=3600.0)
        provider = _CountingProvider()
        runtime.semantic_provider = provider
        first = BASE_TIME + timedelta(minutes=1)
        try:
            event = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            provider.suggestions = _grounded_reinterpretation(event.event.event_id)
            # First refresh: a grounded bundle, so it applies and the attempt is
            # recorded - that recorded time is what the next call must respect.
            first_outcome = runtime.deep_refresh(now=first, force=True)
            assert first_outcome.ran is True, first_outcome.reason
            assert provider.calls == 1
            # Several rounds, each advancing the tick clock. With the elapsed time
            # anchored to the last tick, every one of these would have looked "idle"
            # and the minimum interval could never have held.
            for index in range(5):
                runtime.lazy_tick(first + timedelta(minutes=index))
            outcome = runtime.deep_refresh(now=first + timedelta(minutes=6))
            assert outcome.ran is False
            assert outcome.reason == "min_interval_not_elapsed"
            assert provider.calls == 1
        finally:
            runtime.close()

    def test_the_interval_survives_a_restart(self, tmp_path: Any) -> None:
        """A restart must not forget the request it already paid for."""
        config = build_config()
        config.storage.database_path = str(tmp_path / "runtime.sqlite3")
        config.semantic.deep_refresh_min_interval_seconds = 3600.0
        config.semantic.unresolved_backlog_threshold = 1
        provider = _CountingProvider()
        attempt_at = BASE_TIME + timedelta(minutes=1)
        event_id = ""

        runtime = Runtime(config=config, created_at=BASE_TIME)
        runtime.semantic_provider = provider
        try:
            event = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            event_id = event.event.event_id
            provider.suggestions = _grounded_reinterpretation(event_id)
            first_outcome = runtime.deep_refresh(now=attempt_at, force=True)
            assert first_outcome.ran is True, (
                f"the first session must actually refresh before a restart can "
                f"forget it; got reason={first_outcome.reason!r}"
            )
        finally:
            runtime.close()
        assert provider.calls == 1

        # A second Runtime over the same database: same config, same provider
        # double. Nothing is carried over in process memory except the provider.
        recovered = Runtime(config=config, created_at=BASE_TIME)
        recovered.semantic_provider = provider
        try:
            outcome = recovered.deep_refresh(now=attempt_at + timedelta(minutes=10))
            assert outcome.ran is False
            assert outcome.reason == "min_interval_not_elapsed", (
                "the recovered Runtime re-spent because the attempt timestamp was "
                "process-local rather than persisted in runtime state"
            )
            assert provider.calls == 1
        finally:
            recovered.close()

    def test_zero_hours_is_a_measurement_not_a_sentinel(self) -> None:
        """``0.0`` means "just refreshed"; "never" is expressed as ``None``.

        Both calls use the same config (backlog threshold 1, minimum interval 1h) and
        the same backlog size, so the only difference between the two outcomes is the
        elapsed-time argument. That is what makes the pair a statement about the
        sentinel rather than about the configuration.
        """
        pacing = SemanticConfig(
            unresolved_backlog_threshold=1, deep_refresh_min_interval_seconds=3600.0
        )
        # 0.0 h is a real measurement ("just now"): the interval has not elapsed, so
        # cost control refuses even though the backlog is over threshold.
        recently = evaluate_triggers(
            unresolved_count=5, hours_since_last_refresh=0.0, config=pacing
        )
        assert recently.should_refresh is False
        assert recently.reason == "min_interval_not_elapsed"
        # ``None`` is "never refreshed": there is no interval to enforce, so the
        # backlog rule is free to fire.
        never = evaluate_triggers(
            unresolved_count=5,
            hours_since_last_refresh=None,
            has_previous_refresh=False,
            config=pacing,
        )
        assert never.should_refresh is True, never.reason
        assert never.reason == "unresolved_backlog"

    def test_a_negative_interval_does_not_stall_the_rule(self) -> None:
        """A nonsense value must be ignored rather than read as "just refreshed"."""
        trigger = evaluate_triggers(
            unresolved_count=5,
            hours_since_last_refresh=-3.0,
            has_previous_refresh=True,
            config=SemanticConfig(
                unresolved_backlog_threshold=1, deep_refresh_min_interval_seconds=3600.0
            ),
        )
        assert trigger.should_refresh is True

    def test_an_empty_runtime_is_never_idle_enough(self) -> None:
        """Idle is not a reason to spend when there is nothing to understand."""
        runtime = _runtime()
        try:
            signals = runtime._refresh_signals(now=BASE_TIME + timedelta(days=3))
            assert signals["has_material"] is False
            trigger = evaluate_triggers(unresolved_count=0, **signals)
            assert trigger.reason != "idle_refresh"
            assert trigger.should_refresh is False
        finally:
            runtime.close()

    def test_an_empty_runtime_spends_nothing_end_to_end(self) -> None:
        """The same guarantee on the real heartbeat, which is where it is reached.

        A Runtime that has never been spoken to has been "idle" since its creation
        epoch, so an epoch-anchored idle rule would spend a request on the very
        first heartbeat of a brand-new deployment.
        """
        runtime = _runtime(deep_refresh_min_interval_seconds=0.0)
        # Empty is not the same as ungrounded: with nothing to understand the right
        # answer is "not_needed", and the provider is never even called. (A provider
        # that *was* called and answered nothing would report "empty_suggestions"
        # with ran=False and one call counted.)
        provider = _CountingProvider(DeepRefreshSuggestions(degraded=False))
        runtime.semantic_provider = provider
        try:
            outcome = runtime.endogenous_round(
                now=BASE_TIME + timedelta(hours=48), force=True
            )
            assert outcome.deep_refresh["ran"] is False
            assert outcome.deep_refresh["reason"] == "not_needed"
            assert provider.calls == 0
        finally:
            runtime.close()


class TestMalformedProviderSuggestions:
    """A provider is third-party code; a strange reply must degrade, not raise."""

    @pytest.mark.parametrize(
        "answer",
        [
            "not-a-suggestion-set",
            [{"summary": "x", "sources": ["evt_1"]}],
            {"unexpected": "shape"},
            42,
            object(),
        ],
        ids=["string", "list", "unknown-mapping", "int", "object"],
    )
    def test_a_malformed_reply_is_reported_as_absent(self, answer: Any) -> None:
        runtime = _runtime()
        runtime.semantic_provider = _CountingProvider(answer)
        try:
            runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            outcome = runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=1), force=True)
            assert outcome.ran is False
            assert outcome.reason in {"no_suggestions", "empty_suggestions"}
            # The evidence is untouched: a strange answer is not an understanding.
            assert runtime.projections.semantics.unresolved_count() == 1
        finally:
            runtime.close()

    def test_a_mapping_reply_is_parsed_through_the_wire_parser(self) -> None:
        runtime = _runtime()
        try:
            event = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            runtime.semantic_provider = _CountingProvider(
                {
                    "reinterpretations": [
                        {"content": "那是失望。", "sources": [event.event.event_id]}
                    ]
                }
            )
            outcome = runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=1), force=True)
            assert outcome.ran is True
            assert outcome.applied.get("reinterpretation") == 1
            assert outcome.provider == "counting"
        finally:
            runtime.close()


class TestRefreshProvenanceIsPerOperation:
    """A proposal claims only what its operations actually referenced."""

    def test_the_proposal_does_not_claim_the_whole_backlog(self) -> None:
        runtime = _runtime()
        try:
            first = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            unrelated = runtime.process_user_message(
                content="随便吧，都行。", timestamp=BASE_TIME + timedelta(minutes=1)
            )
            runtime.semantic_provider = _CountingProvider(
                DeepRefreshSuggestions(
                    degraded=False,
                    reinterpretations=[
                        {"content": "那是失望。", "sources": [first.event.event_id]}
                    ],
                )
            )
            runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=5), force=True)

            claimed = _proposal_source_ids(runtime)
            assert first.event.event_id in claimed
            assert unrelated.event.event_id not in claimed, (
                "the refresh claimed an event that no operation referred to"
            )
            assert runtime.projections.semantics.unresolved_count() == 1
        finally:
            runtime.close()

    def test_an_operation_citing_a_non_event_source_claims_only_that(self) -> None:
        """Provenance is the union of the operation sources, whatever they name."""
        runtime = _runtime()
        try:
            first = runtime.process_user_message(content="算了，也没什么。", timestamp=BASE_TIME)
            candidate = MemoryCandidate(
                candidate_id=new_id("memory_candidate"),
                summary="用户提到过面试",
                value=0.9,
                source_event_ids=[first.event.event_id],
            )
            with runtime.db.transaction() as conn:
                runtime.projections.memory.upsert_candidate(conn, candidate)
            runtime.semantic_provider = _CountingProvider(
                DeepRefreshSuggestions(
                    degraded=False,
                    memory_suggestions=[
                        {
                            "summary": "他其实很在意这次面试",
                            "sources": [candidate.candidate_id],
                        }
                    ],
                )
            )
            outcome = runtime.deep_refresh(now=BASE_TIME + timedelta(minutes=5), force=True)
            assert outcome.ran is True
            assert outcome.applied.get("memory") == 1
            assert _proposal_source_ids(runtime) == {candidate.candidate_id}
        finally:
            runtime.close()


class TestRefreshPatternContractIsCompatible:
    """A shared rule table is a cross-module contract, not a private detail.

    ``unfinished.RESOLUTION_PATTERNS`` is consumed by :func:`protocol._satisfies`
    through tuple unpacking. Widening the rows (to carry the subjects a rule may
    settle) therefore has to be a two-sided change, and a one-sided one fails at
    runtime with ``ValueError: too many values to unpack`` on the *reconcile* path -
    long after the diff looks fine. These assertions pin the shape and exercise the
    real consumer so the two modules cannot drift apart silently again.
    """

    def test_every_row_carries_a_pattern_a_reason_and_subjects(self) -> None:
        from companion_runtime.unfinished import RESOLUTION_PATTERNS

        assert RESOLUTION_PATTERNS, "the rule table must not be empty"
        for row in RESOLUTION_PATTERNS:
            assert len(row) == 3, f"a row has {len(row)} fields; consumers unpack three"
            pattern, reason, topics = row
            assert hasattr(pattern, "search"), row
            assert isinstance(reason, str) and reason
            assert isinstance(topics, tuple)

    def test_the_protocol_consumer_unpacks_it(self) -> None:
        """``protocol._satisfies`` must answer, not raise, for either outcome."""
        from companion_runtime.protocol import _satisfies

        assert _satisfies("面试过啦！！", "follow_up") is True
        assert _satisfies("今天天气不错", "follow_up") is False


# --------------------------------------------------------------------------------------
# 3. Semantic false positives
# --------------------------------------------------------------------------------------


class TestOrdinaryDepartureIsNotADeath:
    """``走了`` is the ordinary way to say "I'm leaving"."""

    @pytest.mark.parametrize(
        "text",
        ["走了", "我先走了", "那我走了", "我走了啊", "他走了", "走吧"],
    )
    def test_departure_phrases_do_not_settle_as_loss(self, text: str) -> None:
        settlement = classify_event(text)
        assert settlement is None, f"{text!r} was settled as {settlement}"

    @pytest.mark.parametrize(
        ("text", "source"),
        [
            ("我奶奶去世了", "major_loss"),
            ("他昨天过世了", "major_loss"),
            ("家里有人不在了", "major_loss"),
            ("我去参加了葬礼", "major_loss"),
        ],
    )
    def test_unambiguous_death_words_still_settle(self, text: str, source: str) -> None:
        settlement = classify_event(text)
        assert settlement is not None, text
        assert settlement.source == source
        assert settlement.direction == "-"


class TestNegatedAnchorsDoNotSettle:
    """A negated anchor states the opposite; the rule layer must refuse."""

    @pytest.mark.parametrize(
        "text",
        ["我没觉得我喜欢你", "我不喜欢你", "我没有想你", "我不开心", "我没有很难过", "我不讨厌你"],
    )
    def test_a_negated_anchor_stays_unresolved(self, text: str) -> None:
        assert classify_event(text) is None, text

    @pytest.mark.parametrize(
        "text",
        ["我喜欢你", "我真的很想你", "谢谢你陪着我", "我今天很难过", "对，我喜欢你"],
    )
    def test_unnegated_anchors_still_settle(self, text: str) -> None:
        assert classify_event(text) is not None, text

    def test_the_specific_regression_from_the_audit(self) -> None:
        assert classify_event("我没觉得我喜欢你") is None, (
            "the anchor was matched inside a negation, so the event settled as affection"
        )


# --------------------------------------------------------------------------------------
# 4. Unfinished matters
# --------------------------------------------------------------------------------------


class TestResolutionIsSubjectAware:
    """A completion statement settles what it is about, and nothing more."""

    def _live(self) -> list[UnfinishedMatter]:
        return [
            UnfinishedMatter(unfinished_id="u_interview", title="等待面试结果"),
            UnfinishedMatter(unfinished_id="u_trip", title="关心行程是否顺利"),
        ]

    def test_arriving_home_does_not_close_an_interview(self) -> None:
        resolved = unfinished_module.detect_resolution(_event("我到家了"), live=self._live())
        assert [identifier for identifier, _reason in resolved] == ["u_trip"]

    def test_an_unrelated_statement_settles_nothing(self) -> None:
        assert unfinished_module.detect_resolution(_event("今天天气不错"), live=self._live()) == []

    def test_a_result_closes_the_matters_about_results(self) -> None:
        resolved = unfinished_module.detect_resolution(
            _event("面试过啦！！"), live=self._live()
        )
        assert "u_interview" in {identifier for identifier, _reason in resolved}

    def test_the_single_matter_case_is_unchanged(self) -> None:
        """The pre-existing contract still holds when only one matter is open."""
        live = [UnfinishedMatter(unfinished_id="u1", title="等待面试结果")]
        assert unfinished_module.detect_resolution(_event("面试过啦！！"), live=live) == [
            ("u1", "result_reported")
        ]


class TestCompletionPhrasesDoNotRecreateObligations:
    """The phrase that ends an obligation must not open one about the same thing."""

    def test_an_arrival_creates_nothing(self) -> None:
        proposals = unfinished_module.detect(_event("我到家了"), config=RuntimeConfig())
        assert proposals == [], [proposal.title for proposal in proposals]

    def test_a_promise_about_a_trip_still_creates_a_matter(self) -> None:
        proposals = unfinished_module.detect(
            _event("明天要出差，落地告诉你"), config=RuntimeConfig()
        )
        assert [proposal.title for proposal in proposals] == ["关心行程是否顺利"]

    def test_an_interview_still_creates_its_matter(self) -> None:
        proposals = unfinished_module.detect(
            _event("明天下午面试，结束告诉你结果。"), config=RuntimeConfig()
        )
        assert [proposal.title for proposal in proposals] == ["等待面试结果"]

    def test_the_settled_subject_is_not_re_opened_end_to_end(self) -> None:
        runtime = _runtime()
        try:
            created = runtime.process_user_message(
                content="明天要出差，落地告诉你", timestamp=BASE_TIME
            )
            assert len(created.unfinished_created) == 1
            arrival = runtime.process_user_message(
                content="我到家了", timestamp=BASE_TIME + timedelta(hours=20)
            )
            assert arrival.unfinished_resolved == created.unfinished_created
            assert arrival.unfinished_created == [], (
                "the arrival re-created the obligation it had just discharged"
            )
            assert runtime.projections.unfinished.list_open() == []
        finally:
            runtime.close()

    def test_an_interview_matter_survives_an_arrival(self) -> None:
        runtime = _runtime()
        try:
            runtime.process_user_message(
                content="明天下午面试，结束告诉你结果。", timestamp=BASE_TIME
            )
            arrival = runtime.process_user_message(
                content="我到家了", timestamp=BASE_TIME + timedelta(hours=20)
            )
            assert arrival.unfinished_resolved == []
            assert len(runtime.projections.unfinished.list_open()) == 1
        finally:
            runtime.close()


# --------------------------------------------------------------------------------------
# 5. Memory deduplication and archival
# --------------------------------------------------------------------------------------


def _memory_projection() -> tuple[Database, MemoryProjection]:
    """Return a migrated in-memory database and its memory projection."""
    db = Database(":memory:")
    db.migrate()
    return db, MemoryProjection(db)


class TestMemoryDeduplicationIsContentBased:
    """Sharing a source event is not sharing a fact."""

    def test_distinct_facts_from_one_message_are_not_merged(self) -> None:
        db, projection = _memory_projection()
        try:
            with db.transaction() as conn:
                # One message, two facts: both candidates cite the same event.
                for index, summary in enumerate(
                    ["用户不喜欢被连续追问在干嘛", "用户的生日是三月三号"]
                ):
                    projection.upsert_candidate(
                        conn,
                        MemoryCandidate(
                            candidate_id=f"mcd_{index}",
                            summary=summary,
                            kind=MemoryKind.EPISODIC.value,
                            source_event_ids=["evt_shared"],
                            value=0.8,
                            topics=["用户"],
                        ),
                    )
                result = memory_module.consolidate(
                    projection, conn, config=RuntimeConfig(), now=BASE_TIME
                )
            assert len(result.consolidated) == 2, (
                "distinct facts sharing a source event were merged into one"
            )
            assert {memory.summary for memory in projection.list_memories()} == {
                "用户不喜欢被连续追问在干嘛",
                "用户的生日是三月三号",
            }
        finally:
            db.close()

    def test_the_same_fact_is_still_merged(self) -> None:
        db, projection = _memory_projection()
        try:
            with db.transaction() as conn:
                for index in range(2):
                    projection.upsert_candidate(
                        conn,
                        MemoryCandidate(
                            candidate_id=f"mcd_{index}",
                            summary="用户喜欢咖啡",
                            kind=MemoryKind.USER_PREFERENCE.value,
                            source_event_ids=["evt_a", "evt_b"][: index + 1],
                            value=0.8,
                            topics=["咖啡", "喜欢"],
                        ),
                    )
                result = memory_module.consolidate(
                    projection, conn, config=RuntimeConfig(), now=BASE_TIME
                )
            assert len(result.consolidated) == 1
            assert result.skipped == 1
            assert len(projection.list_memories()) == 1
        finally:
            db.close()

    def test_a_restatement_of_the_same_fact_is_merged(self) -> None:
        """Longer and shorter versions of one fact are one fact."""
        db, projection = _memory_projection()
        try:
            with db.transaction() as conn:
                for index, summary in enumerate(["用户喜欢咖啡", "用户喜欢手冲咖啡"]):
                    projection.upsert_candidate(
                        conn,
                        MemoryCandidate(
                            candidate_id=f"mcd_{index}",
                            summary=summary,
                            kind=MemoryKind.USER_PREFERENCE.value,
                            source_event_ids=[f"evt_{index}"],
                            value=0.8,
                            topics=["咖啡", "喜欢"],
                        ),
                    )
                result = memory_module.consolidate(
                    projection, conn, config=RuntimeConfig(), now=BASE_TIME
                )
            assert len(result.consolidated) == 1
            assert result.skipped == 1
        finally:
            db.close()


class TestArchivedMemoriesAreNotInjected:
    """Archival means "no longer part of what I know"."""

    def _seed(self, projection: MemoryProjection, db: Database) -> None:
        memory = Memory(
            memory_id="mem_archived",
            kind=MemoryKind.EPISODIC.value,
            summary="很久以前的一件事",
            topics=["面试"],
            importance=0.9,
            confidence=0.8,
            source_event_ids=["evt_old"],
            created_at=BASE_TIME - timedelta(days=30),
        )
        with db.transaction() as conn:
            projection.upsert_memory(conn, memory)
            projection.upsert_activation(
                conn,
                ActivatedMemory(
                    memory_id=memory.memory_id,
                    activation=0.9,
                    last_recalled_at=BASE_TIME,
                    recall_count=1,
                ),
            )
            projection.set_memory_status(conn, memory.memory_id, MemoryStatus.ARCHIVED.value)

    def test_the_store_does_not_return_it(self) -> None:
        db, projection = _memory_projection()
        try:
            self._seed(projection, db)
            store = MemoryStore(projection, RuntimeConfig())
            assert store.activated_memories(limit=5) == []
            hits = store.retrieve(
                RetrievalCue(query_text="面试", now=BASE_TIME), limit=5
            )
            assert hits == []
        finally:
            db.close()

    def test_the_prompt_selection_filters_it(self) -> None:
        db = Database(":memory:")
        db.migrate()
        projections = Projections(db, "companion")
        projections.ensure_defaults(BASE_TIME)
        try:
            self._seed(projections.memory, db)
            assert projections.memory.list_activated(limit=5), "the raw pool still holds it"
            assert projections.memory.list_activated_memories(limit=5) == []
            assert select_memories(projections, limit=5) == []
        finally:
            db.close()

    def test_the_approach_drive_ignores_it(self) -> None:
        db, projection = _memory_projection()
        try:
            self._seed(projection, db)
            store = MemoryStore(projection, RuntimeConfig())
            assert store.activation_strength() == 0.0
        finally:
            db.close()


# --------------------------------------------------------------------------------------
# 6. User-model uncertainty uses evidence, not vector length
# --------------------------------------------------------------------------------------


class TestUncertaintyTracksEvidence:
    """Uncertainty must fall when evidence arrives, per behaviour class."""

    def _model(self, db: Database) -> UserInteractionModel:
        return UserInteractionModel(UserModelProjection(db), RuntimeConfig())

    def test_uncertainty_drops_as_one_class_accumulates_evidence(self) -> None:
        db = Database(":memory:")
        db.migrate()
        action = {"type": "contact", "proactive": True}
        context = {"busy_probability": 0.0, "hours_since_contact": 12.0}
        try:
            model = self._model(db)
            before = model.predict(action=action, context=context)
            for _index in range(8):
                with db.transaction() as conn:
                    model.observe(
                        conn,
                        action=action,
                        context=context,
                        reaction=BehaviourReaction(replied=True, reply_length=30),
                        now=BASE_TIME,
                        observed_at=BASE_TIME,
                    )
            after = model.predict(action=action, context=context)
            assert after.uncertainty < before.uncertainty, (
                "uncertainty did not move with the evidence"
            )
            assert model.behaviour_evidence("proactive_contact") == 8
        finally:
            db.close()

    def test_an_unobserved_class_is_less_certain_than_an_observed_one(self) -> None:
        db = Database(":memory:")
        db.migrate()
        contact = {"type": "contact", "proactive": True}
        question = {"type": "curious_question", "proactive": True, "question": True}
        context = {"busy_probability": 0.0, "hours_since_contact": 12.0}
        try:
            model = self._model(db)
            for _index in range(8):
                with db.transaction() as conn:
                    model.observe(
                        conn,
                        action=contact,
                        context=context,
                        reaction=BehaviourReaction(replied=True, reply_length=30),
                        now=BASE_TIME,
                        observed_at=BASE_TIME,
                    )
            observed = model.predict(action=contact, context=context)
            unobserved = model.predict(action=question, context=context)
            assert model.behaviour_evidence("curious_question") == 0
            assert unobserved.uncertainty > observed.uncertainty
        finally:
            db.close()

    def test_the_evidence_count_is_persisted(self) -> None:
        db = Database(":memory:")
        db.migrate()
        action = {"type": "contact", "proactive": True}
        context = {"busy_probability": 0.0, "hours_since_contact": 12.0}
        try:
            model = self._model(db)
            with db.transaction() as conn:
                model.observe(
                    conn,
                    action=action,
                    context=context,
                    reaction=BehaviourReaction(replied=True, reply_length=10),
                    now=BASE_TIME,
                    observed_at=BASE_TIME,
                )
            assert self._model(db).behaviour_evidence("proactive_contact") == 1
        finally:
            db.close()

    def test_a_row_without_counts_does_not_claim_certainty(self) -> None:
        """A model stored before the field existed starts from a conservative guess."""
        assert UserInteractionModel._load_class_counts({}) == {
            cls: 0 for cls in BEHAVIOUR_CLASSES
        }
        migrated = UserInteractionModel._load_class_counts({"delta": {"reply": {"x": [0.1]}}})
        assert migrated["reply"] == 1
        assert migrated["follow_up"] == 0


# --------------------------------------------------------------------------------------
# 7. Explanation cache key includes the dominant identity
# --------------------------------------------------------------------------------------


def _active_emotion(
    identifier: str, *, intensity: float, direction: str, label: str | None
) -> EmotionEvent:
    """Build one active emotion event for cache-key tests."""
    return EmotionEvent(
        emotion_event_id=identifier,
        source_event_id="evt_1",
        direction=direction,
        intensity=intensity,
        activation=0.5,
        semantic_label=label,
    )


class TestExplanationCacheKey:
    """Two different feelings of the same strength are different states."""

    def test_a_different_dominant_event_changes_the_key(self) -> None:
        state = RuntimeState()
        negative = [_active_emotion("e1", intensity=0.6, direction="-", label="委屈")]
        positive = [_active_emotion("e2", intensity=0.6, direction="+", label="欣喜")]
        assert EmotionExplainer.cache_key(state, negative) != EmotionExplainer.cache_key(
            state, positive
        )

    def test_the_same_identity_is_stable_across_event_ids(self) -> None:
        state = RuntimeState()
        first = [_active_emotion("e1", intensity=0.6, direction="-", label="委屈")]
        second = [_active_emotion("e9", intensity=0.6, direction="-", label="委屈")]
        assert EmotionExplainer.cache_key(state, first) == EmotionExplainer.cache_key(
            state, second
        )

    def test_the_payload_key_matches_the_state_key(self) -> None:
        """The provider's cache and the Runtime's cache must agree on identity."""
        explainer = EmotionExplainer(None, RuntimeConfig())  # type: ignore[arg-type]
        state = RuntimeState()
        active = [_active_emotion("e1", intensity=0.6, direction="-", label="委屈")]
        payload = explainer._build_input(state, active)
        assert EmotionExplainer.cache_key_from_payload(payload) == EmotionExplainer.cache_key(
            state, active
        )

    def test_a_corrupt_state_value_does_not_break_the_key(self) -> None:
        state = RuntimeState()
        state.mood_valence = float("nan")
        key = EmotionExplainer.cache_key(state, [])
        assert key.startswith("v0.0|")
        assert "dnone" in key and "lnone" in key


class TestTheProviderSeesTheSameCacheKey:
    """The key the explainer computed is the key the provider is given."""

    class _FakeProvider:
        """Provider double that records the cache key it was handed."""

        name = "fake"

        def __init__(self) -> None:
            self.keys: list[str] = []

        def available(self) -> bool:
            return True

        def explain_state(self, payload: Mapping[str, Any], *, state_key: str = "") -> Any:
            self.keys.append(state_key)
            return {
                "experience": "x",
                "focus": "x",
                "conflict": "x",
                "impulse": "x",
                "inhibition": "x",
                "expression": "x",
            }

    def test_the_explainer_passes_its_own_key(self) -> None:
        db = Database(":memory:")
        db.migrate()
        try:
            provider = self._FakeProvider()
            explainer = EmotionExplainer(
                EmotionProjection(db), RuntimeConfig(), provider=provider
            )
            state = RuntimeState()
            active = [_active_emotion("e1", intensity=0.6, direction="-", label="委屈")]
            result = explainer.explain(state=state, active=active, now=BASE_TIME)
            assert provider.keys == [result["cache_key"]]
            assert provider.keys[0] == EmotionExplainer.cache_key(state, active)
            assert "委屈" in provider.keys[0]
        finally:
            db.close()

    def test_a_future_stamped_entry_is_never_served(self) -> None:
        """A negative age is below every TTL, so it must be rejected explicitly."""
        db = Database(":memory:")
        db.migrate()
        try:
            projection = EmotionProjection(db)
            key = "v0.0|a0.0|i0.1|r0.5|p0.0|m0.0|+|dnone|lnone"
            written_at = BASE_TIME + timedelta(hours=1)
            with db.transaction() as conn:
                projection.store_explanation(
                    conn,
                    cache_key=key,
                    payload={"experience": "written in the future"},
                    source="template",
                    now=written_at,
                )
            assert projection.cached_explanation(key, BASE_TIME, 3600) is None
            assert projection.cached_explanation(key, written_at, 3600) is not None
        finally:
            db.close()


class TestNeighbouringMathsIsStillSane:
    """Guard the surrounding arithmetic so a "fix" here cannot quietly break it."""

    def test_uncertainty_stays_a_probability(self) -> None:
        db = Database(":memory:")
        db.migrate()
        try:
            model = UserInteractionModel(UserModelProjection(db), RuntimeConfig())
            prediction = model.predict(
                action={"type": "contact", "proactive": True},
                context={"busy_probability": 0.2, "hours_since_contact": 3.0},
            )
            assert 0.0 <= prediction.uncertainty <= 1.0
            assert not math.isnan(prediction.uncertainty)
        finally:
            db.close()
