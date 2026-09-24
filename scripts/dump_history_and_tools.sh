#!/usr/bin/env bash
# 取 trace 同一条记录里的 tools / chat_provider，以及 20001 会话的历史消息（主 LLM 的 contexts）。
set -u

docker exec astrbot-test sh -c 'cat /AstrBot/data/logs/astrbot.trace.log' > /tmp/trace.raw
python3 - <<'PY'
import json

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
    fields = record["fields"]
    print("  tools            =", fields.get("tools"))
    print("  stream           =", fields.get("stream"))
    print("  chat_provider    =", json.dumps(fields.get("chat_provider"), ensure_ascii=False))
PY

echo
echo "=== 20001 会话的历史（conversations.content）==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json
import sqlite3

con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
cols = [row[1] for row in con.execute("pragma table_info(conversations)")]
for row in con.execute("select * from conversations where conversation_id like '%20001%'"):
    data = dict(zip(cols, row))
    print("  conversation_id=%s persona_id=%s token_usage=%s" % (
        data.get("conversation_id"), data.get("persona_id"), data.get("token_usage")))
    messages = json.loads(data.get("content") or "[]")
    print("  消息 %d 条:" % len(messages))
    for message in messages:
        role = message.get("role")
        parts = message.get("content")
        if isinstance(parts, list):
            text = " / ".join(str(p.get("text", p)) for p in parts)
        else:
            text = str(parts)
        print("    [%-9s] %s" % (role, text[:200].replace("\n", " ⏎ ")))
con.close()
PY

echo
echo "=== 所有会话一览（谁有历史、多少条）==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json
import sqlite3

con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
for conversation_id, content, persona_id, updated in con.execute(
        "select conversation_id, content, persona_id, updated_at from conversations"
        " order by updated_at desc"):
    try:
        count = len(json.loads(content or "[]"))
    except ValueError:
        count = -1
    print("  %-42s persona=%-8s 消息=%-4d 更新=%s" % (
        conversation_id, persona_id, count, str(updated)[:19]))
con.close()
PY
