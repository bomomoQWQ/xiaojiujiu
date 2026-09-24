#!/usr/bin/env bash
# 上线准备 第 1 步：快照 + 清理清单（只读 + 归档，不删任何东西）。
set -u
STACK=/home/bomomo/astrbot_test
BETA=/mnt/xz/xiaojiujiu-beta
STAMP=$(date +%Y%m%d-%H%M%S)
DEST="$BETA/snapshots/$STAMP"

echo "=== 1) 快照 Runtime 全部数据（含 8 个实例 + 舰队状态）==="
mkdir -p "$DEST"
docker run --rm -v astrbot_test_runtime-fleet-data:/data \
  -v "$BETA":/export -v "$STACK/src/xiaojiujiu/scripts":/scripts:ro \
  python:3.12-slim \
  python /scripts/snapshot_beta.py --data-root /data --root /export/snapshots \
  --fleet http://192.168.1.15:8800 --note "before launch cleanup" 2>&1 | tail -3

echo
echo "=== 2) 快照 AstrBot 数据与配置 ==="
cp "$STACK/data/data_v4.db" "$DEST/astrbot_data_v4.db" 2>/dev/null \
  && echo "  data_v4.db -> $DEST" || echo "  data_v4.db 复制失败（权限？）"
cp "$STACK/data/cmd_config.json" "$DEST/astrbot_cmd_config.json" 2>/dev/null \
  && echo "  cmd_config.json -> $DEST" || echo "  cmd_config.json 复制失败"
cp "$STACK/data/config/astrbot_plugin_companion_runtime_config.json" "$DEST/" 2>/dev/null \
  && echo "  插件配置 -> $DEST" || true
cp "$STACK/data/config/astrbot_plugin_word_filter_config.json" "$DEST/" 2>/dev/null \
  && echo "  word_filter 配置 -> $DEST" || true
cp "$STACK/人格设定.md" "$DEST/" 2>/dev/null || cp "$STACK/src/xiaojiujiu/人格设定.md" "$DEST/" 2>/dev/null || true
cp "$STACK/astrbot.yml" "$STACK/fleet.yml" "$DEST/" 2>/dev/null || true
du -sh "$DEST" 2>/dev/null | sed 's/^/  快照体积: /'

echo
echo "=== 3) 清理清单（将被清掉的东西）==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import sqlite3
con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
print("  --- AstrBot conversations（聊天记录，将清空）---")
for cid, count, updated in con.execute(
        "select conversation_id, json_array_length(content), updated_at from conversations"
        " order by updated_at desc limit 8"):
    print("    %s  %-5s 条  %s" % (cid[:12], count, str(updated)[:19]))
total = con.execute("select count(*), sum(json_array_length(content)) from conversations").fetchone()
print("    合计 %d 个会话 / %s 条消息" % (total[0], total[1]))
print("  --- 将保留 ---")
for table in ("personas",):
    rows = con.execute("select count(*) from %s" % table).fetchone()[0]
    print("    %s: %d 行（人格设定，不清）" % (table, rows))
con.close()
PY

docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3

print("  --- Runtime 认知库（将整体重置）---")
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    counts = {}
    for name in ("raw_events", "memories", "unfinished_matters", "candidate_intents",
                 "active_emotion_events", "boundaries"):
        try:
            counts[name] = con.execute("select count(*) from %s" % name).fetchone()[0]
        except sqlite3.Error:
            counts[name] = "-"
    con.close()
    print("    %-34s %s" % (tag, counts))
PY

echo
echo "  快照路径: $DEST"
