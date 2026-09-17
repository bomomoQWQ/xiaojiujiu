#!/usr/bin/env bash
# 测试环境 NapCat 的端口与连接方式（含 WebUI 入口与 OneBot 反向 WS 目标）。
set -u
echo "=== 端口映射（测试 vs live，别搞混）==="
docker ps --format '{{.Names}}\t{{.Ports}}' | grep -E 'napcat|astrbot|xxj-onebot'

echo
echo "=== napcat 配置文件 ==="
ls -la /home/bomomo/astrbot_test/napcat-test-data/config/ 2>/dev/null

echo
echo "--- onebot 配置（反向 WS 目标 / token / 端口）---"
for f in /home/bomomo/astrbot_test/napcat-test-data/config/napcat_*.json; do
  [ -f "$f" ] || continue
  echo "file: $f"
  python3 - "$f" <<'PY'
import json, sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("network", "musicSignUrl", "enableLocalFile2Url", "parseMultMsg"):
    if key in data:
        print(f"  {key}:", json.dumps(data[key], ensure_ascii=False)[:600])
PY
done

echo
echo "--- webui 配置 ---"
for f in /home/bomomo/astrbot_test/napcat-test-data/config/webui.json; do
  [ -f "$f" ] && python3 -c "
import json,sys
d=json.load(open('$f',encoding='utf-8'))
print('  keys:', sorted(d.keys()))
for k in ('host','port','token','prefix','loginRate'):
    if k in d: print(f'  {k}:', d[k])
"
done

echo
echo "=== 登录的 QQ 号 ==="
ls /home/bomomo/astrbot_test/napcat-test-data/config/ | grep -oE 'napcat_[0-9]+' || true

echo
echo "=== WebUI 从本机可达吗 ==="
curl -s -o /dev/null -w '  http://192.168.1.15:6098 -> HTTP %{http_code}\n' --max-time 8 http://192.168.1.15:6098/ || echo "  不可达"
