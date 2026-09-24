#!/usr/bin/env bash
# 把测试前端改成"显式启用 profile 才启动"，防止裸 compose up 把它带回来
# （它在场会让主动投递全灭 —— 2026-09-17 刚验证过）。
set -u
cd /home/bomomo/astrbot_test
cp astrbot.yml "astrbot.yml.bak-$(date +%Y%m%d-%H%M%S)"

python3 - <<'PY'
from pathlib import Path

path = Path("/home/bomomo/astrbot_test/astrbot.yml")
lines = path.read_text(encoding="utf-8").splitlines()
out: list[str] = []
inserted = False
for line in lines:
    stripped = line.strip()
    if stripped == "frontend:" and not inserted:
        out.append(line)
        out.append("    # 只在显式启用 profile 时启动：这个容器会作为第二个 OneBot 客户端接进")
        out.append("    # 同一个平台，而 AstrBot 的主动发送不带 self_id（见 HANDOFF 的")
        out.append("    # \"主动消息从来没有一条送出去过\" 一节），两个客户端会让它挑不出连接、")
        out.append("    # 主动投递静默全灭。要跑模拟测试时：")
        out.append("    #   docker compose -p astrbot_test -f astrbot.yml --profile legacy-frontend up -d frontend")
        out.append("    profiles: [\"legacy-frontend\"]")
        inserted = True
        continue
    out.append(line)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("inserted:", inserted)
PY

echo
echo "=== 语法与展开校验 ==="
docker compose -p astrbot_test -f astrbot.yml config --services 2>&1
echo "--- 带 profile 时应包含 frontend ---"
docker compose -p astrbot_test -f astrbot.yml --profile legacy-frontend config --services 2>&1

echo
echo "=== 确认服务没被意外改动（还是那几个）==="
docker compose -p astrbot_test -f astrbot.yml config 2>/dev/null | grep -E "^  [a-z-]+:$|container_name" | head -20
