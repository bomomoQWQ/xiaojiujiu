#!/usr/bin/env bash
# Does a stop sequence cut the deep refresh right after the JSON closes?
#
# max_tokens is a ceiling, not a spend, so a large one is the right default (the API
# allows up to 384K; unset defaults to 8K non-thinking). What a large ceiling must
# not do is let one background call ramble, so this checks that a stop sequence ends
# the generation as soon as the object is complete, and that the reply still parses.
#
# Usage:  bash scripts/fleet_probe_stop_seq.sh
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, os, sys, urllib.request

sys.path.insert(0, "/app/runtime/src")
from companion_runtime.config import load_config
from companion_runtime.deep_refresh import build_request
from companion_runtime.providers import DEEP_REFRESH_SYSTEM_PROMPT, parse_deep_refresh
from companion_runtime.runtime import Runtime

cfg = load_config()
cfg.conversation_id = "default:FriendMessage:1670681411"
cfg.storage.database_path = "/data/default-friendmessage-1670681411/companion.sqlite3"
rt = Runtime(cfg)
provider = rt.semantic_provider
wire = json.dumps(
    build_request(runtime=rt, limit=cfg.semantic.max_operations_per_refresh).to_dict(),
    ensure_ascii=False,
    sort_keys=True,
)
print("configured max_tokens:", provider.max_tokens)


def call(label, **extra):
    body = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": DEEP_REFRESH_SYSTEM_PROMPT},
            {"role": "user", "content": wire},
        ],
        "temperature": 0,
        "max_tokens": provider.max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    body.update(extra)
    req = urllib.request.Request(
        provider.base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ.get("CR_SEMANTIC_API_KEY", ""),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=150) as resp:
        answer = json.loads(resp.read().decode("utf-8"))
    choice = answer["choices"][0]
    text = choice["message"]["content"]
    usage = answer.get("usage") or {}
    try:
        parsed = parse_deep_refresh(json.loads(text), provider="probe")
        ok = (f"parsed: reinterpretations={len(parsed.reinterpretations)} "
              f"memories={len(parsed.memory_suggestions)} degraded={parsed.degraded}")
    except Exception as exc:
        ok = f"PARSE FAILED {type(exc).__name__}: {exc}"
    print(f"[{label}] reply={len(text)} completion_tokens={usage.get('completion_tokens')} "
          f"finish={choice.get('finish_reason')} | {ok}")
    print("    tail:", repr(text[-40:]))


call("no stop")
call("stop brace newline", stop=["\n}\n"])
call("stop two variants", stop=["\n}\n", "\n}\r\n"])
PY
