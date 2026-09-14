"""数据集切分器：离线、确定性、防泄漏。

三个必须解决的问题
------------------
1. **可复现**：同一份输入 + 同一种子 → 完全相同的切分（按 id 排序后再洗牌，
   不依赖文件顺序、不依赖 dict 迭代顺序）。
2. **防泄漏**：训练集与验证/测试集之间不得存在近似重复样本，
   否则评测分数会虚高。这里在切分后跨 split 做 Jaccard 检测并把命中的
   样本从 val/test 移到 train（安全方向），同时报告。
3. **分层**：默认按 task 分层；也可叠加 direction / event_kind 等键，
   避免某一类样本在小验证集里完全缺失。
"""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..config import SplitConfig
from ..contracts import TASK_NAMES
from ..utils.dedup import Deduplicator, jaccard, serialized_text, shingles, stable_hash
from ..utils.io import read_jsonl, write_json, write_jsonl
from ..utils.secrets import utc_now_iso
from ..validators import validate_record

LOGGER = logging.getLogger("qboss_training.splitter")

SPLITS: tuple[str, ...] = ("train", "val", "test")


@dataclass
class SplitReport:
    """切分结果与体检报告。"""

    counts: dict[str, int] = field(default_factory=dict)
    by_task: dict[str, dict[str, int]] = field(default_factory=dict)
    duplicates_removed: int = 0
    leakage_moved: int = 0
    leakage_details: list[dict[str, Any]] = field(default_factory=list)
    invalid_records: int = 0
    dropped_ids: list[str] = field(default_factory=list)
    fingerprints: dict[str, str] = field(default_factory=dict)
    seed: int = 0
    stratify_by: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "by_task": {key: dict(value) for key, value in self.by_task.items()},
            "duplicates_removed": self.duplicates_removed,
            "leakage_moved": self.leakage_moved,
            "leakage_details": self.leakage_details[:100],
            "invalid_records": self.invalid_records,
            "dropped_ids": self.dropped_ids[:100],
            "fingerprints": dict(self.fingerprints),
            "seed": self.seed,
            "stratify_by": list(self.stratify_by),
            "created_at": self.created_at,
        }


def _stratum_key(record: Mapping[str, Any], keys: Sequence[str]) -> str:
    """构造分层键。"""
    parts: list[str] = []
    for key in keys:
        if key == "task":
            parts.append(str(record.get("task", "?")))
        elif key == "event_kind":
            parts.append(str((record.get("meta") or {}).get("event_kind", "?")))
        elif key == "direction":
            value = (record.get("output") or {}).get("direction")
            if value is None:
                emotions = (record.get("input") or {}).get("active_emotions") or []
                value = emotions[0].get("direction") if emotions else "?"
            parts.append(str(value))
        else:
            parts.append(str(_deep_get(record, key, "?")))
    return "|".join(parts)


def _deep_get(node: Any, dotted: str, default: Any) -> Any:
    current = node
    for part in dotted.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return default
    return current


def _allocate(total: int, ratios: Sequence[float]) -> list[int]:
    """按比例分配整数，最大余数法，保证总和精确。"""
    raw = [total * ratio for ratio in ratios]
    floors = [int(value) for value in raw]
    remainder = total - sum(floors)
    order = sorted(
        range(len(raw)), key=lambda index: (-(raw[index] - floors[index]), index)
    )
    for index in order[:remainder]:
        floors[index] += 1
    return floors


