#!/usr/bin/env bash
# 查 994959351 在公告前后（11:50-12:10 CST）的全部收发，以及各容器真正停止的时刻。
set -u

echo "=== 1) 服务的实际停止时刻 ==="
for c in astrbot-test xxj-runtime-fleet xxj-napcat-test; do
  printf "  %-20s " "$c"
  docker inspect "$c" --format 'StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}}' 2>/dev/null
done

echo
echo "=== 2) 994959351 在 09-18 11:50 之后的全部收发（NapCat 日志，停着的容器也能读）==="
docker logs xxj-napcat-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -a "(994959351)" | awk '$1=="09-18"' | awk '{ if ($2 >= "11:50:00") print }' | tail -40

echo
echo "=== 3) 公告前后各 3 分钟内的全部流量（看公告是否触发对方的自动回复）==="
docker logs xxj-napcat-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aE "接收 <- |发送 -> " | awk '$1=="09-18" && $2 >= "11:53:00" && $2 <= "12:00:00"' | tail -40

echo
echo "=== 4) 公告那一批发送的结果：9 条里每条的时间与对象 ==="
docker logs xxj-napcat-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -a "系统消息" | awk '{print "  " $1, $2, $NF}' | head -12

echo
echo "=== 5) 有没有"发送失败/被限制"之类的日志 ==="
docker logs xxj-napcat-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aiE "失败|风控|限制|risk|blocked|retcode=[^0]|err" | tail -15
