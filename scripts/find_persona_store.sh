#!/usr/bin/env bash
# 人格存在哪、读的时候缓存吗、有没有官方的热更新入口。
set -u
DB=/home/bomomo/astrbot_test/data/data_v4.db

echo "=== 1) data_v4.db 里的表 ==="
python3 - "$DB" <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for (name,) in con.execute(
    "select name from sqlite_master where type='table' order by 1"
):
    print("  ", name)
PY

echo
echo "=== 2) 找 persona 相关的表/列 ==="
python3 - "$DB" <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for (name,) in con.execute("select name from sqlite_master where type='table' order by 1"):
    cols = [r[1] for r in con.execute(f"pragma table_info({name})")]
    if any("persona" in c.lower() or "prompt" in c.lower() or "system" in c.lower() for c in cols):
        print(f"  {name}: {', '.join(cols)}")
PY

echo
echo "=== 3) AstrBot 源码里人格怎么读的（有没有缓存/重载入口）==="
docker exec astrbot-test sh -c 'grep -rn "persona" /AstrBot/astrbot/core/*.py 2>/dev/null | head -5'
echo "--- persona manager 文件 ---"
docker exec astrbot-test sh -c 'find /AstrBot/astrbot -iname "*persona*" | head -10'
echo "--- 有没有 reload/refresh 之类的方法 ---"
docker exec astrbot-test sh -c 'grep -rn "def .*persona\|persona.*cache\|reload_persona\|update_persona" /AstrBot/astrbot/core/persona* 2>/dev/null | head -20'
