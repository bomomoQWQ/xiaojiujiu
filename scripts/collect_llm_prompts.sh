#!/usr/bin/env bash
# 驱动两条主 LLM 路径并收集证据：
#   A) 普通对话轮：前端注入一条用户消息 -> 走 agent 流水线 -> trace 记下拼装后的 system_prompt
#   B) 主动渲染：对假用户实例强制一次内源决策 -> 产生 render 行动 -> 取它的 prompt
set -u

echo "=== A1) 我们注入的临时块（假用户实例 8794）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8794/context/render-block', data=b'{}',
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=10) as r:
    d = json.loads(r.read().decode())
block = d.get('block') or ''
print('  ephemeral=%s version=%s 长度=%d' % (d.get('ephemeral'), d.get('version'), len(block)))
print('  ----8<----')
for line in block.splitlines():
    print('  | ' + line)
print('  ----8<----')
" 2>&1

echo
echo "=== A2) 注入一条模拟用户消息（走完整对话链路）==="
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request

body = json.dumps({"text": "提示词审阅用：今天过得怎么样"}).encode()
request = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=10) as response:
    print("  /send ->", response.status)
PY
sleep 20
echo "  AstrBot 侧这一轮:"
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "Prepare to send|sel_persona|astr_agent" | tail -4

echo
echo "=== B) 对 8794 强制一次内源决策（可能产出 render 行动）==="
docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
body = json.dumps({'force': True, 'create_attempt': True}).encode()
req = urllib.request.Request('http://127.0.0.1:8794/endogenous', data=body,
                             headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=120) as r:
    d = json.loads(r.read().decode())
out = (d.get('decision') or {}).get('outcome') or {}
print('  acted=%s reason=%s hazard=%s adv=%s' % (
    out.get('acted'), out.get('reason'), out.get('hazard'), out.get('advantage')))
print('  attempt=%s outbox=%s' % (d.get('attempt_id'), d.get('outbox_id')))
" 2>&1
sleep 8

echo
echo "=== C) trace 文件里的两轮记录 ==="
docker exec astrbot-test sh -c 'ls -la /AstrBot/data/logs/ 2>/dev/null | tail -3'
docker exec astrbot-test sh -c 'tail -40 /AstrBot/data/logs/astrbot.trace.log 2>/dev/null' \
  | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except ValueError:
        continue
    print('  [%s] %s umo=%s' % (d.get('action'), d.get('name'), d.get('umo')))
    fields = d.get('fields') or {}
    for key in ('system_prompt', 'tools', 'chat_provider', 'persona_id', 'resp'):
        if key in fields:
            value = fields[key]
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if len(text) > 4000:
                text = text[:4000] + ' …(截断)'
            print('      %s = %s' % (key, text.replace(chr(10), chr(10) + '        ')))
"

echo
echo "=== D) 8794 的 outbox / attempt（主动渲染的载荷）==="
docker exec xxj-runtime-fleet python3 - <<'PY'
import json
import sqlite3

con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for row in con.execute("select outbox_id, kind, status, created_at, payload_json from outbox"
                       " order by created_at desc limit 3"):
    print("  %s %-7s %-10s %s" % (row["outbox_id"][:14], row["kind"], row["status"],
                                  str(row["created_at"])[:19]))
    payload = json.loads(row["payload_json"] or "{}")
    for key in ("intent", "goal", "constraints", "attempt_id", "based_on_version"):
        if key in payload:
            print("      %s = %s" % (key, json.dumps(payload[key], ensure_ascii=False)[:200]))
con.close()
PY
