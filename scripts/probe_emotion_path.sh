#!/usr/bin/env bash
# 实测：发一条带显式情绪标记的消息，看规则路径能否产生情绪事件并推动 mood（valence/arousal）。
set -u

echo "=== 1) 发之前的状态 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
print("  情绪事件:", con.execute("select count(*) from active_emotion_events").fetchone()[0])
print("  mood:", con.execute("select mood_valence, mood_arousal from runtime_state").fetchone())
print("  event_semantics:", con.execute("select count(*) from event_semantics").fetchone()[0])
con.close()
PY

echo
echo "=== 2) 发一条显式情绪消息 ==="
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "谢谢你，真的很喜欢你这样陪我，今天心情特别好"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 22

echo
echo "=== 3) 发之后的状态 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
print("  情绪事件:", con.execute("select count(*) from active_emotion_events").fetchone()[0])
print("  mood:", con.execute("select mood_valence, mood_arousal from runtime_state").fetchone())
print("  --- 最近的情绪事件 ---")
for row in con.execute("select emotion_event_id, direction, intensity, activation, created_at"
                       " from active_emotion_events order by created_at desc limit 3"):
    print("    ", row)
print("  --- 最近 settled 的语义行 ---")
for row in con.execute("select event_id, semantic_status, direction, intensity_band, confidence,"
                       " settlement_source from event_semantics order by settled_at desc limit 3"):
    print("    ", row)
con.close()
PY

echo
echo "=== 4) 注入块里的情绪段（看是否变了）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8794/context/render-block', data=b'{}',
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=60) as r:
    block = (json.loads(r.read().decode()).get('block') or '')
for line in block.splitlines():
    if line.startswith('- 长期感受') or line.startswith('- 表达底色'):
        print('  ' + line)
"
