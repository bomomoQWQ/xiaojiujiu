#!/usr/bin/env bash
# 强制一次内源轮次（qq01），看它是否决定行动、以及主动发送能否走通。
set -u
PORT=8787
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)

echo "=== 1) 这一轮之前的处境 ==="
docker exec -i xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, sys, urllib.request

port, ip = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=10) as resp:
    d = json.load(resp)
print("  allow_proactive:", d.get("allow_proactive"), " open_unfinished:", d.get("open_unfinished"))
PY
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3
from datetime import datetime, timezone

path = "/data/default-friendmessage-qq01/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
row = con.execute(
    "select decided_at, reason, hazard, advantage, silence_utility from decisions"
    " order by decided_at desc limit 1"
).fetchone()
print("  最近一条判决:", row)
if row:
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 3600.0
    print(f"  距现在 {age:.2f} 小时（hazard 就是在这个区间上积分的）")
st = con.execute(
    "select approach_impulse, restraint, pressure, cooldown_until, foreground_pause_until"
    " from runtime_state"
).fetchone()
print(f"  impulse={st[0]:.3f} restraint={st[1]:.3f} pressure={st[2]:.4f}")
print(f"  cooldown_until={st[3]}  foreground_pause_until={st[4]}")
PY

echo
echo "=== 2) 强制内源轮次 ==="
docker exec -i xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, sys, urllib.request

port, ip = sys.argv[1], sys.argv[2]
body = json.dumps({"force": True, "create_attempt": True}).encode()
req = urllib.request.Request(
    f"http://{ip}:{port}/endogenous", data=body,
    headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(req, timeout=120) as resp:
    data = json.load(resp)
decision = data.get("decision") or {}
print("  acted:", decision.get("acted"))
print("  reason:", decision.get("reason"))
print("  hazard:", decision.get("hazard"), " advantage:", decision.get("advantage"),
      " silence:", decision.get("silence_utility"))
print("  chosen:", decision.get("chosen_candidate_id"))
print("  attempt_id:", data.get("attempt_id"), " outbox_id:", data.get("outbox_id"))
print("  next_wake_at:", data.get("next_wake_at"))
print("  refresh:", (data.get("deep_refresh") or {}).get("reason"))
PY

echo
echo "=== 3) 等 15 秒看投递结果 ==="
sleep 15
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, sqlite3

path = "/data/default-friendmessage-qq01/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
row = con.execute(
    "select attempt_id, state, created_at from action_attempts order by created_at desc limit 1"
).fetchone()
if row:
    print(f"  最新 attempt {row['attempt_id']} state={row['state']} created={str(row['created_at'])[:19]}")
    for ev in con.execute(
        "select created_at, from_state, to_state, reason from attempt_events"
        " where attempt_id = ? order by created_at", (row["attempt_id"],),
    ):
        print(f"    {str(ev['created_at'])[11:19]} {ev['from_state']} -> {ev['to_state']}  {ev['reason']}")
print("  outbox 最新:")
for r in con.execute(
    "select kind, status, payload_json, created_at from outbox order by rowid desc limit 3"
):
    payload = json.loads(r["payload_json"] or "{}")
    text = " ".join(str(payload.get("text") or "").split())
    print(f"    {r['kind']:7s} {r['status']:10s} {str(r['created_at'])[11:19]} {text[:70]!r}")
PY

echo
echo "=== 4) 插件侧日志（这一轮）==="
docker logs astrbot-test --since 2m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -12
