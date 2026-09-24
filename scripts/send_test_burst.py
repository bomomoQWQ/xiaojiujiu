"""Fire several messages from several people at once against the test frontend.

The single-message path is verified; what has never been exercised is three people
typing in the same second -- per-session serialisation, whether every message
still gets its own reply, and how far the queue pushes latency out.

Requests go out from a thread pool so they really overlap, and every request's
round-trip time is printed (that is the *accept* latency, not the reply latency;
the reply latency is read back from the frontend's own frame log).

Usage::

    python scripts/send_test_burst.py --users 20001,20002,20003 --each 2
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_URL = "http://127.0.0.1:6300"

#: Distinct, natural sentences per person, so a reply that mixes two people up is
#: visible in the text rather than only in the counts.
LINES = [
    "我刚从超市回来，买了点水果。",
    "今天降温了，你那边冷不冷？",
    "我昨天睡得不太好，今天有点困。",
    "我在想要不要换个工作，你觉得呢？",
]

_lock = threading.Lock()


def send(
    url: str,
    user_id: str,
    text: str,
    timeout: float,
    attempts: int = 4,
) -> tuple[str, str, float, str]:
    """POST one message as ``user_id``; return ``(user, text, seconds, outcome)``.

    A connection reset is retried: the frontend's accept queue is the usual victim
    of a simultaneous burst, and a harness that drops input would make the whole
    load test a lie. The number of retries is reported, so a run that only
    succeeded because of them is visible rather than silently clean.
    """
    body = json.dumps({"text": text, "user_id": user_id}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/send",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    started = time.monotonic()
    outcome = "ERR unknown"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            event = payload.get("event") or {}
            outcome = str(event.get("message_id", "ok"))
            if attempt > 1:
                outcome += f" (retry {attempt - 1})"
            break
        except (urllib.error.URLError, ConnectionResetError, OSError) as error:
            outcome = f"ERR {error}"
            if attempt < attempts:
                time.sleep(0.2 * attempt)
    elapsed = time.monotonic() - started
    with _lock:
        print(f"  {user_id} -> {outcome} in {elapsed * 1000:.0f}ms  {text[:24]}")
    return user_id, text, elapsed, outcome


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help=f"frontend root (default: {DEFAULT_URL})")
    parser.add_argument("--users", default="20001,20002,20003", help="comma-separated user ids")
    parser.add_argument("--each", type=int, default=2, help="messages per user")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout")
    args = parser.parse_args()

    users = [item.strip() for item in args.users.split(",") if item.strip()]
    jobs = [
        (user, LINES[(index + offset) % len(LINES)])
        for offset, user in enumerate(users)
        for index in range(args.each)
    ]

    print(f"firing {len(jobs)} messages from {len(users)} people at once")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda job: send(args.url, job[0], job[1], args.timeout), jobs))
    print(f"all accepted in {time.monotonic() - started:.2f}s")
    failures = [item for item in results if not item[3][0].isdigit()]
    retried = [item for item in results if "retry" in item[3]]
    print(f"send failures: {len(failures)}; needed a retry: {len(retried)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
