#!/usr/bin/env bash
# 重建后真人落在哪个端口，插件有没有跟上（路由靠注册表，不靠端口号）。
set -u
echo "=== status / routes ==="
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
for p in d["people"]:
    print("  ", p["person"], "port", p["port"], "pid", p["pid"], p["health"])
'
curl -s http://127.0.0.1:8800/fleet/routes
echo
echo
echo "=== people.json ==="
cat /home/bomomo/astrbot_test/fleet-data/people.json
echo
echo
echo "=== 插件最近日志（注册表同步 / target）==="
docker logs astrbot-test --since 3m 2>&1 | grep -iE "companion_runtime|target|registry" | tail -8
echo
echo "=== 直接问那个端口的实例 ==="
PORT=$(curl -s http://127.0.0.1:8800/fleet/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["people"][0]["port"])')
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
docker exec xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, sys, urllib.request

port, ip = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=10) as resp:
    d = json.load(resp)
print("  /health ->", d.get("status"), "events", d.get("raw_events"),
      "unresolved", (d.get("semantics") or {}).get("unresolved"),
      "deep_refresh", d.get("deep_refresh"))
PY
