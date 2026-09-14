"""Print one raw llama.cpp completion so the output shape can be inspected."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path("/mnt/f/理解痞老板")
sys.path.insert(0, str(ROOT / "training" / "src"))

from qboss_training.sft.format import build_messages  # noqa: E402


def main() -> int:
    """Send one request for each task and print the raw text."""
    path = Path("/mnt/e/companion_runtime_backup/generated/splits_full/test.jsonl")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seen: set[str] = set()
    for record in records:
        task = record["task"]
        if task in seen:
            continue
        seen.add(task)
        messages = build_messages(record)[:2]
        grammar_name = "appraisal.gbnf" if task == "event_eval" else "explanation.gbnf"
        grammar = (ROOT / "runtime" / "grammars" / grammar_name).read_text(encoding="utf-8")
        body = {
            "model": "m",
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 320,
            "grammar": grammar,
        }
        request = urllib.request.Request(
            "http://127.0.0.1:8080/v1/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"]
        print(f"=== {task} ===")
        print(repr(text[:400]))
        print("finish:", payload["choices"][0].get("finish_reason"), "usage:", payload.get("usage"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
