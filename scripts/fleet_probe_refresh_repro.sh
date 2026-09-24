#!/usr/bin/env bash
# Deep-refresh request/answer against the real database path, printing both sides.
#
# Two earlier attempts misled themselves: one built a Runtime on the default
# database (0 unresolved rows -> the model correctly answered "nothing"), so the
# path is set explicitly here and the unresolved count is printed before the call.
#
# Usage:  bash scripts/fleet_probe_refresh_repro.sh
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, sys

sys.path.insert(0, "/app/runtime/src")
from companion_runtime.config import load_config
from companion_runtime.deep_refresh import build_request
from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT, parse_deep_refresh
from companion_runtime.runtime import Runtime

cfg = load_config()
cfg.conversation_id = "default:FriendMessage:qq01"
cfg.storage.database_path = "/data/default-friendmessage-qq01/companion.sqlite3"
rt = Runtime(cfg)
provider = rt.semantic_provider
print("db:", cfg.storage.database_path)
print("unresolved in db:", rt.projections.semantics.unresolved_count())

request = build_request(runtime=rt, limit=cfg.semantic.max_operations_per_refresh)
payload = request.to_dict()
items = payload.get("unresolved_events") or []
print("== request ==")
print("  unresolved_events:", len(items))
if items:
    print("  keys of one item:", sorted(items[0].keys()))
    print("  [0]:", json.dumps(items[0], ensure_ascii=False)[:600])
print("  key_quotes:", json.dumps(payload.get("key_quotes"), ensure_ascii=False)[:500])
print("  situation.facts:", json.dumps(payload["situation"]["facts"], ensure_ascii=False)[:300])

wire = json.dumps(payload, ensure_ascii=False, sort_keys=True)
print("  wire chars:", len(wire))

raw = provider._chat(
    DEEP_REFRESH_SYSTEM_PROMPT,
    wire,
    timeout=float(getattr(provider, "deep_timeout_s", 45.0)),
    grammar=None,
    extra_body=None,
)
print("== raw reply length:", len(raw))
print(raw[:900])
suggestions = parse_deep_refresh(provider._parse_raw_json(raw), provider="probe")
print("== parsed: degraded =", suggestions.degraded, "reason =", repr(suggestions.reason))
print("   reinterpretations:", len(suggestions.reinterpretations))
print("   memory_suggestions:", len(suggestions.memory_suggestions))
PY
