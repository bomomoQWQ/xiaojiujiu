"""干跑：列出每个实例内部未完之事的相似度，供人工判定 + 预测候选池会缩小多少。

只读。不写任何东西。
  strict  —— 部署上去的 _same_subject 判定（bigram>=0.77 且 差异<=0.26）
  score   —— 剥掉模板词后的 core 上的 token Jaccard（人工看的分数）
  diff    —— 核心差异占比
还打印每个 matter 被哪些候选引用（sources_json 里的 unfinished:<id>），
用来预测作废后候选池会塌掉几个。
"""
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app/runtime/src")
from companion_runtime import unfinished as u  # noqa: E402

PORTS = {
    "default-friendmessage-qq01": 8787, "default-friendmessage-qq02": 8788,
    "default-friendmessage-qq03": 8789, "default-friendmessage-qq04": 8790,
    "default-friendmessage-qq08": 8791, "default-friendmessage-qq06": 8792,
    "default-friendmessage-qq05": 8793,
}


def score(a, b):
    A, B = u.topic_tokens(u._subject_core(a)), u.topic_tokens(u._subject_core(b))
    if not A or not B:
        return 0.0, 0.0
    jac = len(A & B) / len(A | B)
    diff = (len(A - B) + len(B - A)) / max(len(A), len(B))
    return jac, diff


total_pairs = 0
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    name = os.path.basename(os.path.dirname(path))
    tag = name.replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    matters = con.execute(
        "select unfinished_id, title, priority, status, created_at, expire_at, source_event_ids"
        " from unfinished_matters where status in ('open','waiting','due','muted')"
        " order by created_at").fetchall()
    cands = con.execute(
        "select candidate_id, type, status, sources_json, intent from candidate_intents"
        " where status in ('new','active')").fetchall()
    con.close()

    print("=" * 100)
    print("用户 %s  端口 %s   活跃 matter %d 件  活跃候选 %d 个" % (
        tag, PORTS.get(name, "?"), len(matters), len(cands)))
    for m in matters:
        refs = [c["candidate_id"][-6:] for c in cands
                if m["unfinished_id"] in (c["sources_json"] or "")]
        print("  %s pri=%.2f created=%s exp=%s  候选=%s" % (
            m["unfinished_id"][-6:], m["priority"], str(m["created_at"])[5:16],
            str(m["expire_at"])[5:16], refs or "无"))
        print("      %s" % m["title"][:76])

    pairs = []
    for i, a in enumerate(matters):
        for b in matters[i + 1:]:
            jac, diff = score(a["title"], b["title"])
            strict = u._same_subject(a["title"], b["title"])
            if jac >= 0.30 or strict:
                pairs.append((jac, diff, strict, a, b))
    pairs.sort(reverse=True, key=lambda x: x[0])
    if pairs:
        print("  --- 同实例内部的相似对（人工看）---")
        for jac, diff, strict, a, b in pairs:
            total_pairs += 1
            print("  %.3f  差异%5.1f%%  strict=%-5s  %s" % (jac, diff * 100, strict,
                                                          a["title"][:42]))
            print("                              %s  (%s)" % (b["title"][:42], b["unfinished_id"][-6:]))
    print()
print("总计待人工判定的相似对: %d" % total_pairs)
