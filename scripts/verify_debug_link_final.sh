#!/usr/bin/env bash
# 验收：链路是否稳定 + 端到端有没有真的把消息送进 Runtime。
set -u

echo "=== 1) 前端这次的连接日志（带时间戳，看有没有连上）==="
docker logs -t --tail 40 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "connected to|link down" | tail -8

echo
echo "=== 2) 现在记一次报错数，等 30 秒再看有没有新增 ==="
before=$(docker logs xxj-onebot 2>&1 | grep -c "link down" || true)
echo "  当前 link down 行数: $before"
sleep 30
after=$(docker logs xxj-onebot 2>&1 | grep -c "link down" || true)
echo "  30 秒后: $after  $([ "$after" -eq "$before" ] && echo '✅ 没有新增（链路稳定）' || echo '⚠️ 仍在掉线')"

echo
echo "=== 3) AstrBot 侧适配器连接行 ==="
docker logs --since 5m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -c "适配器已连接"

echo
echo "=== 4) 端到端：Runtime 有没有收到前端模拟用户的消息 ==="
docker exec xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import glob
import os
import sqlite3

cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
          - dt.timedelta(minutes=15)).isoformat()
found = False
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    rows = con.execute("select count(*), max(timestamp) from raw_events where timestamp > ?",
                       (cutoff,)).fetchone()
    con.close()
    if rows[0]:
        found = True
        print("  %-16s 近 15 分钟 %-5d 条，最新 %s" % (tag, rows[0], str(rows[1])[:19]))
if not found:
    print("  （还没有：前端要主动发消息或 AstrBot 调 API 才会产生事件）")
PY

echo
echo "=== 5) 舰队实例（前端模拟用户可能触发自动开实例）==="
curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  实例数 %d' % len(d.get('people', [])))
for p in d.get('people', [])[-4:]:
    print('   %-34s port=%-5s %s' % (p.get('person'), p.get('port'), p.get('health')))
"

echo
echo "=== 6) 前端控制面：可以直接驱动它发一条模拟用户消息吗 ==="
curl -s -o /tmp/o -w '  POST /state -> %{http_code}\n' --max-time 5 http://127.0.0.1:6300/state || echo "  6300 不通"
head -c 300 /tmp/o 2>/dev/null; echo
