"""两天半的封测日志深读：她说的话、主动性效果、学习进展、成本、异常。

读的是最新导出批次（export_beta_data.py 写的 JSONL），不碰线上库。
运行位置：服务器上直接跑（导出就在本地机械盘）。
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BETA = Path("/mnt/xz/xiaojiujiu-beta")
CST = timedelta(hours=8)

MARKDOWN = re.compile(r"(\*\*|^#|^\s*[-*]\s|^>|\x60|\[.*?\]\(.*?\))", re.M)
BUREAUCRAT = ("首先", "其次", "总之", "另外", "最后一点", "综上所述", "希望对你有所帮助")
SERVICE = ("随时找我", "很高兴", "希望", "祝你", "不用急", "有什么需要", "加油")


def latest_run() -> Path:
    runs = sorted((p for p in BETA.glob("20*-*-*_*") if p.is_dir()), key=lambda p: p.name)
    return runs[-1]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def parse(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def cst(moment: datetime | None) -> str:
    return (moment + CST).strftime("%m-%d %H:%M") if moment else "-"


def main() -> None:
    run = latest_run()
    print("批次: %s" % run)
    people = sorted((run / "people").iterdir())
    print("人: %d\n" % len(people))

    totals = Counter()
    provider_by_person: dict[str, int] = {}

    print("=" * 100)
    print("A) 每个人的活动与她说的话")
    print("=" * 100)
    for person in people:
        tag = person.name.replace("default-friendmessage-", "")
        events = read_jsonl(person / "events.jsonl")
        users = [e for e in events if e.get("event_type") == "user_message"]
        hers = [e for e in events if e.get("event_type") == "assistant_message" and (e.get("content") or "").strip()]
        proactive = [e for e in events if e.get("event_type") == "proactive_sent"]
        summaries = [e for e in events if e.get("event_type") == "candidate_proposal"]
        lengths = [len(e["content"]) for e in hers]

        # 主动性是否被接住：proactive_sent 之后 6 小时内有没有用户消息
        answered = 0
        for act in proactive:
            sent_at = parse(act.get("timestamp"))
            if not sent_at:
                continue
            if any(parse(u.get("timestamp")) and 0 < (parse(u["timestamp"]) - sent_at).total_seconds() <= 6 * 3600
                   for u in users):
                answered += 1

        markdown = sum(1 for e in hers if MARKDOWN.search(e["content"] or ""))
        service = sum(1 for e in hers if any(w in (e["content"] or "") for w in SERVICE))
        long_ones = sum(1 for n in lengths if n > 60)
        within_30 = sum(1 for n in lengths if n <= 30)
        dupes = Counter(e["content"] for e in hers if (e.get("content") or "").strip())
        top_dupes = [(text, n) for text, n in dupes.most_common(3) if n > 1]

        total_events = len(events)
        totals["users"] += len(users)
        totals["hers"] += len(hers)
        totals["proactive"] += len(proactive)
        totals["candidates"] += len(summaries)

        print("\n### %s" % tag)
        first = min((parse(e.get("timestamp")) for e in events if parse(e.get("timestamp"))), default=None)
        last = max((parse(e.get("timestamp")) for e in events if parse(e.get("timestamp"))), default=None)
        print("  事件 %d（%s → %s）  用户 %d 条  她 %d 条  主动 %d 条  候选 %d" % (
            total_events, cst(first), cst(last), len(users), len(hers), len(proactive), len(summaries)))
        if lengths:
            print("  她的长度：中位 %d  90分位 %d  最长 %d ｜ ≤30字 %d/%d  超60字 %d ｜ Markdown %d ｜ 客服腔 %d" % (
                statistics.median(lengths), sorted(lengths)[int(len(lengths) * 0.9) - 1] if len(lengths) > 1 else lengths[0],
                max(lengths), within_30, len(lengths), long_ones, markdown, service))
        if proactive:
            print("  主动被接住：%d/%d（6 小时内有人回）" % (answered, len(proactive)))
        if top_dupes:
            print("  重复最多：%s" % "；".join("%r×%d" % (t[:24], n) for t, n in top_dupes))
        # 最长三条，看是不是"客服"
        for e in sorted(hers, key=lambda item: -len(item["content"]))[:2]:
            text = " / ".join((e["content"] or "").split())
            if len(text) > 60:
                print("    最长的之一：%s" % text[:150])

        # 每人的 provider 调用（只有 ran=1 才算真调用）
        refreshes = read_jsonl(person / "refresh_runs.jsonl")
        applied = sum(1 for r in refreshes if r.get("ran") and r.get("provider"))
        degraded = sum(1 for r in refreshes if r.get("degraded"))
        provider_by_person[tag] = applied
        totals["provider"] += applied
        totals["degraded"] += degraded
        if applied or degraded:
            print("  deep refresh：真调用 %d 次，降级 %d 次" % (applied, degraded))

    print()
    print("=" * 100)
    print("B) 学习与记忆（她到底记住了什么）")
    print("=" * 100)
    for person in people:
        tag = person.name.replace("default-friendmessage-", "")
        tables = person / "tables"
        memories = read_jsonl(tables / "memories.jsonl")
        obs = read_jsonl(tables / "interaction_observations.jsonl")
        summary = {}
        try:
            summary = json.loads((person / "summary.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        kinds = Counter(m.get("kind") for m in memories)
        durable = sum(n for k, n in kinds.items() if k in ("user_preference", "stable_knowledge", "relationship"))
        model = (summary.get("user_model") or {})
        print("  %-14s 记忆 %-3d（durable %-2d %s）  观测 %-2d  模型 observations=%s eff=%s  未决=%s" % (
            tag, len(memories), durable, dict(kinds), len(obs),
            model.get("observations"), model.get("effective_count"),
            (summary.get("unresolved") if summary else "-")))

    print()
    print("=" * 100)
    print("C) 成本与异常")
    print("=" * 100)
    print("  provider 调用（=deep refresh 真调用）合计 %d 次：" % totals["provider"])
    for tag, count in sorted(provider_by_person.items(), key=lambda item: -item[1]):
        if count:
            print("     %-14s %d" % (tag, count))
    print("  fleet 合计：用户 %d 条 / 她 %d 条 / 主动 %d 条 / 候选 %d" % (
        totals["users"], totals["hers"], totals["proactive"], totals["candidates"]))

    print()
    print("  各实例日志里的异常计数（error/traceback/500/租约丢失/超时）：")
    for person in people:
        tag = person.name.replace("default-friendmessage-", "")
        log = person / "runtime.log"
        if not log.exists():
            continue
        text = log.read_text(encoding="utf-8", errors="replace")
        stats = {
            "error": len(re.findall(r"\bERROR\b", text)),
            "traceback": text.count("Traceback"),
            "500": len(re.findall(r"\s500\s", text)),
            "lease_lost": len(re.findall(r"lease.*(lost|refus|409)", text, re.I)),
            "timeout": len(re.findall(r"timeout|timed out", text, re.I)),
        }
        interesting = {k: v for k, v in stats.items() if v}
        print("     %-14s %s" % (tag, interesting or "干净"))
    # 日志体积与内容构成
    print()
    print("  日志体积（一天约 3-4MB/人）：")
    for person in people[:3]:
        log = person / "runtime.log"
        if log.exists():
            print("     %-14s %.1f MB" % (person.name.replace("default-friendmessage-", ""),
                                          log.stat().st_size / 1024 / 1024))


if __name__ == "__main__":
    main()
