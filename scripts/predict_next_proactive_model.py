"""推算下一次主动消息的时间分布。

模型（与 runtime 实现一一对应）：
  lam(adv) = hazard_base(3.0e-5) * softplus(4.0 * adv)
  p_round  = 1 - exp(-lam * dt)          dt ≈ 891s（实测 10:22:29 -> 10:37:20）
  每轮独立，故 P(前 n 轮都不开口) = Π (1 - p)
  冷却 2400s 期间不掷骰（reason=cooldown_active, hazard=0）

时间轴以"冷却结束"为 0 点；第一轮在冷却结束后 FIRST_TRIAL_OFFSET_S 秒。

用法: python predict_next_proactive_model.py [冷却结束UTC ISO] [adv ...]
"""
from __future__ import annotations

import datetime as dt
import math
import sys

BASE, BETA = 3.0e-5, 4.0
DT = 891.0
FIRST_TRIAL_OFFSET_S = 291.0  # 11:02:29 冷却结束 -> 11:07:20 第一轮


def softplus(x: float) -> float:
    return math.log1p(math.exp(x)) if x < 50 else x


def lam(adv: float) -> float:
    return BASE * softplus(BETA * adv)


def p_round(adv: float) -> float:
    return 1.0 - math.exp(-lam(adv) * DT)


def curve(adv: float, *, horizon_h: float = 72.0):
    """返回 [(距冷却结束的小时数, P(已开口))]。"""
    out = []
    n = 0
    while True:
        t = FIRST_TRIAL_OFFSET_S + n * DT
        if t > horizon_h * 3600.0:
            break
        p = p_round(adv)
        out.append((t / 3600.0, 1.0 - (1.0 - p) ** (n + 1)))
        n += 1
    return out


def stats(adv: float):
    cv = curve(adv)
    q = {}
    for target in (0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99):
        q[target] = next((h for h, c in cv if c >= target), None)
    mean_h = (FIRST_TRIAL_OFFSET_S + DT / p_round(adv)) / 3600.0
    return q, mean_h


def main() -> None:
    cd_end = dt.datetime.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    advs = [float(x) for x in sys.argv[2:]] or [0.26, 0.30, 0.35, 0.40, 0.42]
    print("hazard_base=%.1e hazard_beta=%.1f dt=%.0fs 第一轮偏移=%.0fs" % (
        BASE, BETA, DT, FIRST_TRIAL_OFFSET_S))
    if cd_end:
        print("冷却结束 = %s UTC = %s CST" % (
            cd_end.strftime("%m-%d %H:%M"),
            (cd_end + dt.timedelta(hours=8)).strftime("%m-%d %H:%M")))
        print("(.5h 后第一轮 = %s UTC)" % (
            (cd_end + dt.timedelta(seconds=FIRST_TRIAL_OFFSET_S)).strftime("%m-%d %H:%M")))
    print()

    head = "  adv      lam(/s)   每轮p    期望h   中位   25%    75%    90%    99%"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for adv in advs:
        q, mean_h = stats(adv)
        def r(x):
            return "  -  " if x is None else "%5.1f" % x
        print("  %+.3f  %.2e  %5.2f%%  %5.2f  %s %s %s %s %s" % (
            adv, lam(adv), p_round(adv) * 100, mean_h,
            r(q[0.5]), r(q[0.25]), r(q[0.75]), r(q[0.9]), r(q[0.99])))
    print()
    print("  （表内小时数 = 距冷却结束的时间；adv 越大越激进）")
    print()

    for adv in advs:
        q, mean_h = stats(adv)
        print("  === adv=%+.3f  lam=%.2e  每轮 %.2f%% ===" % (adv, lam(adv), p_round(adv) * 100))
        for h in (1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 48):
            print("      %2dh 内开口 %5.1f%%" % (h, (1 - math.exp(-lam(adv) * h * 3600)) * 100))
        if cd_end:
            for label, h in (("25%", q[0.25]), ("中位", q[0.5]), ("75%", q[0.75]),
                             ("90%", q[0.9]), ("期望", mean_h)):
                if h is None:
                    continue
                when = cd_end + dt.timedelta(seconds=h * 3600.0)
                print("      %-4s %s UTC = %s CST" % (
                    label, when.strftime("%m-%d %H:%M"),
                    (when + dt.timedelta(hours=8)).strftime("%m-%d %H:%M")))
        print()


if __name__ == "__main__":
    main()
