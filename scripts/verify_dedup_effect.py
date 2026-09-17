"""等一轮去重之后的判决，量两件事：
  1) advantage / hazard 变成多少（对比去重前）
  2) 候选效用的"并列"是否消失（去重前 1670681411 有 9 个 need/unf 完全相同的 follow_up，
     靠 softmax 抽签决定说哪件 —— 这才是"反复翻旧账"的机制）
"""
import json
import sqlite3
import time

TARGETS = ("1670681411", "994959351")
CUTOFF = "2026-09-17T12:45:00"

print("等待 %s 之后的判决（最多 16 分钟）..." % CUTOFF, flush=True)
for _ in range(32):
    ready = True
    for tag in TARGETS:
        con = sqlite3.connect("file:/data/default-friendmessage-%s/companion.sqlite3?mode=ro" % tag,
                              uri=True)
        row = con.execute(
            "select count(*) from decisions where decided_at > ? and delta_t < 2000", (CUTOFF,)
        ).fetchone()
        con.close()
        if row[0] == 0:
            ready = False
    if ready:
        print("两个实例都有了新判决", flush=True)
        break
    time.sleep(30)

for tag in TARGETS:
    con = sqlite3.connect("file:/data/default-friendmessage-%s/companion.sqlite3?mode=ro" % tag,
                          uri=True)
    con.row_factory = sqlite3.Row
    print()
    print("=" * 92)
    print("用户 %s" % tag)
    print("  去重前后对比（delta_t<2000 的真实轮次）:")
    for r in con.execute(
            "select decided_at, reason, advantage, hazard, delta_t, silence_utility from decisions"
            " where delta_t < 2000 order by decided_at desc limit 6"):
        print("    %s %-24s adv=%+.5f haz=%.2e sil=%.4f" % (
            r["decided_at"][:19], r["reason"], r["advantage"], r["hazard"], r["silence_utility"]))

    newest = con.execute(
        "select decided_at, payload_json from decisions where delta_t < 2000"
        " order by decided_at desc limit 1").fetchone()
    live = con.execute(
        "select candidate_id, type, status, internal_need, unfinished_relevance"
        " from candidate_intents where status in ('new','active')").fetchall()
    con.close()
    print("  当前活跃候选 %d 个:" % len(live))
    for c in live:
        print("    %s %-17s need=%.3f unf=%.3f" % (
            c["candidate_id"][-6:], c["type"], c["internal_need"], c["unfinished_relevance"]))
    if newest:
        payload = json.loads(newest["payload_json"] or "{}")
        asm = payload.get("assessments") or []
        totals = sorted(((a.get("utility") or a).get("total") or 0) for a in asm)
        if totals:
            top = totals[-1]
            ties = sum(1 for t in totals if abs(t - top) < 1e-6)
            print("  该轮候选效用: %s" % " ".join("%.4f" % t for t in totals))
            print("  >>> 并列最高效用的候选数: %d（去重前 1670681411 是 9）" % ties)
