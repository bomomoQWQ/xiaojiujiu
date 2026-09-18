"""分析 /tmp/qq_all.txt（NapCat 的 QQ 层收发记录）：自动回复、回环、以及时间异常。

只读分析，不改任何东西。
"""
import re
from collections import Counter, defaultdict

PATH = "/tmp/qq_all.txt"
LINE = re.compile(
    r"^(\d\d-\d\d) (\d\d:\d\d:\d\d) \[info\] .*?(接收 <- |发送 -> )私聊 \((\d+)\) ?(.*)$")

rows = []
for raw in open(PATH, encoding="utf-8", errors="replace"):
    m = LINE.match(raw.rstrip("\n"))
    if m:
        day, time, direction, qq, text = m.groups()
        rows.append({
            "stamp": "%s %s" % (day, time), "day": day, "time": time,
            "dir": "in" if direction.startswith("接收") else "out",
            "qq": qq, "text": text.strip(),
        })
print("解析到 %d 条" % len(rows))
print("跨度 %s .. %s" % (rows[0]["stamp"], rows[-1]["stamp"]))

# ---------------------------------------------------------------- 时间单调性
print()
print("=== 时间异常检查 ===")
backwards = []
for i in range(1, len(rows)):
    if rows[i]["stamp"] < rows[i - 1]["stamp"]:
        backwards.append((rows[i - 1]["stamp"], rows[i]["stamp"]))
print("  时间倒流的相邻对: %d %s" % (len(backwards), backwards[:3]))
dup = Counter(r["stamp"] for r in rows)
print("  同一秒出现多条的秒数: %d（最多 %d 条/秒）" % (
    sum(1 for v in dup.values() if v > 1), max(dup.values())))

# ---------------------------------------------------------------- 自动回复
print()
print("=== [自动回复] 统计 ===")
by_person = defaultdict(list)
for r in rows:
    by_person[r["qq"]].append(r)
for qq, items in sorted(by_person.items(), key=lambda kv: -len(kv[1])):
    auto = [r for r in items if "自动回复" in r["text"]]
    ins = [r for r in items if r["dir"] == "in"]
    outs = [r for r in items if r["dir"] == "out"]
    mark = "  ** 有自动回复 **" if auto else ""
    print("  %-12s 收 %4d / 发 %4d   自动回复 %3d（占收到的 %.0f%%）%s" % (
        qq, len(ins), len(outs), len(auto),
        100 * len(auto) / len(ins) if ins else 0, mark))
    if auto:
        print("      最早 %s   最晚 %s" % (auto[0]["stamp"], auto[-1]["stamp"]))
        print("      自动回复的文本: %s" % Counter(r["text"] for r in auto).most_common(3))

# ---------------------------------------------------------------- 回环：连续发送
print()
print("=== 回环检查：一次用户输入之后，她连发多少条 ===")
for qq, items in sorted(by_person.items(), key=lambda kv: -len(kv[1])):
    runs = []
    run = 0
    for r in items:
        if r["dir"] == "out":
            run += 1
        else:
            if run:
                runs.append(run)
            run = 0
    if run:
        runs.append(run)
    if not runs:
        continue
    print("  %-12s 连发段数 %4d，最长 %3d 条，中位 %.0f 条" % (
        qq, len(runs), max(runs), sorted(runs)[len(runs) // 2]))

# ---------------------------------------------------------------- 994959351 的回环片段
print()
print("=== 994959351：自动回复触发的往复（最后 40 条）===")
for r in by_person["994959351"][-40:]:
    flag = "  <== 自动回复" if "自动回复" in r["text"] else ""
    print("  %s %s %s%s" % (r["stamp"], "←" if r["dir"] == "in" else "→", r["text"][:58], flag))

print()
print("=== 994959351：自动回复第一次出现前后 30 条（看它怎么开始的）===")
items = by_person["994959351"]
first = next((i for i, r in enumerate(items) if "自动回复" in r["text"]), None)
if first is not None:
    for r in items[max(0, first - 15): first + 15]:
        flag = "  <== 自动回复" if "自动回复" in r["text"] else ""
        print("  %s %s %s%s" % (r["stamp"], "←" if r["dir"] == "in" else "→", r["text"][:58], flag))
