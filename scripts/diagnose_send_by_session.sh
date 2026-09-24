#!/usr/bin/env bash
# aiocqhttp 适配器的 send_by_session：主动发送时的 self_id 从哪来。
set -u
echo "=== send_by_session 实现 ==="
docker exec astrbot-test sh -c 'grep -n "async def send_by_session" -A 40 /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py'

echo
echo "=== 适配器里 self_id / routing 的其它出现处 ==="
docker exec astrbot-test sh -c 'grep -n "self_id\|routing_params\|_api_clients\|bot_self_id" /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py'

echo
echo "=== 该平台配置里的 self_id（哪个 QQ 号被声明为机器人）==="
docker exec astrbot-test sh -c 'python3 -c "
import json
d = json.load(open(\"/AstrBot/data/cmd_config.json\", encoding=\"utf-8-sig\"))
for p in (d.get(\"platform\") or []):
    t = p.get(\"type\") or p.get(\"name\") or \"?\"
    cfg = p.get(\"config\") or {}
    sid = cfg.get(\"self_id\") or cfg.get(\"bot_uin\") or cfg.get(\"qq\") or cfg.get(\"id\")
    print(f\"  {t}: id={p.get(\\\"id\\\")} self_id={sid!r} enabled={p.get(\\\"enable\\\")}\")
"'
