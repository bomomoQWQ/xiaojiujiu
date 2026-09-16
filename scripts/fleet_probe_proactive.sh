#!/usr/bin/env bash
# 她真的主动发出去过消息吗：看 proactive/outbox 痕迹。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

path = "/data/default-friendmessage-1670681411/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

print("== 所有 system/主动 相关事件 ==")
for row in con.execute(
    "select created_at, event_type, content from raw_events"
    " where event_type != 'candidate_proposal' and content != 'foreground_pause'"
    " and content != 'context_rendered' order by created_at"
):
    print(f"  {str(row[0])[:19]}  {row[1]:16s} {str(row[2])[:50]}")

print()
print("== outbox ==")
try:
    cols = [d[1] for d in con.execute("pragma table_info(outbox)")]
    print("  columns:", ", ".join(cols))
    for row in con.execute("select * from outbox order by rowid desc limit 6"):
        print("  ", str(row)[:220])
except sqlite3.Error as exc:
    print("  ", exc)

print()
print("== action_attempts ==")
for row in con.execute(
    "select attempt_id, state, created_at, updated_at from action_attempts order by rowid desc limit 6"
):
    print("  ", row)

print()
print("== candidate_intents（她的动机池） ==")
for row in con.execute(
    "select intent_id, goal, status, confidence, created_at from candidate_intents order by rowid desc limit 6"
):
    print("  ", row)
PY
