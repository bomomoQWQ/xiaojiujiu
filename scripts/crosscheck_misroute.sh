#!/usr/bin/env bash
# 对账：测试前端收到的 send_private_msg，和 Runtime 渲染给真人的文案，是不是同一批。
set -u
echo "=== A) 测试前端收到的 AstrBot API 调用（send_private_msg）==="
python3 - /home/bomomo/astrbot_test/frontend-logs/onebot.jsonl <<'PY'
import json, sys

seen = []
with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") != "api":
            continue
        payload = row.get("payload") or {}
        if payload.get("action") != "send_private_msg":
            continue
        params = payload.get("params") or {}
        text = "".join(
            str(seg.get("data", {}).get("text", ""))
            for seg in (params.get("message") or [])
            if isinstance(seg, dict) and seg.get("type") == "text"
        )
        seen.append((row.get("at"), params.get("user_id"), params.get("self_id"), text[:60]))

print(f"  共 {len(seen)} 条 send_private_msg")
for at, user_id, self_id, text in seen[-25:]:
    print(f"    user_id={user_id} self_id={self_id} {text!r}")
PY

echo
echo "=== B) Runtime 侧渲染好的主动文案（本该发给谁）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, json, os, sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    qq = person.split("-")[-1]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = con.execute(
        "select status, payload_json from outbox where kind = 'send' order by rowid"
    ).fetchall()
    for status, payload in rows:
        data = json.loads(payload or "{}")
        text = " ".join(str(data.get("text") or "").split())
        print(f"    -> QQ {qq} [{status}] {text[:60]!r}")
PY
