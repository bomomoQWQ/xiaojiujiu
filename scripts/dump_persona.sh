#!/usr/bin/env bash
# 人体现有全文（存到本地便于精确改写）+ 我能不能拿到 dashboard 凭据走 API。
set -u
echo "=== 苏清徽提示词全文（含长度）==="
python3 - /home/bomomo/astrbot_test/data/data_v4.db <<'PY'
import sqlite3, sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = con.execute(
    "select system_prompt from personas where persona_id = '苏清徽'"
).fetchone()
text = row[0] or ""
print(f"  长度: {len(text)} 字")
print("----BEGIN----")
print(text)
print("----END----")
PY

echo
echo "=== 容器内能否读 cmd_config.json（决定我能否走 API 鉴权）==="
docker exec astrbot-test sh -c 'ls -la /AstrBot/data/cmd_config.json' 2>&1 | head -2
docker exec astrbot-test sh -c 'python3 -c "
import json
d = json.load(open(\"/AstrBot/data/cmd_config.json\", encoding=\"utf-8-sig\"))
dash = d.get(\"dashboard\") or {}
print(\"  dashboard keys:\", sorted(dash.keys()))
print(\"  username:\", dash.get(\"username\"))
print(\"  password:\", \"<已设置>\" if dash.get(\"password\") else None)
"' 2>&1 | head -5

echo
echo "=== WebUI 是否可从本机访问 ==="
curl -s -o /dev/null -w '  http://192.168.1.15:6186 -> HTTP %{http_code}\n' --max-time 8 http://192.168.1.15:6186/ || echo "  不可达"
