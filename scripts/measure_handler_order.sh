#!/usr/bin/env bash
# 实测修好之后的分发顺序：DEBUG 级会为每个被调用的插件处理器打印 "plugin -> 名 - 处理器"
#   a) 发一条 [自动回复] -> 期望：word_filter 出现在列表里，而我们的 on_message_observed 不出现
#      （事件被 stop -> 链在此处 break）
#   b) 发一条正常消息    -> 期望：两个都出现（word_filter 先，我们的后）
set -u

echo "=== 开 DEBUG ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import datetime as dt
import json
import shutil

path = "/AstrBot/data/cmd_config.json"
shutil.copy2(path, path + ".bak-dbg3-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
cfg["log_level"] = "DEBUG"
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
print("  log_level -> DEBUG")
PY
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done

send() {
  docker exec -i xxj-onebot python3 - "$1" <<'PY'
import json
import sys
import urllib.request
body = json.dumps({"text": sys.argv[1]}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send(%s) -> %s" % (sys.argv[1][:16], urllib.request.urlopen(req, timeout=10).status))
PY
}

echo
echo "=== a) 发 [自动回复] 顺序测量 ==="
MARK=$(date +%s)
send "[自动回复] 顺序测量"
sleep 16
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aE "plugin -> |屏蔽词过滤" | tail -12

echo
echo "=== b) 发正常消息 顺序测量 ==="
send "正常消息：顺序测量"
sleep 20
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aE "plugin -> |屏蔽词过滤" | tail -12

echo
echo "=== 恢复 INFO ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json

path = "/AstrBot/data/cmd_config.json"
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
cfg["log_level"] = "INFO"
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
print("  log_level -> INFO")
PY
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done
