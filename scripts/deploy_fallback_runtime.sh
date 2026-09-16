#!/usr/bin/env bash
# 把单实例回退 Runtime 也换到刚构建的镜像，保持全栈同一 image id。
set -u
cd /home/bomomo/astrbot_test
echo "=== 重建前 ==="
docker inspect xxj-runtime-test --format '{{.Image}}' || true

docker compose -p astrbot_test -f astrbot.yml up -d --force-recreate runtime 2>&1 | tail -3

for i in $(seq 1 10); do
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/health || true)
  if [ "$code" = "200" ]; then
    echo "fallback runtime healthy after $((i * 3))s"
    break
  fi
done

echo "=== 全栈 image id ==="
docker inspect xxj-runtime-fleet xxj-runtime-test --format '{{.Name}} {{.Image}} {{.Config.Image}}'
echo "=== 回退实例健康 ==="
docker exec xxj-runtime-test python3 -c "
import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=10) as r:
    d = json.load(r)
print('events =', d.get('raw_events'), 'semantics =', (d.get('semantics') or {}).get('unresolved'), 'db =', d.get('storage', {}).get('database_path'))
" 2>&1 | tail -3
