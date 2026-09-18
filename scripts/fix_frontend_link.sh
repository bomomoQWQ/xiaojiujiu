#!/usr/bin/env bash
# 看 WS 握手两侧的日志，然后重启前端（AstrBot 的监听已就绪，前端可能卡在长退避里）。
set -u

echo "=== 1) AstrBot 日志尾部（找握手/拒绝）==="
docker logs --tail 40 astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -22

echo
echo "=== 2) 前端这次运行的完整输出（前 12 行 + 后 6 行）==="
docker logs xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | head -12
echo "  ..."
docker logs --tail 6 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'

echo
echo "=== 3) 重启前端 ==="
docker restart xxj-onebot >/dev/null && echo "  已重启"

echo
echo "=== 4) 等适配器连接（最多 90 秒）==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
for i in $(seq 1 30); do
  sleep 3
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  if [ "$now" -gt "$before" ]; then
    echo "  已连接（$((i*3))s）：$before -> $now"
    break
  fi
done
docker logs --tail 12 astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -6

echo
echo "=== 5) 前端现在的状态 ==="
docker logs --tail 8 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'

echo
echo "=== 6) 端到端：前端有没有真的在跟 AstrBot 收发 ==="
tail -4 /home/bomomo/astrbot_test/frontend-logs/onebot.jsonl 2>/dev/null | cut -c1-220
