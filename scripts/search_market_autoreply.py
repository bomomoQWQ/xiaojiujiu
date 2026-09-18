"""在 AstrBot 插件市场索引里搜"能不能过滤 QQ 自动回复 / 重复消息 / 刷屏"。

索引位置：/AstrBot/data/plugins.json（{timestamp, data:{2162 条}, md5}）。
只读。
"""
import json
import re

MARKET = "/AstrBot/data/plugins.json"
KEYS = ["自动回复", "自动回复过滤", "过滤消息", "消息过滤", "复读", "重复消息", "刷屏",
        "去重", "拦截", "屏蔽词", "广告", "auto_reply", "autoreply", "repetition",
        "dedup", "duplicate", "spam"]

with open(MARKET, encoding="utf-8-sig") as handle:
    market = json.load(handle)
items = list(market["data"].values())
print("市场条目 %d，更新时间 %s" % (len(items), market.get("timestamp")))
print()

blobs = []
for it in items:
    if not isinstance(it, dict):
        continue
    blob = json.dumps(it, ensure_ascii=False)
    blobs.append((it, blob))

for key in KEYS:
    hits = [(it, blob) for it, blob in blobs if key.lower() in blob.lower()]
    if not hits:
        print("  %-14s 0 条" % key)
        continue
    print("  %-14s %d 条" % (key, len(hits)))

print()
print("=== 逐条看「自动回复」命中的 ===")
for it, blob in blobs:
    if "自动回复" not in blob:
        continue
    name = it.get("name")
    if name == "AstrBot Official Plugin Market":
        continue
    print("  * %s" % name)
    print("    repo=%s  author=%s  version=%s" % (
        it.get("repo"), it.get("author"), it.get("version")))
    print("    %s" % str(it.get("desc"))[:220].replace("\n", " "))

print()
print("=== 可能相关：过滤/复读/刷屏/去重/屏蔽 ===")
seen = set()
for key in ("消息过滤", "复读", "刷屏", "去重", "拦截", "屏蔽词", "广告"):
    for it, blob in blobs:
        name = it.get("name")
        if name in seen or name == "AstrBot Official Plugin Market":
            continue
        if key in blob:
            seen.add(name)
            print("  [%s] %-44s %s" % (key, name, str(it.get("desc"))[:110].replace("\n", " ")))
            print("         repo=%s" % it.get("repo"))
