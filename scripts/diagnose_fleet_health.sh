#!/usr/bin/env bash
# 为什么 fleet 容器 unhealthy：是不是 healthcheck 指着镜像默认端口 8787。
set -u
echo "=== 镜像/容器的 healthcheck 定义 ==="
docker inspect xxj-runtime-fleet --format '{{json .Config.Healthcheck}}'
echo
echo "=== 最近的健康检查输出 ==="
docker inspect xxj-runtime-fleet --format '{{json .State.Health}}' | python3 -m json.tool | tail -20
echo
echo "=== 8787 上现在有人监听吗 ==="
docker exec xxj-runtime-fleet /bin/sh -c 'cat /proc/net/tcp | awk "{print \$2}" | grep -i ":224B" || echo "  (没有 8787 监听: 0x224B)"'
echo
echo "=== 控制面自己的健康 ==="
curl -s -o /dev/null -w '  /fleet/status -> HTTP %{http_code}\n' http://127.0.0.1:8800/fleet/status
