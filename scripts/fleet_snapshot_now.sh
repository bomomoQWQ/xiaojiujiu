#!/usr/bin/env bash
set -u
curl -s http://127.0.0.1:8800/fleet/status -o /tmp/fs.json
python3 - <<'PY'
import json

d = json.load(open("/tmp/fs.json", encoding="utf-8"))
people = d.get("people", [])
print("instances:", d.get("count"), "healthy:", sum(1 for p in people if p["health"] == "ok"))
for p in people:
    print(f"  {p['person']:42s} {p['health']:4s} events={p['raw_events']} "
          f"unres={p['unresolved']} matters={p['open_unfinished']} "
          f"refresh={p['deep_refresh_attempts']}/{p['deep_refresh_settled']} "
          f"degraded={p['deep_refresh_degraded']}")
PY
