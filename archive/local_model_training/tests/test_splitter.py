"""切分器测试：确定性、分层、防泄漏。"""

from __future__ import annotations

from typing import Any

import pytest

from qboss_training.config import SplitConfig
from qboss_training.data.splitter import (
    SPLITS,
    assert_task_coverage,
    describe_splits,
    detect_leakage,
    load_and_split,
    prepare_records,
    resolve_leakage,
    split_records,
    write_splits,
)
from qboss_training.utils.io import read_jsonl


class TestPrepareRecords:
    def test_invalid_records_are_dropped(self, small_fixture_records: list[dict]) -> None:
        broken = dict(small_fixture_records[0])
        broken = {**broken, "id": "broken", "output": {"direction": "?"}}
        kept, duplicates, invalid, dropped = prepare_records(
            [*small_fixture_records, broken]
        )
        assert invalid == 1
        assert "broken" in dropped
        assert len(kept) == len(small_fixture_records)

    def test_duplicates_are_removed(self, small_fixture_records: list[dict]) -> None:
        duplicated = [*small_fixture_records, dict(small_fixture_records[0])]
        kept, duplicates, invalid, _ = prepare_records(duplicated, near_threshold=None)
        assert duplicates == 1
        assert len(kept) == len(small_fixture_records)

    def test_validation_can_be_skipped(self) -> None:
        kept, _, invalid, _ = prepare_records(
            [{"id": "x", "task": "event_eval", "input": {"a": 1}, "output": {"b": 2}}],
            validate=False,
        )
        assert invalid == 0
        assert len(kept) == 1


class TestSplitRecords:
    def test_deterministic_with_same_seed(self, fixture_records: list[dict]) -> None:
        config = SplitConfig(seed=42)
        first, _ = split_records(fixture_records, config)
        second, _ = split_records(fixture_records, config)
        for name in SPLITS:
            assert [item["id"] for item in first[name]] == [
                item["id"] for item in second[name]
            ]

    def test_different_seed_changes_assignment(self, fixture_records: list[dict]) -> None:
        left, _ = split_records(fixture_records, SplitConfig(seed=1))
        right, _ = split_records(fixture_records, SplitConfig(seed=2))
        assert [item["id"] for item in left["train"]] != [
            item["id"] for item in right["train"]
        ]

    def test_all_records_are_assigned_exactly_once(
        self, fixture_records: list[dict]
    ) -> None:
        splits, _ = split_records(fixture_records, SplitConfig())
        seen: list[str] = []
        for name in SPLITS:
            seen.extend(item["id"] for item in splits[name])
        assert sorted(seen) == sorted(item["id"] for item in fixture_records)
        assert len(seen) == len(set(seen)), "有样本被分到多个 split"

    def test_ratios_are_respected(self, fixture_records: list[dict]) -> None:
        splits, report = split_records(
            fixture_records,
            SplitConfig(train_ratio=0.8, val_ratio=0.1, test_ratio=0.1, leakage_threshold=0),
        )
        total = sum(report.counts.values())
        assert total == len(fixture_records)
        assert report.counts["train"] / total == pytest.approx(0.8, abs=0.06)
        assert report.counts["test"] > 0

    def test_stratification_keeps_ratio_per_task(
        self, fixture_records: list[dict]
    ) -> None:
        splits, report = split_records(
            fixture_records,
            SplitConfig(stratify_by=("task",), leakage_threshold=0),
        )
        # 两个任务各 30 条，train 里应大致各占 80%
        assert report.by_task["event_eval"]["train"] >= 20
        assert report.by_task["emotion_explain"]["train"] >= 20
        assert report.by_task["event_eval"]["test"] > 0
        assert report.by_task["emotion_explain"]["test"] > 0

    def test_stratify_by_direction(self, fixture_records: list[dict]) -> None:
        splits, _ = split_records(
            fixture_records,
            SplitConfig(stratify_by=("task", "direction"), leakage_threshold=0),
        )
        directions = {
            (item.get("output") or {}).get("direction")
            for item in splits["train"]
            if item["task"] == "event_eval"
        }
        assert len(directions) >= 3, "分层后 train 应覆盖多种方向"

    def test_fingerprints_are_recorded(self, fixture_records: list[dict]) -> None:
        _, report = split_records(fixture_records, SplitConfig())
        assert set(report.fingerprints) == set(SPLITS)
        assert all(len(value) == 16 for value in report.fingerprints.values())

    def test_ratios_normalized_when_not_summing_to_one(
        self, fixture_records: list[dict]
    ) -> None:
        """比例为 2:1:1 时应等价于 0.5:0.25:0.25。"""
        splits, report = split_records(
            fixture_records,
            SplitConfig(
                train_ratio=2, val_ratio=1, test_ratio=1, leakage_threshold=0
            ),
        )
        total = sum(report.counts.values())
        assert report.counts["train"] / total == pytest.approx(0.5, abs=0.05)

    def test_empty_input_is_safe(self) -> None:
        splits, report = split_records([], SplitConfig())
        assert report.total if hasattr(report, "total") else True
        assert all(report.counts[name] == 0 for name in SPLITS)
        assert splits["train"] == []

    def test_small_dataset_does_not_lose_records(self) -> None:
        from qboss_training.fixtures import build_fixture_records

        records = build_fixture_records(event_eval_count=2, emotion_explain_count=2)
        splits, report = split_records(records, SplitConfig(leakage_threshold=0))
        assert sum(report.counts.values()) == 4


