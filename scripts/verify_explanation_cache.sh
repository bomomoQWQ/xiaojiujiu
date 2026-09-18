#!/usr/bin/env bash
# 部署"解释契约"修复并验证：
#   1) 强刷一次 -> applied 里应出现 psychological_interpretation，emotion_explanations 从 0 变 1
#   2) POST /explain 应返回 cache_hit=true（说明块里那六行现在读的是缓存，而不是每次重算）
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
echo "=== 2) 先在假用户上制造两条未决事件 ==="
for text in "嗯，算了" "你忙你的吧，我不打扰"; do
  docker exec -i xxj-onebot python3 - "$text" <<'PY'
import json, sys, urllib.request
body = json.dumps({"text": sys.argv[1]}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send(%s) -> %s" % (sys.argv[1], urllib.request.urlopen(req, timeout=10).status))
PY
  sleep 8
done
sleep 10
docker exec -i xxj-runtime-fleet python3 - "$FAKE" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
print("  未决=%d 解释缓存=%d" % (
    con.execute("select count(*) from event_semantics where semantic_status='unresolved'").fetchone()[0],
    con.execute("select count(*) from emotion_explanations").fetchone()[0]))
con.close()
PY

echo
echo "=== 3) 强制刷新（期望 applied 里出现 psychological_interpretation）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
body = json.dumps({'force': True}).encode()
req = urllib.request.Request('http://127.0.0.1:8794/cognition/refresh', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())
print('  ran=%s reason=%s' % (d.get('ran'), d.get('reason')))
print('  applied=%s settled_events=%s' % (d.get('applied'), d.get('settled_events')))
print('  violations=%s' % ((d.get('violations') or [])[:3],))
"
sleep 3
docker exec -i xxj-runtime-fleet python3 - "$FAKE" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
print("  未决=%d 解释缓存=%d" % (
    con.execute("select count(*) from event_semantics where semantic_status='unresolved'").fetchone()[0],
    con.execute("select count(*) from emotion_explanations").fetchone()[0]))
for row in con.execute("select cache_key, source, substr(payload_json,1,160) from emotion_explanations"):
    print("    缓存行: source=%s payload=%s" % (row[1], row[2]))
con.close()
PY

echo
echo "=== 4) POST /explain：应 cache_hit=true（六行现在读缓存）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
body = json.dumps({}).encode()
req = urllib.request.Request('http://127.0.0.1:8794/explain', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=120) as r:
    d = json.loads(r.read().decode())
print('  source=%s cache_hit=%s' % (d.get('source'), d.get('cache_hit')))
for key in ('experience', 'focus', 'conflict', 'impulse', 'inhibition', 'expression'):
    print('    %-11s %s' % (key, d.get(key)))
"
