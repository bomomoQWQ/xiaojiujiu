#!/usr/bin/env bash
# 看调试链路现在到底通没通：适配器连接、前端是否还在报错、有没有真的在收发。
set -u

echo "=== 1) AstrBot 最近的适配器连接事件 ==="
docker logs --since 15m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "适配器已连接|Loading IM platform" | tail -5

echo
echo "=== 2) 前端最近 10 行（还在报错吗）==="
docker logs --tail 10 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'

echo
echo "=== 3) 前端 jsonl 里最近 6 条（有收发就是通了）==="
tail -6 /home/bomomo/astrbot_test/frontend-logs/onebot.jsonl 2>/dev/null \
  | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    payload = d.get('payload') or {}
    action = payload.get('action') or payload.get('post_type') or ''
    text = ''
    for part in (payload.get('params', {}).get('message') or []):
        if isinstance(part, dict) and part.get('type') == 'text':
            text = part.get('data', {}).get('text', '')[:40]
    print('  %-10s %-10s %-22s %s' % (d.get('direction'), d.get('kind'), action, text))
"

echo
echo "=== 4) Runtime 侧有没有收到前端模拟用户的消息 ==="
docker exec xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3
import datetime as dt

cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
          - dt.timedelta(minutes=30)).isoformat()
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    rows = con.execute(
        "select count(*), max(timestamp) from raw_events where timestamp > ?", (cutoff,)
    ).fetchone()
    con.close()
    if rows[0]:
        print("  %-14s 近 30 分钟事件 %-5d 最新 %s" % (tag, rows[0], str(rows[1])[:19]))
PY
echo "  (空 = 前端还没把消息送到 Runtime)"

echo
echo "=== 5) 舰队现在的实例（前端模拟用户可能触发自动开实例）==="
curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('people', []):
    print('  %-34s port=%-5s %s' % (p.get('person'), p.get('port'), p.get('health')))
"
