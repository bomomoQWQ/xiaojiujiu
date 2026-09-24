#!/usr/bin/env bash
# Recreate the fleet on the freshly built image and wait for it to come back.
set -u
cd /home/bomomo/astrbot_test
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d --force-recreate runtime-fleet 2>&1 | tail -5

for i in $(seq 1 12); do
  sleep 5
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/fleet/dashboard || true)
  if [ "$code" = "200" ]; then
    echo "control surface up after $((i * 5))s"
    break
  fi
  echo "waiting... ($code)"
done

curl -s http://127.0.0.1:8800/fleet/status -o /tmp/fleet-status.json
python3 - <<'PY'
import json

data = json.load(open("/tmp/fleet-status.json", encoding="utf-8"))
people = data.get("people", [])
print("instances:", data.get("count"), "healthy:", sum(1 for p in people if p["health"] == "ok"))
for person in people:
    if "qq01" in person["person"]:
        print("real tester:", person)
PY

echo "--- image in use ---"
docker inspect xxj-runtime-fleet --format '{{.Image}} {{.Config.Image}}'
echo "--- new code present in the container ---"
docker exec xxj-runtime-fleet python3 -c "
import sys; sys.path.insert(0, '/app/runtime/src')
from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT
from companion_runtime.providers import RemoteAPIProvider
p = RemoteAPIProvider('https://x/v1', model='m', api_key='k')
print('json_mode =', p.json_mode)
print('prompt has 不要编造:', '不要编造' in DEEP_REFRESH_SYSTEM_PROMPT)
print('prompt has 证据不足:', '证据不足' in DEEP_REFRESH_SYSTEM_PROMPT)
"
