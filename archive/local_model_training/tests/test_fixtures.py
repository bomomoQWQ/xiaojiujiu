"""合成夹具测试。

夹具是"离线验证校验器"的基础设施，因此它自己必须可靠：
**正常夹具必须全部通过校验，`include_invalid=True` 的样本必须全部被抓到。**
如果这条不成立，所有"用夹具验证校验器"的测试都会变成假绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qboss_training.fixtures import (
    build_fixture_records,
    main,
    make_emotion_explain_record,
    make_event_eval_record,
    write_fixture,
)
from qboss_training.utils.dedup import Deduplicator
from qboss_training.utils.io import read_jsonl
from qboss_training.validators import validate_record, validate_records


class TestCleanFixtures:
    def test_all_records_pass_validation(self) -> None:
        records = build_fixture_records(event_eval_count=30, emotion_explain_count=30)
        report = validate_records(records)
        assert report.failed == 0, report.to_dict()["failures"][:2]

    def test_covers_both_tasks(self) -> None:
        records = build_fixture_records(event_eval_count=5, emotion_explain_count=7)
        tasks = [record["task"] for record in records]
        assert tasks.count("event_eval") == 5
        assert tasks.count("emotion_explain") == 7

    def test_ids_are_unique(self) -> None:
        records = build_fixture_records(event_eval_count=30, emotion_explain_count=30)
        ids = [record["id"] for record in records]
        assert len(ids) == len(set(ids))

    def test_deterministic_for_same_seed(self) -> None:
        first = build_fixture_records(seed=99)
        second = build_fixture_records(seed=99)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_different_seed_changes_content(self) -> None:
        first = build_fixture_records(seed=1)
        second = build_fixture_records(seed=2)
        assert json.dumps(first, sort_keys=True) != json.dumps(second, sort_keys=True)

    def test_event_eval_covers_all_directions(self) -> None:
        records = [
            record
            for record in build_fixture_records(event_eval_count=30)
            if record["task"] == "event_eval"
        ]
        directions = {record["output"]["direction"] for record in records}
        assert directions == {"+", "-", "0", "+-"}

    def test_emotion_explain_covers_conflict_and_no_conflict(self) -> None:
        records = [
            record
            for record in build_fixture_records(emotion_explain_count=30)
            if record["task"] == "emotion_explain"
        ]
        flags = {record["input"]["conflict_present"] for record in records}
        assert flags == {True, False}

    def test_emotion_explain_covers_intensity_range(self) -> None:
        records = [
            record
            for record in build_fixture_records(emotion_explain_count=30)
            if record["task"] == "emotion_explain"
        ]
        intensities = [record["input"]["active_emotions"][0]["intensity"] for record in records]
        assert min(intensities) < 0.45  # 覆盖"轻微"
        assert max(intensities) > 0.7  # 覆盖"较强"

    def test_evidence_is_grounded_in_event_text(self) -> None:
        """evidence 必须是事件文本的片段，否则会误触发 EV06。"""
        for index in range(30):
            record = make_event_eval_record(index, __import__("random").Random(index))
            evidence = record["output"]["evidence"]
            assert evidence in record["input"]["current_event"]["text"]

    def test_metadata_records_provenance(self) -> None:
        records = build_fixture_records(event_eval_count=3)
        meta = records[0]["meta"]
        assert meta["source"] == "fixture"
        assert meta["scenario_id"]
        assert meta["event_kind"]

    def test_semantic_distinctness_is_high_enough(self) -> None:
        """夹具样本必须语义互不相同，否则会被去重器吃掉，
        "用夹具测试去重/切分"就变成了空测。"""
        records = build_fixture_records(event_eval_count=30, emotion_explain_count=30)
        deduper = Deduplicator()  # 默认阈值
        kept = sum(
            1
            for record in records
            if deduper.accept(record["task"], record["input"], record["id"])[0]
        )
        assert kept >= int(len(records) * 0.85), f"仅保留 {kept}/{len(records)}"


class TestInvalidFixtures:
    """`include_invalid=True` 必须**确定性地**产出可被校验器抓到的违规样本。"""

    def test_event_eval_invalid_is_caught(self) -> None:
        record = make_event_eval_record(7, __import__("random").Random(7), make_invalid=True)
        report = validate_record(record)
        assert not report.ok
        assert any(item.code == "EV02_DIRECTION_SIGNAL" for item in report.violations)

    def test_emotion_explain_invalid_is_caught(self) -> None:
        record = make_emotion_explain_record(
            7, __import__("random").Random(7), make_invalid=True
        )
        report = validate_record(record)
        assert not report.ok
        assert any(item.code == "EX02_DIRECTION_FLIP" for item in report.violations)

    def test_invalid_variant_differs_from_clean_variant(self) -> None:
        """违规样本与正常样本必须真的不同，否则测试会假绿。"""
        import random

        clean = make_event_eval_record(7, random.Random(7))
        broken = make_event_eval_record(7, random.Random(7), make_invalid=True)
        assert clean["output"] != broken["output"]
        assert validate_record(clean).ok
        assert not validate_record(broken).ok

    def test_invalid_flag_can_be_off(self) -> None:
        import random

        record = make_event_eval_record(7, random.Random(7), make_invalid=False)
        assert validate_record(record).ok

    def test_batch_with_invalid_has_failures(self) -> None:
        records = build_fixture_records(
            event_eval_count=8, emotion_explain_count=8, include_invalid=True
        )
        report = validate_records(records)
        assert report.failed == 2  # 每个任务各一条
        codes = set(report.violation_counts)
        assert "EV02_DIRECTION_SIGNAL" in codes
        assert "EX02_DIRECTION_FLIP" in codes


class TestWriteFixture:
    def test_writes_jsonl(self, tmp_path: Path) -> None:
        path = write_fixture(tmp_path / "f.jsonl", event_eval_count=4, emotion_explain_count=3)
        assert path.exists()
        rows = read_jsonl(path)
        assert len(rows) == 7

    def test_output_is_sorted_by_id(self, tmp_path: Path) -> None:
        path = write_fixture(tmp_path / "f.jsonl")
        rows = read_jsonl(path)
        assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)

    def test_cli_make(self, tmp_path: Path) -> None:
        target = tmp_path / "cli.jsonl"
        exit_code = main(
            ["make", "--output", str(target), "--event-eval", "3", "--emotion-explain", "2"]
        )
        assert exit_code == 0
        assert len(read_jsonl(target)) == 5

    def test_cli_include_invalid(self, tmp_path: Path) -> None:
        target = tmp_path / "cli_bad.jsonl"
        main(
            [
                "make",
                "--output", str(target),
                "--event-eval", "8",
                "--emotion-explain", "8",
                "--include-invalid",
            ]
        )
        report = validate_records(read_jsonl(target))
        assert report.failed == 2


class TestFixtureIsNotTrainingData:
    def test_documented_as_test_only(self) -> None:
        """夹具句式极有限，模块文档必须明确警告不能当训练数据。"""
        from qboss_training import fixtures

        doc = fixtures.__doc__ or ""
        assert "不能" in doc
        assert "训练数据" in doc
