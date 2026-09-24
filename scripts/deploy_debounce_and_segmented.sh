#!/usr/bin/env bash
# 部署插件改动 + 打开输入防抖 + 重启 astrbot-test + 验证。
set -u
P=/home/bomomo/astrbot_test/data/plugins/astrbot_plugin_companion_runtime
CFG=/home/bomomo/astrbot_test/data/config/astrbot_plugin_companion_runtime_config.json
BK=/home/bomomo/astrbot_test/backups

echo "=== 1) 备份插件配置 ==="
mkdir -p "$BK"
cp -a "$CFG" "$BK/plugin-config-$(date +%Y%m%d-%H%M%S).json"
ls -1 "$BK" | tail -3

echo
echo "=== 2) 拉代码 ==="
cd "$P"
git pull --ff-only 2>&1 | tail -3
git log --oneline -1
echo "新字段在不在:"
grep -c "input_debounce" main.py companion_runtime/settings.py _conf_schema.json

echo
echo "=== 3) 打开输入防抖（2500ms）==="
python3 - "$CFG" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
before = {k: cfg.get(k) for k in ("input_debounce_ms", "input_debounce_max_chars")}
cfg["input_debounce_ms"] = 2500
cfg.setdefault("input_debounce_max_chars", 4000)
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
print("  之前:", before)
print("  现在: input_debounce_ms=%s input_debounce_max_chars=%s" % (
    cfg["input_debounce_ms"], cfg["input_debounce_max_chars"]))
PY

echo
echo "=== 4) 重启 astrbot-test（这个窗口里发的消息会静默丢失）==="
docker restart astrbot-test >/dev/null
for i in $(seq 1 30); do
  sleep 5
  if docker logs astrbot-test --since 3m 2>&1 | grep -q "适配器已连接"; then
    echo "  适配器已连接（${i}0s 内）"
    break
  fi
done

echo
echo "=== 5) 插件加载情况 ==="
docker logs astrbot-test --since 5m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE "companion|防抖|debounce|Traceback|载入|loaded" | tail -12

echo
echo "=== 6) 生效确认 ==="
python3 - "$CFG" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
print("  插件: input_debounce_ms=%s max_chars=%s inject=%s observe=%s" % (
    cfg.get("input_debounce_ms"), cfg.get("input_debounce_max_chars"),
    cfg.get("inject_enabled"), cfg.get("observe_mode")))
PY
python3 - <<'PY'
import json
with open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig") as handle:
    seg = json.load(handle)["platform_settings"]["segmented_reply"]
print("  分段: enable=%s regex=%r cleanup=%r threshold=%s" % (
    seg["enable"], seg["regex"], seg["content_cleanup_rule"], seg["words_count_threshold"]))
PY
