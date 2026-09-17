#!/usr/bin/env bash
# 只读估算"下一轮会掷出什么骰子"。
#
#   context 由 _situation_context 的规则重建（/situation 返回的不是它）：
#     busy_probability   <- 距上条用户消息的小时数（<0.5h 0.15 / >=8h 0.60 / >=4h 0.45 / else 0.25）
#     recent_contact_count <- 近 repeat_window(3600s) 内的 proactive_sent 事件数
#     hours_since_contact  <- 距 last_contact_at（可能为负 -> features 里 clamp 到 0）
#   先用 10:01 那轮的已知预测做自校验，再算当前。
# 两个端点都显式不做 tick，因此不改变任何状态。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import json
import math
import sqlite3
import urllib.request

BASE = "http://127.0.0.1:8787"
DB = "file:/data/default-friendmessage-1670681411/companion.sqlite3?mode=ro"
VALUES = {"boundary_respect": 0.05, "user_care": 1.0, "relationship_maintenance": 1.0,
          "stability_commitment": 1.0, "conflict_directness": 1.0, "curiosity": 1.0}
G = {"internal_gain": 0.65, "urgency_gain": 0.7, "user_gain": 0.85, "relation_gain": 0.45,
     "boundary_cost_gain": 0.75, "interrupt_gain": 0.18, "risk_gain": 0.1,
     "uncertainty_penalty": 0.12}
NEUTRAL_W, NEGATIVE_W = 0.85, 1.4
HAZARD_BASE, HAZARD_BETA = 3.0e-5, 4.0


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def parse(ts):
    return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def busy_of(hours, replied_recently):
    b = 0.25
    if replied_recently:
        b = 0.15
    elif hours >= 8.0:
        b = 0.60
    elif hours >= 4.0:
        b = 0.45
    return b


def make_ctx(*, now, last_user, last_contact, recent_contacts, permission):
    hu = (now - parse(last_user)).total_seconds() / 3600.0 if last_user else 99.0
    hc = (now - parse(last_contact)).total_seconds() / 3600.0 if last_contact else 0.0
    return {
        "busy_probability": busy_of(hu, hu * 3600.0 < 1800.0),
        "recent_contact_count": recent_contacts,
        "hours_since_contact": hc,
        "user_active_now": hu * 3600.0 < 300.0,
        "ever_boundary": False,
        "novelty": 0.6,
        "explicit_permission": permission,
        "now": now.isoformat(),
        "local_hour": (now + dt.timedelta(hours=8)).hour,
    }


def utility(pred, unf, need, ea=0.2):
    reply = pred["reply_probability"]
    pos = pred["positive_probability"]
    cont = pred["continue_probability"]
    risk = pred["boundary_risk"]
    unc = pred["uncertainty"]
    good = pred.get("good_outcome_probability")
    neutral = pred.get("neutral_outcome_probability")
    bad = pred.get("bad_outcome_probability")
    conf = pred.get("outcome_confidence")
    if conf is None:
        conf = pred.get("confidence", 0.0)
    urgency = G["urgency_gain"] * max(unf * 1.25, need * 0.8) if (unf > 0 or need > 0) else 0.0
    internal = G["internal_gain"] * (0.45 * need + 0.35 * unf + 0.20 * clamp(ea)) + urgency
    user = G["user_gain"] * reply * (0.55 * good + 0.45 * cont
                                     - conf * (NEUTRAL_W * neutral + NEGATIVE_W * bad))
    relation = G["relation_gain"] * (0.5 * VALUES["relationship_maintenance"] * clamp(unf + 0.35)
                                     + 0.3 * VALUES["user_care"] * pos
                                     + 0.2 * VALUES["stability_commitment"] * cont)
    bcost = G["boundary_cost_gain"] * VALUES["boundary_respect"] * max(risk, 0.0)
    icost = G["interrupt_gain"] * (0.6 * risk + 0.4 * (1.0 - reply))
    kcost = G["risk_gain"] * unc * (0.5 + risk)
    total = internal + user + relation - bcost - icost - kcost - G["uncertainty_penalty"] * unc
    return total, dict(internal=internal, user=user, relation=relation, bcost=bcost,
                       icost=icost, kcost=kcost, uncpen=G["uncertainty_penalty"] * unc,
                       reply=reply, pos=pos, cont=cont, risk=risk, unc=unc, conf=conf)


con = sqlite3.connect(DB, uri=True)
row = con.execute("select * from runtime_state").fetchone()
cols = [d[0] for d in con.execute("select * from runtime_state").description]
st = dict(zip(cols, row))
n_pro = con.execute("select count(*) from raw_events where event_type='proactive_sent'"
                    " and timestamp >= ?", ((dt.datetime.now(dt.timezone.utc)
                                             - dt.timedelta(seconds=3600)).isoformat(),)).fetchone()[0]
