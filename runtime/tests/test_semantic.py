"""Tests for coarse semantic settlement (architecture patch v0.2, sections 9-13).

The rule table is the only thing standing between an ambiguous user message and
a permanent change to the character's long-term state, so these tests are written
from the failure side: the important assertions are the ones that require the
classifier to *refuse* to answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from companion_runtime.semantic import (
    ANCHORS,
    AmbiguityVeto,
    CoarseSettlement,
    IntensityBand,
    SemanticStatus,
    UnresolvedRecord,
    ambiguity_veto,
    band_to_intensity,
    classify_event,
    normalize_for_anchors,
    potential_relevance,
    resolve_backlog,
    settlement_to_evaluation,
)

BASE = datetime.fromisoformat("2026-03-01T09:00:00+00:00")


class TestRefusalIsTheDefault:
    """The canonical ambiguous event from patch v0.2 section 11 must stay open."""

    @pytest.mark.parametrize(
        "text",
        [
            "算了，也没什么。",
            "算了",
            "也没什么",
            "随便吧，都行。",
            "可能吧，我也不知道。",
            "也许吧。",
            "还好啦。",
            "一般般吧。",
            "再说吧，看情况。",
            "无所谓。",
        ],
    )
    def test_ambiguous_events_are_not_settled(self, text: str) -> None:
        assert classify_event(text) is None, text

    def test_exact_patch_example_is_unresolved(self) -> None:
        """Patch v0.2 names this string explicitly; it must not be guessed."""
        assert classify_event("算了，也没什么。") is None

    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_empty_text_is_not_settled(self, text: str) -> None:
        assert classify_event(text) is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"event_type": "tool_result"},
            {"event_type": "internal_wake"},
            {"actor": "system"},
        ],
    )
    def test_non_conversational_events_are_not_settled(self, kwargs: dict) -> None:
        assert classify_event("谢谢你", **kwargs) is None


class TestExplicitAnchorsSettle:
    """Explicit evidence is what the coarse layer exists for."""

    @pytest.mark.parametrize(
        ("text", "direction", "source"),
        [
            ("谢谢你，我今天真的被你安慰到了。", "+", "explicit_positive_feedback"),
            ("太感谢了", "+", "explicit_positive_feedback"),
            ("我喜欢你", "+", "explicit_affection"),
            ("面试过啦！", "+", "explicit_good_news"),
            ("我考上研究生了", "+", "explicit_good_news"),
            ("对不起，是我不好", "+", "explicit_repair"),
            ("我奶奶去世了", "-", "major_loss"),
            ("我被裁了", "-", "major_setback"),
            ("你根本不懂我", "-", "explicit_conflict"),
            ("我今天很难过。", "-", "explicit_distress"),
            ("我今晚想自己待着", "-", "explicit_need_for_space"),
            ("我不想聊这个", "-", "explicit_refusal"),
        ],
    )
    def test_anchor_settles_with_the_expected_reading(
        self, text: str, direction: str, source: str
    ) -> None:
        settlement = classify_event(text)
        assert settlement is not None, text
        assert settlement.direction == direction
        assert settlement.source == source
        assert settlement.intensity in {band.value for band in IntensityBand}
        assert 0.0 <= settlement.confidence <= 1.0
        assert settlement.semantic_label is None, "the rule layer never names an emotion"

    def test_fillers_do_not_hide_the_feeling(self) -> None:
        """``我今天很难过`` must still match the ``很难过`` anchor."""
        assert normalize_for_anchors("我今天很难过") == "很难过"
        settlement = classify_event("我今天真的很难过")
        assert settlement is not None
        assert settlement.source == "explicit_distress"

    def test_a_strong_anchor_survives_a_hedge(self) -> None:
        """``谢谢你，其实也没什么`` is still an expression of thanks."""
        settlement = classify_event("谢谢你，其实也没什么")
        assert settlement is not None
        assert settlement.source == "explicit_positive_feedback"

    def test_blunt_refusal_settles_but_hedged_refusal_does_not(self) -> None:
        assert classify_event("不行") is not None
        assert classify_event("算了") is None


class TestAmbiguityVeto:
    """The veto table itself is part of the contract."""

    def test_veto_reports_the_marker_and_reason(self) -> None:
        veto = ambiguity_veto("算了，也没什么。")
        assert isinstance(veto, AmbiguityVeto)
        assert veto.needle in {"算了", "也没什么", "没什么"}

    def test_direct_text_has_no_veto(self) -> None:
        assert ambiguity_veto("谢谢你") is None

    def test_every_ambiguity_marker_is_covered_by_a_negative_case(self) -> None:
        """Guard against someone extending the marker table without a test."""
        for veto in __import__("companion_runtime.semantic", fromlist=["AMBIGUITY_MARKERS"]).AMBIGUITY_MARKERS:
            assert classify_event(veto.needle) is None, veto.needle


class TestAnchorTableIntegrity:
    """Cheap structural invariants that keep the table honest."""

    def test_anchors_use_the_shared_vocabularies(self) -> None:
        for anchor in ANCHORS:
            assert anchor.direction in {"+", "-", "0", "+-"}, anchor
            assert anchor.intensity in {band.value for band in IntensityBand}, anchor
            assert 0.0 < anchor.confidence <= 1.0, anchor
            assert anchor.needles, anchor
            assert anchor.source, anchor

    def test_no_anchor_collides_with_an_ambiguity_marker(self) -> None:
        for anchor in ANCHORS:
            for needle in anchor.needles:
                assert ambiguity_veto(needle) is None, (anchor.source, needle)


class TestRelevancePrioritisation:
    """Relevance only orders the refresh queue; it never changes state."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", "low"),
            ("嗯嗯", "low"),
            ("你觉得我们以后会一直这样吗", "high"),
            ("明天要面试，有点担心", "medium"),
            ("今天路上看到一只很可爱的猫，拍了照片", "medium"),
        ],
    )
    def test_relevance_bands(self, text: str, expected: str) -> None:
        assert potential_relevance(text) == expected

    def test_open_matters_raise_a_short_message(self) -> None:
        assert potential_relevance("嗯", hours_since_contact=0.0) == "low"
        assert (
            potential_relevance("嗯", hours_since_contact=12.0, has_open_matters=True) == "low"
        ), "a two-character message stays low even with open matters"
        assert (
            potential_relevance("那件事你记得吧", hours_since_contact=12.0, has_open_matters=True)
            == "medium"
        )


