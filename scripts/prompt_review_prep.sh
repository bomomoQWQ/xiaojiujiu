#!/usr/bin/env bash
# 为"审阅所有发给主 LLM 的提示词"做准备：
#   1) 打开 AstrBot 的 trace（记录拼装后的 system_prompt / 工具 / provider）
#   2) 取人格（data_v4.db 的 personas.system_prompt）
#   3) 取我们注入的临时块（/context/render-block，假用户实例 8794）
set -u

CFG=/AstrBot/data/cmd_config.json

echo "=== 1) 打开 trace 并重启 ==="
docker exec -u 0 -i astrbot-test python3 - "$CFG" <<'PY'
import json
import shutil
import sys
import datetime as dt

path = sys.argv[1]
stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(path, path + ".bak-trace-" + stamp)
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
print("  之前: trace_enable=%s trace_log_enable=%s" % (
    cfg.get("trace_enable"), cfg.get("trace_log_enable")))
cfg["trace_enable"] = True
cfg["trace_log_enable"] = True
cfg["trace_log_path"] = "logs/astrbot.trace.log"
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
with open(path, encoding="utf-8-sig") as handle:
    now = json.load(handle)
print("  现在: trace_enable=%s trace_log_enable=%s path=%s" % (
    now.get("trace_enable"), now.get("trace_log_enable"), now.get("trace_log_path")))
PY
docker restart astrbot-test >/dev/null && echo "  已重启"

echo
echo "=== 2) 人格（AstrBot 的 personas 表）==="
docker exec -i astrbot-test python3 - <<'PY'
import sqlite3

con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
cols = [row[1] for row in con.execute("pragma table_info(personas)")]
print("  列:", cols)
for row in con.execute("select * from personas"):
    data = dict(zip(cols, row))
    print("  --- persona %s (%s) ---" % (data.get("persona_id"), data.get("name")))
    print("  begin_dialogs:", str(data.get("begin_dialogs"))[:120])
    prompt = data.get("system_prompt") or ""
    print("  system_prompt（%d 字）:" % len(prompt))
    for line in prompt.splitlines():
        print("    | " + line)
con.close()
PY

echo
echo "=== 3) 等 AstrBot 起来 ==="
for i in $(seq 1 20); do
  sleep 3
  if docker logs --since 2m astrbot-test 2>&1 | grep -q "适配器已连接"; then
    echo "  适配器已连接（$((i*3))s）"
    break
  fi
done

echo
echo "=== 4) 我们注入的临时块（假用户实例 8794）==="
docker exec xxj-runtime-fleet python3 - <<'PY'
import json
import urllib.request

request = urllib.request.Request("http://127.0.0.1:8794/context/render-block",
                                 data=b"{}", headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.loads(response.read().decode())
print("  ephemeral=%s version=%s" % (payload.get("ephemeral"), payload.get("version")))
print("  ---- block 开始 ----")
for line in (payload.get("block") or "").splitlines():
    print("  | " + line)
print("  ---- block 结束（%d 字）----" % len(payload.get("block") or ""))
PY
