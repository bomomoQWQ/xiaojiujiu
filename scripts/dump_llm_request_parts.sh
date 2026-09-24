#!/usr/bin/env bash
# 取主 LLM 请求的其余组成部分：provider 配置（决定 system prompt 里加什么）+ 会话历史。
set -u

echo "=== 1) 与提示词有关的配置项 ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json

with open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
provider = cfg.get("provider_settings", {}) or {}
keys = ("datetime_system_prompt", "prompt_prefix", "identifier", "persona",
        "max_context_length", "dequeue_context_length", "streaming_response",
        "unsupported_streaming_strategy", "web_search", "tool_use_mode",
        "computer_use", "reply_with_mention", "segmented_reply")
for key in keys:
    value = provider.get(key, "<未设置>")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    print("  provider_settings.%-30s = %s" % (key, value))
for section in ("skills", "knowledgebase", "agent"):
    block = cfg.get(section)
    if isinstance(block, dict):
        print("  %s = %s" % (section, json.dumps(block, ensure_ascii=False)[:300]))
print("  persona 列表（配置里）:", json.dumps(cfg.get("persona"), ensure_ascii=False)[:200])
PY

echo
echo "=== 2) 数据库里的会话历史（主 LLM 实际拿到的那部分）==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json
import sqlite3

con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
names = [row[0] for row in con.execute("select name from sqlite_master where type='table'")]
candidates = [n for n in names
              if any(token in n.lower() for token in ("conversation", "message", "history"))]
print("  相关表:", candidates)
for name in candidates:
    cols = [row[1] for row in con.execute("pragma table_info(%s)" % name)]
    rows = con.execute("select count(*) from %s" % name).fetchone()[0]
    print("  %-32s rows=%-5d %s" % (name, rows, cols))
    if rows and ("content" in cols or "message" in cols):
        for row in con.execute("select * from %s order by rowid desc limit 6" % name):
            data = dict(zip(cols, row))
            text = str(data.get("content") or data.get("message") or "")[:90]
            print("       %s | %s | %s" % (str(data.get("created_at") or data.get("timestamp"))[:19],
                                           str(data.get("role") or data.get("sender_id") or "")[:12], text))
con.close()
PY
