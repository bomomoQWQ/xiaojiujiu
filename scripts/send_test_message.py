"""Drive the OneBot test frontend with real messages, encoded as UTF-8.

The test stack in ``HANDOFF.md`` exposes a front-end control surface
(``POST /send``, ``GET /transcript``, ``GET /state``, ``GET /frames``) that feeds
a real AstrBot instance. This script is the smallest reliable way to type into
it.

It exists because the obvious route is wrong: PowerShell's
``Invoke-RestMethod -Body <string>`` sends the body in the console's code page, so
every Chinese character arrives as ``?`` -- and AstrBot's own ``core.event_bus``
log line shows the damage, which means it happens *before* AstrBot, not in the
Runtime. Encoding the JSON explicitly as UTF-8 bytes fixes it.

Usage::

    python scripts/send_test_message.py --url http://192.168.1.15:6300 \\
        "你好，我叫小林" "我每周三晚上要去上吉他课，别记错了。"

    # Read back what the bot answered
    python scripts/send_test_message.py --transcript
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:6300"


def _post(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
    """POST one JSON body as UTF-8 bytes and return the decoded response."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url: str, timeout: float) -> str:
    """GET a URL and return its decoded body."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("text", nargs="*", help="messages to send, in order")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"frontend root (default: {DEFAULT_URL})")
    parser.add_argument(
        "--user-id",
        default=None,
        help="send as this OneBot user id (the frontend fixes one at start-up; "
        "passing another simulates a second person)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=20.0,
        help="seconds to wait after each message so the bot can finish replying",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout")
    parser.add_argument("--transcript", action="store_true", help="print the transcript and exit")
    args = parser.parse_args()

    if args.transcript or not args.text:
        print(_get(f"{args.url}/transcript", args.timeout))
        return 0

    for index, text in enumerate(args.text):
        payload: dict[str, object] = {"text": text}
        if args.user_id is not None:
            payload["user_id"] = str(args.user_id)
        try:
            response = _post(f"{args.url}/send", payload, args.timeout)
        except urllib.error.URLError as error:
            print(f"send failed: {error}")
            return 1
        event = response.get("event")
        message_id = event.get("message_id") if isinstance(event, dict) else "?"
        print(f"sent id={message_id}: {text}")
        if index + 1 < len(args.text):
            time.sleep(args.gap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
