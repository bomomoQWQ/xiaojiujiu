#!/usr/bin/env bash
# 1) 00:13 CST 前后 OneBot 连接发生了什么；2) 失败后有没有重试路径。
set -u

echo "=== AstrBot 日志里 OneBot 连接事件（最近 6 小时）==="
docker logs astrbot-test --since 6h 2>&1 | grep -iE 'aiocqhttp|适配器|adapter|connect|disconnect|websocket|断开|重连' | tail -25

echo
echo "=== 当前 OneBot / napcat 状态 ==="
docker logs xxj-napcat-test --since 30m 2>&1 | tail -6
echo "--- astrbot 里平台是否在线 ---"
docker logs astrbot-test --since 10m 2>&1 | tail -5

echo
echo "=== 插件如何处理执行失败（有没有 nack/retry）==="
docker exec astrbot-test sh -c 'grep -n "nack\|retry\|retryable\|ActionExecutionError\|execut" /AstrBot/data/plugins/astrbot_plugin_companion_runtime/main.py | head -30'
