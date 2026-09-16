#!/usr/bin/env bash
# Find a rewriting of the guilty sentence that keeps the no-fabrication guardrail
# without collapsing the answer.
#
# Bisect result: the shipped prompt's final sentence ("answer with empty arrays when
# the evidence is insufficient, do not guess") is the single ingredient that turns a
# ~2000-char answer into 57 tokens of empty collections. The guardrail's purpose is
# "do not invent events that are not in the input"; the model reads the sentence as
# "if the intent is not explicit, say nothing", which is the opposite of what a
# reinterpretation pass is for.
#
# Each candidate is the shipped prompt with that sentence replaced. The measurement
# is reply length plus whether the parsed suggestions are non-empty.
#
# Usage:  bash scripts/fleet_probe_prompt_fix.sh
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
payload = build_request(runtime=rt, limit=cfg.semantic.max_operations_per_refresh).to_dict()
wire = json.dumps(payload, ensure_ascii=False, sort_keys=True)

GUILTY = "\u8bc1\u636e\u4e0d\u8db3\u65f6\u8fd4\u56de\u7a7a\u6570\u7ec4\u6216\u7a7a\u5bf9\u8c61\uff0c\u4e0d\u8981\u731c\u6d4b\u3002"

candidates = {
    "as shipped": DEEP_REFRESH_SYSTEM_PROMPT,
    "no sentence": DEEP_REFRESH_SYSTEM_PROMPT.replace(GUILTY, ""),
    "fabricate-only":
        DEEP_REFRESH_SYSTEM_PROMPT.replace(
            GUILTY,
            "\u4e0d\u8981\u7f16\u9020\u8f93\u5165\u4e2d\u6ca1\u6709\u7684\u4e8b\u4ef6\u3001"
            "\u4f1a\u8bdd\u6216\u65f6\u95f4\u6233\u3002",
        ),
    "infer-but-mark":
        DEEP_REFRESH_SYSTEM_PROMPT.replace(
            GUILTY,
            "\u53ef\u4ee5\u57fa\u4e8e\u8f93\u5165\u63a8\u65ad\u610f\u56fe\u4e0e\u60c5\u7eea\uff0c"
            "\u4f46\u4e0d\u8981\u7f16\u9020\u8f93\u5165\u4e2d\u6ca1\u6709\u7684\u4e8b\u4ef6\uff1b"
            "\u786e\u5b9e\u65e0\u6cd5\u5224\u65ad\u65f6\u624d\u7559\u7a7a\u3002",
        ),
    "confidence-wording":
        DEEP_REFRESH_SYSTEM_PROMPT.replace(
            GUILTY,
            "\u7ed9\u51fa\u4f60\u7684\u63a8\u65ad\u5e76\u6807\u6ce8 confidence\uff0c"
            "\u4e0d\u8981\u7f16\u9020\u8f93\u5165\u4e2d\u4e0d\u5b58\u5728\u7684\u4e8b\u4ef6\u3002",
        ),
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
    try:
        parsed = parse_deep_refresh(json.loads(text), provider="probe")
        summary = (
            f"reinterpretations={len(parsed.reinterpretations)} "
            f"memories={len(parsed.memory_suggestions)} "
            f"candidates={len(parsed.candidate_intent_operations)} "
            f"unfinished={len(parsed.unfinished_matter_suggestions)} "
            f"user_model={len(parsed.user_model_evidence_suggestions)} "
            f"psych={len(parsed.psychological_interpretation)}"
        )
    except Exception as exc:
        summary = f"PARSE FAILED {type(exc).__name__}"
    print(f"[{label}] reply={len(text)} completion_tokens={usage.get('completion_tokens')} "
          f"finish={choice.get('finish_reason')} | {summary}")


for name, prompt in candidates.items():
    call(name, prompt)
PY
