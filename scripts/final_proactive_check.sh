#!/usr/bin/env bash
# 收尾核对：投递结果、时钟、前端、全栈健康。
set -u
echo "=== 1) 那条主动消息的完整链条 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, sqlite3

path = "/data/default-friendmessage-qq01/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
att = con.execute(
    "select attempt_id, state, created_at from action_attempts order by created_at desc limit 1"
).fetchone()
print(f"  attempt {att['attempt_id']} state={att['state']}")
for ev in con.execute(
    "select created_at, from_state, to_state, reason from attempt_events"
    " where attempt_id = ? order by created_at", (att["attempt_id"],),
):
    print(f"    {str(ev['created_at'])[11:19]} {ev['from_state']:>14s} -> {ev['to_state']:<14s} {ev['reason']}")
row = con.execute(
    "select status, payload_json from outbox where kind = 'send' order by rowid desc limit 1"
).fetchone()
print(f"  send 行: {row['status']}")
print(f"  文案: {' '.join(str(json.loads(row['payload_json']).get('text') or '').split())}")
print(f"  今日已送达条数（预算计数）: "
      f"{con.execute('select contact_count_today from runtime_state').fetchone()[0]}")
PY

echo
echo "=== 2) 全栈健康 ==="
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  fleet:", d["count"], "healthy:", sum(1 for p in d["people"] if p["health"]=="ok"))
for p in d["people"]:
    print(f"   {p[\"person\"]:42s} {p[\"health\"]:4s} events={p[\"raw_events\"]:<5} "
          f"unres={p[\"unresolved\"]:<4} matters={p[\"open_unfinished\"]:<3} "
          f"refresh={p[\"deep_refresh_attempts\"]}/{p[\"deep_refresh_settled\"]} "
          f"deg={p[\"deep_refresh_degraded\"]}")
'
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-|astrbot-test'

echo
echo "=== 3) 该实例的调度是否恢复正常 ==="
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
docker exec -i xxj-runtime-fleet python3 - "$IP" <<'PY'
import json, sys, urllib.request

with urllib.request.urlopen(f"http://{sys.argv[1]}:8787/schedule", timeout=10) as resp:
    s = json.load(resp)
print("  next_wake_at:", s["plan"]["next_wake_at"], "delay:", s["plan"]["delay_seconds"],
      "reasons:", s["plan"]["reasons"])
PY
