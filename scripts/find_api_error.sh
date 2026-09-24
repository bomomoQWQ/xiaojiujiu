#!/usr/bin/env bash
# ApiNotAvailable 到底是谁的定义（不在 /AstrBot/*.py，扩大搜索面）。
set -u
echo "=== 搜 site-packages 与全盘 .py ==="
docker exec astrbot-test sh -c 'grep -rln "ApiNotAvailable" /usr/local/lib 2>/dev/null | head -6'
docker exec astrbot-test sh -c 'grep -rln "ApiNotAvailable" /AstrBot 2>/dev/null | head -6'
echo "--- 全盘（含非 .py）取名 ---"
docker exec astrbot-test sh -c 'grep -rl "ApiNotAvailable" / --exclude-dir=proc --exclude-dir=sys 2>/dev/null | head -8'

echo
echo "=== 插件里怎么格式化这个错误 ==="
docker exec astrbot-test sh -c 'sed -n "100,135p" /AstrBot/data/plugins/astrbot_plugin_companion_runtime/astrbot_executor.py'

echo
echo "=== truncate_error 实现 ==="
docker exec astrbot-test sh -c 'grep -rn "def truncate_error" -A 12 /AstrBot/data/plugins/astrbot_plugin_companion_runtime --include=*.py | head -20'
