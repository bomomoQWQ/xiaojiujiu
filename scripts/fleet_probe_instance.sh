#!/usr/bin/env bash
# Read one live per-person Runtime's database from inside the fleet container.
#
# The per-person ports are docker-network only and the container has no DNS, so
# "just look at that person's Runtime" always comes down to: copy a small
# python file into xxj-runtime-fleet and run it there. This is that, with the
# three things worth seeing: the raw-event histogram, the decisions taken, and
# whether anything was injected.
#
# Usage (from the host):  QQ=1670681411 bash scripts/fleet_probe_instance.sh
# (the QQ travels as an environment variable, not as $1: argument passing through
#  ssh + shell quoting has bitten this project enough times already)
set -u
QQ=${QQ:-1670681411}
echo "person: default-friendmessage-$QQ"

cat > /tmp/fleet_dump.py <<PY
import collections, json, sqlite3

path = "/data/default-friendmessage-$QQ/companion.sqlite3"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print("== raw_events by (type, content) ==")
for t, c, n in con.execute(
    "select event_type, content, count(*) from raw_events group by 1,2 order by 3 desc"
):
    print(f"  {n:4d}  {t:18s} {(c or '')[:36]}")
print("== decisions ==")
rows = con.execute(
    "select decided_at, trigger, reason, acted, hazard, advantage, silence_utility"
    " from decisions order by decided_at"
).fetchall()
for row in rows:
    print("  ", row)
print("  total:", len(rows), "acted:", sum(1 for r in rows if r[3]))
print("== state samples ==")
print("  ", con.execute("select count(*) from state_samples").fetchone()[0])
print("== semantics ==")
print("  ", dict(con.execute("select semantic_status, count(*) from event_semantics group by 1")))
PY

docker cp /tmp/fleet_dump.py xxj-runtime-fleet:/tmp/fleet_dump.py >/dev/null
docker exec xxj-runtime-fleet python3 /tmp/fleet_dump.py
rm -f /tmp/fleet_dump.py