def prepare_records(
    records: Iterable[Mapping[str, Any]],
    *,
    validate: bool = True,
    near_threshold: float = 0.95,
    require_input_schema: bool = True,
) -> tuple[list[dict[str, Any]], int, int, list[str]]:
    """校验 + 去重。返回 ``(保留, 去重数, 非法数, 被丢弃 id)``。"""
    kept: list[dict[str, Any]] = []
    dropped_ids: list[str] = []
    duplicates = 0
    invalid = 0
    deduper = Deduplicator(near_threshold=near_threshold or None)

    for index, record in enumerate(records):
        record_id = str(record.get("id") or f"idx_{index}")
        if validate:
            report = validate_record(
                record, index=index, require_input_schema=require_input_schema
            )
            if not report.ok:
                invalid += 1
                dropped_ids.append(record_id)
                continue

        task = str(record.get("task", ""))
        model_input = record.get("input") or {}
        ok, _hit = deduper.accept(task, model_input, record_id)
        if not ok:
            duplicates += 1
            continue
        kept.append(dict(record))

    return kept, duplicates, invalid, dropped_ids


def detect_leakage(
    train: Sequence[Mapping[str, Any]],
    others: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.9,
) -> list[tuple[int, str, float]]:
    """找出 ``others`` 中与 ``train`` 近似重复的样本。

    Returns:
        ``[(others 中的下标, train 中命中的 id, 相似度), ...]``
    """
    if threshold <= 0 or not train:
        return []

    index: dict[str, list[tuple[str, frozenset[str]]]] = defaultdict(list)
    for record in train:
        record_id = str(record.get("id", "?"))
        key = str(record.get("task", ""))
        index[key].append(
            (record_id, shingles(serialized_text(key, record.get("input") or {})))
        )

    hits: list[tuple[int, str, float]] = []
    for position, record in enumerate(others):
        key = str(record.get("task", ""))
        candidate = shingles(serialized_text(key, record.get("input") or {}))
        if not candidate:
            continue
        best_id = ""
        best_score = 0.0
        for record_id, reference in index.get(key, ()):
            score = jaccard(candidate, reference)
            if score > best_score:
                best_score = score
                best_id = record_id
        if best_score >= threshold:
            hits.append((position, best_id, best_score))
    return hits


def resolve_leakage(
    train: list[dict[str, Any]],
    val: list[dict[str, Any]],
    test: list[dict[str, Any]],
    *,
    threshold: float = 0.9,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
]:
    """把 val/test 中与 train 近似重复的样本移入 train（安全方向）。

    Returns:
        ``(train, val, test, 移动条数, 明细)``
    """
    details: list[dict[str, Any]] = []
    moved = 0

    hits_val = detect_leakage(train, val, threshold=threshold)
    if hits_val:
        positions = {position for position, _, _ in hits_val}
        for position, target_id, score in hits_val:
            details.append(
                {
                    "from": "val",
                    "id": val[position].get("id"),
                    "matched_train_id": target_id,
                    "similarity": round(score, 4),
                }
            )
        train.extend(val[position] for position in sorted(positions))
        val = [item for index, item in enumerate(val) if index not in positions]
        moved += len(positions)

    hits_test = detect_leakage(train, test, threshold=threshold)
    if hits_test:
        positions = {position for position, _, _ in hits_test}
        for position, target_id, score in hits_test:
            details.append(
                {
                    "from": "test",
                    "id": test[position].get("id"),
                    "matched_train_id": target_id,
                    "similarity": round(score, 4),
                }
            )
        train.extend(test[position] for position in sorted(positions))
        test = [item for index, item in enumerate(test) if index not in positions]
        moved += len(positions)

    return train, val, test, moved, details


