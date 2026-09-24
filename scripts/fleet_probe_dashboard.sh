#!/usr/bin/env bash
# 验收看板：refresh 计分板是否出现在 status JSON 与 dashboard HTML 里。
set -u
echo "=== /fleet/status 里真人这一行 ==="
curl -s http://127.0.0.1:8800/fleet/status -o /tmp/fs.json
python3 - <<'PY'
import json

data = json.load(open("/tmp/fs.json", encoding="utf-8"))
for person in data.get("people", []):
    if "qq01" in person["person"] or "20001" in person["person"]:
        print(" ", json.dumps(person, ensure_ascii=False))
PY

echo
echo "=== dashboard HTML：表头与真人那一行 ==="
curl -s http://127.0.0.1:8800/fleet/dashboard -o /tmp/dash.html
grep -o '<tr><th>.*</tr>' /tmp/dash.html | head -1
grep -o "<tr><td><b>default-friendmessage-qq01</b>.*</tr>" /tmp/dash.html | head -1
echo
echo "=== 从本机（Windows 侧）可访问性 ==="
curl -s -o /dev/null -w 'dashboard HTTP %{http_code}\n' http://192.168.1.15:8800/fleet/dashboard
