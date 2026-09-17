#!/usr/bin/env bash
# c′：复活 attempt（带审计事件）+ outbox 行，完成"只测投递这一跳"。
set -u
ATT=att_b8476e1e2a21
ROW=obx_3c651c4bf751
DB=/data/default-friendmessage-1670681411/companion.sqlite3

echo "=== 1) 改动（并留下审计事件）==="
docker exec -i xxj-runtime-fleet python3 - "$ATT" "$ROW" "$DB" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone

att, row_id, path = sys.argv[1], sys.argv[2], sys.argv[3]
now = datetime.now(timezone.utc).isoformat()
con = sqlite3.connect(path, timeout=15)
con.execute("pragma busy_timeout = 15000")
con.row_factory = sqlite3.Row

before = con.execute(
    "select state from action_attempts where attempt_id = ?", (att,)
).fetchone()["state"]
with con:
    con.execute(
        "update action_attempts set state = 'ready_to_send', updated_at = ? where attempt_id = ?",
        (now, att),
    )
    # 如实记账：这是运维手动复活，不是系统自己走的一步。
    con.execute(
        "insert into attempt_events(attempt_event_id, attempt_id, from_state, to_state,"
        " reason, runtime_version, created_at) values (?,?,?,?,?,?,?)",
        (f"mem_manual_{now.replace(':', '').replace('-', '')[:14]}", att,
         before, "ready_to_send", "manual_revival:delivery_verification", 0, now),
    )
    con.execute(
        "update outbox set status = 'pending', attempts = 0, lease_owner = null,"
        " lease_expires_at = null, available_at = ?, acked_at = null,"
        " last_error = 'manual retry: revived to verify delivery' where outbox_id = ?",
        (now, row_id),
    )
print(f"  attempt {att}: {before} -> ready_to_send（已记审计事件）")
print(f"  outbox {row_id}: -> pending, attempts=0")
con.close()
PY

echo
echo "=== 2) 盯 90 秒 ==="
docker exec -i xxj-runtime-fleet python3 - "$ATT" "$ROW" "$DB" <<'PY'
import json, sqlite3, sys, time

att, row_id, path = sys.argv[1], sys.argv[2], sys.argv[3]
last = None
for step in range(45):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = con.execute(
        "select status, attempts, last_error from outbox where outbox_id = ?", (row_id,)
    ).fetchone()
    state = con.execute(
        "select state from action_attempts where attempt_id = ?", (att,)
    ).fetchone()[0]
    snapshot = (row[0], row[1], state, str(row[2])[:70])
    if snapshot != last:
        print(f"  [{time.strftime('%H:%M:%S')}] outbox={row[0]} attempts={row[1]} "
              f"attempt={state} error={snapshot[3]}")
        last = snapshot
    if row[0] in ("delivered", "failed", "rejected"):
        break
    con.close()
    time.sleep(2)
con.close()
PY

echo
echo "=== 3) 事件链 ==="
docker exec -i xxj-runtime-fleet python3 - "$ATT" "$DB" <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[2]}?mode=ro", uri=True)
for row in con.execute(
    "select created_at, from_state, to_state, reason from attempt_events"
    " where attempt_id = ? order by created_at", (sys.argv[1],),
):
    print(f"  {str(row[0])[11:19]} {row[1]} -> {row[2]}  {row[3]}")
PY

echo
echo "=== 4) 插件日志 ==="
docker logs astrbot-test --since 3m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -12