n_pro_all = con.execute("select count(*) from raw_events where event_type='proactive_sent'").fetchone()[0]
n_bound = con.execute("select count(*) from boundaries").fetchone()[0]
con.close()
NOW = dt.datetime.now(dt.timezone.utc)
print("now = %s UTC   proactive_sent 近1h=%d 累计=%d  boundaries=%d" % (
    NOW.strftime("%H:%M:%S"), n_pro, n_pro_all, n_bound))
print("last_user_message=%s  last_contact=%s  cooldown_until=%s  last_exchange=%s" % (
    str(st["last_user_message_at"])[:19], str(st["last_contact_at"])[:19],
    str(st["cooldown_until"])[:19], str(st["last_exchange_at"])[:19]))

print()
print("=== 自校验：用 10:01 的 context 复现账本里的预测 ===")
print("    账本: reply=0.589628 pos=0.618689 cont=0.63714 risk=0.267853 unc=0.852776")
for perm in (False, True):
    ctx = make_ctx(now=parse("2026-09-17T10:01:19+00:00"),
                   last_user="2026-09-17T06:46:10+00:00", last_contact=None,
                   recent_contacts=0, permission=perm)
    p = post("/user-model/predict", {"action": {"type": "follow_up", "proactive": True}, "context": ctx})
    print("    permission=%-5s -> reply=%.6f pos=%.6f cont=%.6f risk=%.6f unc=%.6f" % (
        perm, p["reply_probability"], p["positive_probability"],
        p["continue_probability"], p["boundary_risk"], p["uncertainty"]))

print()
print("=== 当前 -> 冷却结束后第一轮（11:07:20 UTC）的预测 ===")
first_round = parse(st["cooldown_until"]) + dt.timedelta(seconds=291)
for ctype, unf, need in (("follow_up", 0.5, 0.65), ("contact", 0.0, 0.781335)):
    for perm in (False, True):
        ctx = make_ctx(now=first_round, last_user=st["last_user_message_at"],
                       last_contact=st["last_contact_at"], recent_contacts=n_pro,
                       permission=perm)
        p = post("/user-model/predict", {"action": {"type": ctype, "proactive": True}, "context": ctx})
        total, parts = utility(p, unf, need)
        print("  %-10s perm=%-5s reply=%.4f pos=%.4f cont=%.4f risk=%.4f unc=%.4f -> U=%.4f" % (
            ctype, perm, parts["reply"], parts["pos"], parts["cont"], parts["risk"],
            parts["unc"], total))
        if ctype == "follow_up" and perm is False:
            print("      分解: internal=%.4f user=%.4f relation=%.4f bcost=%.4f icost=%.4f"
                  " kcost=%.4f uncpen=%.4f" % (parts["internal"], parts["user"], parts["relation"],
                                               parts["bcost"], parts["icost"], parts["kcost"],
                                               parts["uncpen"]))
            BEST = total

# --- U_silence（下一轮时刻）
hours = (first_round - parse(st["last_exchange_at"])).total_seconds() / 3600.0
ct = clamp(1.0 - hours / 24.0) * 0.5
sil = (0.28 + 0.45 * st["restraint"] + 0.30 * 0.0 + 0.25 * ct
       + 0.42 * st["approach_impulse"] - 1.2 * st["pressure"] ** 2)
print()
print("=== U_silence（距上次交流 %.2f h -> cooldown_term=%.4f）===" % (hours, ct))
print("  U_silence = %.4f  (imp=%.4f restr=%.4f pres=%.4f)" % (
    sil, st["approach_impulse"], st["restraint"], st["pressure"]))
adv = BEST - sil
lam = HAZARD_BASE * (math.log1p(math.exp(HAZARD_BETA * adv)) if adv < 50 else HAZARD_BETA * adv)
print()
print("=== 结论 ===")
print("  U_max = %.4f  U_silence = %.4f  -> advantage = %+.4f" % (BEST, sil, adv))
print("  lambda = %.3e /s   每轮(891s) p = %.2f%%" % (lam, (1 - math.exp(-lam * 891)) * 100))
print("  期望间隔 %.2f h   中位 %.2f h" % (1 / lam / 3600, math.log(2) / lam / 3600))
for h in (1, 3, 6, 12, 18, 24):
    print("     %2dh 内开口概率 %5.1f%%" % (h, (1 - math.exp(-lam * h * 3600)) * 100))
PY
