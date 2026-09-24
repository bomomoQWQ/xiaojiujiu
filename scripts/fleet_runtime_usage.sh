#!/usr/bin/env bash
# 现在到底有几个 Runtime 在"真用"：谁有真人消息，谁只是验证残留。
set -u

echo "=== runtime 相关容器 ==="
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'xxj-runtime' || true

echo
echo "=== people.json 名册 ==="
cat /home/bomomo/astrbot_test/fleet-data/people.json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
people = d.get("people", d) if isinstance(d, dict) else d
print("count:", len(people))
for item in people:
    if isinstance(item, dict):
        print("  ", item.get("session"), "port", item.get("port"))
' 2>/dev/null || echo "(读取失败)"

echo
echo "=== 每人：事件数 / 真人消息数 / 是否模拟号 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, os, sqlite3

rows = []
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    events = con.execute("select count(*) from raw_events").fetchone()[0]
    users = con.execute(
        "select count(*) from raw_events where event_type='user_message' and content != ''"
    ).fetchone()[0]
    empty = con.execute(
        "select count(*) from raw_events where event_type='user_message' and content = ''"
    ).fetchone()[0]
    last = con.execute("select max(created_at) from raw_events").fetchone()[0]
    con.close()
    rows.append((person, events, users, empty, last))

print(f"{'person':42s} {'events':>7s} {'真人消息':>8s} {'空正文':>7s}  last_event")
for person, events, users, empty, last in rows:
    print(f"{person:42s} {events:7d} {users:8d} {empty:7d}  {str(last)[:19]}")
PY

echo
echo "=== 回退实例 xxj-runtime-test 在收什么 ==="
docker exec xxj-runtime-test python3 -c "
import sqlite3
con = sqlite3.connect('file:/data/companion.sqlite3?mode=ro', uri=True)
print('  conversation_id 分布:', con.execute('select conversation_id, count(*) from raw_events group by 1').fetchall())
print('  事件总数:', con.execute('select count(*) from raw_events').fetchone()[0])
" 2>&1 | tail -4

echo
echo "=== 资源占用 ==="
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' 2>/dev/null | grep -E 'xxj-runtime|NAME' || true

echo
echo "=== 插件当前路由来源 ==="
docker exec astrbot-test python3 -c "
import json
p='/AstrBot/data/config/astrbot_plugin_companion_runtime_config.json'
d=json.load(open(p,encoding='utf-8-sig'))
for k in ('observe_mode','session_routes','route_registry_url','route_auto_provision','runtime_base_url'):
    print(f'  {k} = {d.get(k)!r}')
" 2>&1 | tail -8
