"""拿她真实的历史发言，对比分段规则改动前后的气泡数。

旧: regex="[。？]+"                cleanup=""
新: regex="[^\\n]+"                cleanup="[。]+$"
阈值: 60 字（只有短于阈值的消息才分段）
"""
import json
import re
import sqlite3

DB = "file:/data/default-friendmessage-1670681411/companion.sqlite3?mode=ro"
THRESHOLD = 60
OLD_REGEX, OLD_CLEANUP = "[。？]+", ""
NEW_REGEX, NEW_CLEANUP = "[^\\n]+", "[。]+$"


def split_with(text, regex, cleanup):
    if len(text) > THRESHOLD:
        return [text]
    try:
        segs = re.findall(regex, text, re.DOTALL | re.MULTILINE)
    except re.error:
        return ["<regex error>"]
    out = []
    for seg in segs:
        if cleanup:
            seg = re.sub(cleanup, "", seg)
        seg = seg.strip()
        if seg:
            out.append(seg)
    return out


con = sqlite3.connect(DB, uri=True)
rows = con.execute(
    "select content from raw_events where event_type='assistant_message'"
    " and content is not null order by timestamp desc limit 60"
).fetchall()
con.close()

total_old = total_new = shown = 0
for (content,) in rows:
    text = content if isinstance(content, str) else str(content)
    if not text.strip():
        continue
    old = split_with(text, OLD_REGEX, OLD_CLEANUP)
    new = split_with(text, NEW_REGEX, NEW_CLEANUP)
    total_old += len(old)
    total_new += len(new)
    if shown < 10 and old != new:
        shown += 1
        print("  原文 : %s" % text.replace("\n", "\\n"))
        print("  旧 %d 条: %s" % (len(old), json.dumps(old, ensure_ascii=False)))
        print("  新 %d 条: %s" % (len(new), json.dumps(new, ensure_ascii=False)))
        print()

print("统计: %d 条发言  旧规则共 %d 个气泡  新规则共 %d 个气泡  (-%.0f%%)" % (
    len(rows), total_old, total_new, (1 - total_new / total_old) * 100 if total_old else 0))
