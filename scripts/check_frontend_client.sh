#!/usr/bin/env bash
# 测试前端（self_id 10001）现在连着吗？它和真 QQ 是否共用一个 aiocqhttp 平台实例。
set -u
echo "=== 测试前端容器状态与日志 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep xxj-onebot
echo "--- 它的 ws 日志尾部 ---"
tail -5 /home/bomomo/astrbot_test/frontend-logs/onebot.jsonl 2>/dev/null || echo "  (无 jsonl 日志)"
echo "--- 它的 /state ---"
curl -s --max-time 8 http://127.0.0.1:6300/state | head -c 400 || echo "  不可达"
echo
echo "--- 它自报是否连上 AstrBot ---"
curl -s --max-time 8 http://127.0.0.1:6300/state | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print("  parse fail:", exc); raise SystemExit
for k in ("connected","ws","errors","sent","received","self_id","user_id"):
    if k in d:
        print(f"  {k}: {d[k]}")
print("  keys:", sorted(d.keys()))
' 2>/dev/null || true

echo
echo "=== AstrBot 侧：平台实例与 self_id ==="
docker exec astrbot-test sh -c 'grep -rn "platform_id\|get_platforms\|platform_manager" /AstrBot/astrbot/core/star/context.py | head -10'
echo "--- 有没有暴露 platform 列表的 API ---"
docker exec astrbot-test sh -c 'grep -rn "def send_message" -A 25 /AstrBot/astrbot/core/star/context.py | head -45'
