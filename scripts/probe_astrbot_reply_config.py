"""摸清 astrbot-test 的回复分段/防抖现状，并在插件市场索引里搜防抖插件。

注意：这个脚本是通过
    docker exec -i astrbot-test python3 - < /tmp/probe.py
喂给容器的，所以不能再嵌套 heredoc。
"""
import json
import os
import re

CFG = "/AstrBot/data/cmd_config.json"
MARKET = "/AstrBot/data/plugins.json"

print("=== 主配置 platform_settings（回复相关）===")
data = json.load(open(CFG, encoding="utf-8-sig"))  # AstrBot 写盘带 BOM
print("  顶层键:", sorted(data.keys()))
ps = data.get("platform_settings") or {}
print("  platform_settings 键:", sorted(ps.keys()))
for key in (
    "segmented_reply",
    "reply_with_quote",
    "empty_mention_waiting",
    "empty_mention_waiting_need_reply",
    "friend_message_needs_wake_prefix",
    "ignore_bot_self_message",
    "ignore_at_all",
    "no_permission_reply",
):
    if key in ps:
        print("  %-38s = %s" % (key, json.dumps(ps[key], ensure_ascii=False)))

print()
print("=== 全配置里任何像'防抖/合并/等待/分段'的键 ===")
pat = re.compile(r"debounce|merg|防抖|合并|aggregat|batch|segmented|split|wait", re.I)


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, "%s.%s" % (path, k) if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, "%s[%d]" % (path, i))
    else:
        if path and pat.search(path):
            s = json.dumps(node, ensure_ascii=False)
            if len(s) > 120:
                s = s[:120] + "..."
            print("  %-58s = %s" % (path, s))


walk(data)

print()
print("=== 插件市场索引 ===")
if not os.path.exists(MARKET):
    print("  (没有 %s)" % MARKET)
else:
    size = os.path.getsize(MARKET)
    print("  文件大小 %.1f MB" % (size / 1e6))
    try:
        market = json.load(open(MARKET, encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print("  解析失败:", exc)
        market = None
    if market is not None:
        print("  类型:", type(market).__name__)
        items = []
        if isinstance(market, dict):
            for k, v in market.items():
                if isinstance(v, list):
                    items = v
                    print("  列表键:", k, "长度", len(v))
                    break
            else:
                items = list(market.values())
        elif isinstance(market, list):
            items = market
        print("  条目数:", len(items))
        if items:
            print("  单条样例:", json.dumps(items[0], ensure_ascii=False)[:400])
        hits = 0
        for it in items:
            blob = json.dumps(it, ensure_ascii=False)
            if re.search(r"防抖|合并消息|消息合并|debounce|merger", blob, re.I):
                hits += 1
                name = it.get("name") or it.get("id") or it.get("repo") or "?"
                desc = (it.get("desc") or it.get("description") or "")[:90]
                repo = it.get("repo") or it.get("url") or ""
                print("  * %-44s %s" % (str(name)[:44], repo))
                print("      %s" % desc)
        print("  命中:", hits)
