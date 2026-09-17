#!/usr/bin/env bash
# 重复的 10 条各自的 source_event_ids 是什么（决定去重该按事件还是按主题）。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, json, sqlite3

path = glob.glob("/data/default-friendmessage-1670681411/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print("  == 每条未尽之事的来源事件 ==")
for row in con.execute(
    "select unfinished_id, status, source_event_ids, substr(title,1,34), created_at"
    " from unfinished_matters order by created_at"
):
    print(f"   {row[0]} | {row[1]} | {row[2]} | {row[3]} | {str(row[4])[:19]}")
print()
print("  == 这些来源事件是什么 ==")
ids = set()
for row in con.execute("select source_event_ids from unfinished_matters"):
    try:
        for item in json.loads(row[0] or "[]"):
            ids.add(item)
    except Exception:
        pass
for event_id in sorted(ids):
    row = con.execute(
        "select created_at, event_type, substr(coalesce(content,''),1,30) from raw_events where event_id = ?",
        (event_id,),
    ).fetchone()
    print("   ", event_id, "->", row)
PY
