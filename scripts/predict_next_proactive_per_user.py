"""逐个用户推算"下次主动回复"的时间分布（v3）。

    lam(adv) = hazard_base(3.0e-5) * softplus(4.0 * adv)
    每轮 ~898s 掷一次：p = 1 - exp(-lam * dt)，存活 = exp(-lam * dt)
        （v2 把这里乘反了：p_none *= (1 - exp(-lam*dt)) 让第一轮 hit 就 95%）

窗口语义：last_sent <= 判决时刻 < last_sent + 3600s ⇒ 窗口内（advantage 被压低）。
取值：每个桶取**最新一条实测**（advantage 随 regime 漂移，取整段均值会把早晨的高值
      混进当前估计）；同时打印均值/条数以便判断可信度。
"""
import datetime as dt
import glob
import math
import os
import sqlite3

BASE, BETA = 3.0e-5, 4.0
STEP = 898.0
COOLDOWN = 2400.0
REPEAT_WINDOW = 3600.0
DAILY_CAP = 12
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


def softplus(x):
    return math.log1p(math.exp(x)) if x < 50 else x


def lam_of(adv):
    return BASE * softplus(BETA * adv)


def parse(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "").replace("+00:00", ""))
    except (ValueError, TypeError):
        return None


def cst(t):
    return (t + CST).strftime("%m-%d %H:%M") if t else "-"


def avg(values):
    return sum(values) / len(values) if values else None


records = []
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    name = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    st = con.execute("select * from runtime_state").fetchone()
    sent = con.execute(
        "select timestamp from raw_events where event_type='proactive_sent'"
        " order by timestamp desc limit 1").fetchone()
    dec = con.execute(
        "select decided_at, reason, advantage, delta_t from decisions"
        " where delta_t < 2000 order by decided_at desc limit 25").fetchall()
    con.close()
    last_sent = parse(sent["timestamp"]) if sent else None
    window_end = last_sent + dt.timedelta(seconds=REPEAT_WINDOW) if last_sent else None
    depressed, normal, other = [], [], []
    for row in dec:  # dec 已按时间倒序 => 每个桶的第一条即"最新"
        when = parse(row["decided_at"])
        adv = row["advantage"]
        if when is None:
            continue
        if row["reason"] not in ("hazard_triggered", "hazard_not_triggered") or adv is None:
            other.append((row["decided_at"], row["reason"], adv, row["delta_t"]))
            continue
        if last_sent and window_end and last_sent <= when < window_end:
            depressed.append(adv)
        else:
            normal.append(adv)
    records.append({
        "name": name, "st": st, "last_sent": last_sent, "window_end": window_end,
        "depressed": depressed, "normal": normal, "other": other,
        "in_window": bool(window_end and NOW < window_end),
    })

ratios = [d[0] / n[0] for r in records
          for d, n in [(r["depressed"], r["normal"])] if d and n and n[0]]
RATIO = avg(ratios)

print("now = %s UTC = %s CST" % (NOW.strftime("%H:%M:%S"), cst(NOW)))
print("窗口内/正常 的最新一条实测比值: %s（%s）" % (
    "%.3f" % RATIO if RATIO else "无", [r["name"].split("-")[-1] for r in records if r["depressed"]]))
print()

summary = []
for item in records:
    st = item["st"]
    tag = item["name"].replace("default-friendmessage-", "")
    print("=" * 92)
    print("用户 %s   端口 %s" % (tag, PORTS.get(item["name"], "?")))
    adv_normal = item["normal"][0] if item["normal"] else None
    adv_dep = item["depressed"][0] if item["depressed"] else None
    dep_source = "实测"
    if adv_dep is None and adv_normal is not None and RATIO:
        adv_dep, dep_source = adv_normal * RATIO, "折算 ×%.2f" % RATIO
    if adv_dep is None:
        adv_dep, dep_source = adv_normal, "退回正常值"
    if adv_normal is None:
        adv_normal = adv_dep
    print("  advantage 正常 %s（最新/均值 %s，%d 条） | 窗口内 %s [%s]（%d 条）" % (
        "-" if adv_normal is None else "%+.4f" % adv_normal,
        "-" if not item["normal"] else "%+.4f" % avg(item["normal"]), len(item["normal"]),
        "-" if adv_dep is None else "%+.4f" % adv_dep, dep_source, len(item["depressed"])))
    print("  最近一次真实判决: %s" % (
        item["other"][0][0][:19] if not item["normal"] and not item["depressed"] and item["other"]
        else "(见上)"))

    cooldown = parse(st["cooldown_until"]) if st else None
    count = (st["contact_count_today"] or 0) if st else 0
    allow = st["allow_proactive"] if st else 1
    start = NOW if not cooldown or cooldown <= NOW else cooldown
    print("  冷却至 %s%s | 已发 %s/%s | allow=%s | 最后用户消息 %s | regime=%s" % (
        cst(cooldown), "" if cooldown and cooldown > NOW else "（已过）", count, DAILY_CAP,
        allow, cst(parse(st["last_user_message_at"])) if st else "-",
        "窗口内 %s 关闭" % cst(item["window_end"]) if item["in_window"] else "正常"))

    if not allow or count >= DAILY_CAP or adv_normal is None:
        reason = ("硬边界禁止" if not allow else
                  "今日额度用完" if count >= DAILY_CAP else "无实测判决")
        print("  >>> %s" % reason)
        summary.append((tag, None, None, reason, None))
        continue

    t, p_survive, marks = start, 1.0, {}
    for _ in range(400):
        if t >= NOW + dt.timedelta(hours=72):
            break
        adv = adv_dep if (item["window_end"] and t < item["window_end"]) else adv_normal
        p_survive *= math.exp(-lam_of(adv) * STEP)          # 存活 = exp(-lam*dt)
        hit = 1.0 - p_survive
        for label, target in (("25%", 0.25), ("中位", 0.5), ("75%", 0.75), ("90%", 0.9)):
            if label not in marks and hit >= target:
                marks[label] = t
        t += dt.timedelta(seconds=STEP)

    adv_now = adv_dep if item["in_window"] else adv_normal
    p_round = 1.0 - math.exp(-lam_of(adv_now) * STEP)
    print("  当前 λ %.2e /s | 每轮 %.2f%% | 期望轮数 %.0f" % (
        lam_of(adv_now), p_round * 100, 1.0 / p_round))
    print("  >>> 25%% %s | **中位 %s** | 75%% %s | 90%% %s   (CST, 距现在 %.1f/%s h)" % (
        cst(marks.get("25%")), cst(marks.get("中位")), cst(marks.get("75%")), cst(marks.get("90%")),
        (marks["中位"] - NOW).total_seconds() / 3600 if marks.get("中位") else 0,
        ("%.1f" % ((marks["90%"] - NOW).total_seconds() / 3600)) if marks.get("90%") else "-"))
    summary.append((tag, marks.get("中位"), marks.get("90%"),
                    "窗口内" if item["in_window"] else "", cst(item["last_sent"]) if item["last_sent"] else "从未"))

print()
print("=" * 92)
print("%-13s %-16s %-16s %-8s %s" % ("用户", "中位(50%)", "90%", "regime", "上次主动"))
for tag, med, p90, note, last in sorted(summary, key=lambda x: (x[1] is None, x[1] or NOW)):
    print("%-13s %-16s %-16s %-8s %s" % (tag, cst(med) if med else "-", cst(p90) if p90 else "-", note, last))
