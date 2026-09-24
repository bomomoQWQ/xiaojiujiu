#!/usr/bin/env bash
# 找 AstrBot 的发送接口与 API key 存储。注意：本脚本必须落地成文件后 bash 执行，
# 不能 pipe 给 bash —— 里面的 heredoc 会抢 stdin。
set -u

echo "=== A) OpenAPI 规范里 im/ 相关路径 ==="
docker exec -i astrbot-test python3 - <<'PY'
import json
import urllib.request

for url in ("http://127.0.0.1:6185/api/v1/openapi.json",
            "http://127.0.0.1:6185/openapi.json",
            "http://127.0.0.1:6185/api/openapi.json"):
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            spec = json.loads(response.read().decode())
    except Exception as exc:  # noqa: BLE001
        print("  %s -> %s" % (url, exc))
        continue
    print("  来自 %s，paths=%d" % (url, len(spec.get("paths", {}))))
    for path in sorted(spec.get("paths", {})):
        if "/im/" in path or "openapi" in path:
            print("    %s  %s" % (path, sorted(spec["paths"][path].keys())))
    schemas = spec.get("components", {}).get("schemas", {}) or {}
    for name, schema in schemas.items():
        if "Im" in name or "Message" in name:
            print("    schema %s = %s" % (name, json.dumps(schema, ensure_ascii=False)[:400]))
    break
PY

echo
echo "=== B) API key 存在哪 ==="
docker exec -i astrbot-test python3 - <<'PY'
import sqlite3

con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
names = [row[0] for row in con.execute(
    "select name from sqlite_master where type='table' order by name")]
print("  全部表 %d 个，含 key/api/user 的:" % len(names))
for name in names:
    low = name.lower()
    if any(token in low for token in ("key", "api", "user", "dashboard", "auth", "scope")):
        cols = [row[1] for row in con.execute("pragma table_info(%s)" % name)]
        rows = con.execute("select count(*) from %s" % name).fetchone()[0]
        print("    %-28s rows=%-4d %s" % (name, rows, cols))
PY
