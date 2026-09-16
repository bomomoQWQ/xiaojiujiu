#!/usr/bin/env bash
# Is the model answering the deep-refresh prompt, or is the reply being lost?
#
# The live instance reports deep refresh = empty_suggestions with degraded=false,
# which means valid JSON came back that carried none of the six expected fields.
# This asks the configured provider the same question directly and prints the raw
# completion, so "model returns {}" and "we fail to read the model" can be told
# apart. Read-only: it never touches any Runtime's state.
#
# Usage:  bash scripts/fleet_probe_provider.sh
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json, os, urllib.request

key = os.environ.get("CR_SEMANTIC_API_KEY", "")
base = os.environ.get("CR_SEMANTIC_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
model = os.environ.get("CR_SEMANTIC_MODEL", "deepseek-chat")
print("provider:", base, model, "key:", "configured" if key else "MISSING")

system = (
    "\u4f60\u662f\u957f\u671f\u966a\u4f34\u89d2\u8272\u7684\u6df1\u5c42\u8ba4\u77e5\u6574\u7406\u5668\uff0c"
    "\u53ea\u5728\u4f4e\u9891\u7684\u540e\u53f0\u5237\u65b0\u4e2d\u88ab\u8c03\u7528\u3002"
    "\u53ea\u8f93\u51fa\u4e00\u4e2a JSON \u5bf9\u8c61\uff0c\u5b57\u6bb5\u56fa\u5b9a\u4e3a reinterpretations, "
    "psychological_interpretation, candidate_intent_operations, memory_suggestions, "
    "unfinished_matter_suggestions, user_model_evidence_suggestions\u3002"
)
user = json.dumps(
    {
        "unresolved_events": [
            {"event_id": "evt_1", "content": "\u4f60\u5f88\u5173\u5fc3\u6211\u561b\uff08\uff09\u8c03\u8bd5\u597d\u624b\u4e0a\u7684\u4e1c\u897f\u5c31\u7761"},
            {"event_id": "evt_2", "content": "\u540c qq \u6635\u79f0"},
        ],
        "mood": {"valence": 0.0, "arousal": 0.0},
        "situation": [],
        "active_emotions": [],
        "memories": [],
        "unfinished": [],
        "user_model_summary": "",
        "candidates": [],
        "key_quotes": [],
    },
    ensure_ascii=False,
)

body = {
    "model": model,
    "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ],
    "temperature": 0,
    "max_tokens": 1024,
}
req = urllib.request.Request(
    base + "/chat/completions",
    data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    text = payload["choices"][0]["message"]["content"]
    print("--- raw completion (first 800 chars) ---")
    print(text[:800])
    print("--- finish_reason:", payload["choices"][0].get("finish_reason"))
    print("--- usage:", payload.get("usage"))
except Exception as exc:
    print("CALL FAILED:", type(exc).__name__, exc)
PY
