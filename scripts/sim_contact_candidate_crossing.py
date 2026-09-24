"""qq06：那个 resting contact 候选什么时候能翻过沉默？

这个实例池子里只有它（0 未完之事 / 0 记忆 / 0 情绪 / 0 用户消息增量），所以就是设计文档里
说的"孤独本身够不够越线"那一场赛跑。公式全部来自 runtime 源码，并且**已用账本逐项复现过**：

    internal = 0.8525*need + 0.026                       (与账本 0.455106 一致，need=0.503)
    V_contact = sigmoid(-0.25 + 2.6*I + 1.4*P - 0.9*R)   (candidate.py:293)
    need = clamp(0.25 + 0.7*V_contact)
    U_contact = internal + K,  K = 0.159911               (user+relation-各项成本-uncpen，预测相关)
    U_silence = 0.28 + 0.45R + 0.42I - 1.2*P^2 + 0.25*0.5
                (最后一项：last_contact_at 为 None -> cooldown_term=0.5；boundary_risk=0)
    过线条件（motivation.py:902，严格大于）：U_contact > U_silence，否则 eligible 为空、hazard=0

状态前推用源码里的目标函数与步进：
    impulse_logit  = -1.40 + 1.85*a - 0.40*busy
    restraint_logit= -0.14 + 0.45*busy            (uncertainty=0.35, values=yandere 档)
    a = min(1, hours_since_exchange/36)           busy 由"距上条用户消息"决定
    dP/dt = 6e-5*(1-P)*softplus(4(I-R)) - 4e-5*P*softplus(4(R-I))
need 只在候选被重建时刷新（candidate.py:1113 的 `if "contact" not in existing_types`），
TTL=6h，所以是每 6 小时按当时状态刷新一次，中间冻结。
"""
import datetime as dt
import math

TAU_I, TAU_R = 5400.0, 9000.0
KAPPA_P, KAPPA_M = 6.0e-5, 4.0e-5
BETA = 4.0
STEP = 898.0
TTL = 6 * 3600.0
HAZARD_BASE, HAZARD_BETA = 3.0e-5, 4.0

NOW = dt.datetime(2026, 9, 17, 11, 47, 0)
LAST_EXCHANGE = dt.datetime(2026, 9, 17, 5, 28, 34)
CST = dt.timedelta(hours=8)

I0, R0, P0 = 0.2095, 0.5083, 0.0651
K = 0.159911          # 预测相关的那部分（held constant —— 会缓慢漂移，见脚本末尾说明）


def softplus(x):
    return math.log1p(math.exp(x)) if x < 50 else x


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def busy_at(now):
    hours = (now - LAST_EXCHANGE).total_seconds() / 3600.0
    if hours >= 8.0:
        return 0.60
    if hours >= 4.0:
        return 0.45
    return 0.25


def targets(now):
    a = min(1.0, (now - LAST_EXCHANGE).total_seconds() / 3600.0 / 36.0)
    busy = busy_at(now)
    imp = clamp(sigmoid(-1.40 + 1.85 * a - 0.40 * busy))
    res = clamp(sigmoid(-0.14 + 0.45 * busy))
    return imp, res, a, busy


def need_from(I, R, P):
    v = clamp(sigmoid(-0.25 + 2.6 * I + 1.4 * P - 0.9 * R))
    return clamp(0.25 + 0.7 * v), v


def u_contact(need):
    return (0.8525 * need + 0.026) + K


def u_silence(I, R, P):
    return 0.28 + 0.45 * R + 0.42 * I - 1.2 * P * P + 0.25 * 0.5


def haz(adv):
    return HAZARD_BASE * softplus(HAZARD_BETA * adv)


