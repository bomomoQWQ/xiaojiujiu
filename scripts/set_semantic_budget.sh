#!/usr/bin/env bash
# Raise the completion ceiling on the live test fleet to 65536 and recreate it.
#
# 4096 was chosen from "measured need is ~1200 tokens", which is the wrong way round
# for a ceiling: the API allows up to 384K, only generated tokens are billed, so the
# budget should never be the thing that truncates a reply. Truncation is exactly what
# broke the deep refresh (finish_reason=length -> invalid JSON -> empty suggestions).
set -u
FLEET=/home/bomomo/astrbot_test/fleet.yml
cp "$FLEET" "$FLEET.bak-$(date +%Y%m%d-%H%M%S)"

sed -i 's/^      CR_SEMANTIC_MAX_TOKENS: .*/      CR_SEMANTIC_MAX_TOKENS: 65536/' "$FLEET"
grep -n 'CR_SEMANTIC_MAX_TOKENS' "$FLEET"

cd /home/bomomo/astrbot_test
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d --force-recreate runtime-fleet 2>&1 | tail -3

for i in $(seq 1 15); do
  sleep 5
  healthy=$(curl -s http://127.0.0.1:8800/fleet/status | grep -o '"health": "ok"' | wc -l)
  echo "  t=$((i * 5))s healthy=$healthy"
  [ "$healthy" -ge 14 ] && break
done

docker exec xxj-runtime-fleet python3 -c "
import sys; sys.path.insert(0, '/app/runtime/src')
from companion_runtime.config import load_config
from companion_runtime.providers import build_provider
p = build_provider(load_config().semantic)
print('max_tokens =', p.max_tokens, ' json_mode =', p.json_mode)
"
