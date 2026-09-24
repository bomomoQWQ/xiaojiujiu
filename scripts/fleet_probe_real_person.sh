#!/usr/bin/env bash
# 真人那个实例最近发生了什么（有人刚聊过？结算/情绪动了没）。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sqlite3

path = "/data/default-friendmessage-qq01/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

print("== 最近 14 条事件 ==")
for row in con.execute(
    "select created_at, event_type, substr(coalesce(content,''),1,34) from raw_events"
    " order by created_at desc limit 14"
):
    print(f"  {str(row[0])[:19]}  {row[1]:18s} {row[2]}")

print()
print("== 语义结算 ==")
for row in con.execute("select semantic_status, count(*) from event_semantics group by 1"):
    print("  ", row)

print("== 刷新账本 ==")
for row in con.execute(
    "select ran_at, trigger, ran, reason, operations, settled_events, degraded from refresh_runs"
    " order by ran_at"
):
    print("  ", row)

print("== 情绪/心情 ==")
print("   active_emotion_events:", con.execute("select count(*) from active_emotion_events").fetchone()[0])
print("   emotion_explanations:", con.execute("select count(*) from emotion_explanations").fetchone()[0])
print("   reappraisals:", con.execute("select count(*) from reappraisals").fetchone()[0])
print("   memories:", con.execute("select count(*) from memories").fetchone()[0])
print("   state:", con.execute(
    "select mood_valence, mood_arousal, approach_impulse, restraint, pressure, version from runtime_state"
).fetchone())

print("== 状态曲线采样 ==")
for row in con.execute("select sampled_at, mood_valence, approach_impulse, restraint from state_samples order by sampled_at"):
    print("  ", row)

print("== 判决 ==")
for row in con.execute("select decided_at, trigger, acted, reason, hazard, advantage, silence_utility from decisions order by decided_at"):
    print("  ", row)
PY
