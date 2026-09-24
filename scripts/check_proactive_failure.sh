#!/usr/bin/env bash
# 主动发送为什么失败：attempt_events 的 reason、outbox 的错误、时间线。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, json, os, sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    attempts = con.execute(
        "select attempt_id, state, created_at, updated_at, intent from action_attempts"
        " order by created_at"
    ).fetchall()
    if not attempts:
        continue
    print(f"  ===== {person} =====")
    for attempt in attempts:
        print(f"    attempt {attempt['attempt_id']} state={attempt['state']} "
              f"created={str(attempt['created_at'])[:19]}")
        print(f"      intent: {str(attempt['intent'])[:70]}")
        for ev in con.execute(
            "select created_at, from_state, to_state, reason from attempt_events"
            " where attempt_id = ? order by created_at",
            (attempt["attempt_id"],),
        ):
            print(f"      {str(ev['created_at'])[11:19]} {ev['from_state']:>14s} -> "
                  f"{ev['to_state']:<14s} {ev['reason']}")
        for row in con.execute(
            "select kind, status, payload_json from outbox where outbox_id in"
            " (select outbox_id from outbox) and json_extract(payload_json,'$.attempt_id') = ?",
            (attempt["attempt_id"],),
        ):
            payload = json.loads(row["payload_json"] or "{}")
            print(f"      outbox {row['kind']} status={row['status']} "
                  f"text={str(payload.get('text') or payload.get('candidate_id') or '')[:60]!r}")
    print()
PY
