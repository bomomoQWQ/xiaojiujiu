#!/usr/bin/env bash
# 根因：AstrBot 的 plugin_set 是插件白名单（waking_check/stage.py:166），
# word_filter 不在里面 -> 它的处理器不会被激活。把它加进去，然后重新验收。
set -u

echo "=== 1) 当前 plugin_set ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json
cfg = json.load(open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig"))
print("  plugin_set =", json.dumps(cfg.get("plugin_set"), ensure_ascii=False))
PY

echo
echo "=== 2) 把 astrbot_plugin_word_filter 加进白名单 ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import datetime as dt
import json
import shutil

path = "/AstrBot/data/cmd_config.json"
shutil.copy2(path, path + ".bak-pluginset-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
current = cfg.get("plugin_set")
if current == ["*"]:
    print("  已经是全开（['*']），不需要改")
else:
    names = list(current or [])
    if "astrbot_plugin_word_filter" not in names:
        names.append("astrbot_plugin_word_filter")
    cfg["plugin_set"] = names
    with open(path, "w", encoding="utf-8-sig") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=2)
    print("  改后 =", json.dumps(names, ensure_ascii=False))
PY

echo
echo "=== 3) 重启 ==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done

echo
echo "=== 4) 验收 a：发一条 [自动回复] （期望：不回复 + 不进 Runtime）==="
MARK_BEFORE=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "[自动回复] 。"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 20
MARK_AFTER=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
echo "  Prepare to send 条数: $MARK_BEFORE -> $MARK_AFTER（期望不变）"
echo "  --- 拦截日志 ---"
docker logs --since 2m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aE "屏蔽词过滤" | tail -3

echo
echo "=== 5) 验收 b：正常消息（期望：正常回复，不误伤）==="
NORMAL_BEFORE=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request
body = json.dumps({"text": "验收用正常消息：在忙吗"}).encode()
req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
print("  /send ->", urllib.request.urlopen(req, timeout=10).status)
PY
sleep 22
NORMAL_AFTER=$(docker logs astrbot-test 2>&1 | grep -c "Prepare to send" || true)
echo "  Prepare to send 条数: $NORMAL_BEFORE -> $NORMAL_AFTER（期望 +1 以上）"
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aE "Prepare to send" | tail -2

echo
echo "=== 6) 验收 c：被拦的那条有没有进 Runtime raw_events ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import sqlite3

cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
          - dt.timedelta(minutes=5)).isoformat()
con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
rows = con.execute(
    "select event_type, timestamp, substr(content,1,46) from raw_events"
    " where timestamp > ? order by timestamp desc limit 8", (cutoff,)).fetchall()
con.close()
print("  近 5 分钟事件 %d 条:" % len(rows))
for event_type, stamp, content in rows:
    print("    %s %-18s %s" % (str(stamp)[:19], event_type, content))
print("  其中含「自动回复」的: %d（期望 0）" % sum(1 for r in rows if "自动回复" in (r[2] or "")))
PY
