#!/usr/bin/env bash
# 部署后核对：修复在容器里、看板数字、以及刚才那 15 秒有没有丢消息。
set -u
echo "=== 容器里有没有新代码 ==="
docker exec xxj-runtime-fleet sh -c 'grep -c "already_spoken_for" /app/runtime/src/companion_runtime/reducer.py'
docker exec xxj-runtime-fleet sh -c 'grep -c "def already_spoken_for" /app/runtime/src/companion_runtime/unfinished.py'

echo
echo "=== /fleet/status ==="
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
for p in d["people"]:
    print("  ", p["person"], "| health", p["health"], "| events", p["raw_events"],
          "| open matters", p["open_unfinished"], "| unresolved", p["unresolved"],
          "| refresh", p["deep_refresh_attempts"], "/", p["deep_refresh_settled"],
          "| degraded", p["deep_refresh_degraded"], "| restarts", p["restarts"])
'

echo
echo "=== 重启前后事件有没有断档（最近 8 条）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, sqlite3

path = glob.glob("/data/default-friendmessage-qq01/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
for row in con.execute(
    "select created_at, event_type, substr(coalesce(content,''),1,36) from raw_events"
    " order by created_at desc limit 8"
):
    print("   ", str(row[0])[:19], row[1], row[2])
PY

echo
echo "=== 看板（从本机 HTTP 取）==="
curl -s -o /tmp/dash.html -w '  dashboard HTTP %{http_code}\n' http://192.168.1.15:8800/fleet/dashboard
grep -o '<tr><td>.*</tr>' /tmp/dash.html | head -1 | sed -E 's/<[^>]+>/ | /g'

echo
echo "=== 日志里有没有 error ==="
echo -n "  astrbot 近 5 分钟: "
docker logs astrbot-test --since 5m 2>&1 | grep -ciE "error|traceback" || true
echo -n "  实例近 5 分钟: "
docker exec xxj-runtime-fleet /bin/sh -c "tail -400 /data/logs/default-friendmessage-qq01.log | grep -ciE 'error|traceback| 500 ' || true"
