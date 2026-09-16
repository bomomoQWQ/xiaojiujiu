"""Read one person's exported week back as a turn-by-turn trace.

This is the view an optimization pass is actually done from, so it puts the three
things that belong together on the same lines:

* what the user said, and what the character answered (from ``events.jsonl``);
* what the character had been *told* before answering -- the injected block, from
  the ``context_rendered`` records (sizes always, text when it was kept);
* what the motivational game did about it -- the verdict, the hazard, and the
  candidates that lost (from ``decisions.jsonl``).

The tail adds the two ledgers a week is judged from: the deep-refresh attempts
(``refresh_runs.jsonl``, declined attempts included) and the reappraisals they
produced, because "the mood curve is flat" is answered by whether anything ever
came back to settle the backlog.

Reads only the export, so it needs neither Docker nor the Runtime to be running::

    python scripts/replay_session.py --export /mnt/xz/xiaojiujiu-beta/<run>/people/<person>
    python scripts/replay_session.py --export <dir> --since 2026-09-17 --limit 200
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, tolerating a missing one."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def short(value: Any, length: int = 68) -> str:
    """Return one printable line for a text field."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


def top_candidate(payload: dict[str, Any]) -> tuple[str, float, float]:
    """Return ``(candidate_id, total, silence)`` for the best candidate of a verdict."""
    outcome = payload.get("outcome") or {}
    utilities = outcome.get("utilities") or []
    best_id, best_total = "", float("-inf")
    for item in utilities:
        total = float(item.get("total") or 0.0)
        if total > best_total:
            best_id, best_total = str(item.get("candidate_id") or ""), total
    return best_id, best_total, float(outcome.get("silence_utility") or 0.0)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", required=True, help="one person's export directory")
    parser.add_argument("--since", default=None, help="ISO date/time lower bound")
    parser.add_argument("--limit", type=int, default=400, help="turns to print at most")
    parser.add_argument("--full", action="store_true", help="print full texts, not excerpts")
    args = parser.parse_args()

    root = Path(args.export)
    summary_path = root / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        print(f"# {summary.get('person')} · {summary.get('user_turns')} user turns · "
              f"{summary.get('decisions')} decisions · {summary.get('context_renders')} renders")
        print(f"# window {summary.get('first_event_at')} .. {summary.get('last_event_at')}")
        print(f"# decision reasons: {json.dumps(summary.get('decision_reasons'), ensure_ascii=False)}")
        print()

    events = read_jsonl(root / "events.jsonl")
    decisions = read_jsonl(root / "decisions.jsonl")
    refreshes = read_jsonl(root / "refresh_runs.jsonl")
    reappraisals = read_jsonl(root / "tables" / "reappraisals.jsonl")
    semantics = read_jsonl(root / "tables" / "event_semantics.jsonl")
    if args.since:
        events = [row for row in events if str(row.get("created_at") or "") >= args.since]
        decisions = [row for row in decisions if str(row.get("decided_at") or "") >= args.since]
        refreshes = [row for row in refreshes if str(row.get("ran_at") or "") >= args.since]

    width = None if args.full else 68
    printed = 0
    for row in events:
        kind = row.get("event_type")
        stamp = str(row.get("created_at") or "")[11:19]
        content = row.get("content")
        if kind in ("user_message", "assistant_message"):
            who = "我 " if kind == "user_message" else "TA "
            print(f"{stamp} {who} {short(content, width or 100000)}")
            printed += 1
        elif content == "context_rendered":
            meta = row.get("metadata_json") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            sections = meta.get("sections") or {}
            sizes = ", ".join(f"{name}:{size}" for name, size in sections.items())
            print(f"{stamp}    └ injected v{meta.get('version')} "
                  f"({meta.get('trigger')}, {meta.get('chars')} chars) {sizes}")
            if args.full and meta.get("text"):
                print("        " + str(meta["text"]).replace("\n", "\n        "))
        elif content == "foreground_pause":
            continue
        elif kind == "system" and content in ("proactive_committed", "proactive_sent", "reconcile:rerender"):
            print(f"{stamp}    └ {content}")
        if printed >= args.limit:
            print(f"... truncated at {args.limit} turns")
            break

    print()
    print(f"# decisions in window: {len(decisions)}")
    for row in decisions[-12:] if not args.full else decisions:
        payload = row.get("payload_json") or {}
        if not isinstance(payload, dict):
            payload = {}
        candidate, total, silence = top_candidate(payload)
        verdict = "ACTED" if row.get("acted") else "silent"
        line = (
            f"  {str(row.get('decided_at'))[11:19]} {verdict:<6} {row.get('reason'):<26} "
            f"hazard={row.get('hazard'):<9} p={row.get('action_probability')} "
            f"best={total:.3f} vs silence={silence:.3f}"
        )
        print(line)
        if row.get("acted"):
            print(f"      chose {row.get('chosen_candidate_id')} "
                  f"({short(payload.get('outcome', {}).get('reason'), 40)})")

    # The deferred-interpretation ledger. A person's mood only moves when an event is
    # settled, so these two blocks answer the question the daily report raises: did
    # anything ever come back for the backlog, and what did it decide it meant?
    unresolved = sum(1 for row in semantics if row.get("semantic_status") == "unresolved")
    print()
    print(f"# deep refreshes: {len(refreshes)} · settled "
          f"{sum(int(row.get('settled_events') or 0) for row in refreshes)} events · "
          f"{unresolved} still unresolved")
    for row in refreshes[-12:] if not args.full else refreshes:
        payload = row.get("payload_json") or {}
        if not isinstance(payload, dict):
            payload = {}
        applied = payload.get("applied") or row.get("applied") or {}
        applied_text = ",".join(f"{k}:{v}" for k, v in applied.items()) or "-"
        print(f"  {str(row.get('ran_at'))[11:19]} ran={int(row.get('ran') or 0)} "
              f"{str(row.get('reason')):<20} trigger={short(row.get('trigger'), 22):<22} "
              f"ops={row.get('operations')} settled={row.get('settled_events')} "
              f"degraded={int(row.get('degraded') or 0)} applied={applied_text} "
              f"{row.get('latency_ms')}ms")
        violations = payload.get("violations") or []
        if violations:
            first = violations[0]
            print(f"      violations={len(violations)} first={first.get('kind')}/"
                  f"{first.get('reason')}")

    if reappraisals:
        print()
        print(f"# reappraisals: {len(reappraisals)}")
        for row in reappraisals[-8:] if not args.full else reappraisals:
            print(f"  {str(row.get('created_at'))[11:19]} {short(row.get('content'), 90)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
