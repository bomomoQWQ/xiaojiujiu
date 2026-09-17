#!/usr/bin/env bash
# 复活前先看清：outbox 表结构、目标行的全部字段、以及 claim 需要什么条件。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, sqlite3

path = "/data/default-friendmessage-1670681411/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("=== outbox 表结构 ===")
for row in con.execute("pragma table_info(outbox)"):
    print(f"  {row['name']:22s} {row['type']:8s} notnull={row['notnull']} default={row['dflt_value']}")

print()
print("=== 所有 outbox 行 ===")
for row in con.execute("select * from outbox order by rowid"):
    data = dict(row)
    payload = json.loads(data.pop("payload_json") or "{}")
    print(f"  {json.dumps(data, ensure_ascii=False)}")
    print(f"      text={str(payload.get('text'))[:60]!r} attempt={payload.get('attempt_id')}")

print()
print("=== 目标 attempt 的状态 ===")
for row in con.execute("select * from action_attempts"):
    data = dict(row)
    print(f"  {data.get('attempt_id')} state={data.get('state')} "
          f"candidate={data.get('candidate_id')}")
PY

echo
echo "=== claim 的 SQL（什么样才可被领取）==="
grep -n "def claim" -A 45 /home/bomomo/astrbot_test/src/xiaojiujiu/runtime/src/companion_runtime/projections.py | grep -E "SELECT|WHERE|status|lease|available|attempts|LIMIT" | head -20
