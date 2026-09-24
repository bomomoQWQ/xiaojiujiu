#!/usr/bin/env bash
# 重启测试 NapCat，并验证：QQ 登录 + 反向 WS 连回 AstrBot。
set -u
echo "=== 重启前 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-napcat-test|astrbot-test'
echo "--- napcat 最近日志 ---"
docker logs xxj-napcat-test --since 5m 2>&1 | tail -5

echo
echo "=== 重启 ==="
docker restart xxj-napcat-test

echo "=== 等 QQ 登录 + WS 连上（最多 120s）==="
logged=0
for i in $(seq 1 24); do
  sleep 5
  recent=$(docker logs xxj-napcat-test --since 3m 2>&1)
  if echo "$recent" | grep -qiE "登录成功|已登录|login success|快速登录|在线"; then
    echo "  t=$((i * 5))s: QQ 已登录"
    logged=1
    break
  fi
  if echo "$recent" | grep -qiE "二维码|qrcode|扫码|需要登录"; then
    echo "  t=$((i * 5))s: 需要扫码登录！"
    logged=2
    break
  fi
  echo "  t=$((i * 5))s ..."
done

echo
echo "=== NapCat 日志尾部 ==="
docker logs xxj-napcat-test --since 3m 2>&1 | tail -15

echo
echo "=== AstrBot 侧：适配器有没有重新连上 ==="
for i in $(seq 1 12); do
  if docker logs astrbot-test --since 3m 2>&1 | grep -q "适配器已连接"; then
    echo "  astrbot: aiocqhttp 适配器已连接"
    break
  fi
  sleep 5
  echo "  t=$((i * 5))s waiting for adapter ..."
done
docker logs astrbot-test --since 3m 2>&1 | grep -iE "适配器|aiocqhttp" | tail -5

echo
echo "=== 二维码文件（若需要扫码）==="
ls -la --time-style=full-iso /home/bomomo/astrbot_test/napcat-qrcode.png 2>/dev/null || echo "  (没有二维码文件)"
find /home/bomomo/astrbot_test/napcat-test-data -name '*.png' -newermt '-10 minutes' 2>/dev/null | head -3

echo
echo "=== 现状 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-napcat-test|astrbot-test'
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  fleet instances:", d["count"])
for p in d["people"]:
    print("  ", p["person"], p["health"], "events", p["raw_events"])
'
