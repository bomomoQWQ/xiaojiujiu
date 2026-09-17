#!/usr/bin/env bash
# 跳钟之后的时钟调和：把"排期类"锚点拉回挂钟，让她恢复正常节奏。
# 只改排期/时钟锚点，不改历史（decision / attempt / attempt_events 一律保留原样）。
set -u
QQ=1670681411
DB=/data/default-friendmessage-1670681411/companion.sqlite3

docker exec -i xxj-runtime-fleet python3 - "$DB" <<'PY'
import json, sqlite3, sys
from datetime import datetime, timedelta, timezone

path = sys.argv[1]
now = datetime.now(timezone.utc)
con = sqlite3.connect(path, timeout=15)
con.execute("pragma busy_timeout = 15000")
con.row_factory = sqlite3.Row

row = con.execute(
    "select updated_at, last_tick_at, cooldown_until, foreground_pause_until, meta_json"
    " from runtime_state"
).fetchone()
meta = json.loads(row["meta_json"] or "{}")
print("=== 调和前 ===")
print(f"  updated_at={row['updated_at']} last_tick_at={row['last_tick_at']}")
print(f"  cooldown_until={row['cooldown_until']} pause={row['foreground_pause_until']}")
for key in sorted(meta):
    if "at" in key:
        print(f"  meta.{key}={meta[key]}")

# 她刚真的发过一条，所以冷却按"现在 + 默认 40 分钟"重建，而不是清掉。
meta["last_decision_at"] = now.isoformat()
meta["last_deep_refresh_at"] = now.isoformat()
with con:
    con.execute(
        "update runtime_state set updated_at = ?, last_tick_at = ?, cooldown_until = ?,"
        " foreground_pause_until = null, meta_json = ?",
        (
            now.isoformat(),
            now.isoformat(),
            (now + timedelta(seconds=2400)).isoformat(),
            json.dumps(meta),
        ),
    )

row = con.execute(
    "select updated_at, last_tick_at, cooldown_until, meta_json from runtime_state"
).fetchone()
print()
print("=== 调和后 ===")
print(f"  updated_at={row['updated_at']} last_tick_at={row['last_tick_at']}")
print(f"  cooldown_until={row['cooldown_until']}")
print(f"  meta.last_decision_at={json.loads(row['meta_json']).get('last_decision_at')}")
print()
print("  历史未动（保留跳钟期间的记录）：")
print("   decisions 最新:", con.execute(
    "select max(decided_at) from decisions").fetchone()[0])
print("   attempts 最新:", con.execute(
    "select max(created_at) from action_attempts").fetchone()[0])
con.close()
PY

echo
echo "=== 健康与调度 ==="
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
docker exec -i xxj-runtime-fleet python3 - "$IP" <<'PY'
import json, sys, urllib.request

ip = sys.argv[1]
with urllib.request.urlopen(f"http://{ip}:8787/health", timeout=10) as resp:
    d = json.load(resp)
print("  health:", d.get("status"), " allow_proactive:", d.get("allow_proactive"),
      " open_unfinished:", d.get("open_unfinished"))
with urllib.request.urlopen(f"http://{ip}:8787/schedule", timeout=10) as resp:
    s = json.load(resp)
print("  schedule:", json.dumps(s, ensure_ascii=False)[:300])
PY
