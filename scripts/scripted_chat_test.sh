#!/usr/bin/env bash
# 脚本化试聊：每一轮打不同的管线，然后把她的回复与各层状态一起打出来。
#   1 已结算的负面（应经评估器产生情绪）
#   2 偏好（durable candidate）
#   3 身份（durable candidate）
#   4 关系（durable candidate）
#   5 含糊（应保持未决、且不产生情绪）
#   6 连发两条（防抖：应只回一轮）
#   7 [自动回复] 。（应被 word_filter 拦下，无回复）
set -u
FAKE=/data/default-friendmessage-20001/companion.sqlite3

send() {
  docker exec -i xxj-onebot python3 - "$1" <<'PY'
import json, sys, urllib.request
body = json.dumps({"text": sys.argv[1]}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  → %s" % sys.argv[1])
urllib.request.urlopen(req, timeout=10).read()
PY
}

replies() {
  docker logs --since 3m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
    | grep -a "Prepare to send" | tail -"$1" | sed -E 's/^.*20001: /  ← /'
}

echo "=== 试聊开始 ==="

echo
echo "[1/7] 已结算的负面：我今晚想自己待着"
send "我今晚想自己待着"
sleep 22
replies 1

echo
echo "[2/7] 偏好：我特别喜欢下雨天"
send "我特别喜欢下雨天，一到雨天心情就特别好"
sleep 22
replies 1

echo
echo "[3/7] 身份：我是做后端的"
send "我是做后端的，平时在北京上班，最近在赶一个项目"
sleep 22
replies 1

echo
echo "[4/7] 关系：谢谢你今天陪我聊这么久"
send "谢谢你今天陪我聊这么久"
sleep 22
replies 1

echo
echo "[5/7] 含糊：嗯，算了（期望：未决、且不产生情绪）"
send "嗯，算了"
sleep 22
replies 1

echo
echo "[6/7] 连发两条（期望：只回一轮）"
BEFORE=$(docker exec astrbot-test sh -c 'grep -c astr_agent_prepare /AstrBot/data/logs/astrbot.trace.log' || echo 0)
send "对了"
sleep 1
send "你还在吗"
sleep 24
AFTER=$(docker exec astrbot-test sh -c 'grep -c astr_agent_prepare /AstrBot/data/logs/astrbot.trace.log' || echo 0)
echo "  astr_agent_prepare: $BEFORE → $AFTER（期望 +1）"
replies 1

echo
echo "[7/7] 自动回复标记（期望：不回复）"
MARK_BEFORE=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
send "[自动回复] 。"
sleep 18
MARK_AFTER=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
echo "  Prepare to send: $MARK_BEFORE → $MARK_AFTER（期望不变）"
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | grep -a "屏蔽词过滤" | tail -1

echo
echo "=== 各层状态 ==="
docker exec -i xxj-runtime-fleet python3 - "$FAKE" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
q = lambda sql: con.execute(sql).fetchone()[0]
print("  mood_valence=%.4f mood_arousal=%.4f  情绪事件=%d  未决=%d" % (
    *con.execute("select mood_valence, mood_arousal from runtime_state").fetchone(),
    q("select count(*) from active_emotion_events"),
    q("select count(*) from event_semantics where semantic_status='unresolved'")))
print("  --- 情绪事件 ---")
for row in con.execute("select direction, round(intensity,3), substr(created_at,12,8)"
                       " from active_emotion_events order by created_at desc limit 5"):
    print("    %s %-6s %s" % row)
print("  --- 候选 kind 分布 ---")
print("   ", dict(con.execute("select kind, count(*) from memory_candidates group by kind").fetchall()))
print("  --- memories kind 分布 ---")
print("   ", dict(con.execute("select kind, count(*) from memories group by kind").fetchall()) or "（空）")
con.close()
PY

echo
echo "=== 注入块 ==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8794/context/render-block', data=b'{}',
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=60) as r:
    block = (json.loads(r.read().decode()).get('block') or '')
for line in block.splitlines():
    if line.startswith(('- 长期感受', '- 克制', '【必要记忆】', '- ')) and '事实' not in line:
        print('  ' + line)
" | head -14
