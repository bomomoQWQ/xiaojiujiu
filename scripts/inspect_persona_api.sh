#!/usr/bin/env bash
# 热更新人格的确切路由 + 鉴权方式 + 当前提示词全文。
set -u
echo "=== personas.py 全部路由 ==="
docker exec astrbot-test sh -c 'grep -nE "@router\.(get|post|put|patch|delete)" -A 3 /AstrBot/astrbot/dashboard/api/personas.py | head -60'

echo
echo "=== dashboard 鉴权怎么做的 ==="
docker exec astrbot-test sh -c 'ls /AstrBot/astrbot/dashboard/api/'
docker exec astrbot-test sh -c 'grep -rn "Depends\|jwt\|token" /AstrBot/astrbot/dashboard/api/personas.py | head -8'
echo "--- 有没有 auth 路由 ---"
docker exec astrbot-test sh -c 'grep -rln "auth/login\|def login" /AstrBot/astrbot/dashboard/ | head -5'

echo
echo "=== cmd_config.json 里的 dashboard 段（看用户名/是否已改密码）==="
python3 - /home/bomomo/astrbot_test/data/cmd_config.json <<'PY'
import json, sys

data = json.load(open(sys.argv[1], encoding="utf-8-sig"))
dash = data.get("dashboard") or {}
print("  keys:", sorted(dash.keys()))
for key in ("username", "password", "host", "port"):
    value = dash.get(key)
    if key == "password" and value:
        print(f"  {key}: <已设置 {len(str(value))} 字符>")
    else:
        print(f"  {key}: {value!r}")
PY

echo
echo "=== 当前 persona 列表 ==="
python3 - /home/bomomo/astrbot_test/data/data_v4.db <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for row in con.execute(
    "select persona_id, length(system_prompt), updated_at from personas order by sort_order"
):
    print(f"   id={row[0]!r} len={row[1]} updated={row[2]}")
PY
