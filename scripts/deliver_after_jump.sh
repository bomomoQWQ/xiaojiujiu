#!/usr/bin/env bash
# 把跳钟造成"未来排期"的 pending 行拉回挂钟，然后盯完投递。
set -u
QQ=1670681411
DB=/data/default-friendmessage-1670681411/companion.sqlite3

docker exec -i -e QQ="$QQ" xxj-runtime-fleet python3 - "$DB" <<'PY'
import json, sqlite3, sys, time
from datetime import datetime, timezone

path = sys.argv[1]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def pull_back(label):
    """把仍是 pending 且排期在未来的行拉回现在。"""
    con = sqlite3.connect(path, timeout=15)
    con.execute("pragma busy_timeout = 15000")
    con.row_factory = sqlite3.Row
    fixed = []
    for row in con.execute(
        "select outbox_id, kind, status, available_at from outbox"
        " where status = 'pending' and available_at > ?", (now_iso(),)
    ):
        with con:
            con.execute(
                "update outbox set available_at = ? where outbox_id = ?",
                (now_iso(), row["outbox_id"]),
            )
        fixed.append(f"{row['kind']}({row['available_at'][:19]} -> now)")
    con.close()
    if fixed:
        print(f"  [{label}] 拉回排期: {', '.join(fixed)}")
    return fixed


print("=== 1) 拉回 render 行的排期 ===")
pull_back("render")

print()
print("=== 2) 盯 90 秒：渲染完成 → 出现 send 行 → 再拉回 → 投放 ===")
last = None
for _ in range(45):
    pull_back("send" if _ else "render")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select kind, status, attempts, last_error from outbox order by rowid desc limit 2"
    ).fetchall()
    att = con.execute(
        "select state from action_attempts order by created_at desc limit 1"
    ).fetchone()["state"]
    snap = tuple((r["kind"], r["status"], r["attempts"]) for r in rows) + (att,)
    if snap != last:
        print(f"  [{time.strftime('%H:%M:%S')}] attempt={att}")
        for r in rows:
            print(f"      {r['kind']:7s} {r['status']:10s} attempts={r['attempts']} "
                  f"err={str(r['last_error'])[:70]}")
        last = snap
    con.close()
    if rows and rows[0]["kind"] == "send" and rows[0]["status"] in ("delivered", "failed", "rejected"):
        break
    time.sleep(2)

print()
print("=== 3) 事件链与文案 ===")
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
att = con.execute(
    "select attempt_id from action_attempts order by created_at desc limit 1"
).fetchone()["attempt_id"]
for ev in con.execute(
    "select created_at, from_state, to_state, reason from attempt_events"
    " where attempt_id = ? order by created_at", (att,),
):
    print(f"  {str(ev['created_at'])[11:19]} {ev['from_state']:>14s} -> {ev['to_state']:<14s} {ev['reason']}")
print()
for r in con.execute(
    "select kind, status, payload_json from outbox order by rowid desc limit 2"
):
    payload = json.loads(r["payload_json"] or "{}")
    text = " ".join(str(payload.get("text") or "").split())
    print(f"  {r['kind']:7s} {r['status']:10s} {text[:95]!r}")
PY

echo
echo "=== 4) 插件日志 ==="
docker logs astrbot-test --since 4m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -14
