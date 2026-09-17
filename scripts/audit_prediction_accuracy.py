"""审计"每人下次主动回复"那张表的准确度：两个之前没管的误差源。

误差源 1（候选池）：候选的 internal_need 只在**创建时**算一次（candidate.py:1113），
TTL=default_ttl_seconds(21600s=6h)。池子到期后，下一轮重建会按当时的状态**重新定价**，
advantage 可能明显不同 —— 我用来推算的那个 adv 可能基于一个已经死掉的池子。

误差源 2（advantage 漂移）：adv = U_max - U_silence，两边都随状态漂：
  · silence_utility 随 impulse/restraint/pressure 漂
  · 预测侧的 busy_probability 在"距上条用户消息 4h / 8h"处**跳台阶**（0.25/0.45/0.60）
    -> reply_probability 变 -> user 项变。这是阶跃，不是渐变。
"""
import datetime as dt
import glob
import json
import math
import os
import sqlite3

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
CST = dt.timedelta(hours=8)
PORTS = {
    "default-friendmessage-1670681411": 8787,
    "default-friendmessage-994959351": 8788,
    "default-friendmessage-1913447173": 8789,
    "default-friendmessage-728260403": 8790,
    "default-friendmessage-1070754640": 8791,
    "default-friendmessage-2206929446": 8792,
    "default-friendmessage-2259606745": 8793,
}


def parse(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "").replace("+00:00", ""))
    except (ValueError, TypeError):
        return None


def cst(t):
    return (t + CST).strftime("%m-%d %H:%M") if t else "-"


def busy_of(hours):
    if hours >= 8.0:
        return 0.60
    if hours >= 4.0:
        return 0.45
    return 0.25


print("now = %s UTC = %s CST" % (NOW.strftime("%H:%M:%S"), cst(NOW)))
print()

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    name = os.path.basename(os.path.dirname(path))
    tag = name.replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    st = con.execute("select * from runtime_state").fetchone()
    live = con.execute(
        "select candidate_id, type, status, internal_need, unfinished_relevance, created_at,"
        " expires_at from candidate_intents where status in ('new','active')"
        " order by expires_at").fetchall()
    dec = con.execute(
        "select decided_at, reason, advantage, payload_json, delta_t from decisions"
        " where reason in ('hazard_triggered','hazard_not_triggered') and delta_t < 2000"
        " order by decided_at desc limit 12").fetchall()
    con.close()

    print("=" * 96)
    print("用户 %s   端口 %s" % (tag, PORTS.get(name, "?")))

    # ---- 误差源 1：候选池
    print("  候选池: 活跃 %d 个" % len(live))
    if live:
        soonest = parse(live[0]["expires_at"])
        latest = parse(live[-1]["expires_at"])
        kinds = {}
        for c in live:
            kinds[c["type"]] = kinds.get(c["type"], 0) + 1
        print("      类型分布 %s ；最早到期 %s CST（%.1f h 后），最晚 %s" % (
            kinds, cst(soonest), (soonest - NOW).total_seconds() / 3600 if soonest else 0,
            cst(latest)))
        needs = sorted({round(c["internal_need"], 4) for c in live})
        print("      need 取值 %s" % needs[:6])
    else:
        print("      ** 空池：下一轮只会重建 resting contact 候选（need 会按当时状态重定价）")

    # 最近一次判决用的候选还在不在
    if dec:
        payload = json.loads(dec[0]["payload_json"] or "{}")
        used = {a.get("utility", a).get("candidate_id") for a in (payload.get("assessments") or [])}
        live_ids = {c["candidate_id"] for c in live}
        dead = {cid for cid in used if cid and cid not in live_ids}
        print("      最近判决(%s) 用了 %d 个候选，其中 %d 个现在已不在活跃池里" % (
            dec[0]["decided_at"][:19], len(used), len(dead)))

    # ---- 误差源 2：advantage 漂移
    # 只取"同一 regime"的连续尾段估斜率：窗口内/窗口外是两个不同的水平，
    # 混在一起算出来的斜率毫无意义（1913447173 就是这么被算出 +0.0056/h 的）。
    if len(dec) >= 2:
        sent = con_sent = None
        live_window = None
        print("  advantage 轨迹（近 %d 条，delta_t<2000）:" % len(dec))
        print("      %s" % " ".join(
            "%s=%+.3f" % (r["decided_at"][11:16], r["advantage"]) for r in reversed(dec)))
        values = [r["advantage"] for r in dec]
        newest = values[0]
        # 从最新一条往回走，只要与最新一条的差不超过 0.08 就视为同一 regime
        same = [newest]
        for v in values[1:]:
            if abs(v - newest) > 0.08:
                break
            same.append(v)
        if len(same) >= 2:
            span_rows = dec[: len(same)]
            t0, t1 = parse(span_rows[-1]["decided_at"]), parse(span_rows[0]["decided_at"])
            span = (t1 - t0).total_seconds() / 3600 if t0 and t1 else 0
            if span > 0.1:
                drift = (same[0] - same[-1]) / span
                print("      同一 regime 连续 %d 条跨 %.2f h -> 斜率 %+.4f /h，4h 后约 %+.3f" % (
                    len(same), span, drift, same[0] + drift * 4))
            else:
                print("      同 regime 尾段只有 %.2f h，漂移估不准（样本太密）" % span)
        else:
            print("      最新一条与前面差 >0.08（刚换过 regime），漂移不可估")
    elif dec:
        print("  advantage 轨迹: 只有 1 条样本 %+.4f（无法估漂移）" % dec[0]["advantage"])

    # ---- busy 台阶
    last_user = parse(st["last_user_message_at"])
    if last_user:
        hu = (NOW - last_user).total_seconds() / 3600
        marks = []
        for limit in (4.0, 8.0):
            if hu < limit:
                marks.append("%sh -> %s CST(%.1fh 后)" % (
                    limit, cst(last_user + dt.timedelta(hours=limit)),
                    limit - hu))
        print("  busy: 距上条用户消息 %.2f h -> %.2f%s" % (
            hu, busy_of(hu), ("；台阶：" + "，".join(marks)) if marks else "（已过 8h）"))
    last_contact = parse(st["last_contact_at"])
    if last_contact:
        hc = (NOW - last_contact).total_seconds() / 3600
        print("      距上次主动联系 %.2f h（repeat 窗口已过 %s）" % (
            hc, "是" if hc > 1 else "否，%.0f 分钟后过" % ((1 - hc) * 60)))
