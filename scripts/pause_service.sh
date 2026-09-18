#!/usr/bin/env bash
# 1) 在 NapCat 侧核实公告真的发出去了（QQ 层真相）
# 2) 暂停服务：停 AstrBot（不再回复）+ 停 runtime fleet（不再主动联系）
#    NapCat 保持运行 —— 停它会掉 QQ 登录态，下次要扫码。
set -u

echo "=== 1) NapCat 侧核实公告 ==="
docker logs --tail 400 xxj-napcat-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -a "系统消息" | tail -20
echo
echo "  条数: $(docker logs --tail 400 xxj-napcat-test 2>&1 | grep -ac '系统消息')"

echo
echo "=== 2) 暂停服务 ==="
for name in astrbot-test xxj-runtime-fleet; do
  printf "  停 %-20s " "$name"
  docker stop "$name" >/dev/null && echo "已停"
done

echo
echo "=== 3) 停后状态（NapCat 应仍在运行）==="
docker ps -a --format '{{.Names}}\t{{.Status}}' \
  | grep -Ei "astrbot-test|xxj-runtime-fleet|xxj-napcat-test|xxj-onebot|xxj-runtime-test"

echo
echo "=== 4) 确认她不会再回复/主动发 ==="
printf "  astrbot-test 端口 6186: "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 http://127.0.0.1:6186/ || echo "连不上（预期）"
printf "  fleet 控制面 8800:      "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 http://127.0.0.1:8800/fleet/status || echo "连不上（预期）"
printf "  napcat WebUI 6098:      "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 http://127.0.0.1:6098/ || echo "连不上"
