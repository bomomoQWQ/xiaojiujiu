"""One markdown page per day per person, plus the fleet's totals.

The week is read daily, not at the end: a problem found on day two is worth more
than the same problem found on day eight. Two inputs, both cheap:

* the export directory (``export_beta_data.py``) for history;
* the fleet's ``/fleet/status`` for what is true *now* (provider calls, unresolved
  backlog, restarts) -- those live in the Runtime's memory, not in the database.

Usage::

    python scripts/beta_daily_report.py --export /mnt/xz/xiaojiujiu-beta --date 2026-09-18 \\
        --fleet http://127.0.0.1:8800 --out /mnt/xz/xiaojiujiu-beta/reports
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: A day is "quiet" below this many user turns -- not an error, but worth seeing.
QUIET_TURNS = 3


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, tolerating a missing one."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def fleet_status(url: str) -> dict[str, Any]:
    """Return the fleet's live status, or an empty mapping when unreachable."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/fleet/status", timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {}


def latest_run(export_root: Path) -> Path | None:
    """Return the newest export run under ``export_root``."""
    runs = sorted(path for path in export_root.glob("*/people") if path.is_dir())
    return runs[-1].parent if runs else None


def person_report(people_dir: Path, day: str, live: dict[str, Any]) -> list[str]:
    """Build the markdown body for one day."""
    live_by_person = {str(item.get("person")): item for item in live.get("people", [])}
    lines: list[str] = []
    totals: Counter[str] = Counter()

    for person_dir in sorted(path for path in people_dir.iterdir() if path.is_dir()):
        events = [
            row for row in read_jsonl(person_dir / "events.jsonl")
            if str(row.get("created_at") or "").startswith(day)
        ]
        decisions = [
            row for row in read_jsonl(person_dir / "decisions.jsonl")
            if str(row.get("decided_at") or "").startswith(day)
        ]
        samples = [
            row for row in read_jsonl(person_dir / "state_samples.jsonl")
            if str(row.get("sampled_at") or "").startswith(day)
        ]
        candidates = [
            row for row in read_jsonl(person_dir / "tables" / "memory_candidates.jsonl")
            if str(row.get("created_at") or "").startswith(day)
        ]
        semantics = [
            row for row in read_jsonl(person_dir / "tables" / "event_semantics.jsonl")
            if str(row.get("created_at") or "").startswith(day)
        ]
        user_turns = sum(1 for row in events if row.get("event_type") == "user_message")
        assistant_turns = sum(1 for row in events if row.get("event_type") == "assistant_message")
        renders = sum(1 for row in events if row.get("content") == "context_rendered")
        acted = sum(1 for row in decisions if row.get("acted"))
        unresolved = sum(1 for row in semantics if row.get("semantic_status") == "unresolved")
        reasons = Counter(str(row.get("reason")) for row in decisions)

        totals["user_turns"] += user_turns
        totals["assistant_turns"] += assistant_turns
        totals["decisions"] += len(decisions)
        totals["acted"] += acted

        mood = ""
        if samples:
            first, last = samples[0], samples[-1]
            mood = (
                f"valence {first['mood_valence']:+.3f}→{last['mood_valence']:+.3f}, "
                f"impulse {first['approach_impulse']:.3f}→{last['approach_impulse']:.3f}, "
                f"restraint {first['restraint']:.3f}→{last['restraint']:.3f}"
            )
        live_row = live_by_person.get(f"default-friendmessage-{person_dir.name.split('-')[-1]}", {})
        lines.append(f"### {person_dir.name}")
        lines.append("")
        lines.append(f"- 对话：{user_turns} 提问 / {assistant_turns} 回复，注入渲染 {renders} 次")
        lines.append(f"- 决策：{len(decisions)} 次（主动 {acted} 次）"
                     + (f"，原因分布 {dict(reasons)}" if reasons else ""))
        lines.append(f"- 候选：{len(candidates)} 条新建；语义：{len(semantics)} 条，其中 unresolved {unresolved}")
        if mood:
            lines.append(f"- 状态曲线（{len(samples)} 个采样点）：{mood}")
        if live_row:
            lines.append(
                f"- 实时：events={live_row.get('raw_events')} "
                f"unresolved={live_row.get('unresolved')} "
                f"deep/explain={live_row.get('semantic_calls')}/{live_row.get('explain_calls')} "
                f"restarts={live_row.get('restarts')} health={live_row.get('health')}",
            )
        if user_turns < QUIET_TURNS:
            lines.append(f"- ⚠️ 安静（少于 {QUIET_TURNS} 轮）——要么人没来，要么消息没进来")
        lines.append("")
    lines.append(
        f"**合计**：{totals['user_turns']} 提问 / {totals['assistant_turns']} 回复，"
        f"{totals['decisions']} 次决策（主动 {totals['acted']} 次）",
    )
    return lines


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", required=True, help="export root (contains runs)")
    parser.add_argument("--run", default=None, help="run directory (default: newest)")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--fleet", default="http://127.0.0.1:8800")
    parser.add_argument("--out", default=None, help="where to write the markdown")
    args = parser.parse_args()

    export_root = Path(args.export)
    run = Path(args.run) if args.run else latest_run(export_root)
    if run is None:
        raise SystemExit(f"no export run under {export_root}")
    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    live = fleet_status(args.fleet)
    body = [
        f"# 小九九封测日报 · {day}",
        "",
        f"- 导出批次：`{run.name}`",
        f"- fleet：{live.get('count', '不可达')} 个实例",
        "",
    ]
    body += person_report(run / "people", day, live)

    text = "\n".join(body)
    print(text)
    if args.out:
        target = Path(args.out)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{day}.md"
        path.write_text(text, encoding="utf-8")
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
