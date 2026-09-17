"""未完之事 vs 当前工作情境（"临时记忆"）：到底重不重？

三张表的设计分工（据 providers.py 的产出类型）：
  working_situation_items  <- 不是模型写的：ingest / 投递路径 upsert 的机制性事实，12h 过期
  unfinished_matters       <- 深刷新的 unfinished_matter_suggestions（模型写），priority/expire
  memories                 <- 深刷新的 memory_suggestions（长期）
所以"重叠"有两个可证伪的形态：
  ① 同一批 source_event_ids 被写成多件未完之事（事与事重）
  ② 同一件事同时出现在 situation 与 matters（表与表重）——按文本相似度查（中文用 bigram Jaccard）
"""
import datetime as dt
import glob
import json
import os
import re
import sqlite3

CST = dt.timedelta(hours=8)
NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
JACCARD = 0.35   # 经验阈值；只用来提示"疑似同一件事"，不做结论


def cst(t):
    if not t:
        return "-"
    try:
        return (dt.datetime.fromisoformat(str(t).replace("Z", "").replace("+00:00", ""))
                + CST).strftime("%m-%d %H:%M")
    except ValueError:
        return str(t)[:16]


def bigrams(text):
    clean = re.sub(r"[\s，。、？！！：；「」“”（）()\[\]…—\-]", "", text or "")
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    matters = con.execute(
        "select unfinished_id, title, priority, source_event_ids, status,"
        " created_at, expire_at from unfinished_matters where status='open'").fetchall()
    sits = con.execute(
        "select item_id, kind, content, salience, confidence, source_kind, source_id,"
        " status, expires_at, created_at from working_situation_items"
        " where status='active' order by created_at desc limit 25").fetchall()
    mems = con.execute("select count(*) from memories").fetchone()[0]
    con.close()

    print("=" * 96)
    print("用户 %s   open 未完之事 %d 件 | 活跃 situation %d 条 | memories %d 条" % (
        tag, len(matters), len(sits), mems))

    # ① 事与事：同一批 source_event_ids
    groups = {}
    for m in matters:
        try:
            key = frozenset(json.loads(m["source_event_ids"] or "[]"))
        except ValueError:
            key = frozenset()
        groups.setdefault(key, []).append(m)
    dup = {k: v for k, v in groups.items() if len(v) > 1 and k}
    print("  ① 同一 source_event_ids 下的多件未完之事: %d 组" % len(dup))
    for key, items in list(dup.items())[:4]:
        print("     source=%s" % sorted(key))
        for m in items:
            print("       [%.2f] %s" % (m["priority"], m["title"][:60]))
    # 按文本相似度再查一遍（source 可能不同但说的是同一件事）
    print("  ①b 文本疑似重复的未完之事对 (Jaccard>=%.2f):" % JACCARD)
    seen = set()
    hits = 0
    for i, a in enumerate(matters):
        ga = bigrams(a["title"])
        for b in matters[i + 1:]:
            if jaccard(ga, bigrams(b["title"])) >= JACCARD:
                hits += 1
                if hits <= 6:
                    print("       %.2f  %s" % (jaccard(ga, bigrams(b["title"])), a["title"][:44]))
                    print("             %s" % b["title"][:44])
                seen.add(a["unfinished_id"])
                seen.add(b["unfinished_id"])
    print("       共 %d 对，涉及 %d 件（占 %d 件的 %.0f%%）" % (
        hits, len(seen), len(matters), 100 * len(seen) / max(1, len(matters))))

    # ② 表与表：situation 内容 vs matters 标题
    if sits:
        print("  ② situation 条目（kind / 来源 / 过期）:")
        for s in sits[:8]:
            print("       %-6s %-12s exp=%s | %s" % (
                s["kind"], "%s:%s" % (s["source_kind"], str(s["source_id"])[:12]),
                cst(s["expires_at"]), (s["content"] or "")[:56]))
        pair = 0
        for s in sits:
            gs = bigrams(s["content"])
            for m in matters:
                score = jaccard(gs, bigrams(m["title"]))
                if score >= JACCARD:
                    pair += 1
                    if pair <= 5:
                        print("       ~ %.2f  situation: %s" % (score, (s["content"] or "")[:44]))
                        print("                matter   : %s" % m["title"][:44])
        print("  ② 与未完之事疑似同一件事的 situation 条目: %d / %d" % (pair, len(sits)))
    print()
