#!/usr/bin/env bash
# persona_mgr 是缓存还是每次读库？以及苏清徽现在的提示词全文。
set -u
echo "=== persona_mgr.py 关键部分 ==="
docker exec astrbot-test sh -c 'sed -n "1,110p" /AstrBot/astrbot/core/persona_mgr.py'

echo
echo "=== update_persona 实现（看它是否同时刷新缓存）==="
docker exec astrbot-test sh -c 'sed -n "140,200p" /AstrBot/astrbot/core/persona_mgr.py'

echo
echo "=== 当前 personas 表内容 ==="
python3 - /home/bomomo/astrbot_test/data/data_v4.db <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for row in con.execute(
    "select persona_id, length(system_prompt), updated_at from personas order by sort_order"
):
    print(f"   id={row[0]!r} prompt_len={row[1]} updated={row[2]}")
PY
