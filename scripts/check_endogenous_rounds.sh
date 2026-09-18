#!/usr/bin/env bash
# 所有实例最近一次 endogenous_round 是什么时候（判断调度是否整体停了）。
set -u

docker exec -i xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import glob
import os
import sqlite3

now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    if "decisions" not in tables:
        print("  %-14s 没有 decisions 表" % tag)
        con.close()
        continue
    row = con.execute(
        "select max(decided_at) from decisions where trigger='endogenous_round'").fetchone()
    total = con.execute("select count(*) from decisions").fetchone()[0]
    state = con.execute("select last_tick_at from runtime_state").fetchone()
    latest = row[0]
    age = ""
    if latest:
        try:
            stamp = dt.datetime.fromisoformat(str(latest).replace("Z", "").replace("+00:00", ""))
            age = "（%.1f 小时前）" % ((now - stamp).total_seconds() / 3600.0)
        except Exception:
            pass
    print("  %-14s decisions=%-5d 最近 endogenous=%s %s   last_tick=%s" % (
        tag, total, str(latest)[:19], age, str(state[0])[:19] if state else "-"))
    con.close()
PY
