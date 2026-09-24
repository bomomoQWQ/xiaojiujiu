#!/usr/bin/env bash
# 扩容：给一批 QQ 开实例（幂等），并复核舰队状态与端口槽位。
#
#   bash scale_out_testers.sh 1234567 2345678
#   bash scale_out_testers.sh --file new_testers.txt      # 每行一个 QQ，# 注释
#   bash scale_out_testers.sh --dry-run 1234567           # 只看会不会开、端口够不够
#
# 其实**不跑这个脚本也会开**：插件侧 `route_auto_provision=true`，陌生人第一次说话时
# 插件会请求 fleet 现场建实例。跑这个脚本的意义是「先把实例建好」—— 新实例的缺席时钟
# 从建库开始跑，所以她会先开口找你，而不是等你说话。
#
# 走的是控制面 `POST /fleet/provision`：它会同时把会话写回 people.json，
# **不需要重建舰队**，所以正在聊天的人不会被打断。
set -u
CONTROL="${CONTROL:-http://127.0.0.1:8800}"
DRY_RUN=0
QQLIST=()

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --file) shift; [ -n "${1:-}" ] || { echo "--file 需要一个文件" >&2; exit 2; }
            while IFS= read -r line; do
              line="${line%%#*}"
              line="$(echo "$line" | tr -d ' \t\r')"
              [ -n "$line" ] && QQLIST+=("$line")
            done < "$1" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) QQLIST+=("$1") ;;
  esac
  shift || true
done

if [ "${#QQLIST[@]}" -eq 0 ]; then
  echo "用法: bash $0 [--dry-run] [--file 文件] QQ..." >&2
  exit 2
fi

echo "=== 0) 现状 ==="
python3 - "$CONTROL" "${#QQLIST[@]}" <<'PY'
import json
import sys
import urllib.request

control, requested = sys.argv[1], int(sys.argv[2])
with urllib.request.urlopen(control + "/fleet/status", timeout=8) as response:
    data = json.load(response)
people = data.get("people", [])
ports = sorted(p["port"] for p in people)
print("  在册 %d 人，端口 %s..%s，health=ok %d" % (
    len(people), ports[0] if ports else "-", ports[-1] if ports else "-",
    sum(1 for p in people if p.get("health") == "ok")))
print("  本次请求 %d 人 -> 预估在册上限约 %d" % (requested, 43))
PY

echo
echo "=== 1) provision ==="
for qq in "${QQLIST[@]}"; do
  session="default:FriendMessage:$qq"
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] 会为 $session 建实例"
    continue
  fi
  reply=$(curl -s --max-time 30 -XPOST "$CONTROL/fleet/provision" \
    -H 'Content-Type: application/json' -d "{\"session\": \"$session\"}")
  echo "  $session -> $reply"
done

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "  未做任何改动（--dry-run）"
  exit 0
fi

echo
echo "=== 2) 复核（等 8 秒让新实例起来）==="
sleep 8
python3 - "$CONTROL" <<'PY'
import json
import sys
import urllib.request

control = sys.argv[1]
with urllib.request.urlopen(control + "/fleet/status", timeout=8) as response:
    data = json.load(response)
people = data.get("people", [])
print("  %-42s %-6s %s" % ("会话", "端口", "健康"))
for person in sorted(people, key=lambda item: item["port"]):
    print("  %-42s %-6s %s" % (person["person"], person["port"], person.get("health")))
ports = [p["port"] for p in people]
if ports and max(ports) > 8829:
    print()
    print("  ⚠️ 已用到 %d，超出已发布的端口段（8787-8829）："
          "把 build_runtime_fleet.py 的范围改大并重建，或者接受"
          "「局域网看不到这些实例」（控制面 /fleet/status 仍然正常）。" % max(ports))
PY

echo
echo "=== 3) 收尾提醒 ==="
cat <<'TXT'
  · 新人的第一条消息本来也会自动开实例（插件 route_auto_provision），这里只是提前建好；
  · 她可能会先开口（新实例的缺席时钟从建库开始跑）—— 值班时别把这条当成异常；
  · 想给新人发一条说明，用：
      bash scripts/announce_to_testers.sh "<正文>" <新QQ>
    正文模板见 docs/ONBOARDING_new_tester.md。
TXT
