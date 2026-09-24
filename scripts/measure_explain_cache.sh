#!/usr/bin/env bash
# 实测解释缓存的命中行为：连续调用 / 间隔调用 / 情绪变化后，各自的 cache_hit 与 cache_key。
set -u

probe() {
  docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
body = json.dumps({}).encode()
req = urllib.request.Request('http://127.0.0.1:8794/explain', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())
print('    cache_hit=%-5s source=%-8s key=%s' % (d.get('cache_hit'), d.get('source'), d.get('cache_key')))
"
}

echo "=== 连续三次调用（状态基本不变）==="
probe
probe
probe

echo
echo "=== 隔 30 秒再调（只发生衰减）==="
sleep 30
probe

echo
echo "=== 发一条消息改变情绪，再调（应 miss）==="
docker exec -i xxj-onebot python3 - <<'PY'
import json, urllib.request
body = json.dumps({"text": "谢谢你，今天挺开心的"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 20
probe
echo
echo "=== 缓存表现状 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3
con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
rows = con.execute("select cache_key, source, last_used_at from emotion_explanations"
                   " order by last_used_at desc limit 6").fetchall()
print("  行数=%d" % con.execute("select count(*) from emotion_explanations").fetchone()[0])
for row in rows:
    print("    %s  %s  %s" % (row[0][:60], row[1], str(row[2])[:19]))
con.close()
PY
