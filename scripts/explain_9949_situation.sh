#!/usr/bin/env bash
# qq02 到底发生了什么：区分"人说的话"与"自动回复"，统计她发了多少、什么节奏、有没有停过。
set -u

docker logs xxj-napcat-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -a "(qq02)" > /tmp/9949.txt

python3 - <<'PY'
import re
from collections import Counter

LINE = re.compile(r"^(\d\d-\d\d) (\d\d:\d\d:\d\d) \[info\].*?(接收 <- |发送 -> )私聊 \(qq02\) ?(.*)$")
rows = []
for raw in open("/tmp/9949.txt", encoding="utf-8", errors="replace"):
    m = LINE.match(raw.rstrip("\n"))
    if m:
        day, time, direction, text = m.groups()
        rows.append({
            "stamp": "%s %s" % (day, time), "day": day, "time": time,
            "in": direction.startswith("接收"), "text": text.strip(),
        })
print("总条数 %d（她发 %d，对方来 %d）" % (
    len(rows), sum(1 for r in rows if not r["in"]), sum(1 for r in rows if r["in"])))
print("跨度 %s .. %s" % (rows[0]["stamp"], rows[-1]["stamp"]))

print()
print("=== A) 对方发来的、不是自动回复的（这才是「人说的话」）===")
human = [r for r in rows if r["in"] and "自动回复" not in r["text"]]
print("  共 %d 条：" % len(human))
for r in human:
    print("    %s  %s" % (r["stamp"], r["text"][:70]))

print()
print("=== B) 自动回复的形态 ===")
auto = [r for r in rows if r["in"] and "自动回复" in r["text"]]
print("  共 %d 条" % len(auto))
for text, count in Counter(r["text"] for r in auto).most_common(5):
    print("    %-24s ×%d" % (text[:24], count))

print()
print("=== C) 她发了多少、按小时分布（对方会收到 1200+ 条的震动）===")
out = [r for r in rows if not r["in"]]
per_day = Counter(r["day"] for r in out)
print("  按天:", dict(per_day))
hours = Counter(r["time"][:2] for r in out)
print("  按小时（09-18 全天，她发出去的消息数）:")
for hour in sorted(hours):
    print("    %s 时  %-4d %s" % (hour, hours[hour], "#" * min(60, hours[hour])))

print()
print("=== D) 有没有停过：相邻两条她发的消息间隔 > 30 分钟的断点 ===")
import datetime as dt
gaps = []
prev = None
for r in out:
    now = dt.datetime.strptime("%s %s" % (2026, r["stamp"][:14]), "%Y %m-%d %H:%M:%S")
    if prev and (now - prev).total_seconds() > 1800:
        gaps.append((prev.strftime("%m-%d %H:%M"), now.strftime("%m-%d %H:%M"),
                     (now - prev).total_seconds() / 3600.0))
    prev = now
print("  断点 %d 个:" % len(gaps))
for start, end, hours in gaps[:12]:
    print("    %s → %s  停了 %.1f 小时" % (start, end, hours))
if not gaps:
    print("    （没有超过 30 分钟的间隔 —— 环几乎没停过）")

print()
print("=== E) 一轮循环长什么样（最早 16 条）===")
for r in rows[:16]:
    print("  %s %s %s" % (r["stamp"], "←" if r["in"] else "→", r["text"][:56]))
PY
