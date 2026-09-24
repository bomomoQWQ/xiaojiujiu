#!/usr/bin/env bash
# 诊断：word_filter 的处理器到底有没有被 AstrBot 调用。
# AstrBot 在 DEBUG 级会为每个被调用的插件处理器打印 "plugin -> <插件名> - <处理器名>"
# （pipeline/process_stage/method/star_request.py:46）。
set -u

echo "=== 1) 开 DEBUG 并重启 ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json
import shutil
import datetime as dt

path = "/AstrBot/data/cmd_config.json"
shutil.copy2(path, path + ".bak-dbg2-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
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

echo
echo "=== 2) 发一条带屏蔽词的消息 ==="
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "[自动回复] 诊断用"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 20

echo
echo "=== 3) 这次被调用的插件处理器（DEBUG 行）==="
docker logs --since 2m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aE "plugin -> " | tail -12

echo
echo "=== 4) word_filter 相关的一切日志 ==="
docker logs --since 2m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aiE "word_filter|屏蔽词" | tail -10

echo
echo "=== 5) 恢复 log_level=INFO 并重启 ==="
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
