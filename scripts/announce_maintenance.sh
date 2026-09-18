#!/usr/bin/env bash
# 通过 AstrBot 的 Open API（POST /api/v1/im/messages）以 bot 身份给所有用户发维护公告。
#
# 为什么走这条路：NapCat 没开 HTTP 接口（httpServers 为空），只有一条到 AstrBot 的反向 WS；
# AstrBot 的 Open API 正是"以 bot 身份发消息"的官方口子，走的就是它连到 NapCat 的那条链路。
# 鉴权用 JWT（HS256，claim 里要有非空 username），jwt_secret 从 cmd_config.json 读。
#
# 必须落地成文件运行：里面有 heredoc，pipe 给 bash 会抢 stdin。
set -u

ANNOUNCE='「系统消息」很抱歉打扰您的雅兴，我们需要暂时对苏清徽进行断网维护和更新，在此期间我们会暂停服务，预计再次上线时间为9月19日23.59前。上线后之前的聊天记忆会被清除。感谢您的耐心等待'

docker exec -i -e ANNOUNCE="$ANNOUNCE" astrbot-test python3 - <<'PY'
import datetime
import json
import os
import sqlite3
import urllib.error
import urllib.request

import jwt

BASE = "http://127.0.0.1:6185"
ANNOUNCE = os.environ["ANNOUNCE"]

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
print("JWT 已签发（username=%s）" % username)

# 收件人：优先从 AstrBot 的库里枚举出真正聊过的会话，再用 fleet 已知的 7 人兜底。
sessions = set()
try:
    con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    for table in tables:
        cols = [r[1] for r in con.execute("pragma table_info(%s)" % table)]
        for col in cols:
            if col in ("unified_msg_origin", "umo", "session_id", "conversation_id"):
                try:
                    for (value,) in con.execute(
                            "select distinct %s from %s where %s is not null" % (col, table, col)):
                        text = str(value or "")
                        if ":FriendMessage:" in text:
                            sessions.add(text)
                except sqlite3.Error:
                    pass
    con.close()
except sqlite3.Error as exc:
    print("  枚举会话失败:", exc)

KNOWN = ["1670681411", "994959351", "1913447173", "728260403", "1070754640",
         "2206929446", "2259606745"]
for qq in KNOWN:
    sessions.add("default:FriendMessage:%s" % qq)
targets = sorted(sessions)
print("收件人 %d 个:" % len(targets))
for item in targets:
    print("   ", item)

print()
ok = 0
for umo in targets:
    body = json.dumps({"umo": umo, "message": ANNOUNCE}).encode()
    request = urllib.request.Request(
        BASE + "/api/v1/im/messages", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode()
        print("  %-38s -> %s %s" % (umo, response.status, payload[:120]))
        ok += 1
    except urllib.error.HTTPError as exc:
        print("  %-38s -> HTTP %s %s" % (umo, exc.code, exc.read().decode()[:160]))
    except Exception as exc:  # noqa: BLE001
        print("  %-38s -> %s: %s" % (umo, type(exc).__name__, exc))

print()
print("成功 %d / %d" % (ok, len(targets)))
PY
