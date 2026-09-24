#!/usr/bin/env bash
# 订正历史账本行：修复前写入的"跳过"行被错记成 degraded=1，把它们改回 0。
#
# 依据：那批行的 payload 里没有 provider_called 字段（新代码才写），且 reason 属于
# 从未调用 provider 的几种（disabled / provider_unavailable / not_needed /
# min_interval_not_elapsed / not_attempted）。改动前先冻结一份，改完核对计数。
set -u
BETA=/mnt/xz/xiaojiujiu-beta
SRC=/home/bomomo/astrbot_test/src/xiaojiujiu/scripts

echo "=== 0) 先冻结 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data -v "$BETA":/export \
  -v "$SRC":/scripts:ro python:3.12-slim \
  python /scripts/snapshot_beta.py --data-root /data --root /export/snapshots \
  --fleet http://192.168.1.15:8800 --note "before degraded backfill" 2>&1 | tail -2

echo
echo "=== 1) 订正（dry-run 计数 → 更新 → 复验）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, os, sqlite3

NEVER_CALLED = (
    "disabled",
    "provider_unavailable",
    "not_needed",
    "min_interval_not_elapsed",
    "not_attempted",
)

paths = sorted(glob.glob("/data/*/companion.sqlite3"))
print(f"  databases: {len(paths)}")
for path in paths:
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(path, timeout=15)
    try:
        con.execute("pragma busy_timeout = 15000")
        marks = ",".join("?" * len(NEVER_CALLED))
        # 只碰"我这次 bug 写出来的"行：reason 表示没调用过 provider，且 payload 里没有
        # provider_called —— 新代码写的行一定带这个字段，所以不会被误改。
        where = (
            f"reason in ({marks}) and degraded = 1 "
            "and json_extract(payload_json, '$.provider_called') is null"
        )
        before = con.execute(f"select count(*) from refresh_runs where {where}", NEVER_CALLED).fetchone()[0]
        total = con.execute("select count(*) from refresh_runs").fetchone()[0]
        degraded_before = con.execute("select coalesce(sum(degraded),0) from refresh_runs").fetchone()[0]
        if before:
            with con:
                con.execute(f"update refresh_runs set degraded = 0 where {where}", NEVER_CALLED)
        degraded_after = con.execute("select coalesce(sum(degraded),0) from refresh_runs").fetchone()[0]
        print(f"  {person}: rows={total} fixed={before} degraded {degraded_before} -> {degraded_after}")
    finally:
        con.close()
PY

echo
echo "=== 2) /health 里的聚合 ==="
PORT=$(curl -s http://127.0.0.1:8800/fleet/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["people"][0]["port"])')
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
docker exec -i xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, sys, urllib.request

port, ip = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=10) as resp:
    d = json.load(resp)
print("  deep_refresh:", d.get("deep_refresh"))
PY
