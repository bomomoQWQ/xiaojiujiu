#!/usr/bin/env bash
# 部署 a+c 并在测试栈上验收：
#   a) 已结算事件的情绪现在来自评估器（带价值观敏感度 + 忙碌归因）
#   c) 未决事件在深层刷新里被语义评估 -> 产生情绪 + 结清积压
set -u

REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
STACK=/home/bomomo/astrbot_test
FAKE=/data/default-friendmessage-20001/companion.sqlite3

echo "=== 1) 拉取 + 重建 runtime + 重启舰队 ==="
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

snapshot() {
  docker exec -i xxj-runtime-fleet python3 - "$FAKE" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
def one(sql):
    return con.execute(sql).fetchone()[0]
print("    mood_valence=%.4f mood_arousal=%.4f" % con.execute(
    "select mood_valence, mood_arousal from runtime_state").fetchone())
print("    情绪事件=%d  未决事件=%d" % (
    one("select count(*) from active_emotion_events"),
    one("select count(*) from event_semantics where semantic_status='unresolved'")))
for row in con.execute("select direction, round(intensity,3), created_at from active_emotion_events"
                       " order by created_at desc limit 3"):
    print("      最近: %s" % (row,))
con.close()
PY
}

echo
echo "=== 2) 验收 a：发一条【已结算】的负面消息（自己待着 = 明确的语义锚点）==="
echo "  发送前:"; snapshot
docker exec -i xxj-onebot python3 - <<'PY'
import json, urllib.request
body = json.dumps({"text": "我今晚想自己待着"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 22
echo "  发送后（期望：出现 1 个情绪事件，强度由词表×价值观得出，不是粗结算的固定带位）:"
snapshot

echo
echo "=== 3) 验收 c 的前置：发一条【不会结算】的含糊消息，制造未决积压 ==="
for text in "嗯，算了" "随便吧，你忙你的"; do
  docker exec -i xxj-onebot python3 - "$text" <<'PY'
import json, sys, urllib.request
body = json.dumps({"text": sys.argv[1]}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send(%s) -> %s" % (sys.argv[1], urllib.request.urlopen(req, timeout=10).status))
PY
  sleep 8
done
sleep 12
echo "  现在:"; snapshot

echo
echo "=== 4) 验收 c：强制一次深层刷新（会调用远端语义模型评估未决事件）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
body = json.dumps({'force': True}).encode()
req = urllib.request.Request('http://127.0.0.1:8794/cognition/refresh', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())
print('  ran=%s reason=%s applied=%s settled_events=%s violations=%s' % (
    d.get('ran'), d.get('reason'), d.get('applied'), d.get('settled_events'),
    (d.get('violations') or [])[:3]))
" 2>&1
echo "  刷新后（期望：未决清空 + 出现新的情绪事件）:"; snapshot

echo
echo "=== 5) 注入块的情绪段（期望不再是千篇一律的"平静无波"）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8794/context/render-block', data=b'{}',
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=60) as r:
    block = (json.loads(r.read().decode()).get('block') or '')
for line in block.splitlines():
    if line.startswith(('- 长期感受', '- 在意', '- 拉扯', '- 倾向', '- 克制', '- 表达底色')):
        print('  ' + line)
"
