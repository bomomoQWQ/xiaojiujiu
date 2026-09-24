#!/usr/bin/env bash
# 1) event=None 时 _dispatch_send 怎么定 self_id；2) 停掉测试前端。
set -u
echo "=== _dispatch_send 源码（决定主动发送带什么 self_id）==="
docker exec astrbot-test sh -c 'grep -n "_dispatch_send" -A 45 /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py | sed -n "1,60p"'

echo
echo "=== 停掉测试前端 xxj-onebot ==="
docker stop xxj-onebot

echo
echo "=== 确认已停 ==="
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep xxj-onebot
curl -s -o /dev/null -w '  前端 6300 -> HTTP %{http_code}（连不上才对）\n' --max-time 5 http://127.0.0.1:6300/state || echo "  前端 6300 已不可达 ✓"

echo
echo "=== AstrBot 侧看到前端断开了吗 ==="
sleep 6
docker logs astrbot-test --since 2m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -8

echo
echo "=== 其余容器仍然健康 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-|astrbot-test'
