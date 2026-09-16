#!/usr/bin/env bash
# 死 target 的 outbox 轮询器还在跑吗（有没有连接错误刷屏）。
set -u
echo "=== AstrBot 最近 10 分钟：连接类错误 ==="
docker logs astrbot-test --since 10m 2>&1 | grep -iE "cannot connect|connection refused|ClientConnector|outbox|lease|Target|8788" | tail -20

echo
echo "=== 只看 plugin 自己的日志行（最近 10 分钟）==="
docker logs astrbot-test --since 10m 2>&1 | grep -E "astrbot_plugin_companion_runtime" | tail -20

echo
echo "=== 计数：最近 10 分钟各类日志行数 ==="
docker logs astrbot-test --since 10m 2>&1 | grep -ciE "error" || true
echo "  (上面是 error 计数)"

echo
echo "=== 直接问插件状态：用 /companion_runtime 命令不可行，改看它的 poller 是否发起连接 ==="
echo "--- fleet 容器内 tcp 连接（已摘的端口应当没有连接）---"
docker exec xxj-runtime-fleet sh -c 'cat /proc/net/tcp 2>/dev/null | awk "NR>1 {print \$2}" | head -20' || echo "(无法读取)"
