#!/usr/bin/env bash
# 只取 routing_params 的算法（88-108 行），以及停前端后 AstrBot 的日志。
set -u
echo "=== _dispatch_send 88-108 行（带行号）==="
docker exec astrbot-test sh -c 'awk "NR>=88 && NR<=108 {printf \"%4d  %s\n\", NR, \$0}" /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py'

echo
echo "=== 停掉前端后 AstrBot 近 3 分钟日志 ==="
docker logs astrbot-test --since 3m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -10

echo
echo "=== 当前平台连接数（真 QQ 是否仍在线）==="
docker logs astrbot-test --since 30m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | grep -icE "适配器已连接" || true
docker logs xxj-napcat-test --since 10m 2>&1 | tail -3
