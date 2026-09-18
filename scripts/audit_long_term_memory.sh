#!/usr/bin/env bash
# 长期记忆的现状：每个实例存了什么、来源是什么、有没有进到注入块里。
# 只读。
set -u

echo "=== 1) 与记忆相关的表 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3

path = sorted(glob.glob("/data/*/companion.sqlite3"))[0]
con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
for (name,) in con.execute("select name from sqlite_master where type='table'"):
    if any(token in name.lower() for token in ("mem", "event", "candidate", "emotion")):
        cols = [row[1] for row in con.execute("pragma table_info(%s)" % name)]
        rows = con.execute("select count(*) from %s" % name).fetchone()[0]
        print("  %-26s rows=%-6d %s" % (name, rows, cols[:10]))
con.close()
PY

echo
echo "=== 2) 每个人的记忆：条数 / 种类 / 来源 / 时间 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    table = "memory_items" if "memory_items" in tables else ("memories" if "memories" in tables else None)
    if table is None:
        print("  %-14s 找不到记忆表（有: %s）" % (tag, sorted(t for t in tables if "mem" in t)))
        con.close()
        continue
    cols = [row[1] for row in con.execute("pragma table_info(%s)" % table)]
    total = con.execute("select count(*) from %s" % table).fetchone()[0]
    print("  %-14s 记忆 %d 条  表=%s" % (tag, total, table))
    if total:
        for kind, count in con.execute("select kind, count(*) from %s group by kind" % table):
            print("      %-22s %d" % (kind, count))
        stamp_col = "created_at" if "created_at" in cols else cols[0]
        print("      --- 最近 3 条 ---")
        for row in con.execute("select * from %s order by %s desc limit 3" % (table, stamp_col)):
            data = dict(zip(cols, row))
            text = str(data.get("content") or data.get("summary") or data.get("text") or "")[:60]
            print("        %s [%s] %s" % (str(data.get(stamp_col))[:19], data.get("kind"), text))
    con.close()
PY

echo
echo "=== 3) 记忆候选（semantic 产出的入口）与语义调用计数 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    out = []
    for name in sorted(tables):
        if "memor" in name and "candidate" in name:
            rows = con.execute("select status, count(*) from %s group by status" % name).fetchall()
            out.append("%s=%s" % (name, dict(rows)))
    if out:
        print("  %-14s %s" % (tag, "; ".join(out)))
    con.close()
PY

echo
echo "=== 4) 舰队状态里的记忆/语义相关字段 ==="
curl -s --max-time 10 http://127.0.0.1:8800/fleet/status | python3 -c "
import json, sys
d = json.load(sys.stdin)
for person in d.get('people', []):
    keys = {k: v for k, v in person.items() if any(t in k for t in ('memor', 'semantic', 'deep_refresh', 'unresolved', 'unfinished'))}
    print('  %-34s %s' % (person.get('person'), json.dumps(keys, ensure_ascii=False)))
"

echo
echo "=== 5) 注入块里有没有【必要记忆】段（含内容）==="
for port in 8794 8787 8788; do
  echo "  --- port $port ---"
  docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
try:
    req = urllib.request.Request('http://127.0.0.1:$port/context/render-block', data=b'{}',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        block = (json.loads(r.read().decode()).get('block') or '')
except Exception as exc:
    print('    取不到:', exc); raise SystemExit
lines = block.splitlines()
start = next((i for i, line in enumerate(lines) if '必要记忆' in line), None)
if start is None:
    print('    块里没有【必要记忆】段（块长度 %d）' % len(block))
    for line in lines:
        if line.startswith('【'):
            print('      ' + line)
else:
    for line in lines[start:start + 6]:
        print('    ' + line)
" 2>&1
done
