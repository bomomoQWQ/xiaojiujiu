#!/usr/bin/env bash
# 端到端验收：导出 → 回放 → 日报，全部用真人实例的数据。
set -u
SRC=/home/bomomo/astrbot_test/src/xiaojiujiu/scripts
BETA=/mnt/xz/xiaojiujiu-beta

echo "=== git ==="
cd /home/bomomo/astrbot_test/src/xiaojiujiu && git pull --ff-only 2>&1 | tail -1

echo "=== 1) 导出 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data:ro \
  -v "$BETA":/export -v "$SRC":/scripts:ro \
  python:3.12-slim python /scripts/export_beta_data.py --note "after refresh-ledger deploy" 2>&1 | tail -4

RUN=$(find "$BETA" -mindepth 1 -maxdepth 1 -type d -name '20*-*-*_*' | sort | tail -1)
echo "latest run: $RUN"
echo "=== summary.json（真人） ==="
cat "$RUN/people/default-friendmessage-qq01/summary.json"
echo
echo "=== 2) 回放（真人，尾部） ==="
python3 "$SRC/replay_session.py" --export "$RUN/people/default-friendmessage-qq01" 2>&1 | tail -16
echo
echo "=== 3) 日报（2026-09-16 UTC） ==="
python3 "$SRC/beta_daily_report.py" --export "$BETA" --run "$RUN" --date 2026-09-16 \
  --fleet http://127.0.0.1:8800 --out "$BETA/reports" 2>&1 | sed -n '1,20p'
