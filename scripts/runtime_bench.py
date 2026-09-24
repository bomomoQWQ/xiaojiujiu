"""Measure the Runtime sidecar's own cost on a weak single-core VPS.

The fine-tuned model is optional; the Runtime is not. On a 1-core VPS the
critical path (ingest a message, tick, decide) must still finish in tens of
milliseconds, so this script measures exactly that:

* ``lazy_tick`` after 1 hour and after 30 days of simulated absence,
* the full ``process_user_message`` foreground path,
* resident memory of the process,
* the cost of the SQLite durability guarantees (WAL commit per round).

Everything runs with the local model disabled, which is the Level 0 deployment.

Usage::

    python scripts/runtime_bench.py --rounds 200
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

from companion_runtime.config import RuntimeConfig  # noqa: E402
from companion_runtime.db import Database  # noqa: E402
from companion_runtime.runtime import Runtime  # noqa: E402

BASE = datetime.fromisoformat("2026-03-01T09:00:00+00:00")

MESSAGES = [
    "在吗",
    "今天面试没过，有点难受。",
    "谢谢你一直陪着我。",
    "这几天工作很多，我可能回得慢",
    "算了，也没什么",
    "明天下午面试，结束告诉你结果。",
    "我今晚想自己待着",
    "你上次说的那本书我看完了",
]


def percentile(values: list[float], fraction: float) -> float:
    """Return the given percentile of ``values`` in milliseconds."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))
    return ordered[index] * 1000.0


def main() -> int:
    """Run the benchmark and print a JSON report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--db", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    db_path = args.db or str(Path(tempfile.gettempdir()) / "runtime-bench.sqlite3")
    if Path(db_path).exists():
        Path(db_path).unlink()

    config = RuntimeConfig()
    config.storage.database_path = db_path
    config.storage.mirror_raw_events = False
    runtime = Runtime(config, seed=11, created_at=BASE)

    ingress: list[float] = []
    ticks: list[float] = []
    ancient: list[float] = []

    try:
        for index in range(args.rounds):
            stamp = BASE + timedelta(minutes=index * 7)
            started = time.perf_counter()
            runtime.process_user_message(content=MESSAGES[index % len(MESSAGES)], timestamp=stamp)
            ingress.append(time.perf_counter() - started)

            started = time.perf_counter()
            runtime.lazy_tick(stamp + timedelta(hours=1))
            ticks.append(time.perf_counter() - started)

        # A cold start after a long outage is the worst realistic tick.
        started = time.perf_counter()
        runtime.lazy_tick(BASE + timedelta(days=30))
        ancient.append(time.perf_counter() - started)

        version = runtime.version()
        attempts = runtime.projections.attempts.count_in_flight()
    finally:
        runtime.close()

    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "rounds": args.rounds,
        "db_bytes": Path(db_path).stat().st_size if Path(db_path).exists() else 0,
        "state_version": version,
        "in_flight_attempts": attempts,
        "process_peak_rss_mib": round(peak_kib / 1024.0, 1),
        "cpu_seconds": round(time.process_time(), 2),
        "wall_seconds": round(sum(ingress) + sum(ticks) + sum(ancient), 3),
        "ingress_ms": {
            "mean": round(statistics.fmean(ingress) * 1000.0, 3),
            "p50": round(percentile(ingress, 0.50), 3),
            "p95": round(percentile(ingress, 0.95), 3),
            "max": round(max(ingress) * 1000.0, 3),
        },
        "lazy_tick_1h_ms": {
            "mean": round(statistics.fmean(ticks) * 1000.0, 3),
            "p50": round(percentile(ticks, 0.50), 3),
            "p95": round(percentile(ticks, 0.95), 3),
            "max": round(max(ticks) * 1000.0, 3),
        },
        "lazy_tick_30d_ms": round(ancient[0] * 1000.0, 3),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\ncpu budget check: {report['cpu_seconds']}s CPU for {args.rounds} full rounds", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
