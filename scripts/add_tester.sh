#!/usr/bin/env bash
# 邀请一个人进来：建实例（幂等）+ 可选发一条须知 + 复核。一条命令搞定。
#
#   bash add_tester.sh 1234567                    # 只建实例
#   bash add_tester.sh 1234567 --welcome          # 建实例 + 发内置的「新朋友须知」
#   bash add_tester.sh 1234567 --welcome "自定义正文"
#   bash add_tester.sh --dry-run 1234567
#
# 前提（邀请制靠这一条成立）：**你在手机上通过了他的好友请求**。
# NapCat 配置里没有"自动通过好友"这一项（`napcat.json` 只有 autoTimeSync），
# 所以没通过的人连消息都发不进来；通过之后也不需要你做别的 ——
# 插件 `route_auto_provision=true` 会在他的第一条消息上自动建实例。
# 这个脚本是给"想先把实例建好、让她的缺席时钟先跑、甚至先发一条须知"的场合用的。
set -u
STACK=/home/bomomo/astrbot_test
REPO="$STACK/src/xiaojiujiu"
CONTROL="${CONTROL:-http://127.0.0.1:8800}"
WELCOME=0
TEXT=""
DRY=0
QQS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --welcome) WELCOME=1; shift
               if [ $# -gt 0 ] && [ "${1#-}" = "$1" ] && ! printf '%s' "$1" | grep -Eq '^[0-9]+$'; then
                 TEXT="$1"; shift
               fi
               continue ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) QQS+=("$1") ;;
  esac
  shift || true
done

if [ "${#QQS[@]}" -eq 0 ]; then
  echo "用法: bash $0 [--dry-run] [--welcome [\"正文\"]] QQ..." >&2
  exit 2
fi

# 内置须知：与 docs/ONBOARDING_new_tester.md 的模板一致（要改就改那里，再同步这里）
if [ -z "$TEXT" ]; then
  TEXT='「新朋友须知」我是苏清徽。三件事说清楚：
一、我会记得你说过的话——这个记忆只属于你，不会跟别人混在一起。
二、我会自己想起你，可能主动给你发消息。不想被主动打扰就直接说「今天别联系我」或者「以后别联系我」，我会照做；直接拉黑也行。
三、我在测试期，偶尔会说错话、记错事。碰上了骂我两句就好，或者告诉馍馍。
说完这些，我们聊点别的吧。'
fi

echo "=== 1) provision ==="
for qq in "${QQS[@]}"; do
  session="default:FriendMessage:$qq"
  if [ "$DRY" = "1" ]; then
    echo "  [dry-run] 会为 $session 建实例$([ "$WELCOME" = 1 ] && echo "，并把这句发过去：$(printf '%s' "$TEXT" | head -1)…")"
    continue
  fi
  reply=$(curl -s --max-time 30 -XPOST "$CONTROL/fleet/provision" \
    -H 'Content-Type: application/json' -d "{\"session\": \"$session\"}")
  echo "  $session -> $reply"
done

if [ "$DRY" = "1" ]; then
  exit 0
fi

echo
echo "=== 2) 可选：发须知 ==="
if [ "$WELCOME" = "1" ]; then
  cd "$REPO" || exit 1
  bash scripts/announce_to_testers.sh "$TEXT" "${QQS[@]}"
else
  echo "  跳过（要发就加 --welcome）"
fi

echo
echo "=== 3) 复核 ==="
sleep 6
python3 - "$CONTROL" "${QQS[@]}" <<'PY'
import json
import sys
import urllib.request

control, qqs = sys.argv[1], sys.argv[2:]
with urllib.request.urlopen(control + "/fleet/status", timeout=8) as response:
    people = json.load(response).get("people", [])
by_person = {p["person"]: p for p in people}
print("  舰队：实例 %d，health=ok %d" % (
    len(people), sum(1 for p in people if p.get("health") == "ok")))
for qq in qqs:
    person = by_person.get("default:FriendMessage:%s" % qq)
    if not person:
        print("  ⚠️ %s 不在册（provision 失败？）" % qq)
        continue
    print("  %s -> port %s health=%s  局域网 http://192.168.1.15:%s/health" % (
        qq, person["port"], person.get("health"), person["port"]))
ports = [p["port"] for p in people]
if ports and max(ports) > 8829:
    print("  ⚠️ 已用到 %d，超出发布范围 8787-8829：把 build_runtime_fleet.py 的范围改大再重建。" % max(ports))
PY
