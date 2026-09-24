#!/usr/bin/env bash
# Why does the model answer the real deep-refresh request with six empty collections?
#
# Same real payload (10KB, 12 unresolved events) sent four ways, one change each
# time. The goal is to name the ingredient that silences it, because the live
# instance always gets the empty answer and therefore never settles anything.
#
# Read-only: provider calls only.
#
# Usage:  bash scripts/fleet_probe_refresh_ab.sh
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import copy, json, sys

sys.path.insert(0, "/app/runtime/src")
from companion_runtime.config import load_config
from companion_runtime.deep_refresh import build_request
from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT
from companion_runtime.runtime import Runtime

cfg = load_config()
cfg.conversation_id = "default:FriendMessage:qq01"
cfg.storage.database_path = "/data/default-friendmessage-qq01/companion.sqlite3"
rt = Runtime(cfg)
provider = rt.semantic_provider
payload = build_request(runtime=rt, limit=cfg.semantic.max_operations_per_refresh).to_dict()


def ask(label, body):
    wire = json.dumps(body, ensure_ascii=False, sort_keys=True)
    raw = provider._chat(
        DEEP_REFRESH_SYSTEM_PROMPT,
        wire,
        timeout=float(getattr(provider, "deep_timeout_s", 45.0)),
        grammar=None,
        extra_body=None,
    )
    compact = " ".join(raw.split())
    print(f"[{label}] wire={len(wire)} reply={len(raw)}")
    print("   ", compact[:240])


# A: exactly what the live instance sends
ask("A real payload", payload)

# B: drop the empty fact ("user said:" with nothing after it)
b = copy.deepcopy(payload)
b["situation"]["facts"] = [f for f in b["situation"]["facts"] if f.strip() != "用户说："]
ask("B no empty fact", b)

# C: two events only, same shape as the synthetic probe that answered richly
c = copy.deepcopy(payload)
c["unresolved_events"] = c["unresolved_events"][:2]
c["key_quotes"] = c["key_quotes"][:2]
ask("C two events", c)

# D: no candidates, no key quotes, events only -- closest to the hand-written probe
d = copy.deepcopy(payload)
d["candidates"] = []
d["key_quotes"] = []
d["situation"] = {"facts": [], "unfinished": []}
d["unresolved_events"] = d["unresolved_events"][:2]
ask("D minimal", d)
PY
