"""查流式相关配置：分段回复到底跑不跑。"""
import json
import re

with open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig") as handle:
    cfg = json.load(handle)

print("=== 含 stream / 分段 的配置项 ===")


def walk(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            walk(value, "%s.%s" % (path, key) if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk(value, "%s[%d]" % (path, index))
    else:
        if path and re.search(r"stream|segmented|fallback|t2i|reply", path, re.I):
            text = json.dumps(node, ensure_ascii=False)
            if len(text) > 100:
                text = text[:100] + "..."
            print("  %-62s = %s" % (path, text))


walk(cfg)

print()
print("=== provider_settings 全文 ===")
print(json.dumps(cfg.get("provider_settings"), ensure_ascii=False, indent=2)[:1800])
