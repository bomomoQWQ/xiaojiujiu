#!/usr/bin/env bash
# Would a bigger completion budget fix the deep refresh?
#
# The empty answer is a truncated JSON object: with max_tokens=1024 the model runs
# out mid-object and the runtime's "first { to last }" recovery turns the fragment
# into a valid, all-empty suggestion set. This asks the same request with a larger
# cap and checks that the reply both finishes AND parses into real suggestions.
#
# Usage:  bash scripts/fleet_probe_max_tokens.sh
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, os, sys, urllib.request

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
payload = build_request(runtime=rt, limit=cfg.semantic.max_operations_per_refresh).to_dict()
wire = json.dumps(payload, ensure_ascii=False, sort_keys=True)


def call(label, max_tokens):
    body = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": DEEP_REFRESH_SYSTEM_PROMPT},
            {"role": "user", "content": wire},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        provider.base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ.get("CR_SEMANTIC_API_KEY", ""),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        answer = json.loads(resp.read().decode("utf-8"))
    choice = answer["choices"][0]
    text = choice["message"]["content"]
    print(f"[{label} max_tokens={max_tokens}] reply={len(text)} finish={choice.get('finish_reason')}")
    try:
        parsed = parse_deep_refresh(json.loads(text), provider="probe")
        print("   parsed ok: degraded =", parsed.degraded,
              "reinterpretations =", len(parsed.reinterpretations),
              "memories =", len(parsed.memory_suggestions),
              "candidates =", len(parsed.candidate_intent_operations),
              "unfinished =", len(parsed.unfinished_matter_suggestions),
              "user_model =", len(parsed.user_model_evidence_suggestions),
              "psych =", len(parsed.psychological_interpretation))
    except Exception as exc:
        print("   PARSE FAILED:", type(exc).__name__, exc)
        print("   tail:", text[-120:])
    print("    usage:", answer.get("usage"))


for cap in (1024, 2048, 4096):
    call("shipped prompt", cap)
PY
