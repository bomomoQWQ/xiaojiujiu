"""过夜简报：Runtime 侧。重点看 (b) 的效果、主动消息是否真的发出、有无异常。"""
import datetime as dt
import glob
import json
import os
import sqlite3

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
CST = dt.timedelta(hours=8)
#: 部署 a+b 之后的时点（fleet 容器重建时间附近）
CUTOFF = dt.datetime(2026, 9, 17, 12, 10, 0)

PORTS = {
    "default-friendmessage-1670681411": 8787, "default-friendmessage-994959351": 8788,
    "default-friendmessage-1913447173": 8789, "default-friendmessage-728260403": 8790,
    "default-friendmessage-1070754640": 8791, "default-friendmessage-2206929446": 8792,
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


print("now = %s UTC = %s CST      观察窗口起点 = %s UTC" % (
    NOW.strftime("%m-%d %H:%M"), cst(NOW), CUTOFF.strftime("%m-%d %H:%M")))
print()

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    name = os.path.basename(os.path.dirname(path))
    tag = name.replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    active_matters = con.execute(
        "select count(*) from unfinished_matters where status in ('open','waiting','due','muted')"
    ).fetchone()[0]
    new_matters = con.execute(
        "select unfinished_id, title, priority, created_at from unfinished_matters"
        " where created_at > ? order by created_at", (CUTOFF.isoformat(),)).fetchall()
    active_cands = con.execute(
        "select count(*) from candidate_intents where status in ('new','active')").fetchone()[0]
    decisions = con.execute(
        "select decided_at, reason, advantage, hazard, acted from decisions"
        " where decided_at > ? and delta_t < 2000 order by decided_at desc limit 4",
        (CUTOFF.isoformat(),)).fetchall()
    triggered = con.execute(
        "select count(*) from decisions where decided_at > ? and reason='hazard_triggered'",
        (CUTOFF.isoformat(),)).fetchone()[0]
    sent = con.execute(
        "select timestamp, content from raw_events where event_type='proactive_sent'"
        " and timestamp > ? order by timestamp", (CUTOFF.isoformat(),)).fetchall()
    attempts = con.execute(
        "select attempt_id, state, created_at, rendered_text, failure_reason from action_attempts"
        " where created_at > ? order by created_at", (CUTOFF.isoformat(),)).fetchall()
    refreshes = con.execute(
        "select count(*) n, sum(ran) ran, sum(operations) ops, sum(settled_events) settled,"
        " sum(degraded) degraded, max(ran_at) last_at,"
        " (select reason from refresh_runs where ran_at > ? order by ran_at desc limit 1) last_reason"
        " from refresh_runs where ran_at > ?", (CUTOFF.isoformat(), CUTOFF.isoformat())).fetchone()
    con.close()

    print("=" * 96)
    print("用户 %s  端口 %s" % (tag, PORTS.get(name, "?")))
    print("  活跃未完之事 %d 件 | 活跃候选 %d 个 | 窗口内 hazard 触发 %d 次" % (
        active_matters, active_cands, triggered))
    if new_matters:
        print("  ↓ 窗口内新建的未完之事（看有没有又长出重复的）:")
        for m in new_matters:
            print("     %s  pri=%.2f  %s" % (cst(parse(m["created_at"])), m["priority"],
                                            m["title"][:66]))
    else:
        print("  ↓ 窗口内没有新建未完之事")
    if refreshes and refreshes["n"]:
        print("  深刷新: %d 次，ran=%s，operations=%s，settled=%s，degraded=%s，最后一次 %s（%s）" % (
            refreshes["n"], refreshes["ran"], refreshes["ops"], refreshes["settled"],
            refreshes["degraded"], cst(parse(refreshes["last_at"])), refreshes["last_reason"]))
    else:
        print("  深刷新: 窗口内没有运行记录")
    if decisions:
        print("  最近判决:")
        for d in decisions:
            print("     %s %-22s adv=%+.5f haz=%.2e acted=%s" % (
                d["decided_at"][:19], d["reason"], d["advantage"], d["hazard"], d["acted"]))
    if sent:
        print("  ** 真的发出去了的主动消息 **")
        for s in sent:
            print("     %s  %s" % (s["timestamp"][:19], (s["content"] or "")[:70]))
    else:
        print("  窗口内没有成功发出的主动消息")
    if attempts:
        print("  行动尝试:")
        for a in attempts:
            print("     %s %-14s %s" % (a["created_at"][:19], a["state"],
                                        (a["failure_reason"] or "")[:50]))
            print("        %s" % (a["rendered_text"] or "")[:70])
    print()
