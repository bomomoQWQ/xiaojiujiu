#!/usr/bin/env bash
# 部署"解释缓存容忍度"并复核：
#   连续调用应命中；情绪真变化后应重算（容忍度不能把真变化也吞掉）。
set -u

REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
STACK=/home/bomomo/astrbot_test

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

probe() {
  docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
body = json.dumps({}).encode()
req = urllib.request.Request('http://127.0.0.1:8794/explain', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())
print('    cache_hit=%-5s source=%-8s key=%s' % (d.get('cache_hit'), d.get('source'), (d.get('cache_key') or '')[:52]))
"
}

echo
echo "=== 2) 先造一条缓存（发一条消息 -> 刷一次 -> 缓存里就有解释了）==="
docker exec -i xxj-onebot python3 - <<'PY'
import json, urllib.request
body = json.dumps({"text": "嗯，算了"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 12
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
echo "=== 3) 复核：连续调用（应命中）+ 真变化（应重算）==="
probe
probe
docker exec -i xxj-onebot python3 - <<'PY'
import json, urllib.request
body = json.dumps({"text": "谢谢你，今天真的很开心"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send(情绪变化) ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 20
probe
