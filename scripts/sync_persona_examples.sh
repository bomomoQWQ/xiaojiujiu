#!/usr/bin/env bash
# 把 人格设定.md 的新版（含改好的反例/正例）同步进 AstrBot 的人格库并重启验证。
set -u

REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1) 同步人格 ==="
docker cp "$REPO/人格设定.md" astrbot-test:/tmp/persona_new.md
docker exec -u 0 -i astrbot-test python3 - "$STAMP" <<'PY'
import shutil
import sqlite3
import sys

stamp = sys.argv[1]
with open("/tmp/persona_new.md", encoding="utf-8") as handle:
    new_prompt = handle.read()
print("  新人格 %d 字" % len(new_prompt))
if "这是客服，不是我" not in new_prompt:
    raise SystemExit("文件里没有新版反例特征，中止")

path = "/AstrBot/data/data_v4.db"
shutil.copy2(path, "%s.bak-persona2-%s" % (path, stamp))
con = sqlite3.connect(path, timeout=20)
con.execute("pragma busy_timeout = 20000")
before = con.execute("select system_prompt from personas").fetchone()[0] or ""
with open("/AstrBot/data/persona_backup_%s.txt" % stamp, "w", encoding="utf-8") as handle:
    handle.write(before)
with con:
    con.execute("update personas set system_prompt = ?", (new_prompt,))
after = con.execute("select system_prompt from personas").fetchone()[0] or ""
print("  写入 %d -> %d 字，含「这是客服，不是我」: %s" % (
    len(before), len(after), "这是客服，不是我" in after))
con.close()
print("  旧版备份: /home/bomomo/astrbot_test/data/persona_backup_%s.txt" % stamp)
PY

echo
echo "=== 2) 重启并看拼装后的 system prompt 尾部 ==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done

docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "人格反例更新后的一轮：在吗"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 22
docker exec astrbot-test sh -c 'cat /AstrBot/data/logs/astrbot.trace.log' > /tmp/trace5.raw
python3 - <<'PY'
import json
records = []
for raw in open("/tmp/trace5.raw", encoding="utf-8", errors="replace"):
    i = raw.find("{")
    if i < 0:
        continue
    try:
        d = json.loads(raw[i:])
    except ValueError:
        continue
    if d.get("action") == "astr_agent_prepare":
        records.append(d)
if not records:
    print("  没有 astr_agent_prepare")
else:
    prompt = records[-1]["fields"].get("system_prompt") or ""
    print("  system_prompt 长度 = %d" % len(prompt))
    tail = prompt.strip().splitlines()[-6:]
    print("  --- 尾部 6 行 ---")
    for line in tail:
        print("  | " + line)
PY
