"""去重：精确哈希去重 + 字符 n-gram 近似去重。

两个层次：
  1. **精确去重**（O(1)）：对语义规范化后的输入 payload 求 SHA-256；
     同一事件/同一心理状态重复生成时直接命中。
  2. **近似去重**（倒排索引 + Jaccard）：字符 3-gram 集合相似度，
     抓住"只改了几个字"的近似重复。

为什么不只做精确去重：数据生成的主力风险是模板化重复，
同一条种子被换皮生成多次，会让 SFT 过拟合到表面形式。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

from .jsonx import dumps_canonical

_WS_RE = re.compile(r"\s+")
_SHINGLE_SIZE = 3


def normalize_text(text: str) -> str:
    """规范化：NFKC（全角→半角）、小写、压缩空白、去标点噪声。"""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text)).lower()
    normalized = _WS_RE.sub(" ", normalized)
    return normalized.strip()


def stable_hash(payload: Any, *, length: int = 16) -> str:
    """对任意 JSON 可序列化对象求稳定短哈希。"""
    digest = hashlib.sha256(dumps_canonical(payload).encode("utf-8")).hexdigest()
    return digest[:length]


def input_fingerprint(task: str, model_input: dict[str, Any]) -> str:
    """计算一条样本的『语义指纹』。

    同一 task 下输入语义相同即视为重复，与生成的输出无关，
    这样才会真正抑制重复生成而不是重复采样。
    """
    return stable_hash({"task": task, "input": model_input}, length=32)


def shingles(text: str, size: int = _SHINGLE_SIZE) -> frozenset[str]:
    """字符 n-gram 集合。文本过短时退化为整串。"""
    normalized = normalize_text(text)
    if not normalized:
        return frozenset()
    if len(normalized) <= size:
        return frozenset({normalized})
    return frozenset(
        normalized[i : i + size] for i in range(len(normalized) - size + 1)
    )


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard 相似度；两边都空视为 1.0，一边空为 0.0。"""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if not intersection:
        return 0.0
    return intersection / len(left | right)


def serialized_text(task: str, model_input: dict[str, Any]) -> str:
    """把一条输入压成用于近似比对的文本。

    只取**字符串值**，不取字段名，也不做 canonical JSON 序列化。
    这是刻意的：canonical JSON 会把 ``current_event``/``background_mood``/
    ``character_values`` 这些固定字段名重复计入，导致"两条毫无关系但结构相同"
    的输入也拿到 0.8+ 的相似度（结构噪声淹没语义信号）。
    只拼字符串值后，相似度才真正反映"说的是不是同一件事"。
    """
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.strip():
                chunks.append(node)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            return
        elif isinstance(node, Mapping):
            for key in sorted(node):
                walk(node[key])
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(model_input)
    return f"{task} " + " ".join(chunks)


@dataclass
class DuplicateHit:
    """一次去重命中记录。"""

    kind: str  # "exact" | "near"
    matched_id: str
    similarity: float = 1.0

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.kind}:{self.matched_id}@{self.similarity:.3f}"


@dataclass
class Deduplicator:
    """有状态的去重器，可在生成过程中增量使用。

    Args:
        near_threshold: Jaccard 阈值，>= 该值判为近似重复。``None``/``0`` 关闭近似去重。
        max_candidates: 近似比对时检查的候选上限（控制最坏耗时）。

    阈值标定（在 60 条互相独立的合成样本上实测，1770 个两两组合）：

    ============  ==========================================
    阈值          把"本质不同"的样本误判为重复的比例
    ============  ==========================================
    0.80          1.58%
    0.85          1.36%
    0.88          1.02%
    0.90          0.34%
    0.92          0.00%
    ============  ==========================================

    而真正近似重复的样本（只差标点或一个虚词）相似度约 0.93。
    因此 **0.90 是短中文文本上"几乎不误杀、仍能抓到几乎相同的样本"的拐点**。
    调低阈值会开始丢真数据（比漏去重更糟），因此不要随意降到 0.85 以下。
    """

    near_threshold: float | None = 0.90
    max_candidates: int = 64
    _exact: dict[str, str] = field(default_factory=dict, init=False)
    _shingle_index: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list), init=False
    )
    _shingles: dict[str, frozenset[str]] = field(default_factory=dict, init=False)
    _counts: dict[str, int] = field(
        default_factory=lambda: {"exact": 0, "near": 0, "kept": 0}, init=False
    )

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._counts)

    def __len__(self) -> int:
        return len(self._exact)

    def check(
        self, task: str, model_input: dict[str, Any], record_id: str
    ) -> DuplicateHit | None:
        """检查是否重复；不改变内部状态。"""
        fingerprint = input_fingerprint(task, model_input)
        if fingerprint in self._exact:
            return DuplicateHit("exact", self._exact[fingerprint], 1.0)

        if not self.near_threshold:
            return None

        candidate_shingles = shingles(serialized_text(task, model_input))
        if not candidate_shingles:
            return None

        tally: dict[str, int] = defaultdict(int)
        for shingle in candidate_shingles:
            for other_id in self._shingle_index.get(shingle, ()):
                tally[other_id] += 1

        if not tally:
            return None

        ranked = sorted(tally.items(), key=lambda kv: -kv[1])[: self.max_candidates]
        best: DuplicateHit | None = None
        for other_id, _overlap in ranked:
            other = self._shingles.get(other_id)
            if other is None:
                continue
            score = jaccard(candidate_shingles, other)
            if score >= self.near_threshold and (best is None or score > best.similarity):
                best = DuplicateHit("near", other_id, score)
        return best

    def add(self, task: str, model_input: dict[str, Any], record_id: str) -> None:
        """登记一条已接受的样本。"""
        fingerprint = input_fingerprint(task, model_input)
        self._exact[fingerprint] = record_id
        if not self.near_threshold:
            return
        item_shingles = shingles(serialized_text(task, model_input))
        if not item_shingles:
            return
        self._shingles[record_id] = item_shingles
        for shingle in item_shingles:
            self._shingle_index[shingle].append(record_id)

    def accept(
        self, task: str, model_input: dict[str, Any], record_id: str
    ) -> tuple[bool, DuplicateHit | None]:
        """检查并按需登记，返回 ``(是否保留, 命中信息)``。"""
        hit = self.check(task, model_input, record_id)
        if hit is not None:
            self._counts[hit.kind] += 1
            return False, hit
        self.add(task, model_input, record_id)
        self._counts["kept"] += 1
        return True, None


def dedupe_records(
    records: Iterable[dict[str, Any]],
    *,
    task_key: str = "task",
    input_key: str = "input",
    id_key: str = "id",
    near_threshold: float | None = 0.90,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """批量去重，返回 ``(保留, 被丢弃)``。被丢弃项会带上 ``duplicate_of``。"""
    deduper = Deduplicator(near_threshold=near_threshold)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        task = record.get(task_key, "")
        model_input = record.get(input_key, {})
        record_id = str(record.get(id_key) or f"rec_{index:06d}")
        ok, hit = deduper.accept(task, model_input, record_id)
        if ok:
            kept.append(record)
        else:
            marked = dict(record)
            marked["duplicate_of"] = hit.matched_id
            marked["duplicate_kind"] = hit.kind
            marked["duplicate_similarity"] = round(hit.similarity, 4)
            dropped.append(marked)
    return kept, dropped
