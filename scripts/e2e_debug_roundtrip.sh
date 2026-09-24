#!/usr/bin/env bash
# 端到端验收：往前端的控制面 POST /send 一条"模拟用户"消息，
# 看它是否变成 AstrBot 的一轮对话、并进入 Runtime。
# 注意：这会真的调用一次模型（模拟用户 20001 是前端的假账号）。
set -u

echo "=== 1) 注入一条模拟用户消息 ==="
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request

body = json.dumps({"text": "调试用的第一句：你在吗"}).encode()
request = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=10) as response:
    print("  /send ->", response.status, response.read().decode()[:200])
PY

echo
echo "=== 2) 等 25 秒，看 AstrBot 有没有处理这一轮 ==="
sleep 25
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE "Prepare to send|user_chat|companion_runtime|on_llm|发送" | tail -8

echo
echo "=== 3) Runtime 侧：有没有收到这条用户消息 ==="
docker exec xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import glob
import json
import os
import sqlite3

cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
          - dt.timedelta(minutes=5)).isoformat()
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    rows = con.execute(
        "select event_type, timestamp, substr(content,1,40) from raw_events"
        " where timestamp > ? order by timestamp desc limit 5", (cutoff,)).fetchall()
    con.close()
    if rows:
        print("  %s:" % tag)
        for event_type, stamp, content in rows:
            print("     %s %-18s %s" % (str(stamp)[:19], event_type, content))
PY

echo
echo "=== 4) 舰队列表现状（假用户可能触发自动开实例）==="
curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  实例数', len(d.get('people', [])))
for p in d.get('people', []):
    print('   %-36s port=%-5s %s' % (p.get('person'), p.get('port'), p.get('health')))
"

echo
echo "=== 5) 前端记录里这一轮的收发 ==="
tail -6 /home/bomomo/astrbot_test/frontend-logs/onebot.jsonl 2>/dev/null \
  | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    payload = d.get('payload') or {}
    text = ''
    for part in (payload.get('params', {}).get('message') or []):
        if isinstance(part, dict) and part.get('type') == 'text':
            text = part.get('data', {}).get('text', '')[:40]
    print('  %-10s %-9s %-20s %s' % (d.get('direction'), d.get('kind'),
                                     (payload.get('action') or payload.get('post_type') or '')[:20], text))
"
