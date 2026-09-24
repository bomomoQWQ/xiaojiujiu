"""看 plugins.json 的真实结构 + 找防抖插件；顺便看 rate_limit 是什么。"""
import json
import re

MARKET = "/AstrBot/data/plugins.json"
CFG = "/AstrBot/data/cmd_config.json"

market = json.load(open(MARKET, encoding="utf-8-sig"))
print("=== plugins.json 结构 ===")
for k, v in market.items():
    print("  %-24s %s (len=%s)" % (k, type(v).__name__, len(v) if hasattr(v, "__len__") else "-"))

items = None
node = market.get("data")
if isinstance(node, dict):
    items = list(node.values())
    print("  用 data 字典，条目数:", len(items))
elif isinstance(node, list):
    items = node
    print("  用 data 列表，条目数:", len(items))
if items:
    print("  条目数:", len(items))
    print("  样例:", json.dumps(items[0], ensure_ascii=False)[:500])
    print()
    print("=== 含 防抖/合并/debounce/merger 的条目 ===")
    for it in items:
        blob = json.dumps(it, ensure_ascii=False)
        if re.search(r"防抖|合并|debounce|merger", blob, re.I):
            print("  name=%s" % it.get("name"))
            print("    repo=%s  author=%s  version=%s" % (
                it.get("repo"), it.get("author"), it.get("version")))
            print("    desc=%s" % str(it.get("desc"))[:160])
            print("    tags=%s" % it.get("tags"))
            print()

print()
print("=== rate_limit / unique_session 的定义 ===")
cfg = json.load(open(CFG, encoding="utf-8-sig"))
ps = cfg.get("platform_settings", {})
for k in ("rate_limit", "unique_session", "reply_prefix"):
    print("  %-16s = %s" % (k, json.dumps(ps.get(k), ensure_ascii=False)))
