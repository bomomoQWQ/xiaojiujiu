"""测试者最近还活着吗：每个 Runtime 的最后一条用户消息时间 + 平台。"""
import glob
import json
import os
import sqlite3

paths = sorted(glob.glob("/data/*/companion.sqlite3"))
print("实例数:", len(paths))
print()
print("%-34s %-22s %-14s %s" % ("session", "最后用户消息(UTC)", "平台", "总用户消息数"))
for path in paths:
    name = os.path.basename(os.path.dirname(path))
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        row = con.execute(
            "select timestamp, metadata_json from raw_events where event_type='user_message'"
            " order by timestamp desc limit 1").fetchone()
        total = con.execute(
            "select count(*) from raw_events where event_type='user_message'").fetchone()[0]
        con.close()
    except sqlite3.Error as exc:
        print("%-34s ERR %s" % (name, exc))
        continue
    if not row:
        print("%-34s %-22s %-14s %s" % (name, "(从无)", "-", 0))
        continue
    plat = "?"
    try:
        plat = (json.loads(row[1] or "{}").get("platform")) or "?"
    except ValueError:
        pass
    print("%-34s %-22s %-14s %s" % (name, str(row[0])[:19], plat, total))
