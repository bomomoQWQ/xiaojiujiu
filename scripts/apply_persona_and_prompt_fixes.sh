#!/usr/bin/env bash
# v3：备份写到容器内 /AstrBot/data/（等于宿主 data 目录，两边都能看到），
# render-block 给足超时并重试（舰队刚重建，首次 tick 会跑刷新，可能几十秒）。
set -u

REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1) 更新人格与 skills ==="
docker cp "$REPO/人格设定.md" astrbot-test:/tmp/persona_new.md
docker exec -u 0 -i astrbot-test python3 - "$STAMP" <<'PY'
import shutil
import sqlite3
import sys

stamp = sys.argv[1]
with open("/tmp/persona_new.md", encoding="utf-8") as handle:
    new_prompt = handle.read()
if "富有攻击力" not in new_prompt:
    raise SystemExit("人格文件里没有最新版特征词，中止")
print("  读入人格文件 %d 字" % len(new_prompt))

path = "/AstrBot/data/data_v4.db"
shutil.copy2(path, "%s.bak-persona-%s" % (path, stamp))
con = sqlite3.connect(path, timeout=20)
con.execute("pragma busy_timeout = 20000")
row = con.execute("select persona_id, system_prompt, skills from personas").fetchone()
print("  改前: persona=%s 人格 %d 字 skills=%r" % (row[0], len(row[1] or ""), row[2]))
with open("/AstrBot/data/persona_backup_%s.txt" % stamp, "w", encoding="utf-8") as handle:
    handle.write(row[1] or "")
with con:
    con.execute("update personas set system_prompt = ?, skills = ?", (new_prompt, "[]"))
after = con.execute("select system_prompt, skills from personas").fetchone()
print("  改后: 人格 %d 字 skills=%r" % (len(after[0] or ""), after[1]))
con.close()
print("  旧人格备份: /home/bomomo/astrbot_test/data/persona_backup_%s.txt" % stamp)
PY

echo
echo "=== 2) 重启 astrbot-test ==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null && echo "  已重启"
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done

echo
echo "=== 3) 注入块的时间行（重试三次，每次 60 秒上限）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
for attempt in range(1, 4):
    try:
        req = urllib.request.Request('http://127.0.0.1:8794/context/render-block', data=b'{}',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            block = (json.loads(r.read().decode()).get('block') or '')
        for line in block.splitlines():
            if '时间' in line or '小时' in line:
                print('   ' + line)
        break
    except Exception as exc:
        print('   第 %d 次失败: %s' % (attempt, exc))
"

echo
echo "=== 4) 驱动一轮 ==="
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "人格和技能都更新之后的一轮：你在吗"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 25

echo
echo "=== 5) 实证：system_prompt 内容 ==="
docker exec astrbot-test sh -c 'cat /AstrBot/data/logs/astrbot.trace.log' > /tmp/trace4.raw
python3 - <<'PY'
import json
records = []
for raw in open("/tmp/trace4.raw", encoding="utf-8", errors="replace"):
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
    print("  没有 astr_agent_prepare 记录")
else:
    prompt = records[-1]["fields"].get("system_prompt") or ""
    print("  system_prompt 长度 = %d（改前 4210）" % len(prompt))
    print("  含 ## Skills 块 : %s（期望 False）" % ("## Skills" in prompt))
    print("  含新版人格词    : %s（期望 True）" % ("富有攻击力" in prompt))
    print("  含 Computer Use : %s（期望 False）" % ("Computer Use" in prompt))
    print("  tools = %s" % records[-1]["fields"].get("tools"))
    print("  --- 前 200 字 ---")
    print("  " + prompt[:200].replace("\n", "\n  "))
PY

echo
echo "=== 6) 她的回复 ==="
docker logs --since 2m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "Prepare to send" | tail -2
