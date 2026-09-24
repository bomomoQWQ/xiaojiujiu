"""主动消息的时间审计：她什么时候开口、间隔合不合理、时间词/时间跨度有没有说错。

读的是最新导出批次（export_beta_data.py 写的 JSONL），不碰线上库。
运行位置：服务器上直接跑。
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BETA = Path("/mnt/xz/xiaojiujiu-beta")
CST = timedelta(hours=8)

TIME_WORDS = ("早安", "早上", "上午", "中午", "下午", "晚上", "晚安", "夜里", "凌晨",
              "昨天", "前天", "今天", "明天", "后天", "睡", "熬夜", "几点", "点钟", "天亮")
DURATION = re.compile(r"(\d+)\s*(分钟|小时|天|周|个月)|([一两三几多])\s*(天|小时|分钟|周)")
DAY_WORD = {"昨天": 1, "前天": 2, "今天": 0, "明天": -1, "后天": -2}


def rows(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
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


def hour(moment: datetime | None) -> int | None:
    return (moment + CST).hour if moment else None


def is_night(h: int | None) -> bool:
    return h is not None and (h >= 23 or h < 7)


def brief(text: str, width: int = 34) -> str:
    return " ".join((text or "").split())[:width]


def histogram(hours: list[int]) -> str:
    buckets = Counter(hours)
    return " ".join("%02d:%d" % (h, buckets.get(h, 0)) for h in range(24) if buckets.get(h))


def main() -> None:
    run = sorted(p for p in BETA.glob("20*-*-*_*") if p.is_dir())[-1]
    print("批次 %s" % run.name)
    people = sorted((run / "people").iterdir())
    tag_of = lambda p: p.name.replace("default-friendmessage-", "")

    fleet_pro_hours: list[int] = []
    fleet_dec_hours: list[int] = []
    fleet_wake_hours: list[int] = []
    fleet_night_wake = 0
    fleet_wake_total = 0
    fleet_delta: list[float] = []
    fleet_triggers: Counter = Counter()

    print()
    print("=" * 104)
    print("A) 主动消息逐条：时间（CST）｜距上次用户消息｜距上条她的话｜内容")
    print("=" * 104)
    for person in people:
        tag = tag_of(person)
        events = rows(person / "events.jsonl")
        items = []
        for e in events:
            kind = e.get("event_type")
            stamp = parse(e.get("timestamp"))
            if kind in ("user_message", "assistant_message", "proactive_sent") and stamp:
                items.append((stamp, kind, e.get("content") or ""))
        items.sort(key=lambda row: row[0])
        pro = [row for row in items if row[1] == "proactive_sent"]
        users = [row for row in items if row[1] == "user_message"]
        hers = [row for row in items if row[1] == "assistant_message"]
        if not (pro or users or hers):
            continue
        print("\n### %s  用户 %d ｜ 她回复 %d ｜ 主动 %d" % (tag, len(users), len(hers), len(pro)))
        prev_act = None
        for stamp, _kind, text in pro:
            fleet_pro_hours.append(hour(stamp))
            prior_user = [row[0] for row in users if row[0] < stamp]
            gap_user = (stamp - prior_user[-1]).total_seconds() / 60 if prior_user else None
            gap_act = (stamp - prev_act).total_seconds() / 60 if prev_act else None
            flags = []
            if is_night(hour(stamp)):
                flags.append("夜间")
            if gap_user is not None and gap_user < 5:
                flags.append("追话%.0f分" % gap_user)
            if gap_act is not None and gap_act < 60:
                flags.append("密集%.0f分" % gap_act)
            print("   %s  %-16s %-16s %s  %s" % (
                hm(stamp),
                ("上次用户 %.1fh 前" % (gap_user / 60)) if gap_user is not None else "无用户消息",
                ("上次主动 %.1fh 前" % (gap_act / 60)) if gap_act is not None else "-",
                " ".join(flags) or "  ",
                brief(text)))
            prev_act = stamp

        # 回复时延：用户消息 → 她下一条回复
        latencies = []
        for stamp, _k, _t in users:
            after = [row[0] for row in hers + pro if row[0] > stamp]
            if after:
                latencies.append(((after[0] - stamp).total_seconds() / 60, stamp))
        if latencies:
            values = sorted(v for v, _ in latencies)
            over = [s for v, s in latencies if v > 60]
            print("   回复时延：中位 %.1f 分 ｜ 最长 %.1f 分 ｜ >1h 的 %d 条 %s" % (
                statistics.median(values), values[-1], len(over),
                " ".join(hm(s) for s in over[:6])))

    print()
    print("=" * 104)
    print("B) 她的话里出现的时间词（对照片刻的本地时间，看有没有错位）")
    print("=" * 104)
    for person in people:
        tag = tag_of(person)
        events = rows(person / "events.jsonl")
        items = []
        for e in events:
            kind = e.get("event_type")
            stamp = parse(e.get("timestamp"))
            if kind in ("user_message", "assistant_message", "proactive_sent") and stamp:
                items.append((stamp, kind, e.get("content") or ""))
        items.sort(key=lambda row: row[0])
        users = [row for row in items if row[1] == "user_message"]
        hits = [(s, k, t) for s, k, t in items
                if k in ("assistant_message", "proactive_sent") and any(w in (t or "") for w in TIME_WORDS)]
        day_hits = [(s, t) for s, k, t in items
                    if k in ("assistant_message", "proactive_sent")
                    and any(w in (t or "") for w in DAY_WORD)]
        if not hits and not day_hits:
            continue
        print("\n### %s" % tag)
        for stamp, kind, text in hits:
            print("   %s %s %s" % (hm(stamp), "主动" if kind == "proactive_sent" else "回复", brief(text, 60)))
        for stamp, text in day_hits:
            prior = [row[0] for row in users if row[0] < stamp]
            if not prior:
                print("   %s 日期词但之前没有用户消息 ｜ %s" % (hm(stamp), brief(text, 50)))
                continue
            delta_days = (stamp + CST).date() - (prior[-1] + CST).date()
            tokens = [w for w in DAY_WORD if w in text]
            bad = [w for w in tokens if abs(DAY_WORD[w] - delta_days.days) > 0]
            if bad:
                print("   %s 日期词 %s 但上条用户消息是 %s（差 %d 天）｜ %s" % (
                    hm(stamp), "/".join(bad), hm(prior[-1]), delta_days.days, brief(text, 40)))

    print()
    print("=" * 104)
    print("C) 她声称的时间跨度（分钟/小时/天）vs 真实间隔")
    print("=" * 104)
    for person in people:
        tag = tag_of(person)
        events = rows(person / "events.jsonl")
        items = []
        for e in events:
            if e.get("event_type") in ("user_message", "assistant_message", "proactive_sent"):
                stamp = parse(e.get("timestamp"))
                if stamp:
                    items.append((stamp, e.get("event_type"), e.get("content") or ""))
        items.sort(key=lambda row: row[0])
        users = [row for row in items if row[1] == "user_message"]
        printed = False
        for stamp, kind, text in items:
            if kind == "user_message":
                continue
            match = DURATION.search(text or "")
            if not match:
                continue
            prior = [row[0] for row in users if row[0] < stamp]
            if not prior:
                continue
            real_hours = (stamp - prior[-1]).total_seconds() / 3600
            if not printed:
                print("\n### %s" % tag)
                printed = True
            print("   %s 说「%s」｜ 距上条用户消息 %.1f 小时" % (hm(stamp), match.group(0), real_hours))

    print()
    print("=" * 104)
    print("D) 决策与唤醒的时间分布（全舰队）")
    print("=" * 104)
    for person in people:
        tag = tag_of(person)
        for row in rows(person / "decisions.jsonl"):
            decided = parse(row.get("decided_at"))
            wake = parse(row.get("next_wake_at"))
            if decided:
                fleet_dec_hours.append(hour(decided))
            if wake:
                fleet_wake_hours.append(hour(wake))
                fleet_wake_total += 1
                if is_night(hour(wake)):
                    fleet_night_wake += 1
            if row.get("delta_t") is not None:
                try:
                    fleet_delta.append(float(row["delta_t"]))
                except (TypeError, ValueError):
                    pass
            fleet_triggers[str(row.get("trigger"))] += 1
    print("  决策发生时刻（本地小时）：%s" % histogram(fleet_dec_hours))
    print("  计划唤醒时刻（本地小时）：%s" % histogram(fleet_wake_hours))
    print("  计划唤醒落在夜间（23-07）：%d/%d（%.0f%%）" % (
        fleet_night_wake, fleet_wake_total,
        100.0 * fleet_night_wake / fleet_wake_total if fleet_wake_total else 0.0))
    print("  主动消息实际发送时刻：%s" % histogram(fleet_pro_hours))
    if fleet_delta:
        values = sorted(fleet_delta)
        rounded = Counter(round(v) for v in values if v > 0)
        print("  delta_t（距上次接触秒数）：中位 %.0f ｜ 最小 %.0f ｜ 最大 %.0f ｜ 最常见 %s" % (
            statistics.median(values), values[0], values[-1], rounded.most_common(5)))
    print("  trigger 分布：%s" % dict(fleet_triggers.most_common(8)))


if __name__ == "__main__":
    main()
