"""Measure how much the 2B model starves a latency-sensitive co-tenant.

On a 1-core VPS the Runtime, AstrBot and (optionally) the model share one CPU.
This simulates AstrBot's message path - a short CPU burst every 200 ms - and
reports the latency inflation while the model is generating, both with and
without ``nice``. That is the number that decides whether co-hosting is viable.

Usage::

    python3 scripts/contention_probe.py <baseline|model|model-nice> <cpu-pin> <port>
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request

BURST_MS = 1.0
PERIOD_MS = 200.0
DURATION_S = 20.0


def burn(milliseconds: float) -> None:
    """Burn CPU for approximately ``milliseconds``."""
    deadline = time.perf_counter() + milliseconds / 1000.0
    total = 0
    while time.perf_counter() < deadline:
        total += 1


def fire(port: int) -> None:
    """Send one long generation request, ignoring the response."""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "请写一段三百字的随笔谈谈雨天。"}],
        "max_tokens": 320,
        "temperature": 0.0,
        "stream": False,
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:  # noqa: S310
            response.read()
    except Exception:  # noqa: BLE001 - the probe only cares about contention
        pass


def main() -> int:
    """Run the contention probe and print percentiles."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 8300

    if mode in {"model", "model-nice"}:
        import threading

        threading.Thread(target=fire, args=(port,), daemon=True).start()
        time.sleep(3.0)

    samples: list[float] = []
    end = time.perf_counter() + DURATION_S
    while time.perf_counter() < end:
        release = time.perf_counter() + PERIOD_MS / 1000.0
        started = time.perf_counter()
        burn(BURST_MS)
        samples.append((time.perf_counter() - started) * 1000.0)
        slack = release - time.perf_counter()
        if slack > 0:
            time.sleep(slack)

    ordered = sorted(samples)
    report = {
        "mode": mode,
        "samples": len(samples),
        "burst_target_ms": BURST_MS,
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 2),
        "p99_ms": round(ordered[int(len(ordered) * 0.99) - 1], 2),
        "max_ms": round(max(ordered), 2),
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
