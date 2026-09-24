#!/usr/bin/env bash
# 装 astrbot_plugin_word_filter 并把「[自动回复]」加进屏蔽词，然后端到端验收：
#   a) 模拟一条 "[自动回复] 。" -> 不应有回复，也不应进 Runtime 的 raw_events
#   b) 模拟一条正常消息        -> 正常回复（不误伤）
set -u

PLUGINS=/home/bomomo/astrbot_test/data/plugins
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1) 安装插件 ==="
if [ -d "$PLUGINS/astrbot_plugin_word_filter" ]; then
  echo "  已存在，改为更新"
  cd "$PLUGINS/astrbot_plugin_word_filter" && git pull --ff-only 2>&1 | tail -1
else
  git clone --depth 1 -q https://github.com/yvdi-abc/astrbot_plugin_word_filter \
    "$PLUGINS/astrbot_plugin_word_filter" && echo "  已克隆"
fi
ls -1 "$PLUGINS"

echo
echo "=== 2) 写插件配置（屏蔽词 = [自动回复]）==="
docker exec -u 0 -i astrbot-test python3 - "$STAMP" <<'PY'
import json
import os
import shutil
import sys

stamp = sys.argv[1]
path = "/AstrBot/data/config/astrbot_plugin_word_filter_config.json"
if os.path.exists(path):
    shutil.copy2(path, path + ".bak-" + stamp)
config = {
    "enable_filter": True,
    "filter_mode": "partial",
    "case_sensitive": False,
    "admin_only": True,
    "blocked_words": ["[自动回复]"],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
with open(path, encoding="utf-8") as handle:
    print("  写入:", json.dumps(json.load(handle), ensure_ascii=False))
PY

echo
echo "=== 3) 重启 astrbot-test ==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null && echo "  已重启"
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done
echo "  --- 插件加载日志 ---"
docker logs --since 3m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "屏蔽词|word_filter" | tail -4

echo
echo "=== 4) 验收 a：发一条 [自动回复] 。 ==="
MARK_BEFORE=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "[自动回复] 。"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 18
MARK_AFTER=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
echo "  Prepare to send 条数: $MARK_BEFORE -> $MARK_AFTER（期望不变）"
echo "  --- 插件拦截日志 ---"
docker logs --since 2m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "屏蔽词过滤" | tail -3

echo
echo "=== 5) 验收 b：发一条正常消息（不能误伤）==="
NORMAL_BEFORE=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "正常消息：你在忙吗"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 20
NORMAL_AFTER=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
echo "  Prepare to send 条数: $NORMAL_BEFORE -> $NORMAL_AFTER（期望 +1 或更多）"
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "Prepare to send" | tail -2

echo
echo "=== 6) 验收 c：被拦的那条有没有进 Runtime 的 raw_events ==="
docker exec xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import sqlite3

cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
          - dt.timedelta(minutes=6)).isoformat()
con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
rows = con.execute(
    "select event_type, timestamp, substr(content,1,40) from raw_events"
    " where timestamp > ? order by timestamp desc limit 8", (cutoff,)).fetchall()
con.close()
print("  近 6 分钟事件 %d 条:" % len(rows))
for event_type, stamp, content in rows:
    print("    %s %-18s %s" % (str(stamp)[:19], event_type, content))
print("  其中含「自动回复」的: %d（期望 0）" % sum(1 for r in rows if "自动回复" in (r[2] or "")))
PY
