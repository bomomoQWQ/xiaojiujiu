"""逐个用户推算"下次主动回复"（v4：把 advantage 的漂移与 busy 台阶建进模型）。

v3 用的是"最新一条实测 advantage 当常数"，但实测显示它会单调漂，而且每个人都有一道
busy 台阶（距上条用户消息 4h -> busy 0.45、8h -> 0.60）落在中位之前 —— 于是 v3 的表
**系统性偏早**。这版：

    adv(t) = adv_now + drift * (t - now) - STEP_COST * (已跨过的台阶数)
    窗口内再乘上实测的"窗口内/正常"比值

drift 只用**同一 regime** 的连续尾段估（窗口内/窗口外是两个水平，混算没有意义）；
样本不足 3 条或跨度 < 0.5h 就不外推，并在输出里标注。STEP_COST 取 -0.04：
这是从 qq01 的 ladder 实测推的（busy 0.25->0.45 使 U_max 从 1.1629 掉到 1.1157，
即约 -0.047），0.45->0.60 的跨度小一半，取 -0.04 偏保守。

    lam(adv) = 3.0e-5 * softplus(4*adv)，每轮 ~898s
"""
import datetime as dt
import glob
import math
import os
import sqlite3

BASE, BETA = 3.0e-5, 4.0
STEP = 898.0
REPEAT_WINDOW = 3600.0
DAILY_CAP = 12
STEP_COST = 0.04
NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
CST = dt.timedelta(hours=8)
PORTS = {
    "default-friendmessage-qq01": 8787, "default-friendmessage-qq02": 8788,
    "default-friendmessage-qq03": 8789, "default-friendmessage-qq04": 8790,
    "default-friendmessage-qq08": 8791, "default-friendmessage-qq06": 8792,
    "default-friendmessage-qq05": 8793,
}


def softplus(x):
    return math.log1p(math.exp(x)) if x < 50 else x


def lam_of(adv):
    return BASE * softplus(BETA * max(-0.6, adv))


def parse(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "").replace("+00:00", ""))
    except (ValueError, TypeError):
        return None


def cst(t):
    return (t + CST).strftime("%m-%d %H:%M") if t else "-"


def avg(v):
    return sum(v) / len(v) if v else None


records = []
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    name = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    st = con.execute("select * from runtime_state").fetchone()
    sent = con.execute("select timestamp from raw_events where event_type='proactive_sent'"
                       " order by timestamp desc limit 1").fetchone()
    dec = con.execute(
        "select decided_at, reason, advantage from decisions"
        " where delta_t < 2000 order by decided_at desc limit 25").fetchall()
    con.close()

    last_sent = parse(sent["timestamp"]) if sent else None
    window_end = last_sent + dt.timedelta(seconds=REPEAT_WINDOW) if last_sent else None
    normal, depressed, denied = [], [], []
    for row in dec:
        when, adv = parse(row["decided_at"]), row["advantage"]
        if when is None:
            continue
        if row["reason"] == "no_candidate_beats_silence" and adv is not None and adv > -50:
            denied.append(adv)
            continue
        if row["reason"] not in ("hazard_triggered", "hazard_not_triggered") or adv is None:
            continue
        (depressed if (last_sent and window_end and last_sent <= when < window_end) else normal
         ).append((when, adv))
    records.append({"name": name, "st": st, "last_sent": last_sent, "window_end": window_end,
                    "normal": normal, "depressed": depressed, "denied": denied,
                    "in_window": bool(window_end and NOW < window_end)})

ratios = [(d[0][1] / n[0][1]) for r in records
          for d, n in [(r["depressed"], r["normal"])] if d and n and n[0][1]]
RATIO = avg(ratios) or 0.43

print("now = %s UTC = %s CST    窗口内/正常 实测比值 = %.3f" % (
    NOW.strftime("%H:%M"), cst(NOW), RATIO))
print()

