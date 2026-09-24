#!/usr/bin/env bash
# 只测投递这一跳：把已渲染好的失败 send 行复活成 pending，看插件能否真的发出去。
set -u
BETA=/mnt/xz/xiaojiujiu-beta
SRC=/home/bomomo/astrbot_test/src/xiaojiujiu/scripts
ROW=obx_3c651c4bf751

echo "=== 0) 冻结 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data -v "$BETA":/export \
  -v "$SRC":/scripts:ro python:3.12-slim \
  python /scripts/snapshot_beta.py --data-root /data --root /export/snapshots \
  --fleet http://192.168.1.15:8800 --note "before reviving one failed send row" 2>&1 | tail -2

echo
echo "=== 1) 复活前 ==="
docker exec -i xxj-runtime-fleet python3 - "$ROW" <<'PY'
import os, sqlite3, sys

path = "/data/default-friendmessage-qq01/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
row = con.execute(
    "select status, attempts, max_attempts, lease_owner, available_at, last_error"
    " from outbox where outbox_id = ?", (sys.argv[1],),
).fetchone()
print("  status=%s attempts=%s/%s owner=%s available_at=%s error=%s" % row)
PY

echo
echo "=== 2) 复活（status=pending, attempts=0, 清租约, available_at=now）==="
docker exec -i xxj-runtime-fleet python3 - "$ROW" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone

path = "/data/default-friendmessage-qq01/companion.sqlite3"
now = datetime.now(timezone.utc).isoformat()
con = sqlite3.connect(path, timeout=15)
con.execute("pragma busy_timeout = 15000")
with con:
    con.execute(
        "update outbox set status = 'pending', attempts = 0, lease_owner = null,"
        " lease_expires_at = null, available_at = ?, acked_at = null,"
        " last_error = 'manual retry: revived to verify delivery after stopping the "
        "second OneBot client' where outbox_id = ?",
        (now, sys.argv[1]),
    )
row = con.execute(
    "select status, attempts, available_at, last_error from outbox where outbox_id = ?",
    (sys.argv[1],),
).fetchone()
print("  now:", row)
con.close()
PY

echo
echo "=== 3) 盯 60 秒（插件应当立刻领取 → authorize → send）==="
docker exec -i xxj-runtime-fleet python3 - "$ROW" <<'PY'
import json, sqlite3, sys, time

path = "/data/default-friendmessage-qq01/companion.sqlite3"
row_id = sys.argv[1]
last = None
for _ in range(30):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = con.execute(
        "select status, attempts, lease_owner, last_error from outbox where outbox_id = ?",
        (row_id,),
    ).fetchone()
    if row != last:
        print(f"  [{time.strftime('%H:%M:%S')}] status={row[0]} attempts={row[1]} "
              f"owner={row[2]} error={str(row[3])[:80]}")
        last = row
    if row[0] in ("delivered", "failed", "rejected"):
        break
    con.close()
    time.sleep(2)
con.close()
PY

echo
echo "=== 4) 这条 attempt 的事件链 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

path = "/data/default-friendmessage-qq01/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
for row in con.execute(
    "select created_at, from_state, to_state, reason from attempt_events"
    " where attempt_id = 'att_b8476e1e2a21' order by created_at"
):
    print(f"  {str(row[0])[11:19]} {row[1]} -> {row[2]}  {row[3]}")
PY

echo
echo "=== 5) 插件日志（近 3 分钟）==="
docker logs astrbot-test --since 3m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -12
