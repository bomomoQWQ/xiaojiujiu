#!/usr/bin/env bash
# 1) event=None 时 routing_params 到底怎么算（决定性一段）；
# 2) 停掉前端后，那条卡住的主动尝试有没有走通。
set -u
echo "=== _dispatch_send 开头（routing_params 的来源）==="
docker exec astrbot-test sh -c 'sed -n "80,110p" /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py'

echo
echo "=== aiocqhttp 的 call_action：没 self_id 时怎么挑连接 ==="
docker exec astrbot-test sh -c 'sed -n "120,200p" /usr/local/lib/python3.12/site-packages/aiocqhttp/api_impl.py'

echo
echo "=== 那条卡住的尝试现在什么状态 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, sqlite3

path = "/data/default-friendmessage-1670681411/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for row in con.execute(
    "select attempt_id, state, created_at, updated_at from action_attempts order by created_at"
):
    print(f"  {row['attempt_id']} state={row['state']} created={str(row['created_at'])[:19]} "
          f"updated={str(row['updated_at'])[:19]}")
    for ev in con.execute(
        "select created_at, from_state, to_state, reason from attempt_events"
        " where attempt_id = ? order by created_at desc limit 3",
        (row["attempt_id"],),
    ):
        print(f"     {str(ev['created_at'])[11:19]} {ev['from_state']} -> {ev['to_state']}  {ev['reason']}")
print()
print("  outbox:")
for row in con.execute("select kind, status, updated_at from outbox order by rowid"):
    print(f"    {row['kind']:7s} {row['status']:10s} {str(row['updated_at'])[:19]}")
PY
