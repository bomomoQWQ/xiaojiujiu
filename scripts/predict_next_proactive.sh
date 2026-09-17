#!/usr/bin/env bash
# 推算"下一次主动消息"的时间点。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import json
import math
import os
import sqlite3

BASE, BETA = 3.0e-5, 4.0
COOLDOWN_S = 2400.0
DAILY_CAP = 12
SCHED_MAX_S = 900.0
NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
PERSON = os.environ.get("CR_PERSON", "default-friendmessage-1670681411")


def softplus(x):
    return math.log1p(math.exp(x)) if x < 50 else x


def lam_of(adv):
    return BASE * softplus(BETA * adv)


def parse(ts):
    if not ts:
        return None
    t = str(ts).replace("Z", "").replace("+00:00", "")
    try:
        return dt.datetime.fromisoformat(t)
    except ValueError:
        return None


def fmt(t):
    if t is None:
        return "-"
    d = (t - NOW).total_seconds()
    sign = "+" if d >= 0 else "-"
    a = abs(d)
    if a < 3600:
        rel = "%.0fm" % (a / 60)
    elif a < 86400:
        rel = "%.1fh" % (a / 3600)
    else:
        rel = "%.1fd" % (a / 86400)
    return "%s UTC (%s%s)" % (t.strftime("%m-%d %H:%M"), sign, rel)


def n(v, spec="-"):
    return "-" if v is None else format(v, spec)


path = "/data/%s/companion.sqlite3" % PERSON
con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
con.row_factory = sqlite3.Row

print("now    = %s UTC" % NOW.strftime("%Y-%m-%d %H:%M:%S"))
print("person = %s" % PERSON)
print()

print("=== 最近 14 次判决（倒序）===")
rows = con.execute(
    "select decided_at, trigger, reason, advantage, hazard, action_probability,"
    " delta_t, silence_utility, acted, next_wake_at"
    " from decisions order by decided_at desc limit 14"
).fetchall()
for r in rows:
    print("  %s %-9s %-24s adv=%s haz=%s p=%s dt=%s sil=%s acted=%s ->wake %s" % (
        r["decided_at"][:19], str(r["trigger"])[:9], str(r["reason"])[:24],
        n(r["advantage"], "+.4f"), n(r["hazard"], ".2e"),
        n(r["action_probability"], ".4f"),
        n(r["delta_t"], ".0f"), n(r["silence_utility"], ".3f"),
        r["acted"], (r["next_wake_at"] or "-")[:19]))

print()
print("=== 真实掷骰轮次的 advantage 序列（近 30）===")
# 只保留真正算过 hazard 的轮次：foreground_pause / cooldown_active /
# no_candidate_beats_sil 都不掷骰，其中 cooldown_active 用 -99 当哨兵值，
# 混进均值会把"期望间隔"算成天文数字。
seq = con.execute(
    "select decided_at, reason, advantage, acted from decisions"
    " where reason in ('hazard_triggered', 'hazard_not_triggered')"
    " order by decided_at desc limit 30"
).fetchall()
for r in seq[:12]:
    print("  %s  %-24s adv=%s acted=%s" % (
        r["decided_at"][:19], str(r["reason"])[:24],
        n(r["advantage"], "+.4f"), r["acted"]))
advs = [r["advantage"] for r in seq if r["advantage"] is not None]
if advs:
    mean_adv = sum(advs) / len(advs)
    print("  -> n=%d mean=%+.4f min=%+.4f max=%+.4f" % (
        len(advs), mean_adv, min(advs), max(advs)))

print()
print("=== runtime_state ===")
st = con.execute("select * from runtime_state").fetchone()
if st:
    d = dict(st)
    print("  last_tick_at       = %s" % fmt(parse(d.get("last_tick_at"))))
    print("  last_user_message  = %s" % fmt(parse(d.get("last_user_message_at"))))
    print("  last_contact_at    = %s" % fmt(parse(d.get("last_contact_at"))))
    print("  last_exchange_at   = %s" % fmt(parse(d.get("last_exchange_at"))))
    print("  cooldown_until     = %s" % fmt(parse(d.get("cooldown_until"))))
    print("  foreground_pause   = %s" % fmt(parse(d.get("foreground_pause_until"))))
    print("  contact_count_today= %s (day=%s)" % (
        d.get("contact_count_today"), d.get("contact_day")))
    print("  allow_proactive    = %s" % d.get("allow_proactive"))
    print("  mood v/a/s         = %s / %s / %s" % (
        n(d.get("mood_valence"), ".3f"), n(d.get("mood_arousal"), ".3f"),
        n(d.get("mood_stability"), ".3f")))
    print("  approach_impulse   = %s" % n(d.get("approach_impulse"), ".4f"))
    print("  restraint          = %s" % n(d.get("restraint"), ".4f"))
    print("  pressure           = %s" % n(d.get("pressure"), ".4f"))
    meta = d.get("meta_json")
    if isinstance(meta, (bytes, bytearray)):
        meta = meta.decode("utf-8", "replace")
    try:
        m = json.loads(meta) if meta else {}
    except ValueError:
        m = {"_raw": str(meta)[:300]}
    for k in sorted(m):
        v = m[k]
        if isinstance(v, str) and len(v) > 19 and ("T" in v):
            print("  meta.%-22s = %s" % (k, fmt(parse(v))))
        else:
            s = json.dumps(v, ensure_ascii=False)
            print("  meta.%-22s = %s" % (k, s[:160]))

