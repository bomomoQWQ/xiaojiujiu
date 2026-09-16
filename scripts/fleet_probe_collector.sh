#!/usr/bin/env bash
# 收集器现在到底收了什么：深刷新的结果有没有落盘？
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

path = "/data/default-friendmessage-1670681411/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print("== 所有表 ==")
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by 1")]
print(" ", ", ".join(tables))
print("== raw_events 里有没有 deep_refresh / reappraisal 痕迹 ==")
for row in con.execute(
    "select event_type, content, count(*) from raw_events group by 1,2 order by 3 desc"
):
    print("  ", row)
print("== 观测表行数 ==")
for table in ("decisions", "state_samples", "event_semantics", "interpretation_versions",
              "reappraisals", "memories", "unfinished_matters", "user_model_params"):
    try:
        n = con.execute(f"select count(*) from {table}").fetchone()[0]
        print(f"   {table}: {n}")
    except sqlite3.Error as exc:
        print(f"   {table}: (缺失) {exc}")
print("== 深刷新的时间戳只在 meta 里？ ==")
meta = con.execute("select meta_json from runtime_state limit 1").fetchone()[0]
print("  ", meta[:300])
PY