rows = []
for item in records:
    st = item["st"]
    tag = item["name"].replace("default-friendmessage-", "")
    print("=" * 96)
    print("用户 %s   端口 %s" % (tag, PORTS.get(item["name"], "?")))
    adv_now = item["normal"][0][1] if item["normal"] else None
    if adv_now is None:
        note = ("advantage 恒 ≤ 0（最新 %+.4f）→ eligible 为空、不掷骰" % item["denied"][0]
                if item["denied"] else "无实测判决")
        print("  %s" % note)
        rows.append((tag, None, None, note))
        continue

    # ---- 漂移：同一 regime 的连续尾段
    tail = [item["normal"][0]]
    for when, adv in item["normal"][1:]:
        if abs(adv - adv_now) > 0.08:
            break
        tail.append((when, adv))
    drift, drift_note = 0.0, "不外推（样本不足）"
    if len(tail) >= 3:
        span = (tail[0][0] - tail[-1][0]).total_seconds() / 3600
        if span >= 0.5:
            drift = (tail[0][1] - tail[-1][1]) / span
            drift_note = "%+.4f/h（%d 条跨 %.1fh）" % (drift, len(tail), span)
    # ---- busy 台阶时刻
    last_user = parse(st["last_user_message_at"])
    hu = (NOW - last_user).total_seconds() / 3600 if last_user else 99.0
    steps = [last_user + dt.timedelta(hours=h) for h in (4.0, 8.0)
             if last_user and hu < h]
    print("  advantage 最新 %+.4f | 漂移 %s | 未来 busy 台阶 %s" % (
        adv_now, drift_note, [cst(s) for s in steps] or "无"))

    adv_dep_now = item["depressed"][0][1] if item["depressed"] else adv_now * RATIO
    dep_src = "实测" if item["depressed"] else "折算×%.2f" % RATIO

    def adv_at(t):
        adv = adv_now + drift * ((t - NOW).total_seconds() / 3600.0)
        adv -= STEP_COST * sum(1 for s in steps if t >= s)
        if item["window_end"] and t < item["window_end"]:
            adv *= (adv_dep_now / adv_now) if adv_now else 1.0
        return adv

    cooldown = parse(st["cooldown_until"])
    start = NOW if not cooldown or cooldown <= NOW else cooldown
    count = st["contact_count_today"] or 0
    if not st["allow_proactive"] or count >= DAILY_CAP:
        note = "硬边界禁止" if not st["allow_proactive"] else "今日额度用完"
        print("  >>> %s" % note)
        rows.append((tag, None, None, note))
        continue

    t, survive, marks = start, 1.0, {}
    for _ in range(500):
        if t >= NOW + dt.timedelta(hours=72):
            break
        surv_t = math.exp(-lam_of(adv_at(t)) * STEP)
        if surv_t > 1.0:
            surv_t = 1.0
        survive *= surv_t
        hit = 1.0 - survive
        for lab, q in (("25%", 0.25), ("中位", 0.5), ("75%", 0.75), ("90%", 0.9)):
            if lab not in marks and hit >= q:
                marks[lab] = t
        t += dt.timedelta(seconds=STEP)

    adv_start = adv_at(start)
    p0 = 1 - math.exp(-lam_of(adv_start) * STEP)
    print("  起点 %s | adv %+.4f -> λ %.2e -> 每轮 %.2f%%" % (
        cst(start), adv_start, lam_of(adv_start), p0 * 100))
    print("  >>> 25%% %s | **中位 %s** | 75%% %s | 90%% %s   (CST)" % (
        cst(marks.get("25%")), cst(marks.get("中位")), cst(marks.get("75%")), cst(marks.get("90%"))))
    rows.append((tag, marks.get("中位"), marks.get("90%"),
                 "窗口内比值%s %s" % (dep_src, "，%s 关窗" % cst(item["window_end"])
                                     if item["in_window"] else "")))

print()
print("=" * 96)
print("%-13s %-16s %-16s %s" % ("用户", "中位(50%)，含漂移修正", "90%", "备注"))
for tag, med, p90, note in sorted(rows, key=lambda x: (x[1] is None, x[1] or NOW)):
    print("%-13s %-16s %-16s %s" % (tag, cst(med) if med else "-", cst(p90) if p90 else "-", note))
