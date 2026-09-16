#!/usr/bin/env bash
# Rebuild the runtime image from the checkout, recreate the fleet, wait for health.
# Usage: bash scripts/deploy_test_runtime.sh
set -u
REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
cd "$REPO"
echo "=== git ==="
git pull --ff-only 2>&1 | tail -2
git log --oneline -1

echo "=== build ==="
docker build -t xiaojiujiu-runtime:test . 2>&1 | tail -3

echo "=== recreate fleet ==="
cd /home/bomomo/astrbot_test
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d --force-recreate runtime-fleet 2>&1 | tail -3

for i in $(seq 1 15); do
  sleep 5
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/fleet/status || true)
  echo "  t=$((i * 5))s status=$code"
  if [ "$code" = "200" ]; then
    healthy=$(curl -s http://127.0.0.1:8800/fleet/status | grep -o '"health": "ok"' | wc -l)
    echo "healthy now: $healthy"
    if [ "$healthy" -ge 14 ]; then
      break
    fi
  fi
done

echo "=== final ==="
curl -s http://127.0.0.1:8800/fleet/status | head -c 260
echo
docker exec xxj-runtime-fleet python3 -c "
import sys; sys.path.insert(0, '/app/runtime/src')
from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT as P
print('sources example present:', P.count('\"sources\"'))
print('has 不得编造 id:', '不得编造 id' in P)
"
