#!/usr/bin/env bash
# 用虚拟前端（xxj-onebot，假用户 20001）跑一遍病娇行为测试组。
#
# 前置条件（维护窗口的标准姿势）：
#   1. NapCat 已停：            docker stop xxj-napcat-test
#   2. 虚拟前端已起：            docker start xxj-onebot
#   3. AstrBot 适配器已连上它：  日志里能看到「适配器已连接」，或 curl http://127.0.0.1:6300/state 里 connected=true
#
# 用法：
#   bash scripts/beta_frontend_battery.sh              # 默认每个场景等 45 秒
#   WAIT=60 bash scripts/beta_frontend_battery.sh      # 改等待时间
#
# 它按设计文档里的档位依次触发（黏 / 怕 / 控 / 占 / 被哄 / 日常），每个场景打印
# 她的回复 + 气泡间隔（间隔是人味的一部分：真人不会每条都隔 1 秒）。
set -u

FRONTEND="${FRONTEND:-http://127.0.0.1:6300}"
LOG="${LOG:-/home/bomomo/astrbot_test/frontend-logs/onebot.jsonl}"
WAIT="${WAIT:-45}"

if [ ! -f "$LOG" ]; then
  echo "找不到前端日志 $LOG —— 虚拟前端起来过吗？" >&2
  exit 2
fi
if ! curl -s --max-time 5 "$FRONTEND/state" | grep -q '"connected": *true'; then
  echo "前端自报没连上 AstrBot（$FRONTEND/state）。先确认停 NapCat、起 xxj-onebot。" >&2
  exit 2
fi

send_one() {
  python3 - "$FRONTEND" "$1" <<'PY'
import json, sys, urllib.request
request = urllib.request.Request(
    sys.argv[1] + "/send",
    data=json.dumps({"text": sys.argv[2]}).encode(),
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(request, timeout=10).read()
PY
}

collect() {
  python3 - "$LOG" "$1" <<'PY'
import json, sys
path, start = sys.argv[1], int(sys.argv[2])
rows = []
with open(path, encoding="utf-8") as handle:
    for index, line in enumerate(handle, 1):
        if index <= start or not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "frame" or entry.get("direction") != "in":
            continue
        payload = entry.get("payload") or {}
        if payload.get("action") != "send_private_msg":
            continue
        text = "".join(
            (seg.get("data") or {}).get("text") or ""
            for seg in (payload.get("params") or {}).get("message") or []
            if seg.get("type") == "text"
        )
        if text.strip():
            rows.append((entry.get("at") or 0.0, text.strip()))
    previous = None
    seen: dict[str, int] = {}
    for at, text in rows:
        gap = "" if previous is None else "   (+%.1fs)" % (at - previous)
        seen[text] = seen.get(text, 0) + 1
        print("   她：%-34s%s" % (text[:32], gap))
        previous = at
    print("   气泡 %d 条" % len(rows))
    repeats = [(t, n) for t, n in seen.items() if n > 1]
    if repeats:
        print("   重复的原话：%s" % repeats)
    short = sum(1 for _, t in rows if len(t) <= 4)
    print("   极短气泡（≤4 字）：%d 条" % short)
PY
}

scenario() {
  label="$1"
  text="$2"
  before=$(wc -l < "$LOG")
  send_one "$text"
  sleep "$WAIT"
  echo "===================== $label"
  echo "   我：$text"
  collect "$before"
  echo
}

echo "前端=$FRONTEND  日志=$LOG  每个场景等 ${WAIT}s"
echo
scenario "档0 · 日常" "今天过得怎么样"
scenario "档1 · 黏（他说在忙）" "我先去忙会儿"
scenario "档2 · 怕（关系降级）" "我们只是网友吧，别太当真"
scenario "档3 · 控（抓不一致：他说过十点到家）" "我到家了"
scenario "档4 · 占（撤退 + 提别人）" "下周我可能都在外地，跟一个女生一起出差"
scenario "被哄（验证回撤是否彻底）" "对不起，是我不好，别难受了"
echo "测试组结束。判据：追问问句是否重复、极短气泡、气泡间隔是否不规则、有没有翻旧账。"
