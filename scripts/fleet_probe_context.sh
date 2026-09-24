#!/usr/bin/env bash
# Probe one live per-person Runtime from inside the fleet container.
#
# Why this exists: the per-person ports (8787+, 8801) are only on the docker
# network -- they are NOT published to the host, and the container has no DNS,
# so neither `curl http://127.0.0.1:PORT` from the host nor addressing a person
# by container name works. What does work is running a script *inside*
# xxj-runtime-fleet and talking to the container's own IP.
#
# Closes the loop on the observability instrumentation: call POST /v1/context,
# then read the database back and show that `context_rendered` grew. A 200 alone
# proves nothing -- the recorded event is what the export and replay read.
#
# Usage (from the host):  QQ=qq01 bash scripts/fleet_probe_context.sh
# (the QQ travels as an environment variable: passing arguments through ssh plus
#  shell quoting has bitten this project enough times already)
set -u
QQ=${QQ:-qq01}
PORT=${PORT:-}
if [ -z "$PORT" ]; then
  PORT=$(curl -s http://127.0.0.1:8800/fleet/status \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print([p["port"] for p in d["people"] if "'"$QQ"'" in p["person"]][0])')
fi
PERSON="default-friendmessage-$QQ"
SESSION="default:FriendMessage:$QQ"
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)
echo "person=$PERSON ip=$IP port=$PORT"

cat > /tmp/fleet_probe.py <<PY
import json, sqlite3, urllib.request

path = "/data/$PERSON/companion.sqlite3"


def renders():
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(
            "select count(*) from raw_events where content = 'context_rendered'"
        ).fetchone()[0]
    finally:
        con.close()


before = renders()
print("context_rendered before:", before)
req = urllib.request.Request(
    "http://$IP:$PORT/v1/context",
    data=json.dumps({"session": "$SESSION", "trigger": "llm_request"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = resp.read().decode("utf-8")
        data = json.loads(body)
        print("HTTP", resp.status, "chars", len(body), "version", data.get("version"))
        print("sections:", list((data.get("sections") or {}).keys()))
except Exception as exc:
    print("CALL FAILED:", type(exc).__name__, exc)
after = renders()
print("context_rendered after: ", after, "-> delta", after - before)
PY

docker cp /tmp/fleet_probe.py xxj-runtime-fleet:/tmp/fleet_probe.py >/dev/null
docker exec xxj-runtime-fleet python3 /tmp/fleet_probe.py
rm -f /tmp/fleet_probe.py
