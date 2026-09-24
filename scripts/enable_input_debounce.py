"""在容器内（root）开启输入防抖，并回读确认。"""
import datetime as dt
import json
import shutil

PLUGIN_CFG = "/AstrBot/data/config/astrbot_plugin_companion_runtime_config.json"
MAIN_CFG = "/AstrBot/data/cmd_config.json"
STAMP = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

backup = PLUGIN_CFG + ".bak-" + STAMP
shutil.copy2(PLUGIN_CFG, backup)
print("插件配置备份: %s" % backup)

with open(PLUGIN_CFG, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
before = {k: cfg.get(k) for k in ("input_debounce_ms", "input_debounce_max_chars")}
cfg["input_debounce_ms"] = 2500
cfg.setdefault("input_debounce_max_chars", 4000)
with open(PLUGIN_CFG, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
print("防抖: %s -> input_debounce_ms=%s max_chars=%s" % (
    before, cfg["input_debounce_ms"], cfg["input_debounce_max_chars"]))

with open(PLUGIN_CFG, encoding="utf-8-sig") as handle:
    check = json.load(handle)
assert check["input_debounce_ms"] == 2500, check
print("回读插件配置 OK: input_debounce_ms=%s" % check["input_debounce_ms"])

with open(MAIN_CFG, encoding="utf-8-sig") as handle:
    seg = json.load(handle)["platform_settings"]["segmented_reply"]
print("回读分段配置: enable=%s regex=%r cleanup=%r threshold=%s interval=%s" % (
    seg["enable"], seg["regex"], seg["content_cleanup_rule"],
    seg["words_count_threshold"], seg["interval"]))
