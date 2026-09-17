"""量一下 bigram 阈值 0.6 在真实未完之事标题上的表现。

正样本（应当合并）：同一实例内部、当前 _same_subject 判 False、但看标题明显是同一话题的对。
负样本（不该合并）：跨实例的两两组合 —— 不同人、不同话题，是"绝不应当合并"的近似。

关键要看：散文标题里那段共用套话（"角色是否接受、后续是否真的会一起喝，尚未有结果"）
会不会把**不同主题**也推过阈值。若会，说明纯 bigram 不够，得先剥套话。
"""
import glob
import os
import re
import sqlite3
import sys

sys.path.insert(0, "runtime/src")
from companion_runtime import unfinished  # noqa: E402

THRESHOLDS = (0.45, 0.5, 0.55, 0.6, 0.65, 0.7)


def bigrams(text):
    clean = re.sub(r"[\s，。、？！！：；「」“”（）()\[\]…—\-/]", "", text or "")
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def jac(a, b):
    return len(a & b) / len(a | b) if (a and b) else 0.0


def load(path):
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    rows = [r[0] for r in con.execute("select title from unfinished_matters")]
    con.close()
    return rows


per = {}
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    per[os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")] = load(path)

total = sum(len(v) for v in per.values())
print("标题总数 %d（跨 %d 个实例）" % (total, len(per)))
print()

# ---- 正样本：同实例内部
print("=== 同一实例内部的标题对（按 bigram Jaccard 降序，只看 >=0.45）===")
within = []
for tag, titles in per.items():
    for i, a in enumerate(titles):
        for b in titles[i + 1:]:
            score = jac(bigrams(a), bigrams(b))
            if score >= 0.45:
                within.append((score, tag, a, b, unfinished._same_subject(a, b)))
within.sort(reverse=True, key=lambda x: x[0])
for score, tag, a, b, merged in within:
    print("  %.2f  %-11s 现值=%-5s  %s" % (score, tag, merged, a[:44]))
    print("                        %s" % b[:44])
print("  共 %d 对" % len(within))

# ---- 负样本：跨实例
print()
cross = []
tags = sorted(per)
for i, t1 in enumerate(tags):
    for t2 in tags[i + 1:]:
        for a in per[t1]:
            for b in per[t2]:
                score = jac(bigrams(a), bigrams(b))
                if score >= 0.5:
                    cross.append((score, t1, t2, a, b))
cross.sort(reverse=True, key=lambda x: x[0])
print("=== 跨实例（不同话题，不应合并）里 bigram >= 0.5 的对: %d ===" % len(cross))
for score, t1, t2, a, b in cross[:12]:
    print("  %.2f  %s vs %s" % (score, t1, t2))
    print("        %s" % a[:46])
    print("        %s" % b[:46])

print()
print("=== 各阈值下的行为 ===")
print("  阈值 | 同实例被合并的对 | 跨实例（误）合并的对")
for th in THRESHOLDS:
    pos = sum(1 for s, *_ in within if s >= th)
    neg = sum(1 for s, *_ in cross if s >= th)
    print("  %.2f | %16d | %20d" % (th, pos, neg))
