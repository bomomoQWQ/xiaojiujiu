"""JSON 抽取与去重工具测试。"""

from __future__ import annotations

import pytest

from qboss_training.errors import JsonExtractionError
from qboss_training.utils.dedup import (
    Deduplicator,
    dedupe_records,
    input_fingerprint,
    jaccard,
    normalize_text,
    shingles,
    stable_hash,
)
from qboss_training.utils.jsonx import dumps_canonical, extract_json


class TestExtractJson:
    def test_plain_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_object_with_surrounding_prose(self) -> None:
        text = '好的，评价如下：\n{"direction": "-", "impact": 0.5}\n希望有帮助。'
        assert extract_json(text) == {"direction": "-", "impact": 0.5}

    def test_fenced_json_block(self) -> None:
        text = '结果：\n```json\n{"a": 1, "b": [2, 3]}\n```\n'
        assert extract_json(text) == {"a": 1, "b": [2, 3]}

    def test_fenced_block_without_language(self) -> None:
        assert extract_json("```\n{\"a\": 1}\n```") == {"a": 1}

    def test_trailing_comma_is_repaired(self) -> None:
        assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_nested_trailing_comma_is_repaired(self) -> None:
        assert extract_json('{"a": [1, 2,], "b": {"c": 3,},}') == {
            "a": [1, 2],
            "b": {"c": 3},
        }

    def test_full_width_punctuation_is_repaired(self) -> None:
        text = '｛“direction”： “-”， “impact”： 0.5｝'
        assert extract_json(text) == {"direction": "-", "impact": 0.5}

    def test_braces_inside_strings_do_not_break_scanning(self) -> None:
        text = '前言 {"text": "这里有 } 一个右括号", "n": 1} 后记'
        assert extract_json(text) == {"text": "这里有 } 一个右括号", "n": 1}

    def test_escaped_quotes_inside_strings(self) -> None:
        text = r'{"text": "他说 \"好的\"，然后走了", "n": 2}'
        assert extract_json(text)["n"] == 2

    def test_multiple_objects_returns_first_valid(self) -> None:
        text = '{"a": 1} 以及 {"b": 2}'
        assert extract_json(text) == {"a": 1}

    def test_validate_selects_matching_candidate(self) -> None:
        text = '{"wrong": true} {"direction": "-", "impact": 0.5}'
        result = extract_json(
            text, validate=lambda item: "direction" in item
        )
        assert result == {"direction": "-", "impact": 0.5}

    def test_empty_input_raises(self) -> None:
        with pytest.raises(JsonExtractionError, match="空"):
            extract_json("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(JsonExtractionError):
            extract_json("   \n  ")

    def test_unparseable_raises_with_snippet(self) -> None:
        with pytest.raises(JsonExtractionError) as excinfo:
            extract_json("这完全不是 JSON")
        assert "这完全不是 JSON" in str(excinfo.value)

    def test_validate_never_satisfied_raises(self) -> None:
        with pytest.raises(JsonExtractionError):
            extract_json('{"a": 1}', validate=lambda item: False)

    def test_prefer_object_skips_arrays(self) -> None:
        with pytest.raises(JsonExtractionError):
            extract_json("[1, 2, 3]", prefer_object=True)

    def test_prefer_object_false_accepts_arrays(self) -> None:
        assert extract_json("[1, 2, 3]", prefer_object=False) == [1, 2, 3]

    def test_canonical_dumps_is_stable(self) -> None:
        left = dumps_canonical({"b": 1, "a": {"d": 2, "c": 3}})
        right = dumps_canonical({"a": {"c": 3, "d": 2}, "b": 1})
        assert left == right


class TestNormalization:
    def test_full_width_is_normalized(self) -> None:
        assert normalize_text("ＡＢＣ") == "abc"

    def test_whitespace_is_collapsed(self) -> None:
        assert normalize_text("a \n\t b") == "a b"

    def test_case_is_folded(self) -> None:
        assert normalize_text("HeLLo") == "hello"

    def test_empty_stays_empty(self) -> None:
        assert normalize_text("") == ""


class TestShinglesAndJaccard:
    def test_shingles_of_short_text(self) -> None:
        assert shingles("ab") == frozenset({"ab"})

    def test_shingles_of_empty_text(self) -> None:
        assert shingles("") == frozenset()

    def test_identical_text_is_fully_similar(self) -> None:
        assert jaccard(shingles("今晚想自己待着"), shingles("今晚想自己待着")) == 1.0

    def test_similar_text_scores_high(self) -> None:
        """几乎相同（只差标点）的文本应落在高相似区。"""
        left = shingles("用户说今晚想自己待着，不聊天了")
        right = shingles("用户说今晚想自己待着，不聊天了。")
        assert jaccard(left, right) >= 0.9

    def test_moderately_rewritten_text_scores_middle(self) -> None:
        """改写过的同一件事落在中高区，不一定被判重复（这是可接受的）。"""
        left = shingles("用户说今晚想自己待着，不聊天了")
        right = shingles("用户说今晚想自己待着，暂时不聊天")
        score = jaccard(left, right)
        assert 0.5 < score < 0.9

    def test_different_text_scores_low(self) -> None:
        left = shingles("用户说今晚想自己待着")
        right = shingles("明天面试通过了很高兴")
        assert jaccard(left, right) < 0.2

    def test_both_empty_is_one(self) -> None:
        assert jaccard(frozenset(), frozenset()) == 1.0

    def test_one_empty_is_zero(self) -> None:
        assert jaccard(frozenset({"a"}), frozenset()) == 0.0


class TestStableHash:
    def test_key_order_does_not_matter(self) -> None:
        assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})

    def test_different_content_differs(self) -> None:
        assert stable_hash({"a": 1}) != stable_hash({"a": 2})

    def test_length_is_respected(self) -> None:
        assert len(stable_hash({"a": 1}, length=8)) == 8

    def test_input_fingerprint_is_task_scoped(self) -> None:
        payload = {"x": 1}
        assert input_fingerprint("event_eval", payload) != input_fingerprint(
            "emotion_explain", payload
        )

    def test_input_fingerprint_is_stable(self) -> None:
        payload = {"text": "今晚想自己待着", "mood": {"valence": -0.3}}
        assert input_fingerprint("event_eval", payload) == input_fingerprint(
            "event_eval", dict(payload)
        )


