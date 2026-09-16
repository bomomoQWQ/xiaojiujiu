#!/usr/bin/env bash
# 正向证据：插件确实在跟真人现在这个端口说话。
set -u
PORT=$(curl -s http://127.0.0.1:8800/fleet/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["people"][0]["port"])')
echo "port: $PORT"

echo "=== 该实例的运行日志尾部（插件应当持续 lease outbox）==="
docker exec xxj-runtime-fleet /bin/sh -c "tail -6 /data/logs/default-friendmessage-1670681411.log"

echo
echo "=== /health 直接问（带 -i）==="
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
docker exec -i xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, sys, urllib.request

port, ip = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=10) as resp:
    d = json.load(resp)
print("  status:", d.get("status"), "events:", d.get("raw_events"))
print("  semantics:", d.get("semantics"))
print("  deep_refresh:", d.get("deep_refresh"))
PY

echo
echo "=== lease 计数（近 200 行日志里）==="
docker exec xxj-runtime-fleet /bin/sh -c "grep -c 'outbox/lease' /data/logs/default-friendmessage-1670681411.log || true"
