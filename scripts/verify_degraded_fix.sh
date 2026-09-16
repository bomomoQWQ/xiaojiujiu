#!/usr/bin/env bash
# 容器里跑的是不是带 provider_called 的那版？以及新行到底怎么写的。
set -u
echo "=== 服务器 checkout 版本 ==="
cd /home/bomomo/astrbot_test/src/xiaojiujiu && git log --oneline -1
grep -c "provider_called" runtime/src/companion_runtime/runtime.py

echo
echo "=== 容器启动时间（UTC）==="
docker inspect xxj-runtime-fleet --format '{{.State.StartedAt}}'
date -u '+now: %Y-%m-%dT%H:%M:%SZ'

echo
echo "=== 容器内代码 ==="
docker exec xxj-runtime-fleet sh -c 'grep -c "provider_called" /app/runtime/src/companion_runtime/runtime.py'

echo
echo "=== 强制跑一次，再看最新一行 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, sqlite3, urllib.request

path = "/data/default-friendmessage-1670681411/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

# 触发一次"跳过"路径：deep_refresh 关掉时 reason=disabled，必然 provider_called=False
req = urllib.request.Request(
    "http://127.0.0.1:8801/cognition/refresh",
    data=json.dumps({}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("  refresh ->", json.loads(resp.read().decode())["reason"])
except Exception as exc:
    print("  call failed:", exc)

con2 = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
row = con2.execute(
    "select ran_at, trigger, ran, reason, degraded, payload_json from refresh_runs"
    " order by ran_at desc limit 1"
).fetchone()
print("  latest:", row[0], row[1], "ran=", row[2], row[3], "degraded=", row[4])
payload = json.loads(row[5])
print("  payload: degraded=", payload.get("degraded"), "provider_called=", payload.get("provider_called"))
PY
