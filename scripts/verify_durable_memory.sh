#!/usr/bin/env bash
# 部署 durable 修复并分两段验收：
#   A) 立刻：刷新出的 memory_candidates 的 kind 是否出现 durable（修前全是 episodic）
#   B) 等固化（CR_MEMORY__CONSOLIDATION_INTERVAL_SECONDS=600）后：
#      memories 的 kind 分布 + 注入块【必要记忆】是否变成"他是谁"
set -u

REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
STACK=/home/bomomo/astrbot_test
FAKE=/data/default-friendmessage-20001/companion.sqlite3

echo "=== 1) 拉取 + 重建 + 重启舰队 ==="
cd "$REPO" && git pull --ff-only 2>&1 | tail -1
docker build -q -t xiaojiujiu-runtime:test . 2>&1 | tail -1
cd "$STACK"
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d --force-recreate runtime-fleet 2>&1 | tail -1
for i in $(seq 1 40); do
  sleep 5
  line=$(curl -s --max-time 6 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('0/0'); raise SystemExit
p=d.get('people',[]); print('%d/%d'%(sum(1 for x in p if x.get('health')=='ok'),len(p)))
" 2>/dev/null || echo "0/0")
  case "$line" in 8/8) echo "  舰队 8/8（$((i*5))s）"; break ;; esac
done

echo
echo "=== 2) 发两条"值得长期记住"的话（偏好 + 身份），再强制刷新 ==="
for text in "我特别喜欢下雨天，一到雨天心情就特别好" "我是做后端的，平时在北京上班"; do
  docker exec -i xxj-onebot python3 - "$text" <<'PY'
import json, sys, urllib.request
body = json.dumps({"text": sys.argv[1]}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send(%s) -> %s" % (sys.argv[1][:14], urllib.request.urlopen(req, timeout=10).status))
PY
  sleep 8
done
sleep 8
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
body = json.dumps({'force': True}).encode()
req = urllib.request.Request('http://127.0.0.1:8794/cognition/refresh', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())
print('  refresh applied=%s settled=%s' % (d.get('applied'), d.get('settled_events')))
"

echo
echo "=== A) 立刻：新候选的 kind（修前这里全是 episodic）==="
docker exec -i xxj-runtime-fleet python3 - "$FAKE" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
rows = con.execute("select kind, count(*) from memory_candidates group by kind order by 2 desc").fetchall()
print("  候选 kind 分布: %s" % dict(rows))
for kind, summary in con.execute(
        "select kind, substr(summary,1,44) from memory_candidates order by created_at desc limit 6"):
    print("    %-18s %s" % (kind, summary))
con.close()
PY

echo
echo "=== B) 等固化（11 分钟）后看 memories 与注入块 ==="
sleep 660
docker exec -i xxj-runtime-fleet python3 - "$FAKE" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
rows = con.execute("select kind, count(*) from memories group by kind order by 2 desc").fetchall()
print("  memories kind 分布: %s" % dict(rows))
for kind, summary in con.execute(
        "select kind, substr(summary,1,50) from memories order by created_at desc limit 6"):
    print("    %-18s %s" % (kind, summary))
con.close()
PY
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8794/context/render-block', data=b'{}',
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=60) as r:
    block = (json.loads(r.read().decode()).get('block') or '')
lines = block.splitlines()
start = next((i for i, line in enumerate(lines) if '必要记忆' in line), None)
if start is None:
    print('  块里没有【必要记忆】段')
else:
    for line in lines[start:start + 6]:
        print('  ' + line)
"
