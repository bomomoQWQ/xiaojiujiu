#!/usr/bin/env bash
# 谁抛的 ApiNotAvailable：在运行中的容器里搜代码。
set -u
echo "=== astrbot-test 容器内 ==="
docker exec astrbot-test sh -c 'grep -rn "ApiNotAvailable" /AstrBot --include=*.py 2>/dev/null | head -8'
echo "--- ActionExecutionError ---"
docker exec astrbot-test sh -c 'grep -rn "ActionExecutionError" /AstrBot --include=*.py 2>/dev/null | head -8'

echo
echo "=== runtime 镜像内 ==="
docker exec xxj-runtime-fleet sh -c 'grep -rn "ApiNotAvailable\|ActionExecutionError" /app 2>/dev/null | head -6'

echo
echo "=== 插件目录 ==="
docker exec astrbot-test sh -c 'ls /AstrBot/data/plugins/astrbot_plugin_companion_runtime/'
docker exec astrbot-test sh -c 'grep -rn "send_message\|ActionExecution" /AstrBot/data/plugins/astrbot_plugin_companion_runtime --include=*.py 2>/dev/null | head -12'
