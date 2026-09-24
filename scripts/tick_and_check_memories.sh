#!/usr/bin/env bash
# 补一次 tick 让固化跑起来（新候选是在上次刷新后才创建的，需要一个后续 tick 才会被看到）。
set -u

docker exec -i xxj-onebot python3 - <<'PY'
import json, urllib.request
body = json.dumps({"text": "再随便说一句，好让维护跑一轮"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 25

docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3
con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
rows = con.execute("select kind, count(*) from memories group by kind order by 2 desc").fetchall()
print("  memories kind 分布: %s" % dict(rows))
print("  --- 最近 6 条 memories ---")
for kind, created, summary in con.execute(
        "select kind, created_at, substr(summary,1,46) from memories order by created_at desc limit 6"):
    print("    %-18s %s  %s" % (kind, str(created)[11:19], summary))
print("  --- 仍未固化的候选 ---")
for kind, value, created, summary in con.execute(
        "select kind, value, created_at, substr(summary,1,40) from memory_candidates"
        " where status='pending' order by value desc limit 6"):
    print("    %-18s value=%.2f %s  %s" % (kind, value, str(created)[11:19], summary))
con.close()
PY

echo
echo "=== 注入块【必要记忆】 ==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8794/context/render-block', data=b'{}',
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=60) as r:
    block = (json.loads(r.read().decode()).get('block') or '')
lines = block.splitlines()
start = next((i for i, line in enumerate(lines) if '必要记忆' in line), None)
if start is None:
    print('  没有【必要记忆】段')
else:
    for line in lines[start:start + 6]:
        print('  ' + line)
"
