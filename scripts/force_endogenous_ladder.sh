#!/usr/bin/env bash
# 正确解析判决；按"接下来几小时"的阶梯逐轮强制，直到她决定行动。
#
# 每轮把 now 往前推 ~45 分钟（第一轮推到刚好越过 cooldown 10:41），
# 所以每轮都是一次间隔真实的 hazard 抽签，等价于把这几个小时压缩着过一遍。
set -u
PORT=8787
QQ=qq01
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)

docker exec -i -e QQ="$QQ" xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, os, sqlite3, sys, urllib.request
from datetime import datetime, timedelta, timezone

port, ip, qq = sys.argv[1], sys.argv[2], os.environ["QQ"]
path = f"/data/default-friendmessage-{qq}/companion.sqlite3"


def state():
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = con.execute(
        "select decided_at, reason, hazard, advantage, silence_utility from decisions"
        " order by decided_at desc limit 1"
    ).fetchone()
    con.close()
    return row


def call(now_iso):
    body = json.dumps({"force": True, "create_attempt": True, "now": now_iso}).encode()
    req = urllib.request.Request(
        f"http://{ip}:{port}/endogenous", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)


before = state()
print(f"  起始：最近判决 {before[0][:19]} {before[1]} hazard={before[2]:.4g} adv={before[3]:+.4f}")

base = datetime.fromisoformat(before[0])
for step in range(1, 8):
    when = base + timedelta(minutes=45 * step)
    if when <= datetime.now(timezone.utc):
        continue
    data = call(when.isoformat())
    verdict = (data.get("decision") or {}).get("outcome") or {}
    print(f"  [{step}] now={when.strftime('%H:%M')} acted={verdict.get('acted')} "
          f"reason={verdict.get('reason')} hazard={verdict.get('hazard')} "
          f"adv={verdict.get('advantage')} silence={verdict.get('silence_utility')}")
    if verdict.get("acted"):
        print(f"      chosen={verdict.get('chosen_candidate_id')} "
              f"attempt={data.get('attempt_id')} outbox={data.get('outbox_id')}")
        break
else:
    print("  阶梯走完，她一次都没决定行动")
PY

echo
echo "=== 投递结果（最新 attempt / outbox）==="
docker exec -i -e QQ="$QQ" xxj-runtime-fleet python3 - <<'PY'
import json, os, sqlite3, time

time.sleep(12)
path = f"/data/default-friendmessage-{os.environ['QQ']}/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
row = con.execute(
    "select attempt_id, state, created_at from action_attempts order by created_at desc limit 1"
).fetchone()
if row:
    print(f"  {row['attempt_id']} state={row['state']} created={str(row['created_at'])[:19]}")
    for ev in con.execute(
        "select created_at, from_state, to_state, reason from attempt_events"
        " where attempt_id = ? order by created_at", (row["attempt_id"],),
    ):
        print(f"    {str(ev['created_at'])[11:19]} {ev['from_state']} -> {ev['to_state']}  {ev['reason']}")
for r in con.execute(
    "select kind, status, payload_json, updated_at from outbox order by rowid desc limit 2"
):
    payload = json.loads(r["payload_json"] or "{}")
    text = " ".join(str(payload.get("text") or "").split())
    print(f"  outbox {r['kind']:7s} {r['status']:10s} {str(r['updated_at'])[11:19]} {text[:80]!r}")
PY

echo
echo "=== 插件侧日志 ==="
docker logs astrbot-test --since 3m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -10
