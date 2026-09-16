#!/usr/bin/env bash
# 那条主动消息去哪了：outbox / 投递尝试 / 授权。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, sqlite3

path = "/data/default-friendmessage-1670681411/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def dump(table, limit=8):
    print(f"== {table} ==")
    try:
        rows = con.execute(f"select * from {table} order by rowid desc limit {limit}").fetchall()
    except sqlite3.Error as exc:
        print("  ", exc)
        return
    if not rows:
        print("   (空)")
    for row in rows:
        data = dict(row)
        print("  ", json.dumps(data, ensure_ascii=False, default=str)[:300])


for table in ("outbox", "action_attempts", "attempt_events"):
    dump(table)
    print()

print("== 主动相关事件全文 ==")
for row in con.execute(
    "select created_at, event_type, content, metadata_json from raw_events"
    " where event_type like '%proactive%' or content like '%proactive%' order by created_at"
):
    print(f"  {str(row['created_at'])[:19]} {row['event_type']}: {str(row['content'])[:150]}")
    print(f"      meta: {str(row['metadata_json'])[:220]}")
PY
