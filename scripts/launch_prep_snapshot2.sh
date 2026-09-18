#!/usr/bin/env bash
# 清理 v3：把 AstrBot 侧的东西正确快照下来（容器里看到的是 /export，不是宿主路径）。
set -u
STACK=/home/bomomo/astrbot_test
BETA=/mnt/xz/xiaojiujiu-beta
SNAP_HOST="$BETA/snapshots/2026-09-18_124843"

docker run --rm -i \
  -v "$STACK/data":/astrbot-data:ro \
  -v "$BETA":/export \
  -v "$STACK/src/xiaojiujiu":/repo:ro \
  -v "$STACK":/stack:ro \
  python:3.12-slim \
  python - <<'PY'
import shutil
from pathlib import Path

snap = Path("/export/snapshots/2026-09-18_124843")
print("  快照目录存在: %s" % snap.is_dir())
copies = [
    ("/astrbot-data/data_v4.db", "astrbot_data_v4.db"),
    ("/astrbot-data/cmd_config.json", "astrbot_cmd_config.json"),
    ("/astrbot-data/config/astrbot_plugin_companion_runtime_config.json",
     "plugin_companion_runtime_config.json"),
    ("/astrbot-data/config/astrbot_plugin_word_filter_config.json",
     "plugin_word_filter_config.json"),
    ("/stack/astrbot.yml", "astrbot.yml"),
    ("/stack/fleet.yml", "fleet.yml"),
    ("/repo/人格设定.md", "人格设定.md"),
]
for source, name in copies:
    path = Path(source)
    if not path.exists():
        print("  跳过（源不存在）: %s" % source)
        continue
    target = snap / name
    shutil.copy2(path, target)
    print("  %-40s -> %s（%d 字节）" % (path.name, name, target.stat().st_size))
print()
print("  快照目录内容:")
for item in sorted(snap.rglob("*")):
    if item.is_file():
        print("    %-72s %d" % (str(item.relative_to(snap)), item.stat().st_size))
PY

echo
echo "=== 归档目录（可回滚）==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data python:3.12-slim \
  sh -c 'ls -1 /data; echo "--- 归档内容 ---"; ls -1 /data/_wiped-*/ | head -12; du -sh /data/_wiped-*'
