#!/usr/bin/env bash
# 把 QQ(不支持流式的平台) 的策略从 realtime_segmenting 改成 turn_off，
# 让 result_decorate 真正执行（分段回复 + 剔除尾句号），然后用 NapCat 的发送日志验收。
set -u
LOG() { docker logs xxj-napcat-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'; }
SENT() { LOG | grep -aE "发送 -> 私聊" ; }

echo "=== 1) 备份并修改配置 ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import datetime as dt
import json
import shutil

path = "/AstrBot/data/cmd_config.json"
stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(path, path + ".bak-streaming-" + stamp)
print("  备份:", path + ".bak-streaming-" + stamp)
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
settings = cfg["provider_settings"]
print("  改前: streaming_response=%s strategy=%s" % (
    settings.get("streaming_response"), settings.get("unsupported_streaming_strategy")))
settings["unsupported_streaming_strategy"] = "turn_off"
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
with open(path, encoding="utf-8-sig") as handle:
    check = json.load(handle)["provider_settings"]
print("  改后: streaming_response=%s strategy=%s" % (
    check.get("streaming_response"), check.get("unsupported_streaming_strategy")))
PY

echo
echo "=== 2) 重启（测试者可能正在聊天，这个窗口约 30 秒）==="
before_conn=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
before_sent=$(SENT | wc -l)
echo "  重启前: 连接行=$before_conn  已发送气泡=$before_sent"
docker restart astrbot-test >/dev/null
for i in $(seq 1 36); do
  sleep 5
  now_conn=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  if [ "$now_conn" -gt "$before_conn" ]; then echo "  已连接（$((i*5))s）"; break; fi
done

echo
echo "=== 3) 等新的发送（最多 4 分钟，需要测试者说句话）==="
for i in $(seq 1 24); do
  sleep 10
  now_sent=$(SENT | wc -l)
  if [ "$now_sent" -gt "$before_sent" ]; then
    echo "  出现新气泡（$before_sent -> $now_sent），第 $((i*10))s"
    break
  fi
  [ $((i % 6)) -eq 0 ] && echo "    t=$((i*10))s 还没有新消息…"
done

echo
echo "=== 4) 重启之后她实际发出的每一条（这就是测试者看到的）==="
SENT | tail -20

echo
echo "=== 5) 自动判定 ==="
SENT | tail -20 | python3 -c "
import re, sys
lines = [l.rstrip() for l in sys.stdin if l.strip()]
bubbles = []
for line in lines:
    m = re.search(r'发送 -> 私聊 \((\d+)\) (.*)$', line)
    if m:
        bubbles.append(m.group(2))
print('  抓到 %d 个气泡' % len(bubbles))
trailing = [b for b in bubbles if b.endswith('。')]
print('  结尾是句号的: %d 个 %s' % (len(trailing), trailing[:3]))
punct_only = [b for b in bubbles if b and all(c in '。？！~…，、' for c in b)]
print('  纯标点气泡  : %d 个 %s' % (len(punct_only), punct_only[:5]))
print('  样例:', bubbles[:6])
"