I, R, P = I0, R0, P0
need, v = need_from(I, R, P)
next_ttl = NOW + dt.timedelta(seconds=TTL)
t = NOW
print("now = %s UTC = %s CST" % (NOW.strftime("%H:%M"), (NOW + CST).strftime("%m-%d %H:%M")))
print("起点 I=%.4f R=%.4f P=%.4f -> need=%.4f  U_contact=%.4f  U_silence=%.4f  adv=%+.4f" % (
    I, R, P, need, u_contact(need), u_silence(I, R, P), u_contact(need) - u_silence(I, R, P)))
need_star = (u_silence(I, R, P) - K - 0.026) / 0.8525
print("当前要过线需要 need > %.4f（现在 %.4f）" % (need_star, need))
print()
print("%-7s %-6s %-6s %-6s %-6s %-7s %-9s %-9s %-8s %s" % (
    "时刻", "I", "R", "P", "need", "V", "U_contact", "U_silence", "adv", "过线?"))
crossed_at = None
rounds = 0
while t < NOW + dt.timedelta(hours=72):
    t += dt.timedelta(seconds=STEP)
    rounds += 1
    imp_t, res_t, a, busy = targets(t)
    dt_s = STEP
    I += (imp_t - I) * (1.0 - math.exp(-dt_s / TAU_I))
    R += (res_t - R) * (1.0 - math.exp(-dt_s / TAU_R))
    gap = I - R
    acc = KAPPA_P * (1.0 - P) * softplus(BETA * gap)
    dis = KAPPA_M * P * softplus(-BETA * gap)
    P = clamp(P + (acc - dis) * dt_s)
    if t >= next_ttl:                      # TTL 到期 -> 重建，need 按当时状态刷新
        need, v = need_from(I, R, P)
        next_ttl += dt.timedelta(seconds=TTL)
    uc, us = u_contact(need), u_silence(I, R, P)
    adv = uc - us
    if crossed_at is None and adv > 0:
        crossed_at = t
    if rounds % 12 == 0 or (crossed_at and abs((t - crossed_at).total_seconds()) < STEP):
        print("%-7s %-6.3f %-6.3f %-6.3f %-6.3f %-7.4f %-9.4f %-9.4f %+-8.4f %s" % (
            (t + CST).strftime("%m-%d %H:%M"), I, R, P, need, v, uc, us, adv,
            "★ 过线" if adv > 0 else ""))
        if crossed_at and t >= crossed_at + dt.timedelta(hours=6) and rounds % 12 == 0:
            break

print()
if crossed_at is None:
    print(">>> 72 小时内那个候选始终没能严格超过沉默（需要 need > %.4f）" % need_star)
else:
    print(">>> 过线时刻: %s CST（距现在 %.1f 小时）" % (
        (crossed_at + CST).strftime("%m-%d %H:%M"), (crossed_at - NOW).total_seconds() / 3600))
    adv = u_contact(need) - u_silence(I, R, P)
    print("    过线时 adv=%+.4f -> λ=%.2e -> 每轮 %.2f%%" % (
        adv, haz(adv), (1 - math.exp(-haz(adv) * STEP)) * 100))
    print()
    print("    过线之后的掷骰（从该时刻起，几何分布）:")
    tt, survive = crossed_at, 1.0
    marks = {}
    for _ in range(400):
        adv_t = adv
        survive *= math.exp(-haz(adv_t) * STEP)
        hit = 1 - survive
        for lab, q in (("25%", 0.25), ("中位", 0.5), ("75%", 0.75), ("90%", 0.9)):
            if lab not in marks and hit >= q:
                marks[lab] = tt
        tt += dt.timedelta(seconds=STEP)
    for lab in ("25%", "中位", "75%", "90%"):
        if lab in marks:
            print("      %-4s %s CST" % (lab, (marks[lab] + CST).strftime("%m-%d %H:%M")))
    print()
    print("    注意：过线后 hazard 仍随状态缓慢变化（这里用了过线时的 adv 常数近似），")
    print("    且一旦她真的发出，cooldown 2400s + 之后 1 小时的压低窗口会重新开始。")
