#!/usr/bin/env bash
# 把 astrbot-test 的日志级别临时调到 DEBUG，重启，看前端那条连接为什么被关。
# 结束时会说明怎么恢复（或保留 DEBUG 给调试用）。
set -u

CFG=/AstrBot/data/cmd_config.json

echo "=== 1) 备份并把 log_level 改成 DEBUG ==="
docker exec -u 0 -i astrbot-test python3 - "$CFG" <<'PY'
import json
import shutil
import sys
import datetime as dt

path = sys.argv[1]
stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(path, path + ".bak-log-" + stamp)
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
print("  原本 log_level =", cfg.get("log_level"))
cfg["log_level"] = "DEBUG"
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
with open(path, encoding="utf-8-sig") as handle:
    print("  现在 log_level =", json.load(handle).get("log_level"))
PY

echo
echo "=== 2) 重启 ==="
docker restart astrbot-test >/dev/null && echo "  已重启"

echo
echo "=== 3) 等 60 秒，让前端尝试连（不探测）==="
sleep 60

echo
echo "=== 4) DEBUG 日志里与 WS/握手/关闭 有关的行 ==="
docker logs --since 3m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE "websocket|ws_reverse|aiocqhttp|handshake|x-self|lifecycle|close|reject|token|401|403" \
  | tail -40

echo
echo "=== 5) 适配器连接行数 ==="
docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true

echo
echo "=== 6) 前端最近 5 行 ==="
docker logs --tail 5 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'
