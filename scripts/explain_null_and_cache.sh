#!/usr/bin/env bash
# 证实两件事：
#  1) NULL 的 settlement_source 行 = 由深层刷新结清的（deep_refresh_id 非空）
#  2) 解释缓存为空的路径证据：tasks 表里有没有 emotion_explain / psychological_interpretation
set -u

docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    if "event_semantics" not in tables:
        con.close()
        continue
    rule = con.execute(
        "select count(*) from event_semantics where settlement_source is not null").fetchone()[0]
    refresh = con.execute(
        "select count(*) from event_semantics where settlement_source is null"
        " and semantic_status='resolved' and deep_refresh_id is not null").fetchone()[0]
    other = con.execute(
        "select count(*) from event_semantics where settlement_source is null"
        " and semantic_status='resolved' and deep_refresh_id is null").fetchone()[0]
    unres = con.execute(
        "select count(*) from event_semantics where semantic_status='unresolved'").fetchone()[0]
    print("  %-14s 规则结清=%-5d 刷新结清=%-5d resolved 但两条路径都没有=%-5d 未决=%d" % (
        tag, rule, refresh, other, unres))
    con.close()

print()
print("  === tasks 表里的任务类型分布（解释缓存由哪种提案写入）===")
for path in sorted(glob.glob("/data/*/companion.sqlite3"))[:3]:
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    if "tasks" in tables:
        rows = con.execute("select task_type, count(*) from tasks group by task_type").fetchall()
        print("  %-14s %s" % (tag, dict(rows)))
    # 解释缓存表
    if "emotion_explanations" in tables:
        print("  %-14s emotion_explanations=%d" % (
            tag, con.execute("select count(*) from emotion_explanations").fetchone()[0]))
    con.close()
PY
