#!/usr/bin/env bash
# update_persona 是否刷新内存缓存；以及 WebUI 用的 API 路径。
set -u
echo "=== persona_mgr.update_persona ==="
docker exec astrbot-test sh -c 'grep -n "async def update_persona" -A 40 /AstrBot/astrbot/core/persona_mgr.py'

echo
echo "=== get_v3_persona_data（缓存是怎么重建的）==="
docker exec astrbot-test sh -c 'grep -n "def get_v3_persona_data" -A 35 /AstrBot/astrbot/core/persona_mgr.py'

echo
echo "=== dashboard personas API 路由 ==="
docker exec astrbot-test sh -c 'grep -nE "@.*route|def " /AstrBot/astrbot/dashboard/api/personas.py | head -30'

echo
echo "=== personas 表 ==="
python3 - /home/bomomo/astrbot_test/data/data_v4.db <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for row in con.execute(
    "select persona_id, length(system_prompt), updated_at from personas order by sort_order"
):
    print(f"   id={row[0]!r} len={row[1]} updated={row[2]}")
PY
