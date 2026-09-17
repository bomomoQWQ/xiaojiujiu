#!/usr/bin/env bash
# 部署去重修复 + 清理历史重复的未完之事（先看是否正在聊天）。
set -u
BETA=/mnt/xz/xiaojiujiu-beta

echo "=== 0) 现在是否正在聊天（最后一条事件多久之前）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, sqlite3
from datetime import datetime, timezone

path = glob.glob("/data/default-friendmessage-1670681411/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
last = con.execute("select max(created_at) from raw_events").fetchone()[0]
now = datetime.now(timezone.utc)
ts = datetime.fromisoformat(last)
print(f"  最后一条事件: {last}  （{(now - ts).total_seconds():.0f} 秒前）")
for row in con.execute(
    "select created_at, event_type, substr(coalesce(content,''),1,30) from raw_events"
    " order by created_at desc limit 4"
):
    print("   ", str(row[0])[:19], row[1], row[2])
PY

echo
echo "=== 1) 冻结 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data -v "$BETA":/export \
  -v /home/bomomo/astrbot_test/src/xiaojiujiu/scripts:/scripts:ro python:3.12-slim \
  python /scripts/snapshot_beta.py --data-root /data --root /export/snapshots \
  --fleet http://192.168.1.15:8800 --note "before unfinished dedup cleanup" 2>&1 | tail -2

echo
echo "=== 2) 清理历史重复：每个来源事件只留最早一条，其余标 invalidated ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, json, sqlite3
from datetime import datetime, timezone

path = glob.glob("/data/default-friendmessage-1670681411/companion.sqlite3")[0]
con = sqlite3.connect(path, timeout=15)
con.execute("pragma busy_timeout = 15000")
stamp = datetime.now(timezone.utc).isoformat()

rows = con.execute(
    "select unfinished_id, status, source_event_ids, created_at from unfinished_matters"
    " where status in ('open','waiting','due','muted') order by created_at"
).fetchall()
kept: dict[str, str] = {}
doomed: list[tuple[str, str]] = []
for unfinished_id, status, sources, created_at in rows:
    try:
        ids = json.loads(sources or "[]")
    except Exception:
        ids = []
    key = ",".join(sorted(str(x) for x in ids)) or f"__none__:{unfinished_id}"
    if key in kept:
        doomed.append((unfinished_id, kept[key]))
    else:
        kept[key] = unfinished_id

print(f"  在用条目 {len(rows)} 条 → 保留 {len(kept)} 条，作废 {len(doomed)} 条")
if doomed:
    with con:
        for unfinished_id, primary in doomed:
            con.execute(
                "update unfinished_matters set status = 'invalidated',"
                " resolution_note = ?, updated_at = ? where unfinished_id = ?",
                (
                    f"duplicate of {primary}: the deep refresh re-proposed the same "
                    "obligation (fixed in b17af1c)",
                    stamp,
                    unfinished_id,
                ),
            )
print("  作废明细:")
for unfinished_id, primary in doomed:
    print(f"   {unfinished_id} -> 保留 {primary}")
con.close()
PY

echo
echo "=== 3) 结果 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, sqlite3

path = glob.glob("/data/default-friendmessage-1670681411/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
for row in con.execute("select status, count(*) from unfinished_matters group by 1"):
    print("   ", row)
print("  在用条目:")
for row in con.execute(
    "select unfinished_id, status, substr(title,1,44) from unfinished_matters"
    " where status in ('open','waiting','due')"
):
    print("   ", row[0], row[1], row[2])
PY
