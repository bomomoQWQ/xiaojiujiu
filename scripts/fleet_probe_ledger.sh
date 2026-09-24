#!/usr/bin/env bash
# 账本落库了吗，三个工具读得出来吗。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

path = "/data/default-friendmessage-qq01/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print("== refresh_runs ==")
for row in con.execute(
    "select ran_at, trigger, ran, reason, provider, degraded, operations, settled_events,"
    " latency_ms from refresh_runs order by ran_at"
):
    print("  ", row)
PY
