#!/usr/bin/env bash
# 收尾体检：容器、fleet、镜像、定时任务、磁盘。
set -u
echo "=== 测试栈容器 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-|astrbot-test' || true

echo
echo "=== fleet 健康 ==="
curl -s http://127.0.0.1:8800/fleet/status -o /tmp/fs.json
python3 - <<'PY'
import json

data = json.load(open("/tmp/fs.json", encoding="utf-8"))
people = data.get("people", [])
print("instances:", data.get("count"), "healthy:", sum(1 for p in people if p["health"] == "ok"))
for person in people:
    if person["deep_refresh_attempts"]:
        print(" ", person["person"], "attempts/settled =",
              person["deep_refresh_attempts"], "/", person["deep_refresh_settled"],
              "unresolved =", person["unresolved"], "last =", person["last_refresh_reason"])
PY

echo
echo "=== 镜像一致性 ==="
docker inspect xxj-runtime-fleet xxj-runtime-test --format '{{.Name}} {{.Image}}'

echo
echo "=== 定时任务 ==="
crontab -l

echo
echo "=== 导出盘 ==="
du -sh /mnt/xz/xiaojiujiu-beta
df -h /mnt/xz | tail -1
