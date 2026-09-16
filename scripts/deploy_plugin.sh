#!/usr/bin/env bash
# 更新测试栈的插件并重启 AstrBot，验证真的加载了、适配器连上了。
set -u
P=/home/bomomo/astrbot_test/data/plugins/astrbot_plugin_companion_runtime

echo "=== pull ==="
cd "$P"
git pull --ff-only 2>&1 | tail -3
git log --oneline -1

echo
echo "=== 新代码在不在 ==="
grep -c "TransportUnavailable" companion_runtime/protocol.py
grep -c "TransportUnavailable" companion_runtime/outbox.py
grep -c "is_transport_unavailable" astrbot_executor.py

echo
echo "=== 重启 astrbot-test ==="
docker restart astrbot-test

echo "=== 等适配器连接（这个窗口里发消息会静默丢失）==="
for i in $(seq 1 24); do
  sleep 5
  if docker logs astrbot-test --since 3m 2>&1 | grep -q "适配器已连接"; then
    echo "adapter connected after $((i * 5))s"
    break
  fi
  echo "  t=$((i * 5))s ..."
done

echo
echo "=== 容器里能看到新代码吗 ==="
docker exec astrbot-test sh -c 'grep -c TransportUnavailable /AstrBot/data/plugins/astrbot_plugin_companion_runtime/companion_runtime/protocol.py'

echo
echo "=== 插件有没有加载报错 ==="
docker logs astrbot-test --since 3m 2>&1 | grep -iE "companion|插件|plugin|Traceback|Error|error" | tail -20

echo
echo "=== 最近日志尾部 ==="
docker logs astrbot-test --since 3m 2>&1 | tail -8
