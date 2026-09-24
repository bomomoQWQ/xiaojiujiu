#!/usr/bin/env bash
# 角色人格存在哪、现在写了什么。
set -u
echo "=== AstrBot data 目录结构 ==="
ls /home/bomomo/astrbot_test/data/
echo
echo "=== config 目录 ==="
ls /home/bomomo/astrbot_test/data/config/ 2>/dev/null | head -30

echo
echo "=== 找 persona 相关文件 ==="
find /home/bomomo/astrbot_test/data -maxdepth 3 -iname '*persona*' 2>/dev/null | head -10

echo
echo "=== 可能的 persona 存储（json/db）==="
for f in /home/bomomo/astrbot_test/data/config/persona.json \
         /home/bomomo/astrbot_test/data/persona.json \
         /home/bomomo/astrbot_test/data/data_v4.db; do
  [ -e "$f" ] && ls -la "$f"
done
ls -la /home/bomomo/astrbot_test/data/*.db 2>/dev/null

echo
echo "=== 当前会话绑定的 persona id ==="
python3 - <<'PY'
import json, glob, os

for name in ("cmd_config.json", "astrbot_plugin_companion_runtime_config.json"):
    path = f"/home/bomomo/astrbot_test/data/config/{name}"
    if not os.path.exists(path):
        continue
    data = json.load(open(path, encoding="utf-8-sig"))
    print(f"  {name}: keys={list(data)[:12]}")
PY
