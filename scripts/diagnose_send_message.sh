#!/usr/bin/env bash
# AstrBot 的 Context.send_message 怎么解析平台/self_id；为什么主动会 ApiNotAvailable。
set -u
echo "=== AstrBot 里 send_message 的实现 ==="
docker exec astrbot-test sh -c 'grep -rn "async def send_message" /AstrBot/astrbot/ | head -5'

echo
echo "=== 解析 umo 的部分 ==="
docker exec astrbot-test sh -c 'grep -rn "def get_platform\|def _get_platform\|umo\|unified_msg_origin" /AstrBot/astrbot/core/astr_main_agent.py 2>/dev/null | head -8'
docker exec astrbot-test sh -c 'grep -rln "async def send_message" /AstrBot/astrbot/core/ /AstrBot/astrbot/api/ 2>/dev/null'

echo
echo "=== 当前连进来的 OneBot 客户端（self_id）==="
docker exec astrbot-test sh -c 'grep -rn "_wsr_api\|ApiNotAvailable\|self_id" /usr/local/lib/python3.12/site-packages/aiocqhttp/api_impl.py | head -12'

echo
echo "=== AstrBot aiocqhttp 适配器里 self_id 从哪来 ==="
docker exec astrbot-test sh -c 'find /AstrBot/astrbot -name "*aiocqhttp*"; grep -n "self_id\|bot_self_id\|_bot" /AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py 2>/dev/null | head -15'
