#!/usr/bin/env bash
# 1) 确认 6199 现在还听不听（若不听，验证"监听与连接共存亡"的假设）
# 2) 重启 astrbot-test 重新武装监听，然后**不再探测**，让前端独占连接
# 3) 用前端的日志（而不是我的探测）来确认链路
set -u

echo "=== 1) 6199 现在听不听（只测 TCP，不做握手）==="
docker exec xxj-runtime-fleet python3 -c "
import socket
try:
    s = socket.create_connection(('astrbot', 6199), timeout=4); s.close(); print('  TCP 通（监听在）')
except Exception as exc:
    print('  TCP 不通（监听没了）-> %s' % exc)
"

echo
echo "=== 2) 重启 astrbot-test 重新武装 ==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null && echo "  已重启（before=%s）" %s = "$before" 2>/dev/null || echo "  已重启"
docker restart astrbot-test >/dev/null && echo "  已重启（适配器连接行数基线 $before）"

echo
echo "=== 3) 等前端自己抢上连接（只看日志，不做任何探测）==="
for i in $(seq 1 40); do
  sleep 5
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  if [ "$now" -gt "$before" ]; then
    echo "  适配器已连接（$((i*5))s）：$before -> $now"
    break
  fi
done

echo
echo "=== 4) AstrBot 侧确认 ==="
docker logs --since 5m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "适配器已连接|Loading IM platform|adapter started" | tail -4

echo
echo "=== 5) 前端侧：还在报错吗（最近 8 行）==="
docker logs --tail 8 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'

echo
echo "=== 6) 插件与路由 ==="
docker logs --since 5m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "companion_runtime adapter started|added Runtime target" | tail -3
