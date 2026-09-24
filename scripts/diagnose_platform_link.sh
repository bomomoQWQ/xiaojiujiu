#!/usr/bin/env bash
# 平台连接在那一刻在不在：NapCat 的 WS 状态、AstrBot 的适配器连接/断开历史。
set -u
echo "=== 现在几点 ==="
date '+%F %T %Z'

echo
echo "=== NapCat：连接/登录/断开相关（近 12h）==="
docker logs xxj-napcat-test --since 12h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE "websocket|连接|断开|离线|登录|上线|reconnect|重连|closed|error" \
  | grep -viE "getCrashDetailBean|EGL|gpu|GLContext|viz_main" | tail -25

echo
echo "=== AstrBot：适配器连接/断开事件（近 12h）==="
docker logs astrbot-test --since 12h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE "适配器|aiocqhttp|disconnect|websocket|重连|断开" | tail -20

echo
echo "=== 最后一次成功收到用户消息是什么时候 ==="
docker logs astrbot-test --since 12h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "event_bus:74" | tail -3

echo
echo "=== 最后一次 Prepare to send（回复出口）==="
docker logs astrbot-test --since 12h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "Prepare to send" | tail -3

echo
echo "=== 主动发送失败的时间线（我这版修复的日志）==="
docker logs astrbot-test --since 12h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "outbox:470" | awk '{print $1, $NF}' | tail -12