class TestDeduplicator:
    def test_exact_duplicate_is_rejected(self) -> None:
        deduper = Deduplicator(near_threshold=None)
        payload = {"text": "今晚想自己待着"}
        ok, _ = deduper.accept("event_eval", payload, "r1")
        assert ok
        ok2, hit = deduper.accept("event_eval", dict(payload), "r2")
        assert not ok2
        assert hit is not None and hit.kind == "exact"

    def test_near_duplicate_is_rejected(self) -> None:
        """只差标点的两份输入应被判为近似重复。"""
        deduper = Deduplicator(near_threshold=0.90)
        first = {"current_event": {"text": "用户说今晚想自己待着，不聊天了"}}
        second = {"current_event": {"text": "用户说今晚想自己待着，不聊天了。"}}
        assert deduper.accept("event_eval", first, "r1")[0]
        ok, hit = deduper.accept("event_eval", second, "r2")
        assert not ok
        assert hit is not None and hit.kind == "near"

    def test_default_threshold_does_not_over_reject(self, fixture_records: list[dict]) -> None:
        """默认阈值下，互相独立的样本不应被大量误杀。

        这是阈值标定的回归保护：如果默认值被调到 0.85 以下，
        误杀率会明显上升，这个测试会失败。
        """
        deduper = Deduplicator()  # 使用默认阈值
        kept = sum(
            1 for record in fixture_records if deduper.accept(
                record["task"], record["input"], record["id"]
            )[0]
        )
        # 夹具里所有样本语义互不相同，允许少量高相似被合并，但不应丢掉大半
        assert kept >= int(len(fixture_records) * 0.85), (
            f"默认阈值误杀了过多样本：只保留 {kept}/{len(fixture_records)}"
        )

    def test_distinct_payloads_are_kept(self) -> None:
        deduper = Deduplicator(near_threshold=0.8)
        assert deduper.accept("event_eval", {"text": "alpha 完全不同 甲"}, "r1")[0]
        assert deduper.accept("event_eval", {"text": "beta 毫不相干 乙"}, "r2")[0]
        assert len(deduper) == 2

    def test_same_input_different_task_is_not_duplicate(self) -> None:
        """同一份输入在不同任务下语义不同，不应互相去重。"""
        deduper = Deduplicator(near_threshold=0.9)
        payload = {"text": "同一段文本"}
        assert deduper.accept("event_eval", payload, "ee_1")[0]
        assert deduper.accept("emotion_explain", payload, "ex_1")[0]

    def test_stats_are_counted(self) -> None:
        deduper = Deduplicator(near_threshold=None)
        deduper.accept("event_eval", {"a": 1}, "r1")
        deduper.accept("event_eval", {"a": 1}, "r2")
        stats = deduper.stats
        assert stats["kept"] == 1
        assert stats["exact"] == 1

    def test_check_does_not_mutate_state(self) -> None:
        deduper = Deduplicator(near_threshold=None)
        deduper.accept("event_eval", {"a": 1}, "r1")
        before = len(deduper)
        deduper.check("event_eval", {"b": 2}, "r2")
        assert len(deduper) == before


class TestDedupeRecords:
    def test_batch_dedup_marks_duplicates(self) -> None:
        records = [
            {"id": "a", "task": "event_eval", "input": {"t": 1}},
            {"id": "b", "task": "event_eval", "input": {"t": 1}},
            {"id": "c", "task": "event_eval", "input": {"t": 2}},
        ]
        kept, dropped = dedupe_records(records, near_threshold=None)
        assert [item["id"] for item in kept] == ["a", "c"]
        assert len(dropped) == 1
        assert dropped[0]["id"] == "b"
        assert dropped[0]["duplicate_of"] == "a"
        assert dropped[0]["duplicate_kind"] == "exact"

    def test_empty_input(self) -> None:
        kept, dropped = dedupe_records([], near_threshold=None)
        assert kept == [] and dropped == []
