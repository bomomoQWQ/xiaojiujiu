#!/usr/bin/env bash
# 那 137 秒里发生了什么：Runtime 侧收到过 /authorize 吗，插件侧报了什么。
set -u
echo "=== 1) 租约 TTL 与尝试预算（配置）==="
grep -nE 'lease|attempt' /home/bomomo/astrbot_test/src/xiaojiujiu/runtime/src/companion_runtime/config.py | grep -iE 'ttl|seconds|max|budget' | head -12

echo
echo "=== 2) 各实例 runtime.log 里 send 行相关的调用（各取一个失败窗口）==="
for spec in "default-friendmessage-qq08 08:31 08:35" \
            "default-friendmessage-qq03 06:14 06:17" \
            "default-friendmessage-qq04 09:54 09:57"; do
  set -- $spec
  person=$1; from=$2; to=$3
  echo "--- $person ($from-$to) ---"
  docker exec xxj-runtime-fleet /bin/sh -c \
    "grep -E '($from|$to)' /data/logs/$person.log | grep -E 'authorize|outbox|delivery|rendered' | head -12" || echo "    (无匹配)"
done

echo
echo "=== 3) AstrBot 侧插件对这些主动发送的日志 ==="
docker logs astrbot-test --since 6h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE "outbox|authorize|defer|could not reach|send|lease" | tail -30
