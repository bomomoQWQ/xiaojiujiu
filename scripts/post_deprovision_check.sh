#!/usr/bin/env bash
# 1) /data 根下的 stray 文件是什么、谁写的；2) 插件会不会一直去轮询已摘掉的 target。
set -u

echo "=== /data 根部文件 ==="
docker exec xxj-runtime-fleet /bin/ls -la /data/ | grep -vE '^d' || true
echo "--- companion.sqlite3 里有什么 ---"
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

con = sqlite3.connect("file:/data/companion.sqlite3?mode=ro", uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
print("  tables:", len(tables))
for table in ("raw_events", "runtime_state", "decisions", "refresh_runs"):
    try:
        print(f"  {table}:", con.execute(f"select count(*) from {table}").fetchone()[0])
    except sqlite3.Error as exc:
        print(f"  {table}: {exc}")
try:
    print("  runtime_state row:", con.execute("select conversation_id, version from runtime_state").fetchall())
except sqlite3.Error as exc:
    print("  ", exc)
PY

echo
echo "=== 插件是否还在碰已摘掉的 target（AstrBot 日志）==="
docker logs astrbot-test --since 3m 2>&1 | grep -iE "error|traceback|refused|Cannot connect|8788|8789|8790|8791|8792|8793|8794|8795|8796|8797|8798|8799" | tail -15
echo "--- 有无 companion 相关行 ---"
docker logs astrbot-test --since 3m 2>&1 | grep -iE "companion_runtime" | tail -10

echo
echo "=== 插件端 target 管理代码里有没有"移除"路径 ==="
docker exec astrbot-test sh -c 'grep -n "targets.pop\|del self._targets\|removed\|prune\|drop" /AstrBot/data/plugins/astrbot_plugin_companion_runtime/main.py | head -20'
