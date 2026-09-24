#!/usr/bin/env bash
# 拉最新插件代码、重启、用"日志行数增量"确认这次真的连上了，并回读防抖窗口。
set -u
P=/home/bomomo/astrbot_test/data/plugins/astrbot_plugin_companion_runtime
LOG() { docker logs astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'; }

echo "=== 1) pull ==="
cd "$P" && git pull --ff-only 2>&1 | tail -2 && git log --oneline -1

echo
echo "=== 2) 重启前的计数 ==="
before_conn=$(LOG | grep -c "适配器已连接" || true)
before_start=$(LOG | grep -c "adapter started" || true)
echo "  适配器已连接=$before_conn  adapter started=$before_start"

echo
echo "=== 3) 重启 ==="
docker restart astrbot-test >/dev/null
echo "  已重启，等待新的连接行（这个窗口里发的消息会静默丢失）"
ok=0
for i in $(seq 1 36); do
  sleep 5
  now_conn=$(LOG | grep -c "适配器已连接" || true)
  if [ "$now_conn" -gt "$before_conn" ]; then
    echo "  新的「适配器已连接」在第 $((i*5)) 秒出现（$before_conn -> $now_conn）"
    ok=1
    break
  fi
done
[ "$ok" = 1 ] || echo "  !! 120 秒内没有出现新的连接行"

echo
echo "=== 4) 这次的启动行（应含 debounce=）==="
LOG | grep "adapter started" | tail -1

echo
echo "=== 5) 配置告警 / 异常 ==="
LOG | tail -400 | grep -iE "companion_runtime config|clamped|Traceback|ERROR" | tail -8
echo "  (以上为空即无告警)"

echo
echo "=== 6) 平台连接状态 ==="
LOG | tail -60 | grep -iE "aiocqhttp|OneBot|适配器已连接" | tail -4
