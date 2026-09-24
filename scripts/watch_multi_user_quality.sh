#!/usr/bin/env bash
# 质量层面：空正文过滤是否生效、有没有人被忽略、每人回复的样子。
set -u

echo "=== 1) 空正文的"消息"有没有混进来（插件应当已经挡掉）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, os, sqlite3

print(f"  {'person':42s} {'真人消息':>8s} {'空正文':>7s} {'回复':>5s} {'未结算':>7s}")
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    real = con.execute(
        "select count(*) from raw_events where event_type='user_message' and content != ''"
    ).fetchone()[0]
    empty = con.execute(
        "select count(*) from raw_events where event_type='user_message' and content = ''"
    ).fetchone()[0]
    replies = con.execute(
        "select count(*) from raw_events where event_type='assistant_message'"
    ).fetchone()[0]
    unresolved = con.execute(
        "select count(*) from event_semantics where semantic_status='unresolved'"
    ).fetchone()[0]
    print(f"  {person:42s} {real:8d} {empty:7d} {replies:5d} {unresolved:7d}")
PY

echo
echo "=== 2) 每人最近 2 组对话（看人格与格式）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, os, sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = con.execute(
        "select created_at, event_type, content from raw_events"
        " where event_type in ('user_message','assistant_message') and content != ''"
        " order by created_at desc limit 4"
    ).fetchall()
    print(f"  --- {person} ---")
    for created_at, kind, content in reversed(rows):
        who = "用户" if kind == "user_message" else "  她"
        text = " ".join(str(content).split())
        print(f"    {str(created_at)[11:19]} {who}: {text[:110]}")
    print()
PY

echo "=== 3) 每人的情绪/记忆/判决 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, os, sqlite3

print(f"  {'person':42s} {'判决':>4s} {'记忆':>4s} {'reappr':>6s} {'matters':>7s} "
      f"{'valence':>8s} {'impulse':>7s} {'restraint':>9s}")
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    def count(table):
        try:
            return con.execute(f"select count(*) from {table}").fetchone()[0]
        except sqlite3.Error:
            return -1
    state = con.execute(
        "select mood_valence, approach_impulse, restraint from runtime_state"
    ).fetchone()
    print(f"  {person:42s} {count('decisions'):4d} {count('memories'):4d} "
          f"{count('reappraisals'):6d} {count('unfinished_matters'):7d} "
          f"{state[0]:8.3f} {state[1]:7.3f} {state[2]:9.3f}")
PY