class TestSettlementToEvaluation:
    """The bridge into the numeric layer must stay lossy in the honest direction."""

    def test_conversion_reports_low_confidence_as_high_uncertainty(self) -> None:
        settlement = CoarseSettlement(
            direction="+",
            intensity=IntensityBand.MEDIUM_HIGH.value,
            confidence=0.6,
            source="explicit_positive_feedback",
        )
        evaluation = settlement_to_evaluation(settlement)
        assert evaluation.direction == "+"
        assert evaluation.source == "coarse_rule"
        assert evaluation.uncertainty == pytest.approx(0.4)
        assert evaluation.impact == pytest.approx(band_to_intensity(IntensityBand.MEDIUM_HIGH))

    def test_relation_signal_is_mapped_from_the_source(self) -> None:
        for source, expected in (
            ("explicit_affection", "closeness"),
            ("explicit_conflict", "distance"),
            ("major_loss", "loss"),
            ("explicit_repair", "repair"),
            ("explicit_need_for_space", "distance"),
            ("something_unknown", "neutral"),
        ):
            evaluation = settlement_to_evaluation(
                CoarseSettlement("+", IntensityBand.LOW.value, 0.7, source)
            )
            assert evaluation.relation_signal == expected, source

    def test_band_ordering_is_monotonic(self) -> None:
        bands = [
            IntensityBand.NEGLIGIBLE,
            IntensityBand.LOW,
            IntensityBand.MEDIUM,
            IntensityBand.MEDIUM_HIGH,
            IntensityBand.HIGH,
        ]
        values = [band_to_intensity(band) for band in bands]
        assert values == sorted(values)
        assert band_to_intensity("unknown_band") == band_to_intensity(IntensityBand.MEDIUM)


class TestBacklogSelection:
    """What gets sent to a deep refresh, and what simply ages out."""

    def _record(self, event_id: str, relevance: str, age_hours: float) -> UnresolvedRecord:
        return UnresolvedRecord(
            event_id=event_id,
            potential_relevance=relevance,
            created_at=BASE - timedelta(hours=age_hours),
        )

    def test_stale_records_are_separated(self) -> None:
        live, stale = resolve_backlog(
            [self._record("a", "high", 1.0), self._record("b", "high", 200.0)],
            now=BASE,
            max_age_hours=72.0,
        )
        assert [item.event_id for item in live] == ["a"]
        assert [item.event_id for item in stale] == ["b"]

    def test_high_relevance_is_sent_first(self) -> None:
        live, _stale = resolve_backlog(
            [
                self._record("low", "low", 0.5),
                self._record("high", "high", 5.0),
                self._record("medium", "medium", 1.0),
            ],
            now=BASE,
        )
        assert [item.event_id for item in live] == ["high", "medium", "low"]

    def test_recency_breaks_ties_within_a_band(self) -> None:
        live, _stale = resolve_backlog(
            [self._record("older", "high", 5.0), self._record("newer", "high", 1.0)],
            now=BASE,
        )
        assert [item.event_id for item in live] == ["newer", "older"]

    def test_limit_truncates_without_dropping_into_stale(self) -> None:
        records = [self._record(f"e{index}", "high", index * 0.1) for index in range(10)]
        live, stale = resolve_backlog(records, now=BASE, limit=3)
        assert len(live) == 3
        assert stale == [], "truncation is not ageing out"

    def test_empty_backlog(self) -> None:
        assert resolve_backlog([], now=BASE) == ([], [])


class TestRecordRendering:
    """Records cross the API boundary, so their shape is part of the contract."""

    def test_unresolved_record_marks_its_status(self) -> None:
        record = UnresolvedRecord("evt_1", "high", BASE, reason="no_explicit_anchor")
        payload = record.to_dict()
        assert payload["semantic_status"] == SemanticStatus.UNRESOLVED.value
        assert payload["event_id"] == "evt_1"
        assert payload["potential_relevance"] == "high"

    def test_settlement_round_trips_through_json(self) -> None:
        import json

        settlement = CoarseSettlement("+", IntensityBand.HIGH.value, 0.85, "explicit_affection")
        payload = json.loads(json.dumps(settlement.to_dict()))
        assert payload["direction"] == "+"
        assert payload["intensity"] == "high"
        assert payload["confidence"] == 0.85
