"""为什么 qq03 的主动意愿明显高一截？逐个拆效用分解 + 状态 + 用户模型。

advantage = U_max - U_silence，两边都可能不一样：
  U_max  = internal + user + relation - 各项成本 - uncertainty_penalty*unc
           internal 取决于候选的 need/unf（候选侧）
           user/relation 取决于**预测**（用户模型 + 快速变量 C）
  U_silence = 0.28 + 0.45R + 0.30*risk + 0.25*cooldown_term + 0.42I - 1.2P^2
"""
import datetime as dt
import glob
import json
import os
import sqlite3

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
CST = dt.timedelta(hours=8)
PORTS = {
    "default-friendmessage-qq01": 8787, "default-friendmessage-qq02": 8788,
    "default-friendmessage-qq03": 8789, "default-friendmessage-qq04": 8790,
    "default-friendmessage-qq08": 8791, "default-friendmessage-qq06": 8792,
    "default-friendmessage-qq05": 8793,
}
CAND = "cnd_9c3a3b"


def parse(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "").replace("+00:00", ""))
    except (ValueError, TypeError):
        return None


def cst(t):
    return (t + CST).strftime("%m-%d %H:%M") if t else "-"


print("now = %s CST" % cst(NOW))
print()
head = ("%-12s %-9s %-8s %-8s %-8s %-8s %-8s %-8s %-8s %-8s %-7s %s" % (
    "用户", "候选类型", "need", "internal", "user", "relation", "bcost", "icost", "kcost",
    "U_max", "U_sil", "adv"))
print(head)
print("-" * len(head))

detail = []
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    name = os.path.basename(os.path.dirname(path))
    tag = name.replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    st = con.execute("select * from runtime_state").fetchone()
    row = con.execute(
        "select decided_at, advantage, silence_utility, payload_json from decisions"
        " where delta_t < 2000 and reason in ('hazard_triggered','hazard_not_triggered')"
        " order by decided_at desc limit 1").fetchone()
    live = con.execute(
        "select candidate_id, type, internal_need, unfinished_relevance from candidate_intents"
        " where status in ('new','active')").fetchall()
    um = con.execute("select scope, effective_count, observations from user_model_params").fetchall()
    con.close()
    if row is None:
        print("%-12s （无真实判决）" % tag)
        continue
    payload = json.loads(row["payload_json"] or "{}")
    asm = payload.get("assessments") or []
    if not asm:
        print("%-12s （该轮无候选）" % tag)
        continue
    best = max(((a.get("utility") or a) for a in asm),
               key=lambda u: u.get("total") if u.get("total") is not None else -9e9)
    cid = best.get("candidate_id")
    cand = next((c for c in live if c["candidate_id"] == cid), None)
    ctype = cand["type"] if cand else "(已不在池)"
    need = cand["internal_need"] if cand else float("nan")
    print("%-12s %-9s %-8.4f %-8.4f %-8.4f %-8.4f %-8.4f %-8.4f %-8.4f %-8.4f %-8.4f %+.4f" % (
        tag, ctype, need, best.get("internal") or 0, best.get("user") or 0,
        best.get("relation") or 0, best.get("boundary_cost") or 0, best.get("interrupt_cost") or 0,
        best.get("risk_cost") or 0, best.get("total") or 0, row["silence_utility"] or 0,
        row["advantage"] or 0))
    detail.append((tag, row, best, st, ctype, need, um, len(asm)))

print()
print("=== 逐项对比（重点看 user / relation / U_silence）===")
for tag, row, best, st, ctype, need, um, n in detail:
    print()
    print("用户 %s   端口 %s   判决 %s   候选数 %d" % (
        tag, PORTS.get("default-friendmessage-" + tag, "?"), row["decided_at"][:19], n))
    print("    预测: reply=%.4f pos=%.4f cont=%.4f risk=%.4f unc=%.4f" % (
        best.get("reply_probability") or 0, best.get("positive_probability") or 0,
        best.get("continue_probability") or 0, best.get("boundary_risk") or 0,
        best.get("uncertainty") or 0))
    print("    状态: I=%.4f R=%.4f P=%.4f  -> U_silence=%.4f" % (
        st["approach_impulse"], st["restraint"], st["pressure"], row["silence_utility"]))
    print("    用户模型: %s" % ([dict(r) for r in um] or "无记录（冷启动）"))
    con = sqlite3.connect(
        "file:/data/default-friendmessage-%s/companion.sqlite3?mode=ro" % tag, uri=True)
    rows = con.execute("select candidate_id, type, internal_need, unfinished_relevance"
                       " from candidate_intents where status in ('new','active')").fetchall()
    con.close()
    print("    池内候选: " + " ".join("%s/%s(need=%.3f,unf=%.3f)" % (
        r[0][-6:], r[1], r[2], r[3]) for r in rows))
