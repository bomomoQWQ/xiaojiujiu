#!/usr/bin/env bash
# 按 hazard 公式估"多久主动发一次"。
#
#   λ = hazard_base(3.0e-5) × softplus(hazard_beta(4.0) × advantage)
#   P(act within dt) = 1 - exp(-λ·dt)   ⇒ 期望等待 1/λ
#
# 已用真实数据验证过：真人 16:13 那条 hazard=3.5e-05 反解出 adv=0.1989，
# 账本记的 advantage=0.198875；qq03 的 hazard=7.1e-05 反解 adv=0.567，
# 账本记 0.5693。公式与观测一致。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, math, os, sqlite3

BASE, BETA = 3.0e-5, 4.0
COOLDOWN_S = 2400.0
DAILY_CAP = 12
SCHED_MAX_S = 900.0


def softplus(x):
    return math.log1p(math.exp(x)) if x < 50 else x


def mean_interval(adv):
    lam = BASE * softplus(BETA * adv)
    return lam, (1.0 / lam if lam > 0 else float("inf"))


def per_round_p(adv, dt=SCHED_MAX_S):
    lam, _ = mean_interval(adv)
    return 1.0 - math.exp(-lam * dt)


print(f"  {'person':42s} {'adv最近':>8s} {'λ(/s)':>10s} {'期望间隔':>10s} {'每天≈':>7s} {'900s内概率':>10s}")
rows = []
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    recent = con.execute(
        "select advantage from decisions where reason not in ('foreground_pause')"
        " order by decided_at desc limit 5"
    ).fetchall()
    if not recent:
        print(f"  {person:42s} {'(无判决)':>8s}")
        continue
    adv = sum(r[0] for r in recent) / len(recent)
    lam, interval = mean_interval(adv)
    hours = interval / 3600.0
    per_day = 24.0 / hours if hours > 0 else float("inf")
    print(f"  {person:42s} {adv:8.3f} {lam:10.2e} {hours:9.1f}h {per_day:7.1f} "
          f"{per_round_p(adv) * 100:9.1f}%")
    rows.append((person, adv, hours, per_day))

print()
print("  参照：只改价值观之前的典型 advantage（多数为负或接近 0）")
for adv in (-0.2, 0.0, 0.132, 0.33, 0.57, 1.0):
    lam, interval = mean_interval(adv)
    print(f"    adv={adv:+.3f} -> 期望间隔 {interval / 3600.0:6.1f}h  每天≈{24.0 / (interval / 3600.0):5.1f} 条")
print()
print(f"  约束上限：冷却 {COOLDOWN_S / 60:.0f} 分钟、每天最多 {DAILY_CAP} 条"
      f" ⇒ 只有期望间隔 < {24.0 / DAILY_CAP:.1f}h 时才会撞到日上限")
PY
