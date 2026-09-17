#!/usr/bin/env bash
# a：把 now 一次推 +13 小时（~90% 让 hazard 触发），然后盯完整投递链：
#   决定 → render 行 → 插件渲染 → send 行 → authorize → context.send_message
set -u
QQ=1670681411
PORT=8787
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
BETA=/mnt/xz/xiaojiujiu-beta
SRC=/home/bomomo/astrbot_test/src/xiaojiujiu/scripts

echo "=== 0) 冻结 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data -v "$BETA":/export \
  -v "$SRC":/scripts:ro python:3.12-slim \
  python /scripts/snapshot_beta.py --data-root /data --root /export/snapshots \
  --fleet http://192.168.1.15:8800 --note "before +13h hazard jump" 2>&1 | tail -2

echo
echo "=== 1) 推进时钟直到她决定行动 ==="
docker exec -i -e QQ="$QQ" xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, os, sqlite3, sys, urllib.request
from datetime import datetime, timedelta

port, ip, qq = sys.argv[1], sys.argv[2], os.environ["QQ"]
path = f"/data/default-friendmessage-{qq}/companion.sqlite3"

con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
last = con.execute("select max(decided_at) from decisions").fetchone()[0]
con.close()
base = datetime.fromisoformat(last)
print(f"  最近一条判决：{last[:19]}（实例内部时钟）")

for step in range(1, 4):
    when = base + timedelta(hours=13 * step)
    body = json.dumps({"force": True, "create_attempt": True, "now": when.isoformat()}).encode()
    req = urllib.request.Request(
        f"http://{ip}:{port}/endogenous", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    verdict = (data.get("decision") or {}).get("outcome") or {}
    print(f"  [跳 {13 * step}h → {when.strftime('%m-%d %H:%M')}] acted={verdict.get('acted')} "
          f"reason={verdict.get('reason')} hazard={verdict.get('hazard')} "
          f"adv={verdict.get('advantage')} silence={verdict.get('silence_utility')}")
    if verdict.get("acted"):
        print(f"      chosen={verdict.get('chosen_candidate_id')} attempt={data.get('attempt_id')} "
              f"outbox={data.get('outbox_id')}")
        break
else:
    print("  三次跳跃都没触发")
PY

echo
echo "=== 2) 盯投递链（最多 150 秒）==="
docker exec -i -e QQ="$QQ" xxj-runtime-fleet python3 - <<'PY'
import json, os, sqlite3, time

path = f"/data/default-friendmessage-{os.environ['QQ']}/companion.sqlite3"
last = None
for _ in range(75):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select outbox_id, kind, status, attempts, last_error, created_at from outbox"
        " order by rowid desc limit 2"
    ).fetchall()
    att = con.execute(
        "select attempt_id, state from action_attempts order by created_at desc limit 1"
    ).fetchone()
    snapshot = tuple((r["kind"], r["status"], r["attempts"]) for r in rows) + (att["state"],)
    if snapshot != last:
        stamp = time.strftime("%H:%M:%S")
        print(f"  [{stamp}] attempt={att['state']}")
        for r in rows:
            print(f"      {r['kind']:7s} {r['status']:10s} attempts={r['attempts']} "
                  f"err={str(r['last_error'])[:70]}")
        last = snapshot
    top = rows[0] if rows else None
    if top is not None and top["kind"] == "send" and top["status"] in ("delivered", "failed", "rejected"):
        break
    con.close()
    time.sleep(2)
con.close()
PY

echo
echo "=== 3) 最终：事件链 + 发出去的文案 ==="
docker exec -i -e QQ="$QQ" xxj-runtime-fleet python3 - <<'PY'
import json, os, sqlite3

path = f"/data/default-friendmessage-{os.environ['QQ']}/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
att = con.execute(
    "select attempt_id from action_attempts order by created_at desc limit 1"
).fetchone()["attempt_id"]
for ev in con.execute(
    "select created_at, from_state, to_state, reason from attempt_events"
    " where attempt_id = ? order by created_at", (att,),
):
    print(f"  {str(ev['created_at'])[11:19]} {ev['from_state']:>14s} -> {ev['to_state']:<14s} {ev['reason']}")
print()
for r in con.execute(
    "select kind, status, payload_json from outbox where created_at >="
    " (select created_at from outbox order by rowid desc limit 1) order by rowid"
):
    payload = json.loads(r["payload_json"] or "{}")
    text = " ".join(str(payload.get("text") or "").split())
    print(f"  {r['kind']:7s} {r['status']:10s} {text[:90]!r}")
PY

echo
echo "=== 4) 插件日志（这一轮投递）==="
docker logs astrbot-test --since 4m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -14