def split_records(
    records: Sequence[Mapping[str, Any]],
    config: SplitConfig,
    *,
    validate: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], SplitReport]:
    """执行确定性分层切分。"""
    report = SplitReport(seed=config.seed, stratify_by=tuple(config.stratify_by))
    ratios = config.ratios()

    prepared, duplicates, invalid, dropped_ids = prepare_records(
        records,
        validate=validate,
        near_threshold=config.dedupe_near_threshold,
    )
    report.duplicates_removed = duplicates
    report.invalid_records = invalid
    report.dropped_ids = dropped_ids

    stratify_keys = config.stratify_by or ("task",)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in prepared:
        buckets[_stratum_key(record, stratify_keys)].append(record)

    splits: dict[str, list[dict[str, Any]]] = {name: [] for name in SPLITS}
    for stratum in sorted(buckets):
        items = sorted(buckets[stratum], key=lambda item: str(item.get("id", "")))
        rng = random.Random(f"{config.seed}:{stratum}")
        rng.shuffle(items)
        counts = _allocate(len(items), ratios)
        cursor = 0
        for name, count in zip(SPLITS, counts):
            splits[name].extend(items[cursor : cursor + count])
            cursor += count

    # 跨 split 泄漏处理
    if config.leakage_threshold and config.leakage_threshold > 0:
        train, val, test, moved, details = resolve_leakage(
            splits["train"],
            splits["val"],
            splits["test"],
            threshold=config.leakage_threshold,
        )
        splits["train"], splits["val"], splits["test"] = train, val, test
        report.leakage_moved = moved
        report.leakage_details = details
        if moved:
            LOGGER.info("跨 split 泄漏检测：%d 条近似重复样本已移入 train", moved)

    # 每个 split 内部排序，保证输出稳定
    for name in SPLITS:
        splits[name].sort(key=lambda item: str(item.get("id", "")))
        report.counts[name] = len(splits[name])

    for name in SPLITS:
        for record in splits[name]:
            task = str(record.get("task", "?"))
            bucket = report.by_task.setdefault(
                task, {split: 0 for split in SPLITS}
            )
            bucket[name] += 1

    for name in SPLITS:
        report.fingerprints[name] = stable_hash(
            [item.get("id") for item in splits[name]], length=16
        )

    return splits, report


def write_splits(
    splits: Mapping[str, Sequence[Mapping[str, Any]]],
    report: SplitReport,
    output_dir: str | Path,
    *,
    manifest_name: str = "split_manifest.json",
) -> dict[str, Path]:
    """落盘三个 split 与 manifest。返回文件路径映射。"""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in SPLITS:
        path = directory / f"{name}.jsonl"
        write_jsonl(path, splits.get(name, []))
        paths[name] = path
    manifest_path = directory / manifest_name
    write_json(manifest_path, report.to_dict())
    paths["manifest"] = manifest_path
    return paths


def describe_splits(splits: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """人类可读的切分摘要。"""
    lines: list[str] = []
    for name in SPLITS:
        items = splits.get(name, [])
        task_counts: dict[str, int] = defaultdict(int)
        for record in items:
            task_counts[str(record.get("task", "?"))] += 1
        detail = ", ".join(f"{key}={value}" for key, value in sorted(task_counts.items()))
        lines.append(f"  {name:<5} {len(items):>5} 条  ({detail})")
    return "\n".join(lines)


def load_and_split(
    input_paths: Sequence[str | Path],
    config: SplitConfig,
    output_dir: str | Path,
    *,
    validate: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], SplitReport, dict[str, Path]]:
    """读取一个或多个 JSONL → 校验去重 → 切分 → 落盘。"""
    collected: list[dict[str, Any]] = []
    for path in input_paths:
        records = read_jsonl(path)
        LOGGER.info("读取 %s：%d 条", path, len(records))
        collected.extend(records)

    splits, report = split_records(collected, config, validate=validate)
    paths = write_splits(splits, report, output_dir)
    return splits, report, paths


def assert_task_coverage(
    splits: Mapping[str, Sequence[Mapping[str, Any]]], tasks: Sequence[str] = TASK_NAMES
) -> list[str]:
    """检查 train 是否覆盖了所有任务（漏掉整个任务的训练集会静默失败）。"""
    problems: list[str] = []
    train_tasks = {str(record.get("task")) for record in splits.get("train", [])}
    for task in tasks:
        if task not in train_tasks:
            present = any(
                str(record.get("task")) == task
                for name in SPLITS
                for record in splits.get(name, [])
            )
            if present:
                problems.append(f"任务 {task} 只出现在 val/test，train 中没有样本")
            else:
                problems.append(f"任务 {task} 完全没有样本")
    return problems
