#!/usr/bin/env bash
# 主动消息到底有没有发出去：逐实例看 proactive 事件、action_attempts、outbox。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, os, sqlite3

LIVE = ("proposed", "committed", "rendering", "ready_to_send", "sent", "resolved")

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    acted = con.execute("select count(*) from decisions where acted = 1").fetchone()[0]
    triggered = con.execute(
        "select count(*) from decisions where reason = 'hazard_triggered'"
    ).fetchone()[0]
    attempts = con.execute("select count(*) from action_attempts").fetchone()[0]
    states = con.execute(
        "select state, count(*) from action_attempts group by 1 order by 2 desc"
    ).fetchall()
    outbox = con.execute(
        "select kind, status, count(*) from outbox group by 1,2 order by 3 desc"
    ).fetchall()
    events = con.execute(
        "select event_type, count(*) from raw_events where event_type like 'proactive%'"
        " group by 1"
    ).fetchall()

    print(f"  === {person} ===")
    print(f"    判决 acted=1: {acted}   hazard_triggered: {triggered}")
    print(f"    action_attempts: {attempts}  状态分布: {[(r[0], r[1]) for r in states]}")
    print(f"    outbox(kind,status,count): {[(r[0], r[1], r[2]) for r in outbox]}")
    print(f"    proactive 事件: {[(r[0], r[1]) for r in events]}")
PY
