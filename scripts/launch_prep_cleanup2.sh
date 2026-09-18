#!/usr/bin/env bash
# 清理 v2：修掉 v1 的两个 bug
#   - docker run 少了 -i，heredoc 没进容器 -> 归档没执行
#   - 快照目录是 root 建的，bomomo 写不进去 -> 补快照也要在容器里以 root 身份写
set -u
STACK=/home/bomomo/astrbot_test
BETA=/mnt/xz/xiaojiujiu-beta
SNAP="$BETA/snapshots/2026-09-18_124843"
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 1) 停舰队（清理期间不应有写入）==="
docker stop xxj-onebot >/dev/null 2>&1 || true
docker stop xxj-runtime-fleet >/dev/null && echo "  舰队已停"

echo
echo "=== 2) 归档每个人的认知库（docker run -i 这次带上）==="
docker run --rm -i -v astrbot_test_runtime-fleet-data:/data python:3.12-slim \
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
print("  归档后 /data 下剩余: %s" % sorted(os.listdir(root)))
PY

echo
echo "=== 3) 补齐快照（以 root 写进 root 拥有的快照目录）==="
docker run --rm -i \
  -v astrbot_test_data:/astrbot-data:ro \
  -v "$BETA":/export \
  -v "$STACK/src/xiaojiujiu":/repo:ro \
  python:3.12-slim \
  python - "$SNAP" <<'PY'
import shutil
import sys
from pathlib import Path

snap = Path(sys.argv[1])
copies = [
    (Path("/astrbot-data/cmd_config.json"), snap / "astrbot_cmd_config.json"),
    (Path("/astrbot-data/data_v4.db"), snap / "astrbot_data_v4.db"),
    (Path("/repo/人格设定.md"), snap / "人格设定.md"),
]
for source, target in copies:
    if not source.exists():
        print("  跳过（源不存在）: %s" % source)
        continue
    shutil.copy2(source, target)
    print("  %s -> %s（%d 字节）" % (source.name, target.name, target.stat().st_size))
print("  快照目录内容: %s" % sorted(p.name for p in snap.iterdir()))
PY

echo
echo "=== 4) 启动舰队（应新建空库，并按 env 用 yandere 画像）==="
docker start xxj-runtime-fleet >/dev/null && echo "  舰队已起"
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
curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('people', []):
    print('    %-36s port=%s %s' % (p.get('person'), p.get('port'), p.get('health')))
"

echo
echo "=== 5) 验收：库是空的、画像是 yandere ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import json
import os
import sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    events = con.execute("select count(*) from raw_events").fetchone()[0]
    memories = con.execute("select count(*) from memories").fetchone()[0]
    values = json.loads(con.execute("select values_json from runtime_state").fetchone()[0] or "{}")
    con.close()
    print("  %-14s 事件=%-4d 记忆=%-3d br=%-5s care=%-5s cd=%-5s" % (
        tag, events, memories, values.get("boundary_respect"),
        values.get("user_care"), values.get("conflict_directness")))
PY

echo
echo "=== 6) 假用户实例若被重建，就撤掉 ==="
curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('people', []):
    if '20001' in str(p.get('person')):
        print('  仍在: %s port=%s' % (p.get('person'), p.get('port')))
" 
PORT=$(curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(next((p['port'] for p in d.get('people',[]) if '20001' in str(p.get('person'))), ''))
")
if [ -n "$PORT" ]; then
  echo "  撤掉假用户实例（port $PORT）"
  curl -s -X POST "http://127.0.0.1:8800/fleet/deprovision/$PORT" | head -c 200; echo
else
  echo "  假用户实例已不在舰队里 ✓"
fi
