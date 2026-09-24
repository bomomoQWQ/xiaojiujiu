#!/usr/bin/env bash
# 以 bot 身份给测试者发一条公告（走 AstrBot Open API，即她连到 NapCat 的那条链路）。
#
#   bash announce_to_testers.sh "要发的话"              # 发给舰队在册的所有人
#   bash announce_to_testers.sh "要发的话" qq07   # 额外再发给某个人
#
# 为什么走 Open API 而不是 NapCat HTTP：NapCat 没开 httpServers，只有一条到 AstrBot 的反向 WS；
# AstrBot 的 /api/v1/im/messages 正是"以 bot 身份发消息"的官方口子，用的就是这条链路。
# 鉴权：JWT（HS256，claim 里要有非空 username），jwt_secret 从 cmd_config.json 读。
#
# 发之前必须满足：NapCat 已登录 + AstrBot 适配器已连接（否则 Open API 会 4xx/发送失败）。
# 发之后必须核对：NapCat 日志里出现同样条数的「发送 -> 私聊」——Open API 回 200 只说明
# AstrBot 接受了请求，QQ 层真发出去要看 NapCat。
#
# 收件人规则（踩过坑）：**只发给舰队真正在服务的人**（`/fleet-data/people.json`）+ 命令行显式追加。
# 第一版还从 AstrBot 库里"枚举所有聊过的私聊会话"兜底，结果收件人变成 22 个：里面有一批早已
# 不存在的压测会话（HTTP 400 无法获取用户信息），**还有 bot 自己的 QQ 号 —— 给自己发了一条公告** ✗。
# 所以现在显式排除 bot 自己（从 NapCat 配置文件名 `onebot11_<qq>.json` 读）与假前端用户 20001。
set -u
TEXT="${1:-}"
if [ -z "$TEXT" ]; then
  echo "用法: bash $0 \"公告正文\" [额外QQ ...]" >&2
  exit 2
fi
shift || true
EXTRA="$*"

SERVED_JSON=$(docker exec xxj-runtime-fleet sh -c 'cat /fleet-data/people.json' 2>/dev/null || echo '[]')
SELF_QQ=$(docker exec xxj-napcat-test sh -c 'ls /app/napcat/config/onebot11_*.json 2>/dev/null | head -1' 2>/dev/null \
  | sed -E 's#.*onebot11_([0-9]+)\.json#\1#')
SELF_QQ="${SELF_QQ:-qq09}"
#: 模拟前端的用户号：QQ 里不存在这个人，公告必须跳过。
FAKE_QQ="20001"

docker exec -i -e ANNOUNCE="$TEXT" -e EXTRA="$EXTRA" -e SERVED_JSON="$SERVED_JSON" \
  -e SELF_QQ="$SELF_QQ" -e FAKE_QQ="$FAKE_QQ" -e DRY_RUN="${DRY_RUN:-0}" \
  astrbot-test python3 - <<'PY'
import datetime
import json
import os
import urllib.error
import urllib.request

import jwt

BASE = "http://127.0.0.1:6185"
ANNOUNCE = os.environ["ANNOUNCE"]
EXTRA = [item for item in os.environ.get("EXTRA", "").split() if item]
SELF_QQ = os.environ.get("SELF_QQ", "").strip()
FAKE_QQ = os.environ.get("FAKE_QQ", "").strip()

with open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig") as handle:
    config = json.load(handle)
secret = config["dashboard"]["jwt_secret"]
username = config["dashboard"].get("username") or "bomomo"
token = jwt.encode(
    {"username": username, "exp": datetime.datetime.now(datetime.timezone.utc)
     + datetime.timedelta(hours=2)},
    secret, algorithm="HS256")
if isinstance(token, bytes):
    token = token.decode()

try:
    served = json.loads(os.environ.get("SERVED_JSON") or "[]")
except ValueError:
    served = []
if isinstance(served, dict):
    served = served.get("people", [])

targets = set()
skipped = []
for item in served:
    umo = item if isinstance(item, str) else str(item.get("person") or item.get("umo") or "")
    if not umo:
        continue
    qq = umo.rsplit(":", 1)[-1]
    if qq in (SELF_QQ, FAKE_QQ):
        skipped.append((umo, "bot 自己 / 假前端"))
        continue
    if ":FriendMessage:" not in umo:
        skipped.append((umo, "不是私聊会话"))
        continue
    targets.add(umo)
for qq in EXTRA:
    targets.add("default:FriendMessage:%s" % qq)

ordered = sorted(targets)
print("正文: %s" % ANNOUNCE)
print("服务中 %d 人，本次发 %d 人：" % (len(served), len(ordered)))
for item in ordered:
    print("   ", item)
for umo, why in skipped:
    print("    跳过 %-38s（%s）" % (umo, why))
print()

if os.environ.get("DRY_RUN") == "1":
    print("DRY_RUN=1：只列名单，没有发。")
    raise SystemExit(0)

ok = 0
for umo in ordered:
    body = json.dumps({"umo": umo, "message": ANNOUNCE}).encode()
    request = urllib.request.Request(
        BASE + "/api/v1/im/messages", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = response.read().decode()
        print("  %-38s -> %s %s" % (umo, response.status, payload[:100]))
        ok += 1
    except urllib.error.HTTPError as exc:
        print("  %-38s -> HTTP %s %s" % (umo, exc.code, exc.read().decode()[:160]))
    except Exception as exc:  # noqa: BLE001
        print("  %-38s -> %s: %s" % (umo, type(exc).__name__, exc))

print()
print("成功 %d / %d" % (ok, len(ordered)))
PY
