#!/usr/bin/env bash
# 恢复调试环境：runtime fleet + astrbot-test + 模拟 onebot 前端（xxj-onebot）。
# 真实 QQ 那条线（xxj-napcat-test）保持停止 —— 公告已经说了暂停服务。
#
# 顺序有讲究：fleet 先起（插件的路由注册表要连得上它）→ astrbot-test → 前端
# （前端是反向 WS 的客户端，要连 astrbot 的 6199，所以 astrbot 必须先起来）。
set -u

echo "=== 1) 起 runtime fleet ==="
docker start xxj-runtime-fleet >/dev/null && echo "  已起"
for i in $(seq 1 20); do
  sleep 3
  if curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:8800/fleet/status | grep -q 200; then
    echo "  控制面 200（$((i*3))s）"
    break
  fi
done
curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin); items=d.get('instances') or d.get('people') or []
print('  实例 %d，health=ok 的 %d' % (len(items), sum(1 for i in items if (i.get('health') or i.get('status'))=='ok')))
"

echo
echo "=== 2) 起 astrbot-test ==="
docker start astrbot-test >/dev/null && echo "  已起"

echo
echo "=== 3) 起模拟 onebot 前端（xxj-onebot）==="
docker start xxj-onebot >/dev/null && echo "  已起"

echo
echo "=== 4) 等适配器连接（这次靠前端连上来）==="
for i in $(seq 1 30); do
  sleep 4
  if docker logs --tail 60 astrbot-test 2>&1 | grep -q "适配器已连接"; then
    echo "  适配器已连接（$((i*4))s）"
    break
  fi
  echo "    t=$((i*4))s …"
done

echo
echo "=== 5) 插件加载与路由 ==="
docker logs --tail 120 astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "adapter started|added Runtime target|适配器已连接" | tail -12

echo
echo "=== 6) 前端与容器状态 ==="
docker ps -a --format '{{.Names}}\t{{.Status}}' \
  | grep -Ei "astrbot-test|xxj-runtime|xxj-onebot|xxj-napcat-test" || true

echo
echo "=== 7) 前端在跑什么（它自己会模拟用户说话）==="
docker logs --tail 15 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -15
