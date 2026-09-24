"""备份并修改 astrbot-test 的分段回复配置，然后用她真实的历史发言做前后对比。

改的两项：
  segmented_reply.regex              : ".*?[。？！~…]+|.+$"  ->  "[^\n]+"
      原来每个句号/问号都切一刀；改成只按换行切，即"一段一条"。
  segmented_reply.content_cleanup_rule: ""                   ->  "[。]+$"
      每段结尾的句号剔除（AstrBot v3.4.28 起提供的能力）。

注意：findall 用的切分正则不能带捕获组；cleanup 用 re.sub，可以带。
"""
import datetime as dt
import json
import os
import re
import shutil
import sqlite3

CFG = "/AstrBot/data/cmd_config.json"
DB = "/AstrBot/data/data_v4.db"
STAMP = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
NEW_REGEX = "[^\\n]+"
NEW_CLEANUP = "[。]+$"

# ---------------------------------------------------------------- 备份
backup = CFG + ".bak-segmented-" + STAMP
shutil.copy2(CFG, backup)
print("备份: %s (%d bytes)" % (backup, os.path.getsize(backup)))

with open(CFG, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
seg = cfg["platform_settings"]["segmented_reply"]
before = dict(seg)
seg["regex"] = NEW_REGEX
seg["content_cleanup_rule"] = NEW_CLEANUP
with open(CFG, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
print("已写回（保留 BOM）")
print("  regex              : %r -> %r" % (before.get("regex"), seg["regex"]))
print("  content_cleanup_rule: %r -> %r" % (
    before.get("content_cleanup_rule"), seg["content_cleanup_rule"]))
print("  其余未动: enable=%s threshold=%s interval=%s split_mode=%s" % (
    seg["enable"], seg["words_count_threshold"], seg["interval"], seg["split_mode"]))

# ------------------------------------------------- 拿真实发言做前后对比
print()
print("=== 她真实的历史发言：旧规则 vs 新规则 ===")
threshold = int(seg["words_count_threshold"])
old_regex = before.get("regex") or ""
old_cleanup = before.get("content_cleanup_rule") or ""

con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
rows = con.execute(
    "select content from raw_events where event_type='assistant_message'"
    " and content is not null order by timestamp desc limit 40"
).fetchall()
con.close()


def split_with(text, regex, cleanup):
    if len(text) > threshold:
        return [text]  # 超阈值不分段
    try:
        segs = re.findall(regex, text, re.DOTALL | re.MULTILINE)
    except re.error:
        return ["<regex error>"]
    out = []
    for seg in segs:
        if cleanup:
            try:
                seg = re.sub(cleanup, "", seg)
            except re.error:
                pass
        seg = seg.strip()
        if seg:
            out.append(seg)
    return out


total_old = total_new = shown = 0
for (content,) in rows:
    text = content if isinstance(content, str) else str(content)
    if not text.strip():
        continue
    old = split_with(text, old_regex, old_cleanup)
    new = split_with(text, NEW_REGEX, NEW_CLEANUP)
    total_old += len(old)
    total_new += len(new)
    if shown < 8 and (len(old) != len(new) or old != new):
        shown += 1
        print()
        print("  原文: %r" % text.replace("\n", "\\n"))
        print("  旧 %d 条: %s" % (len(old), json.dumps(old, ensure_ascii=False)))
        print("  新 %d 条: %s" % (len(new), json.dumps(new, ensure_ascii=False)))

print()
print("  统计: %d 条历史发言，旧规则共 %d 个气泡，新规则共 %d 个气泡（-%.0f%%）" % (
    len(rows), total_old, total_new,
    (1 - total_new / total_old) * 100 if total_old else 0))