print()
print("=== outbox / action_attempts ===")
for r in con.execute(
        "select outbox_id, kind, status, priority, available_at, created_at,"
        " attempts, max_attempts, acked_at, last_error from outbox"
        " order by created_at desc limit 6"):
    print("  OUT %s %-10s %-12s avail=%s created=%s att=%s/%s acked=%s err=%s" % (
        r["outbox_id"][:18], str(r["kind"])[:10], str(r["status"])[:12],
        fmt(parse(r["available_at"])), fmt(parse(r["created_at"])),
        r["attempts"], r["max_attempts"],
        (r["acked_at"] or "-")[:19], str(r["last_error"])[:60]))
for r in con.execute(
        "select attempt_id, state, intent, created_at, updated_at, committed_at,"
        " rendered_text, failure_reason from action_attempts"
        " order by created_at desc limit 6"):
    print("  ATT %s %-14s created=%s updated=%s committed=%s" % (
        r["attempt_id"][:20], str(r["state"])[:14], fmt(parse(r["created_at"])),
        fmt(parse(r["updated_at"])), fmt(parse(r["committed_at"]))))
    if r["failure_reason"]:
        print("      fail: %s" % str(r["failure_reason"])[:100])
    if r["rendered_text"]:
        print("      text: %s" % str(r["rendered_text"])[:120])

print()
print("=== 结论：预测 ===")
if advs:
    a = mean_adv
    lam = lam_of(a)
    print("  采用近 %d 条均值 advantage = %+.4f" % (len(advs), a))
    print("  lam = %.3e /s  ->  期望间隔 %.2f h  (中位数 %.2f h)" % (
        lam, 1 / lam / 3600, math.log(2) / lam / 3600))
    print("  连续时间下 P(24h内开口) = %.1f%%" % ((1 - math.exp(-lam * 86400)) * 100))
    for h in (1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 48):
        print("      P(在 %2dh 内) = %5.1f%%" % (h, (1 - math.exp(-lam * h * 3600)) * 100))
    print("  离散化：每 %ds 评估一次 -> 每轮 p = %.4f%%" % (
        SCHED_MAX_S, (1 - math.exp(-lam * SCHED_MAX_S)) * 100))
    print("      每轮不开口概率 = %.4f%%" % (math.exp(-lam * SCHED_MAX_S) * 100))
    print("      期望落空轮数 = %.1f 轮 -> 期望等待 %.2f h" % (
        1 / (1 - math.exp(-lam * SCHED_MAX_S)),
        (1 / (1 - math.exp(-lam * SCHED_MAX_S))) * SCHED_MAX_S / 3600))
cd = parse(st["cooldown_until"]) if st else None
if cd:
    print("  冷却结束 = %s  (距今 %.2f h) -> 在此之前一律 cooldown_active，不掷骰" % (
        cd.strftime("%m-%d %H:%M"), (cd - NOW).total_seconds() / 3600))
    print("  冷却结束后每轮重掷；下表以冷却结束为 0 点")
    print("  %-8s %-22s %s" % ("分位", "距冷却结束", "日历时间(UTC / CST)"))
    for label, q in (("中位", 0.5), ("75%", 0.75), ("90%", 0.9)):
        n = 0
        p = 1 - math.exp(-lam * SCHED_MAX_S)
        while 1 - (1 - p) ** (n + 1) < q:
            n += 1
        when = cd + dt.timedelta(seconds=(n + 1) * SCHED_MAX_S)
        print("  %-8s %-22s %s / %s" % (
            label, "%.1f h" % ((n + 1) * SCHED_MAX_S / 3600),
            when.strftime("%m-%d %H:%M"),
            (when + dt.timedelta(hours=8)).strftime("%m-%d %H:%M")))
PY
