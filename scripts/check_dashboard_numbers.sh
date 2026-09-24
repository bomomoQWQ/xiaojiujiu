#!/usr/bin/env bash
# 核对看板上几个可能不对劲的数字 + NapCat 的 error 是什么。
set -u

echo "=== NapCat 最近 error 长什么样 ==="
docker logs xxj-napcat-test --since 10m 2>&1 | grep -iE "error" | sed -E 's/\x1b\[[0-9;]*m//g' | tail -8

echo
echo "=== 情绪/解释/未尽之事 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, sqlite3

path = glob.glob("/data/default-friendmessage-qq01/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

print("  == 未结算的 3 条是什么 ==")
for row in con.execute(
    "select s.created_at, substr(e.content,1,40), s.potential_relevance, s.unresolved_reason"
    " from event_semantics s join raw_events e on e.event_id = s.event_id"
    " where s.semantic_status = 'unresolved' order by s.created_at"
):
    print("   ", str(row[0])[:19], "|", row[1], "|", row[2], "|", row[3])

print()
print("  == 情绪路径 ==")
for table in ("active_emotion_events", "emotion_explanations", "reappraisals",
              "interpretation_versions", "memories"):
    print(f"   {table}:", con.execute(f"select count(*) from {table}").fetchone()[0])
print("   state:", con.execute(
    "select mood_valence, mood_arousal, mood_stability, approach_impulse, restraint, pressure, version"
    " from runtime_state").fetchone())

print()
print("  == 10 条未尽之事 ==")
for row in con.execute(
    "select title, status, created_at, due_at from unfinished_matters order by created_at desc limit 12"
):
    print("   ", str(row[2])[:19], "|", row[1], "|", str(row[0])[:50])

print()
print("  == 最近 6 个状态采样（看曲线是否在动）==")
for row in con.execute(
    "select sampled_at, mood_valence, approach_impulse, restraint from state_samples"
    " order by sampled_at desc limit 6"
):
    print("   ", str(row[0])[:19], "valence", row[1], "impulse", round(row[2], 3), "restraint", round(row[3], 3))

print()
print("  == 最近 6 条判决 ==")
for row in con.execute(
    "select decided_at, trigger, acted, reason from decisions order by decided_at desc limit 6"
):
    print("   ", str(row[0])[:19], row[1], "acted=" + str(row[2]), row[3])
PY
