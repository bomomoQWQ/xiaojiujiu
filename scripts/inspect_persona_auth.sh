#!/usr/bin/env bash
# 鉴权细节（有没有 API key 可用）+ 苏清徽提示词全文。
set -u
echo "=== 路由前缀 ==="
docker exec astrbot-test sh -c 'grep -rn "include_router" /AstrBot/astrbot/dashboard/app.py | head -10'
docker exec astrbot-test sh -c 'grep -n "router = APIRouter" /AstrBot/astrbot/dashboard/api/personas.py'

echo
echo "=== api_keys.py 支持什么鉴权 ==="
docker exec astrbot-test sh -c 'grep -nE "@router|def |scope" /AstrBot/astrbot/dashboard/api/api_keys.py | head -25'

echo
echo "=== require_persona_scope 怎么校验 ==="
docker exec astrbot-test sh -c 'grep -rn "def require_persona_scope" -A 12 /AstrBot/astrbot/dashboard/ | head -20'

echo
echo "=== auth.py 的登录方式 ==="
docker exec astrbot-test sh -c 'grep -nE "@router|async def|def " /AstrBot/astrbot/dashboard/api/auth.py | head -20'

echo
echo "=== dashboard 配置（用户名/密码是否明文）==="
python3 - /home/bomomo/astrbot_test/data/cmd_config.json <<'PY'
import json, sys

data = json.load(open(sys.argv[1], encoding="utf-8-sig"))
print("  top-level keys:", sorted(data.keys())[:25])
dash = data.get("dashboard") or {}
print("  dashboard keys:", sorted(dash.keys()))
for key, value in dash.items():
    if key == "password" and value:
        print(f"  password: <{len(str(value))} 字符>")
    else:
        print(f"  {key}: {value!r}")
PY

echo
echo "=== personas 表内容 ==="
python3 - /home/bomomo/astrbot_test/data/data_v4.db <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for row in con.execute("select persona_id, system_prompt from personas order by sort_order"):
    print(f"  ---- id={row[0]!r} ----")
    print(row[1])
    print()
PY
