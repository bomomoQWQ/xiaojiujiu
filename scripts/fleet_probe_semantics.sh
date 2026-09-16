#!/usr/bin/env bash
# Why is the emotion curve flat? Reads one person's settlement state: the
# unresolved/resolved split by relevance, whether a deep refresh ever wrote back,
# the mood columns, and the emotion-event counts.
#
# Context: with `settle_on_ingest`, a message the coarse classifier cannot match
# is recorded `unresolved(no_explicit_anchor)` and produces NO emotion event, so a
# Runtime whose messages all land there shows a dead-flat mood while the
# conversation itself looks healthy. See the beta section of HANDOFF.md.
#
# Usage:  QQ=1670681411 bash scripts/fleet_probe_semantics.sh
# (Python travels over stdin: `docker cp` into xxj-runtime-fleet currently fails
#  with "Could not find the file /proc/self/fd".)
set -u
QQ=${QQ:-1670681411}
docker exec -i xxj-runtime-fleet python3 - "$QQ" <<'PY'
import json, sqlite3, sys

qq = sys.argv[1] if len(sys.argv) > 1 else "1670681411"
path = f"/data/default-friendmessage-{qq}/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print("== semantic status x relevance ==")
for row in con.execute(
    "select semantic_status, potential_relevance, count(*) from event_semantics group by 1,2"
):
    print("  ", row)
print("== settled rows (if any) ==")
for row in con.execute(
    "select event_id, direction, intensity_band, confidence, settlement_source, settled_at,"
    " deep_refresh_id from event_semantics where semantic_status = 'resolved'"
):
    print("  ", row)
print("== runtime_state ==")
print("  ", con.execute(
    "select mood_valence, mood_arousal, mood_stability, approach_impulse, restraint, pressure,"
    " version from runtime_state limit 1"
).fetchone())
for table in ("active_emotion_events", "emotion_explanations", "interpretation_versions"):
    try:
        print("  " + table + ":", con.execute("select count(*) from " + table).fetchone()[0])
    except Exception as exc:
        print("  " + table + ": (missing)", exc)
print("== runtime_state meta keys ==")
try:
    meta = con.execute("select meta_json from runtime_state limit 1").fetchone()[0]
    data = json.loads(meta) if meta else {}
    for k, v in sorted(data.items()):
        print("   " + k + " = " + str(v)[:90])
except Exception as exc:
    print("  ", exc)
PY
