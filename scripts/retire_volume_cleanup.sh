#!/usr/bin/env bash
# 摘掉之后把卷也收干净：
#  1) 13 个已摘人员的目录移到 /data/_retired/（数据不删，但不再被导出/日报扫到）
#  2) 删掉我早期探针在 /data 根留下的两个空壳文件
#  3) 用导出+日报验证：现在只剩真人一个
set -u
BETA=/mnt/xz/xiaojiujiu-beta
SRC=/home/bomomo/astrbot_test/src/xiaojiujiu/scripts
KEEP=default-friendmessage-qq01

echo "=== 1) 归档已摘人员的目录 ==="
docker exec -i xxj-runtime-fleet python3 - "$KEEP" <<'PY'
import os, shutil, sys

keep = sys.argv[1]
root = "/data"
retired = os.path.join(root, "_retired")
os.makedirs(retired, exist_ok=True)
moved = []
for name in sorted(os.listdir(root)):
    path = os.path.join(root, name)
    if not os.path.isdir(path) or name in (keep, "_retired", "logs"):
        continue
    target = os.path.join(retired, name)
    if os.path.exists(target):
        print(f"  {name}: already archived")
        continue
    shutil.move(path, target)
    moved.append(name)
print("  moved:", len(moved))
for name in moved:
    print("   ", name)
PY

echo
echo "=== 2) 删掉 /data 根的两个探针空壳 ==="
docker exec xxj-runtime-fleet /bin/sh -c 'ls -la /data/companion.sqlite3 /data/raw_events.jsonl 2>/dev/null'
docker exec xxj-runtime-fleet /bin/rm -f /data/companion.sqlite3 /data/raw_events.jsonl
echo "  删除后 /data 根："
docker exec xxj-runtime-fleet /bin/ls -la /data/

echo
echo "=== 3) 导出（应当只有真人）==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data:ro \
  -v "$BETA":/export -v "$SRC":/scripts:ro \
  python:3.12-slim python /scripts/export_beta_data.py --note "after deprovision" 2>&1 | tail -5

RUN=$(find "$BETA" -mindepth 1 -maxdepth 1 -type d -name '20*-*-*_*' | sort | tail -1)
echo "latest run: $RUN"
ls "$RUN/people/"

echo
echo "=== 4) 日报 ==="
python3 "$SRC/beta_daily_report.py" --export "$BETA" --run "$RUN" --date 2026-09-17 \
  --fleet http://127.0.0.1:8800 --out "$BETA/reports" 2>&1 | head -20

echo
echo "=== 5) 真人实例仍然健康吗 ==="
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  instances:", d["count"])
for p in d["people"]:
    print("  ", p["person"], p["health"], "events", p["raw_events"],
          "attempts/settled", p["deep_refresh_attempts"], "/", p["deep_refresh_settled"])
'
