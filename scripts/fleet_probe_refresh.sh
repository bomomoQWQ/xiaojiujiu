#!/usr/bin/env bash
# Does the existing "look back later" mechanism actually work?
#
# A message the coarse classifier cannot match is stored unresolved and produces
# no emotion event by design; the only way it is ever settled is the deep refresh,
# which is allowed to run when the backlog is big enough or the Runtime has been
# idle long enough. This forces one refresh on the live instance and shows whether
# the backlog shrinks -- i.e. whether the deferred path exists in practice, not
# just in the design notes.
#
# Usage:  QQ=qq01 bash scripts/fleet_probe_refresh.sh
set -u
QQ=${QQ:-qq01}
PORT=$(curl -s http://127.0.0.1:8800/fleet/status \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print([p["port"] for p in d["people"] if "'"$QQ"'" in p["person"]][0])')
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
echo "person=$QQ ip=$IP port=$PORT"

# Ask the Runtime itself to spend: /cognition/refresh with force=true skips the
# pacing guard but nothing else.
curl -s --max-time 180 -XPOST "http://$IP:$PORT/cognition/refresh" \
  -H 'Content-Type: application/json' -d '{"force": true}'
echo

docker exec -i xxj-runtime-fleet python3 - "$QQ" <<'PY'
import sqlite3, sys

qq = sys.argv[1] if len(sys.argv) > 1 else "qq01"
con = sqlite3.connect(f"file:/data/default-friendmessage-{qq}/companion.sqlite3?mode=ro", uri=True)
print("== after refresh ==")
for row in con.execute(
    "select semantic_status, potential_relevance, count(*) from event_semantics group by 1,2"
):
    print("  ", row)
print("  settled rows with a deep_refresh_id:",
      con.execute("select count(*) from event_semantics where deep_refresh_id is not null").fetchone()[0])
print("  emotion events:",
      con.execute("select count(*) from active_emotion_events").fetchone()[0])
print("  mood:", con.execute("select mood_valence, mood_arousal, mood_stability from runtime_state").fetchone())
PY
