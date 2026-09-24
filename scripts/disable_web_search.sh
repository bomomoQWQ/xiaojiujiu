#!/usr/bin/env bash
# 关掉测试栈的联网搜索（provider_settings.web_search=false），保留 tavily key 以便随时开回。
# 重启后用 trace 里的 tools 列表实证那两个工具不再注册。
set -u

CFG=/AstrBot/data/cmd_config.json

echo "=== 0) 线上实例装了哪些插件（判断它是不是同一个角色）==="
docker exec -u 0 astrbot sh -c 'ls -1 /AstrBot/data/plugins/ 2>/dev/null' | head -20

echo
echo "=== 1) 备份并关闭 web_search ==="
docker exec -u 0 -i astrbot-test python3 - "$CFG" <<'PY'
import datetime as dt
import json
import shutil
import sys

path = sys.argv[1]
stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(path, path + ".bak-websearch-" + stamp)
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
ps = cfg.setdefault("provider_settings", {})
print("  之前: web_search=%s web_search_link=%s" % (ps.get("web_search"), ps.get("web_search_link")))
ps["web_search"] = False
ps["web_search_link"] = False
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
with open(path, encoding="utf-8-sig") as handle:
    now = json.load(handle)["provider_settings"]
print("  现在: web_search=%s web_search_link=%s（tavily key 保留，随时可开回）" % (
    now.get("web_search"), now.get("web_search_link")))
PY

echo
echo "=== 2) 重启 astrbot-test ==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null && echo "  已重启（连接基线 $before）"
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done

echo
echo "=== 3) 驱动一轮，看模型实际拿到哪些工具 ==="
docker exec -i xxj-onebot python3 - <<'PY'
import json
import urllib.request

body = json.dumps({"text": "关搜索之后的一轮：你在吗"}).encode()
request = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=10) as response:
    print("  /send ->", response.status)
PY
sleep 22
docker exec astrbot-test sh -c 'grep -o "\"tools\": \[[^]]*\]" /AstrBot/data/logs/astrbot.trace.log | tail -2'
echo "  （期望：只剩 future_task 与 send_message_to_user）"

echo
echo "=== 4) 她的回复（确认没被影响）==="
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "Prepare to send" | tail -2
