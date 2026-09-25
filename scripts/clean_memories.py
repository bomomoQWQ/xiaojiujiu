"""洗记忆层：把近似重复的记忆归档，并修正"谁说的话"被归错类的那批。

为什么需要（2026-09-25 实测）：某人的 37 条长期记忆里，光是「他很在意自己说过的话被记住 /
记岔了就是不在乎」这一件事就占了约 10 条 —— 措辞不同、内容同义 ✗。白占记忆额度，还会把
【必要记忆】冲淡。另外有一条 `user_preference 1.00` = 「我喜欢你」✗ —— 那是**她自己说过的话**，
被存成了"关于他的偏好"，于是她后来会把自己的话当成他的事实复述。

两条规则：
  R1 去重：同一个人，按 importance 降序（同分取更长的），与已保留的任意一条相似度
          ≥ SIMILARITY 就归档（`status='archived'`，遗忘链的终态，不会再进注入块）。
  R2 归因：`kind` 是 user_preference / stable_knowledge，但 summary 以「我」开头 →
          改判 `relationship`（那是"我们之间发生过的事"，不是"他是谁"）。

默认**只打印计划不落库**；加 `--apply` 才写。写入只动 `status`/`archived_at`/`kind`/`updated_at`，
不删任何行 ✓（想回滚就是把 status 改回 active）。

用法（在舰队容器里跑）::

    docker cp scripts/clean_memories.py xxj-runtime-fleet:/tmp/clean_memories.py
    docker exec xxj-runtime-fleet python3 /tmp/clean_memories.py            # dry-run
    docker exec xxj-runtime-fleet python3 /tmp/clean_memories.py --apply    # 落库
"""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

#: 相似度阈值。0.75 能合并"同一件事换了几种说法"，又不会误并两条真正不同的偏好。
SIMILARITY = 0.75

#: 语义分组的**兜底闸**：模型说这两条是同一件事，还得字面也够像才允许合并。
#: 为什么需要：实测这个模型会按**主题**归并（把「他两点五十下课」并进「他四点上课、五点二十下课」✗，
#: 把「他早餐吃小叉烧包」并进「他习惯洗漱完再吃早饭」✗），即使提示里已经给了反例 ✗。
#: 合并错等于丢事实，比留着重复更糟 ✓ 所以加一道字面闸：真·同义改写（约 0.6+）能过，
#: 同主题不同细节（约 0.3-0.5）过不了 ✓
SEMANTIC_GUARD = 0.55

#: 明确的归因修正：这类摘要**是她自己说的话**，却被存成了"关于他的知识"。
#: 只列确认过的，不做启发式 —— 第一人称摘要绝大多数其实是**引用他的话**
#: （「我是李司淮」= 他自己报的名字 ✓），用 `startswith("我")` 那种规则会大面积误伤 ✗。
EXPLICIT_RELABEL: dict[str, str] = {
    "我喜欢你": "relationship",
}

