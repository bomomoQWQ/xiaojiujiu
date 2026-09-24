"""两个导出批次之间的增量：这 8 小时里谁说了什么、她说了什么、主动落在几点、有没有异常。

用法（服务器上跑，读导出、不碰活库）：
    python3 beta_batch_delta.py            # 最后两个批次
    python3 beta_batch_delta.py <旧批次> <新批次>
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BETA = Path("/mnt/xz/xiaojiujiu-beta")
CST = timedelta(hours=8)
NIGHT = lambda h: h >= 23 or h < 7  # noqa: E731

TIME_WORDS = ("早安", "早上", "上午", "中午", "下午", "晚上", "晚安", "夜里", "凌晨",
              "昨天", "前天", "今天", "明天", "后天", "熬夜", "天亮")


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def parse(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def hm(moment: datetime | None) -> str:
    return (moment + CST).strftime("%m-%d %H:%M") if moment else "-"


def brief(text: str, width: int = 46) -> str:
    return " ".join((text or "").split())[:width]


def batch_start(run: Path) -> datetime:
    """Increment window start: the newest event already seen in the older batch.

    Deliberately **not** derived from the directory name alone. The first version
    of this script did ``run.name.replace("_", "T") + ":00+00:00"``, which is not
    a valid ISO string ("2026-09-21T0329:00+00:00"), and the ``ValueError`` fell
    back to "now - 24h" - silently widening a 8-hour window to a full day and
    reporting the previous window's messages as new. Data-derived is the honest
    cut: it can only be too *late* by one export, never a whole day early.
    """
    latest = None
    for person in sorted((run / "people").iterdir()):
        for event in rows(person / "events.jsonl"):
            stamp = parse(event.get("timestamp"))
            if stamp and (latest is None or stamp > latest):
                latest = stamp
    if latest is not None:
        return latest
    try:
        return datetime.strptime(run.name, "%Y-%m-%d_%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        print("!! 无法确定批次起点（既没有事件，名字也不合 %%Y-%%m-%%d_%%H%%M）: %s" % run.name,
              file=sys.stderr)
        return datetime.fromtimestamp(0, tz=timezone.utc)


def main() -> None:
    runs = sorted(p for p in BETA.glob("20*-*-*_*") if p.is_dir())
    if len(sys.argv) >= 3:
        old = BETA / sys.argv[1]
        new = BETA / sys.argv[2]
    else:
        old, new = runs[-2], runs[-1]
    print("旧批次 %s  →  新批次 %s" % (old.name, new.name))

    totals = Counter()
    hours: Counter = Counter()
    for person in sorted((new / "people").iterdir()):
        tag = person.name.replace("default-friendmessage-", "")
        old_events = rows(old / "people" / person.name / "events.jsonl")
        cut = max((parse(e.get("timestamp")) for e in old_events if parse(e.get("timestamp"))),
                  default=batch_start(old))
        events = [e for e in rows(person / "events.jsonl")
                  if (parse(e.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) > cut]
        users = [e for e in events if e.get("event_type") == "user_message"]
        hers = [e for e in events if e.get("event_type") == "assistant_message" and (e.get("content") or "").strip()]
        pro = [e for e in events if e.get("event_type") == "proactive_sent"]
        decisions = rows(person / "decisions.jsonl")
        new_dec = [d for d in decisions
                   if (parse(d.get("decided_at")) or datetime.min.replace(tzinfo=timezone.utc)) > cut]
        refreshes = [r for r in rows(person / "refresh_runs.jsonl")
                     if r.get("ran") and r.get("provider")
                     and (parse(r.get("started_at") or r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) > cut]
        memories = rows(person / "tables" / "memories.jsonl")
        new_mem = [m for m in memories
                   if (parse(m.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) > cut]

        totals["users"] += len(users)
        totals["hers"] += len(hers)
        totals["pro"] += len(pro)
        totals["dec"] += len(new_dec)
        totals["mem"] += len(new_mem)
        totals["ref"] += len(refreshes)

        if not (users or hers or new_dec):
            continue
        print("\n### %s  用户 %d ｜ 她 %d（含主动 %d）｜ 决策 %d ｜ 新记忆 %d ｜ provider %d" % (
            tag, len(users), len(hers), len(pro), len(new_dec), len(new_mem), len(refreshes)))
        for e in users:
            print("   %s 用户 %s" % (hm(parse(e.get("timestamp"))), brief(e.get("content"), 60)))
        for e in pro:
            stamp = parse(e.get("timestamp"))
            hour = (stamp + CST).hour if stamp else None
            if hour is not None:
                hours[hour] += 1
            flag = " ⚠夜间" if hour is not None and NIGHT(hour) else ""
            print("   %s 主动%s %s" % (hm(stamp), flag, brief(e.get("content"), 60)))
        leaked = [e for e in hers if "system_reminder" in (e.get("content") or "")]
        for e in leaked:
            print("   %s 🔴 正文里带 system_reminder：%s" % (hm(parse(e.get("timestamp"))), brief(e.get("content"), 40)))
        for e in hers:
            text = e.get("content") or ""
            hour = (parse(e.get("timestamp")) + CST).hour if parse(e.get("timestamp")) else None
            hit = [w for w in TIME_WORDS if w in text]
            if hit and hour is not None:
                odd = (
                    ("早" in "".join(hit) and hour not in range(5, 12))
                    or (any(w in hit for w in ("晚上", "晚安")) and hour not in range(18, 24) and hour not in range(0, 4))
                    or ("中午" in hit and hour not in range(11, 14))
                )
                if odd:
                    print("   %s ⚠时段词 %s 但本地 %02d 点：%s" % (hm(parse(e.get("timestamp"))), "/".join(hit), hour, brief(text, 50)))

    print("\n=== 增量合计：用户 %d ｜ 她 %d（主动 %d）｜ 决策 %d ｜ 新记忆 %d ｜ provider 调用 %d" % (
        totals["users"], totals["hers"], totals["pro"], totals["dec"], totals["mem"], totals["ref"]))
    if hours:
        print("增量期主动消息（本地小时）：%s" % " ".join("%02d:%d" % (h, hours[h]) for h in sorted(hours)))


if __name__ == "__main__":
    main()
