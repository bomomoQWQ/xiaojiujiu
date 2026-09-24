#!/usr/bin/env bash
# fleet 容器的健康检查改探控制面（8800），而不是镜像默认的 8787。
set -u
FLEET=/home/bomomo/astrbot_test/fleet.yml
cp "$FLEET" "$FLEET.bak-$(date +%Y%m%d-%H%M%S)"

if grep -q 'healthcheck:' "$FLEET"; then
  echo "already has a healthcheck"
else
  python3 - "$FLEET" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
out: list[str] = []
for line in lines:
    if line.strip() == "networks: [test_net]":
        out.extend([
            "    # 镜像自带的 healthcheck 探 127.0.0.1:8787 —— 在这个容器里，8787 只是",
            "    # '恰好占用第一个端口的那个人'。名单里没有他（或那个端口被退休）时没人监听，",
            "    # 于是容器报 unhealthy 而它其实服务得好好的。这个容器的职责是 supervisor，",
            "    # 所以让它对控制面负责；每个人的健康在 /fleet/status 与看板上。",
            "    healthcheck:",
            "      test:",
            "        - CMD",
            "        - python",
            "        - -c",
            "        - \"import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/fleet/status', timeout=4).status == 200 else 1)\"",
            "      interval: 30s",
            "      timeout: 5s",
            "      start_period: 30s",
            "      retries: 3",
        ])
    out.append(line)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("inserted healthcheck")
PY
fi

echo "=== 重建 ==="
cd /home/bomomo/astrbot_test
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d --force-recreate runtime-fleet 2>&1 | tail -3

echo "=== 等 health 变绿 ==="
for i in $(seq 1 20); do
  sleep 5
  state=$(docker inspect xxj-runtime-fleet --format '{{.State.Health.Status}}')
  echo "  t=$((i * 5))s health=$state"
  [ "$state" = "healthy" ] && break
done

echo
echo "=== 结果 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-runtime'
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  instances:", d["count"])
for p in d["people"]:
    print("  ", p["person"], p["health"], "events", p["raw_events"],
          "attempts/settled", p["deep_refresh_attempts"], "/", p["deep_refresh_settled"])
'