class TestLeakage:
    def test_no_leakage_when_disjoint(self, fixture_records: list[dict]) -> None:
        train = fixture_records[:30]
        others = fixture_records[30:]
        assert detect_leakage(train, others, threshold=0.9) == []

    def test_detects_near_duplicate_across_splits(self) -> None:
        base = {
            "id": "train_1",
            "task": "event_eval",
            "input": {"current_event": {"text": "用户说今晚想自己待着，不聊天了"}},
            "output": {},
        }
        near = {
            "id": "test_1",
            "task": "event_eval",
            "input": {"current_event": {"text": "用户说今晚想自己待着，不聊天了。"}},
            "output": {},
        }
        hits = detect_leakage([base], [near], threshold=0.9)
        assert len(hits) == 1
        position, matched_id, score = hits[0]
        assert position == 0
        assert matched_id == "train_1"
        assert score >= 0.9

    def test_resolve_leakage_moves_samples_to_train(self) -> None:
        train = [
            {
                "id": "train_1",
                "task": "event_eval",
                "input": {"current_event": {"text": "用户说今晚想自己待着，不聊天了"}},
            }
        ]
        val = [
            {
                "id": "val_dup",
                "task": "event_eval",
                "input": {"current_event": {"text": "用户说今晚想自己待着，不聊天了。"}},
            },
            {
                "id": "val_ok",
                "task": "event_eval",
                "input": {"current_event": {"text": "明天面试通过了很高兴"}},
            },
        ]
        updated_train, updated_val, updated_test, moved, details = resolve_leakage(
            train, val, [], threshold=0.9
        )
        assert moved == 1
        assert [item["id"] for item in updated_val] == ["val_ok"]
        assert {item["id"] for item in updated_train} == {"train_1", "val_dup"}
        assert details[0]["from"] == "val"
        assert details[0]["matched_train_id"] == "train_1"

    def test_split_records_records_leakage_in_report(
        self, fixture_records: list[dict]
    ) -> None:
        _, report = split_records(
            fixture_records,
            SplitConfig(leakage_threshold=0.9, dedupe_near_threshold=0),
        )
        # 不强制一定有泄漏，但字段必须在
        assert isinstance(report.leakage_moved, int)
        assert isinstance(report.leakage_details, list)


class TestTaskCoverage:
    def test_reports_missing_task(self) -> None:
        splits = {"train": [], "val": [], "test": []}
        problems = assert_task_coverage(splits)
        assert len(problems) == 2
        assert all("完全没有样本" in item for item in problems)

    def test_reports_task_only_in_val(self) -> None:
        splits = {
            "train": [{"task": "event_eval"}],
            "val": [{"task": "emotion_explain"}],
            "test": [],
        }
        problems = assert_task_coverage(splits)
        assert len(problems) == 1
        assert "只出现在 val/test" in problems[0]

    def test_no_problems_when_covered(self, fixture_records: list[dict]) -> None:
        splits, _ = split_records(fixture_records, SplitConfig())
        assert assert_task_coverage(splits) == []


class TestWriteSplits:
    def test_writes_three_files_and_manifest(
        self, tmp_path: Any, fixture_records: list[dict]
    ) -> None:
        splits, report = split_records(fixture_records, SplitConfig())
        paths = write_splits(splits, report, tmp_path)

        assert set(paths) == {"train", "val", "test", "manifest"}
        for name in SPLITS:
            assert paths[name].exists()
            assert len(read_jsonl(paths[name])) == report.counts[name]

        import json

        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        assert manifest["counts"] == report.counts
        assert "fingerprints" in manifest
        assert "seed" in manifest

    def test_load_and_split_roundtrip(
        self, tmp_path: Any, fixture_path: Any
    ) -> None:
        splits, report, paths = load_and_split(
            [fixture_path], SplitConfig(), tmp_path / "out"
        )
        assert sum(report.counts.values()) == 60
        reloaded = read_jsonl(paths["train"])
        assert len(reloaded) == report.counts["train"]

    def test_multiple_input_files(self, tmp_path: Any, small_fixture_records: list[dict]) -> None:
        from qboss_training.utils.io import write_jsonl

        left = tmp_path / "left.jsonl"
        right = tmp_path / "right.jsonl"
        write_jsonl(left, small_fixture_records[:6])
        write_jsonl(right, small_fixture_records[6:])

        _, report, _ = load_and_split(
            [left, right], SplitConfig(leakage_threshold=0), tmp_path / "out"
        )
        assert sum(report.counts.values()) == 12


class TestDescribeSplits:
    def test_renders_each_split(self, fixture_records: list[dict]) -> None:
        splits, _ = split_records(fixture_records, SplitConfig())
        text = describe_splits(splits)
        for name in SPLITS:
            assert name in text
        assert "event_eval" in text
