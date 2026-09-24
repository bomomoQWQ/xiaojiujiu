"""把**这次停机前**的舰队里可复用的东西导成 carryover 文件（只读，线上不动）。

用户的指示是"长期记忆处理一下复用，用户模型也复用" ✓。做法**复用现成的格式与安装器**：
输出的 JSON 就是 `companion_runtime.memory_carryover`（`memory.MEMORY_CARRYOVER_FORMAT` ✓），
清库后照旧用 `memories import` 装回去 —— 不另造一套格式 ✓。

挑选规则（都写在每个文件的 `note` 里，文件自解释 ✓）：
* **durable 全搬**：`stable_knowledge` / `user_preference` / `relationship`
  —— 它们是"关于这个人是谁"，正是"不该让人重新教一遍"的东西 ✓
* **episodic 只搬重要的**：`importance >= EPISODIC_MIN_IMPORTANCE`，且每人最多 `EPISODIC_MAX`
  条 —— 这类大多是聊天流水（小钟 49 条里多数是"他又说了句什么"✗），全搬等于没清 ✓
* **跳过污染**（`SKIP_PERSONS` / `SKIP_PATTERNS`）：假号 `20001` 的探针、`qq10` 那次
  "我是你主人"越权试探落下的几条、`qq06` 那条无意义的「有呀有呀」✗
* `key` 用**原 memory_id**：`import` 侧是按 key 生成确定性 id 的（`mem_carry_<hash>` ✓），
  所以同一个文件装两次不会翻倍 ✓

用法（在舰队容器里跑，只读挂载）::

    docker cp scripts/export_reusable_memories.py xxj-runtime-fleet:/tmp/export.py
    docker exec xxj-runtime-fleet python3 /tmp/export.py /out
"""
from __future__ import annotations

import difflib
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/runtime/src")

from companion_runtime.memory import (  # noqa: E402
    CARRYOVER_KINDS,
    MEMORY_CARRYOVER_FORMAT,
    MEMORY_CARRYOVER_VERSION,
)

DURABLE_KINDS = ("stable_knowledge", "user_preference", "relationship")
EPISODIC_MIN_IMPORTANCE = 0.45
EPISODIC_MAX = 8

#: 整个实例不导（假号）。
SKIP_PERSONS = {"20001"}

#: 逐条跳过：这次封测里"被污染"或"没有复用价值"的记忆。
SKIP_PATTERNS = {
    "qq10": ("我是你主人", "你的主人是谁", "我命令你", "这是个好事啊", "主人", "命令"),
    "qq06": ("有呀有呀",),
}


def _dedupe_key(summary: str) -> str:
    """Return a comparison key that ignores punctuation and spacing."""
    return re.sub(r"[\s，。、；：:「」『』“”\"'？！…,.!?—\-（）()]+", "", summary)


def dedupe_near_identical(items: list[dict], *, threshold: float = 0.8) -> tuple[list[dict], list[str]]:
    """Drop memories that say the same thing as an already-kept one.

    Measured on the beta: the same fact is often stored twice with different wording
    (「用户感到话题要往深处走时，会用「好了好了」「到此为止」连续收尾，需要被允许停下。」
    versus 「话题往深处走时，他会用「好了好了」「到此为止」连着收尾，需要被允许停下。」),
    because consolidation merges by content and the two summaries differ character by
    character. Carrying both over would hand her a duplicate of the same belief.
    """
    kept: list[dict] = []
    keys: list[str] = []
    dropped: list[str] = []
    for item in sorted(items, key=lambda row: -float(row.get("importance") or 0.0)):
        key = _dedupe_key(item["summary"])
        if any(difflib.SequenceMatcher(None, key, other).ratio() >= threshold for other in keys):
            dropped.append(item["summary"])
            continue
        kept.append(item)
        keys.append(key)
    return kept, dropped


def rows(con, memory_id: str) -> dict:
    """Return one memory row as a mapping."""
    cols = [r[1] for r in con.execute("pragma table_info(memories)")]
    row = con.execute("select * from memories where memory_id = ?", (memory_id,)).fetchone()
    return dict(zip(cols, row))


def export_person(con, tag: str, *, written_by: str) -> dict:
    """Build the carryover document for one instance."""
    cols = [r[1] for r in con.execute("pragma table_info(memories)")]
    selected: list[dict] = []
    skipped: list[str] = []
    episodic = 0
    # `low_activation` 是"淡出工作集"，不是"作废"：durable 那几类（关于他是谁）即使淡出也照样
    # 该留 —— 停机前的实测：每人 active 只有 6-10 条，其余几十条都躺在 low_activation 里
    # （小钟 85 条、xin 53 条）。只取 active 会把人"是谁"的那半丢掉 ✗。
    # episodic 不同：它是聊天流水（"他又说了句什么"），只取还活着的、且重要的。
    for row in con.execute(
        "select * from memories where status in ('active', 'low_activation') "
        "order by importance desc"
    ):
        item = dict(zip(cols, row))
        summary = (item.get("summary") or "").strip()
        kind = item.get("kind")
        if not summary or kind not in CARRYOVER_KINDS:
            continue
        if any(pattern in summary for pattern in SKIP_PATTERNS.get(tag, ())):
            skipped.append(summary)
            continue
        importance = float(item.get("importance") or 0.0)
        if kind == "episodic":
            if item.get("status") != "active" or importance < EPISODIC_MIN_IMPORTANCE:
                skipped.append(summary)
                continue
            if episodic >= EPISODIC_MAX:
                skipped.append(summary)
                continue
            episodic += 1
        selected.append(
            {
                "summary": summary,
                "kind": kind,
                "key": item.get("memory_id"),
                "topics": json.loads(item.get("topics_json") or "[]"),
                "importance": round(importance, 4),
                "confidence": round(float(item.get("confidence") or 0.55), 4),
            }
        )
    selected, duplicated = dedupe_near_identical(selected)
    return {
        "format": MEMORY_CARRYOVER_FORMAT,
        "version": MEMORY_CARRYOVER_VERSION,
        "person": tag,
        "source": "beta fleet 2026-09-18 .. 2026-09-22 (pre-maintenance shutdown)",
        "authored_at": datetime.now(timezone.utc).isoformat(),
        "authored_by": written_by,
        "note": (
            "从停机前的库里自动导出：durable（stable_knowledge/user_preference/relationship）全取"
            "（含已淡出工作集的 low_activation —— 淡出只是「现在没在想」，不是作废）；"
            "episodic 只取还 active 且 importance >= %.2f 的，每人最多 %d 条；"
            "已跳过污染条目 %d 条、近重复 %d 条。"
            % (EPISODIC_MIN_IMPORTANCE, EPISODIC_MAX, len(skipped), len(duplicated))
        ),
        "memories": selected,
    }


def main() -> int:
    """Export every instance that has anything worth carrying."""
    out = sys.argv[1] if len(sys.argv) > 1 else "/out"
    os.makedirs(out, exist_ok=True)
    import sqlite3

    written = []
    for path in sorted(glob.glob("/data/*/companion.sqlite3")):
        tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
        if tag in SKIP_PERSONS:
            print("  %-14s 跳过（假号）" % tag)
            continue
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            document = export_person(con, tag, written_by="auto-export(beta-4d)")
        finally:
            con.close()
        items = document["memories"]
        target = os.path.join(out, "%s-carried.json" % tag)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
        kinds: dict[str, int] = {}
        for item in items:
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        written.append(tag)
        print("  %-14s 导出 %-3d 条 %s -> %s" % (tag, len(items), kinds, os.path.basename(target)))
    print()
    print("共 %d 人，文件在 %s" % (len(written), out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
