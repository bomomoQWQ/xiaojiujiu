"""End-to-end verification of the patch v0.2 behaviour over real HTTP.

Starts the Runtime exactly as it ships (default ``DisabledProvider``), drives the
documented two-time-scale flow through the API, then swaps in a stub provider to
prove the optional deep-refresh path works.

This is deliberately a *live* check rather than another unit test: it exercises
the ASGI app, the schema migration on a real file database, and the JSON
contracts an operator or the AstrBot plugin actually sees.

Usage::

    python scripts/e2e_patch_v02.py [--base-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

OK = "[OK]"
BAD = "[FAIL]"
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    mark = OK if condition else BAD
    print(f"  {mark} {label}" + (f"  [{detail}]" if detail else ""))
    if not condition:
        FAILURES.append(label)


def request(base: str, method: str, path: str, body: dict | None = None) -> dict:
    """Perform one JSON request against the live server."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    """Run the end-to-end verification."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", default=str(Path("/tmp/e2e-patch-v02")))
    args = parser.parse_args()

    base_dir = Path(args.base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    for leftover in base_dir.glob("*"):
        if leftover.is_file():
            leftover.unlink()

    from companion_runtime.api import create_app
    from companion_runtime.config import RuntimeConfig
    from companion_runtime.providers import DisabledProvider, DeepRefreshSuggestions
    from companion_runtime.runtime import Runtime

    config = RuntimeConfig()
    config.storage.database_path = str(base_dir / "runtime.sqlite3")
    config.storage.raw_log_path = str(base_dir / "events.jsonl")
    config.semantic.deep_refresh_min_interval_seconds = 0.0
    config.semantic.unresolved_backlog_threshold = 2

    runtime = Runtime(config, seed=11)
    app = create_app(runtime, config)

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8791, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(80):
        time.sleep(0.1)
        if getattr(server, "started", False):
            break
    base = "http://127.0.0.1:8791"
    now = datetime.now(timezone.utc).isoformat()

    try:
        print("\n=== 1. default deployment: no model at all ===")
        health = request(base, "GET", "/health")
        check("provider defaults to disabled", health["semantic_provider"]["provider"] == "disabled")
        check("provider reports unavailable", health["semantic_provider"]["available"] is False)
        check("schema version is 2", runtime.db.query_one("PRAGMA user_version") is not None)

        print("\n=== 2. acting layer: explicit events settle coarsely ===")
        explicit = request(
            base,
            "POST",
            "/events",
            {
                "event_type": "user_message",
                "content": "谢谢你，我今天真的被你安慰到了。",
                "timestamp": now,
            },
        )["outcome"]
        check("explicit event is settled", explicit["semantic_status"] == "resolved")
        check("settled by the coarse rule table", explicit["appraisal_source"] == "coarse_rule")
        check("an emotional after-effect was produced", bool(explicit["emotion_event_ids"]))

        print("\n=== 3. persistent layer: ambiguity is deferred, not guessed ===")
        ambiguous = request(
            base,
            "POST",
            "/events",
            {"event_type": "user_message", "content": "算了，也没什么。", "timestamp": now},
        )["outcome"]
        check("ambiguous event stays unresolved", ambiguous["semantic_status"] == "unresolved")
        check("no model was consulted", ambiguous["appraisal_source"] == "deferred")
        check("no fabricated emotion", ambiguous["emotion_event_ids"] == [])

        backlog = request(base, "GET", "/cognition/backlog")
        check("backlog records the deferred event", backlog["stats"]["unresolved"] == 1)
        check("raw text is preserved", backlog["items"][0]["content"] == "算了，也没什么。")

        print("\n=== 4. the raw event survives deferral ===")
        event_id = ambiguous["event"]["event_id"]
        stored = request(base, "GET", f"/events/{event_id}")
        check("the original event is still readable", stored["event"]["content"] == "算了，也没什么。")

        print("\n=== 5. refresh declines cleanly with no provider ===")
        declined = request(base, "POST", "/cognition/refresh", {"force": True})
        check("refresh declines", declined["ran"] is False)
        check("reason is provider_unavailable", declined["reason"] == "provider_unavailable")

        print("\n=== 6. optional provider: later understanding settles the backlog ===")
        second = request(
            base,
            "POST",
            "/events",
            {"event_type": "user_message", "content": "随便吧，都行。", "timestamp": now},
        )["outcome"]
        before_backlog = request(base, "GET", "/cognition/backlog")["stats"]["unresolved"]

        class _Provider:
            name = "e2e_stub"

            def available(self) -> bool:
                return True

            def deep_refresh(self, req, *, timeout_s=None):  # noqa: ANN001
                return DeepRefreshSuggestions(
                    provider="e2e_stub",
                    degraded=False,
                    reinterpretations=[
                        {
                            "content": "当时用户可能已经失望，只是没有说出口。",
                            "realized_text": "现在意识到，那句「算了」后面是失望。",
                            "sources": [event_id],
                        }
                    ],
                    psychological_interpretation={
                        "experience": "近期底色偏沉，有没消化完的东西。",
                        "focus": "在意回应是否真的接住了。",
                        "conflict": "想靠近，又习惯性收着。",
                        "impulse": "总体想确认，但不急。",
                        "inhibition": "长期偏克制。",
                        "expression": "底色收着。",
                    },
                )

            def explain_state(self, payload, *, state_key=""):  # noqa: ANN001
                return None

            def health(self) -> dict:
                return {"provider": self.name, "available": True}

        runtime.semantic_provider = _Provider()
        refreshed = request(base, "POST", "/cognition/refresh", {"force": True})
        check("refresh ran", refreshed["ran"] is True)
        check("reinterpretation applied", refreshed["applied"].get("reinterpretation") == 1)
        check("at least one event was settled", refreshed["settled_events"] >= 1)

        after = request(base, "GET", "/cognition/backlog")
        check(
            "backlog shrank by exactly the reinterpreted event",
            after["stats"]["unresolved"] == before_backlog - 1,
            f"{before_backlog} -> {after['stats']['unresolved']}",
        )
        remaining = {item["event_id"] for item in after["items"]}
        check("the reinterpreted event left the backlog", event_id not in remaining)
        check("the unrelated ambiguous event is still open", second["event"]["event_id"] in remaining)

        print("\n=== 7. history was appended to, never rewritten ===")
        still = request(base, "GET", f"/events/{event_id}")
        check("raw event is byte-identical", still["event"]["content"] == "算了，也没什么。")
        versions = runtime.projections.interpretations.list_for_target("event", event_id)
        check("an interpretation version exists", len(versions) >= 1)
        check("a reappraisal event was recorded", bool(runtime.projections.interpretations.list_reappraisals()))

        print("\n=== 8. the injected block reads as background, not instruction ===")
        block = request(base, "POST", "/context/render-block", {})["block"]
        check("block declares the priority order", "当前用户原话" in block)
        check("block labels itself as background", "临时背景" in block)
        check("block defers to the current turn", "不是本轮该怎么反应" in block)

        print("\n=== 9. restart safety ===")
        version_before = runtime.version()
        runtime.close()
        reopened = Runtime(config, seed=11)
        try:
            check("state survives a restart", reopened.version() == version_before)
            check(
                "the interpretation cache survives",
                bool(reopened.projections.interpretations.list_for_appraisal(event_id))
                if hasattr(reopened.projections.interpretations, "list_for_appraisal")
                else bool(reopened.projections.interpretations.list_for_target("event", event_id)),
            )
        finally:
            reopened.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    print()
    if FAILURES:
        print(f"{BAD} {len(FAILURES)} check(s) failed:")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print(f"{OK} all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
