#!/usr/bin/env bash
# 从 trace 里取出拼装后的完整 system_prompt（trace 行前面有时间戳前缀，要剥掉）。
set -u

docker exec astrbot-test sh -c 'cat /AstrBot/data/logs/astrbot.trace.log' > /tmp/trace.raw
python3 - <<'PY'
import json

assembled = []
for raw in open("/tmp/trace.raw", encoding="utf-8", errors="replace"):
    brace = raw.find("{")
    if brace < 0:
        continue
    try:
        record = json.loads(raw[brace:])
    except ValueError:
        continue
    if record.get("action") != "astr_agent_prepare":
        continue
    assembled.append((record.get("umo"), record["fields"].get("system_prompt") or ""))

print("找到 %d 份 astr_agent_prepare 记录" % len(assembled))
for umo, prompt in assembled[-1:]:
    print("umo = %s" % umo)
    print("system_prompt 长度 = %d" % len(prompt))
    print("========== SYSTEM PROMPT 全文开始 ==========")
    print(prompt)
    print("========== SYSTEM PROMPT 全文结束 ==========")
PY