ARCHIVED = "archived"
LIVE_STATUSES = ("active", "low_activation")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def semantic_groups(summaries: list[str]) -> list[list[int]]:
    """让语义模型把"同一件事的不同说法"分到一组。

    为什么不能只靠字面相似度：那批重复的措辞差别很大 ——
    「他很在意自己说过的话被记住，记岔了会当成不在乎。」和
    「他记性很细，会数着我说过多少次，记岔了会当成不在乎。」字面相似度只有约 0.6 ✗，
    但它们是同一件事 ✓。字面去重只能捞到最像的那几条，语义去重才洗得动。

    模型只被要求**分组**，不许改写摘要；返回的是编号，落库前会原样打印出来给人看 ✓。
    """
    import urllib.request

    base = os.environ.get("CR_SEMANTIC_BASE_URL", "").rstrip("/")
    key = os.environ.get("CR_SEMANTIC_API_KEY", "")
    model = os.environ.get("CR_SEMANTIC_MODEL", "")
    if not (base and key and model):
        raise SystemExit("缺少 CR_SEMANTIC_* 环境变量，无法做语义分组")

    listing = "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(summaries))
    prompt = (
        "下面是同一个人的记忆摘要，每行一条带编号。请把**说的是同一件事**的条目分组。\n\n"
        "判断标准（很严）：合并后**不能丢掉任何一条具体信息**。"
        "只允许合并**同一个事实的两种说法**（详略不同、措辞不同，指向的仍是同一件事）。\n"
        "示例 —— 该合并：\n"
        "  「他很在意自己说过的话被记住，记岔了会当成不在乎」≈「他记性很细，会数着我说过多少次，记岔了会当成不在乎」（同一件事）\n"
        "示例 —— 不该合并（主题相近但细节不同，合并会丢信息）：\n"
        "  ✗「他两点五十下课，上课时段不方便回消息」与「他早上要拿包赶公交，时间卡得很紧」是两件事\n"
        "  ✗「他早餐吃小叉烧包加鸡蛋」与「他习惯洗漱完再吃早饭」是两件事\n"
        "  ✗「他坐车喜欢靠窗」与「他坐公交车容易晕车」是两件事（一个偏好、一个症状）\n"
        "  ✗「他会用『喵』撒娇接话」与「他用『哭』『qwq』卖萌」是两种不同的说法方式\n\n"
        "只输出 JSON，形如 {\"groups\": [[1,5,9],[2,7]]}；没有该合并的就输出 {\"groups\": []}。"
        "**不要改写任何摘要文字**，只给编号。\n\n" + listing
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个严格的中文语义去重器，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.loads(response.read())
    text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
    match = re.search(r"\{.*\}", text, re.DOTALL)
    data = json.loads(match.group(0) if match else "{}")
    groups = []
    for group in data.get("groups") or []:
        numbers = [int(n) for n in group if isinstance(n, (int, float, str)) and str(n).isdigit()]
        if len(numbers) >= 2:
            groups.append(numbers)
    return groups


def clean_person(path: str, *, apply: bool, semantic: bool = False) -> dict:
    person = os.path.basename(os.path.dirname(path))
    connection = sqlite3.connect(path, timeout=20)
    connection.execute("pragma busy_timeout = 20000")
    rows = connection.execute(
        "select memory_id, kind, summary, importance from memories "
        "where status in (?, ?) and summary is not null and summary <> '' "
        "order by importance desc, length(summary) desc",
        LIVE_STATUSES,
    ).fetchall()

    #: 单链聚类：跟**已出现的任意一条**相似就归并。只跟"保留集"比对是不够的 ——
    #: 同一件事的十种说法是链式的（A≈B、B≈C，但 A 跟 C 差得远），只比保留项时会漏掉大半 ✗
    #: （2026-09-25 dry-run 实测：37 条里那一族 10 条只并掉 3 条）。
    clusters: list[list[str]] = []
    archived: list[tuple[str, str, str]] = []
    for memory_id, _kind, summary, _importance in rows:
        target = next(
            (cluster for cluster in clusters if any(similar(summary, m) >= SIMILARITY for m in cluster)),
            None,
        )
        if target is None:
            clusters.append([summary])
        else:
            archived.append((memory_id, summary, target[0]))
            target.append(summary)

    #: 语义分组（可选）：字面去重捞不到"同一件事换了好几种说法"的那批 ✓
    if semantic:
        by_id = {memory_id: summary for memory_id, _k, summary, _i in rows}
        seen_ids = {memory_id for memory_id, _s, _d in archived}
        for group in semantic_groups([summary for _m, _k, summary, _i in rows]):
            picked = [rows[i - 1] for i in group if 1 <= i <= len(rows)]
            if len(picked) < 2:
                continue
            keeper = picked[0][2]
            for memory_id, _kind, summary, _importance in picked[1:]:
                if memory_id in seen_ids or summary == keeper:
                    continue
                # 兜底闸：模型说是同一件事，还要字面也够像（挡掉"同主题不同事实"）
                if similar(summary, keeper) < SEMANTIC_GUARD:
                    continue
                archived.append((memory_id, by_id[memory_id], keeper))
                seen_ids.add(memory_id)

    relabels = [
        (memory_id, summary, EXPLICIT_RELABEL[summary])
        for memory_id, _kind, summary, _importance in rows
        if summary in EXPLICIT_RELABEL
    ]

    if apply:
        stamp = now()
        for memory_id, _summary, _dup in archived:
            connection.execute(
                "update memories set status = ?, updated_at = ?, archived_at = ? where memory_id = ?",
                (ARCHIVED, stamp, stamp, memory_id),
            )
        for memory_id, _summary, new_kind in relabels:
            connection.execute(
                "update memories set kind = ?, updated_at = ? where memory_id = ?",
                (new_kind, stamp, memory_id),
            )
        connection.commit()
    connection.close()

    return {
        "person": person,
        "scanned": len(rows),
        "kept": len(rows) - len(archived),
        "archived": archived,
        "relabels": relabels,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的落库（默认只打印计划）")
    parser.add_argument("--semantic", action="store_true", help="再加一轮语义去重（调语义模型分组）")
    parser.add_argument("--root", default="/data", help="实例目录（默认容器内 /data）")
    args = parser.parse_args()

    total_scanned = total_archived = total_relabeled = 0
    print("模式：%s%s" % (
        "**落库**" if args.apply else "dry-run（不写库）",
        " + 语义去重" if args.semantic else "（仅字面去重）",
    ))
    print()
    for path in sorted(glob.glob(os.path.join(args.root, "default-*/companion.sqlite3"))):
        result = clean_person(path, apply=args.apply, semantic=args.semantic)
        if not result["archived"] and not result["relabels"]:
            if result["scanned"]:
                print("%-42s %3d 条，无需处理 ✓" % (result["person"], result["scanned"]))
            continue
        print("%-42s %3d 条 -> 保留 %d" % (
            result["person"], result["scanned"], result["kept"]))
        for _mid, summary, dup_of in result["archived"]:
            print("    归档: %s" % summary[:64])
            print("      与: %s" % dup_of[:64])
        for _mid, summary, new_kind in result["relabels"]:
            print("    改判 %s: %s" % (new_kind, summary[:64]))
        total_scanned += result["scanned"]
        total_archived += len(result["archived"])
        total_relabeled += len(result["relabels"])
    print()
    print("合计：扫描 %d 条，归档重复 %d 条，改判 %d 条" % (
        total_scanned, total_archived, total_relabeled))
    if not args.apply and (total_archived or total_relabeled):
        print("要落库就再加 --apply")


if __name__ == "__main__":
    main()
