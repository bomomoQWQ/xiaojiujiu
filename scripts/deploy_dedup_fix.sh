#!/usr/bin/env bash
# 部署 dedup 修复到测试 fleet，并直接验证容器里的代码行为（不只是 grep）。
set -u
REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
cd "$REPO"

echo "=== 1) pull ==="
git pull --ff-only 2>&1 | tail -2
git log --oneline -1

echo
echo "=== 2) 构建镜像 ==="
docker build -t xiaojiujiu-runtime:test . 2>&1 | tail -3

echo
echo "=== 3) 重建 fleet ==="
cd /home/bomomo/astrbot_test
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d --force-recreate runtime-fleet 2>&1 | tail -3

echo
echo "=== 4) 等健康 ==="
for i in $(seq 1 24); do
  sleep 5
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/fleet/status || true)
  if [ "$code" = "200" ]; then
    echo "  控制面 200（$((i*5))s）"
    break
  fi
  echo "  t=$((i*5))s status=$code"
done

echo
echo "=== 5) 容器内行为验证（这才是『部署成功』的判据）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sys
sys.path.insert(0, "/app/runtime/src")
from companion_runtime import unfinished as u
from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT as P

cases = [
    ("同话题：奶茶/去糖奶茶 vs 无糖奶茶（应 True）",
     "用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试无糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。", True),
    ("同话题：当个事办 ± 尾句（应 True）",
     "“当个事办”具体指什么尚未明确，可在自然语境下轻量确认，不必专门追问。",
     "“当个事办”具体指什么尚未明确，可在自然语境下轻量确认。", True),
    ("不同话题：奶茶 vs 咖啡（应 False）",
     "用户邀请角色尝试奶茶/去糖奶茶，角色是否接受、后续是否真的会一起喝，尚未有结果。",
     "用户邀请角色尝试咖啡，角色是否接受、后续是否真的会一起喝，尚未有结果。", False),
    ("不同话题：我试试 vs 换个头像（应 False）",
     "用户说“我试试什么（）”，具体想试什么未明，可留待后续自然聊到。",
     "用户说“换个头像”，具体想试什么未明，可留待后续自然聊到。", False),
    ("模板主题：面试 vs 考试（应 False）", "等待面试结果", "等待考试结果", False),
    ("模板主题：面试 vs 面试通知（应 True）", "等待面试结果", "等待面试结果通知", True),
]
bad = 0
for label, a, b, want in cases:
    got = u._same_subject(a, b)
    ok = "OK " if got == want else "!! "
    bad += 0 if got == want else 1
    print("  %s%s -> %s" % (ok, label, got))
print("  阈值: bigram>=%s  ratio<=%s" % (u.SUBJECT_BIGRAM_THRESHOLD, u.SUBJECT_DIFF_RATIO_LIMIT))
print("  提示词边界: %s" % ("有" if "必须等用户回答才能了结" in P else "**缺失**"))
print("  提示词仍是 JSON 形状示例: %s" % ("是" if "\"sources\"" in P else "**否**"))
print("  仍保留'不要编造'护栏: %s" % ("是" if "不要编造输入中" in P else "**否**"))
print("  失败用例数: %d" % bad)
PY

echo
echo "=== 6) 7 个实例的状态 ==="
curl -s --max-time 10 http://127.0.0.1:8800/fleet/status | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('instances') or data.get('people') or []
ok = sum(1 for i in items if (i.get('health') or i.get('status')) == 'ok')
print('  实例 %d，health=ok 的 %d' % (len(items), ok))
for i in items:
    print('   %-30s %s' % (i.get('session', '?'), i.get('health') or i.get('status')))
"
