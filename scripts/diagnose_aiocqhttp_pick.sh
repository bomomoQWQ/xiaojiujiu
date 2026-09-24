#!/usr/bin/env bash
# 无 self_id 时 aiocqhttp 怎么挑连接（适配器尾部）与平台配置里的 self_id。
set -u
echo "=== 适配器尾部：_wsr_api_clients 附近 ==="
docker exec astrbot-test sh -c 'sed -n "440,490p" /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py'

echo
echo "=== AiocqhttpMessageEvent.send_message 的 event=None 分支 ==="
docker exec astrbot-test sh -c 'grep -n "async def send_message" -A 60 /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py | sed -n "1,70p"'

echo
echo "=== 平台配置（哪些 OneBot 客户端被声明）==="
docker exec astrbot-test python3 - <<'PY'
import json

data = json.load(open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig"))
platforms = data.get("platform") or []
if isinstance(platforms, dict):
    platforms = list(platforms.values())
for item in platforms:
    if not isinstance(item, dict):
        continue
    kind = item.get("type") or item.get("name") or "?"
    cfg = item.get("config") or {}
    keys = sorted(cfg.keys())
    print(f"  type={kind} id={item.get('id')!r} enable={item.get('enable')} cfg_keys={keys}")
    for key in ("self_id", "bot_uin", "qq", "ws_reverse", "reverse_ws", "token"):
        if key in cfg:
            value = cfg[key]
            print(f"      {key} = {'<hidden>' if key == 'token' else value!r}")
PY
