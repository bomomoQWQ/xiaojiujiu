#!/usr/bin/env bash
# 上线准备 第 2 步：清理。
#   1 补快照缺口（root 拥有的 cmd_config.json、人格文件），并统一到同一个快照目录
#   2 停前端与舰队（避免清理期间有写入）
#   3 归档每个人的认知库（移到卷内 _wiped-<stamp>/，可回滚，不是删除）
#   4 清空 AstrBot 聊天记录（保留 personas 与全部配置）
#   5 重启舰队（会按 env 里的 yandere 画像新建空库）与 astrbot-test
#   6 验收：新库为空、画像正确、插件与拦截就位
set -u
STACK=/home/bomomo/astrbot_test
BETA=/mnt/xz/xiaojiujiu-beta
RUNTIME_SNAP=2026-09-18_124843          # snapshot_beta.py 写的那个（UTC 命名）
SNAP="$BETA/snapshots/$RUNTIME_SNAP"
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1) 补快照缺口并统一目录 ==="
mkdir -p "$SNAP"
docker exec -u 0 astrbot-test sh -c 'cat /AstrBot/data/cmd_config.json' > "$SNAP/astrbot_cmd_config.json" \
  && echo "  cmd_config.json -> $SNAP（$(wc -c < "$SNAP/astrbot_cmd_config.json") 字节）"
cp "$STACK/src/xiaojiujiu/人格设定.md" "$SNAP/人格设定.md" 2>/dev/null && echo "  人格设定.md -> $SNAP"
rm -rf "$BETA/snapshots/20260918-204842" && echo "  合并掉重复的 UTC/CST 目录"
ls -1 "$SNAP" | sed 's/^/    /' | head -12

echo
echo "=== 2) 停前端与舰队 ==="
docker stop xxj-onebot >/dev/null && echo "  xxj-onebot 已停（避免假用户写进新库）"
docker stop xxj-runtime-fleet >/dev/null && echo "  xxj-runtime-fleet 已停"

echo
echo "=== 3) 归档每个人的认知库 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data python:3.12-slim \
  python - "$STAMP" <<'PY'
import os
import shutil
import sys

stamp = sys.argv[1]
root = "/data"
archive = os.path.join(root, "_wiped-%s" % stamp)
os.makedirs(archive, exist_ok=True)
moved = []
for name in sorted(os.listdir(root)):
    path = os.path.join(root, name)
    if not os.path.isdir(path) or name.startswith("_"):
        continue
    shutil.move(path, os.path.join(archive, name))
    moved.append(name)
print("  已归档 %d 个实例目录 -> %s" % (len(moved), archive))
for name in moved:
    print("    %s" % name)
PY

echo
echo "=== 4) 清空 AstrBot 聊天记录（保留人格与配置）==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import sqlite3

path = "/AstrBot/data/data_v4.db"
con = sqlite3.connect(path, timeout=20)
con.execute("pragma busy_timeout = 20000")
before = con.execute("select count(*), coalesce(sum(json_array_length(content)),0) from conversations").fetchone()
with con:
    con.execute("delete from conversations")
    try:
        con.execute("delete from platform_message_history")
    except sqlite3.Error:
        pass
after = con.execute("select count(*) from conversations").fetchone()[0]
personas = con.execute("select count(*) from personas").fetchone()[0]
print("  会话 %d -> %d（消息 %s 条已清）；personas 保留 %d 行" % (before[0], after, before[1], personas))
con.close()
PY

echo
echo "=== 5) 重启舰队与 astrbot-test ==="
docker start xxj-runtime-fleet >/dev/null && echo "  舰队已起（会用 env 画像新建空库）"
for i in $(seq 1 40); do
  sleep 5
  line=$(curl -s --max-time 6 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('0/0'); raise SystemExit
p=d.get('people',[]); print('%d/%d'%(sum(1 for x in p if x.get('health')=='ok'),len(p)))
" 2>/dev/null || echo "0/0")
  case "$line" in 7/7|8/8) echo "  舰队 $line（$((i*5))s）"; break ;; esac
done
docker start astrbot-test >/dev/null && echo "  astrbot-test 已起"
for i in $(seq 1 20); do
  sleep 4
  docker logs --since 2m astrbot-test 2>&1 | grep -q "适配器已连接" && { echo "  适配器已连接"; break; }
  docker logs --since 2m astrbot-test 2>&1 | grep -q "Loading IM platform" && [ "$i" -gt 6 ] && echo "  平台已加载（等客户端）" && break
done

echo
echo "=== 6) 验收：新库为空 + 画像正确 + 插件就位 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import json
import os
import sqlite3
import urllib.request

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    events = con.execute("select count(*) from raw_events").fetchone()[0]
    memories = con.execute("select count(*) from memories").fetchone()[0]
    values = json.loads(con.execute("select values_json from runtime_state").fetchone()[0] or "{}")
    con.close()
    print("  %-14s 事件=%-4d 记忆=%-3d br=%-5s care=%-5s" % (
        tag, events, memories, values.get("boundary_respect"), values.get("user_care")))
PY
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json
import sqlite3
con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
print("  AstrBot 会话数=%d，人格=%s（%d 字）" % (
    con.execute("select count(*) from conversations").fetchone()[0],
    con.execute("select persona_id from personas").fetchone()[0],
    con.execute("select length(system_prompt) from personas").fetchone()[0]))
con.close()
m = json.load(open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig"))
print("  插件白名单=%s" % m.get("plugin_set"))
print("  web_search=%s  日志级别=%s" % (
    (m.get("provider_settings") or {}).get("web_search"), m.get("log_level")))
w = json.load(open("/AstrBot/data/config/astrbot_plugin_word_filter_config.json", encoding="utf-8"))
print("  屏蔽词=%s" % w.get("blocked_words"))
PY
