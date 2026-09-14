"""Evaluate the fine-tuned 2B model on the CPU through a llama.cpp server.

This is the deployment-shaped evaluation: the same GGUF file, the same CPU, the
same OpenAI-compatible endpoint the Runtime sidecar talks to. It reports the
numbers that actually gate shipping:

* JSON extraction and schema validity (must be 100% - the grammar guarantees it),
* invariant pass rate per task,
* wall-clock latency per request, which is what the Runtime's appraisal deadline
  has to accommodate,
* generation throughput, which determines the server's practical concurrency.

Usage::

    python scripts/cpu_eval.py --input <test.jsonl> --base-url http://127.0.0.1:8080/v1
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training" / "src"))

from qboss_training.sft.format import build_messages  # noqa: E402
from qboss_training.utils.jsonx import extract_json  # noqa: E402
from qboss_training.validators import validate_output  # noqa: E402

GRAMMAR_DIR = ROOT / "runtime" / "grammars"
GRAMMAR_FOR_TASK = {
    "event_eval": "appraisal.gbnf",
    "emotion_explain": "explanation.gbnf",
}


def _post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """POST a JSON body and return the decoded response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _grammar(task: str) -> str | None:
    """Return the GBNF grammar text for a task, if one is bundled."""
    name = GRAMMAR_FOR_TASK.get(task)
    if not name:
        return None
    path = GRAMMAR_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else None


def main() -> int:
    """Run the CPU evaluation and print a report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--limit", type=int, default=0, help="0 means all rows")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-tokens", type=int, default=320)
    parser.add_argument("--out", help="Optional JSON report path")
    args = parser.parse_args()

    records = [
        json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if args.limit:
        records = records[: args.limit]

    per_task: dict[str, dict[str, float]] = {}
    latencies: list[float] = []
    completion_tokens: list[float] = []
    invariant_failures: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []

    for index, record in enumerate(records, start=1):
        task = str(record["task"])
        messages = build_messages(record)[:2]
        body: dict[str, Any] = {
            "model": "qboss-2b",
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": args.max_tokens,
            "stream": False,
            "cache_prompt": True,
            # The adapter was trained with thinking masked out, so the endpoint
            # must render the template's non-thinking branch.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        grammar = _grammar(task)
        if grammar:
            body["grammar"] = grammar

        started = time.perf_counter()
        try:
            response = _post(f"{args.base_url}/chat/completions", body, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"[{index}/{len(records)}] request failed: {exc}", file=sys.stderr)
            continue
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        usage = response.get("usage") or {}
        completion_tokens.append(float(usage.get("completion_tokens") or 0))

        stats = per_task.setdefault(task, {"n": 0.0, "extract": 0.0, "schema": 0.0, "invariant": 0.0})
        stats["n"] += 1

        text = (response.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            payload = extract_json(text)
        except Exception:  # noqa: BLE001
            continue
        stats["extract"] += 1

        violations = validate_output(task, payload, model_input=record.get("input"))
        errors = [item.to_dict() for item in violations if item.severity == "error"]
        schema_errors = [item for item in errors if item["code"] == "SCHEMA"]
        if not schema_errors:
            stats["schema"] += 1
        if not errors:
            stats["invariant"] += 1
        elif index <= 400:
            invariant_failures.append({"id": record.get("id"), "task": task, "errors": errors[:2]})

        if len(samples) < 6:
            samples.append({"id": record.get("id"), "task": task, "output": payload, "latency_s": round(elapsed, 2)})

    def rate(stats: dict[str, float], key: str) -> float:
        return stats[key] / stats["n"] if stats["n"] else 0.0

    report = {
        "records": len(records),
        "completed": len(latencies),
        "latency_s": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else None,
            "p50": round(statistics.median(latencies), 3) if latencies else None,
            "p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 3) if latencies else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
        "completion_tokens_mean": round(statistics.fmean(completion_tokens), 1) if completion_tokens else None,
        "by_task": {
            task: {
                "n": int(stats["n"]),
                "json_extraction": round(rate(stats, "extract"), 4),
                "schema_valid": round(rate(stats, "schema"), 4),
                "invariants_pass": round(rate(stats, "invariant"), 4),
            }
            for task, stats in sorted(per_task.items())
        },
        "invariant_failures": invariant_failures[:20],
        "samples": samples,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
