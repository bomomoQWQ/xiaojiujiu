#!/usr/bin/env bash
# Sentence-by-sentence bisect of the shipped deep-refresh system prompt.
#
# Facts so far: with the shipped prompt the model deterministically returns
# completion_tokens=57 of six empty collections (finish_reason=stop, so it is a
# choice, not a truncation), while dropping the last sentence yields ~2300 chars of
# real reinterpretations from the same payload. This adds the sentences back one at
# a time and also prints the exact prompt text, so the culprit is named rather than
# guessed at.
#
# Usage:  bash scripts/fleet_probe_prompt_bisect.sh
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, os, sys, urllib.request

sys.path.insert(0, "/app/runtime/src")
from companion_runtime.config import load_config
from companion_runtime.deep_refresh import build_request
from companion_runtime.runtime import Runtime

cfg = load_config()
cfg.conversation_id = "default:FriendMessage:1670681411"
cfg.storage.database_path = "/data/default-friendmessage-1670681411/companion.sqlite3"
rt = Runtime(cfg)
provider = rt.semantic_provider
payload = build_request(runtime=rt, limit=cfg.semantic.max_operations_per_refresh).to_dict()
wire = json.dumps(payload, ensure_ascii=False, sort_keys=True)

S1 = "\u4f60\u662f\u957f\u671f\u966a\u4f34\u89d2\u8272\u7684\u6df1\u5c42\u8ba4\u77e5\u6574\u7406\u5668\uff0c\u53ea\u5728\u4f4e\u9891\u7684\u540e\u53f0\u5237\u65b0\u4e2d\u88ab\u8c03\u7528\u3002"
S2 = ("\u53ea\u8f93\u51fa\u4e00\u4e2a JSON \u5bf9\u8c61\uff0c\u5b57\u6bb5\u56fa\u5b9a\u4e3a reinterpretations, "
      "psychological_interpretation, candidate_intent_operations, memory_suggestions, "
      "unfinished_matter_suggestions, user_model_evidence_suggestions\u3002")
S3 = "\u524d\u4e94\u4e2a\u4e2d\u9664 psychological_interpretation \u662f\u5bf9\u8c61\u5916\uff0c\u5176\u4f59\u90fd\u662f\u6570\u7ec4\u3002"
S4 = ("\u4f60\u53ea\u63d0\u4f9b\u5efa\u8bae\uff0c\u4e0d\u51b3\u5b9a\u4efb\u4f55\u72b6\u6001\u53d8\u66f4\uff0c"
      "\u4e0d\u751f\u6210\u53f0\u8bcd\uff0c\u4e0d\u521b\u9020\u8f93\u5165\u4e2d\u4e0d\u5b58\u5728\u7684\u4e8b\u4ef6\uff1b")
S5 = "\u8bc1\u636e\u4e0d\u8db3\u65f6\u8fd4\u56de\u7a7a\u6570\u7ec4\u6216\u7a7a\u5bf9\u8c61\uff0c\u4e0d\u8981\u731c\u6d4b\u3002"

variants = {
    "S1+S2 (no caution)": S1 + S2,
    "S1+S2+S3": S1 + S2 + S3,
    "S1+S2+S4": S1 + S2 + S4,
    "S1+S2+S5": S1 + S2 + S5,
    "S1+S2+S3+S4 (shipped minus caution)": S1 + S2 + S3 + S4,
    "shipped (S1..S5)": S1 + S2 + S3 + S4 + S5,
}


def call(label, system):
    body = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": wire},
        ],
        "temperature": 0,
        "max_tokens": 4096,
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
    usage = answer.get("usage") or {}
    print(f"[{label}] prompt_chars={len(system)} reply={len(text)} "
          f"completion_tokens={usage.get('completion_tokens')} finish={choice.get('finish_reason')}")


for name, prompt in variants.items():
    call(name, prompt)
PY
