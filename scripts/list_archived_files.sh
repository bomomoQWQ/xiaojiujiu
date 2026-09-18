#!/usr/bin/env bash
# 看归档目录里每个实例的文件构成（含 JSONL 事件镜像与运行日志），说明"SQLite 之外还存了什么"。
set -u
docker run --rm -i -v astrbot_test_runtime-fleet-data:/data python:3.12-slim \
  python - <<'PY'
import glob
import os

roots = sorted(glob.glob("/data/_wiped-*"))
for root in roots:
    print("=== %s ===" % root)
    for person in sorted(glob.glob(os.path.join(root, "default-*"))):
        print("  %s" % os.path.basename(person).replace("default-friendmessage-", ""))
        for path in sorted(glob.glob(os.path.join(person, "*"))):
            size = os.path.getsize(path) / 1048576.0
            print("    %-26s %8.2f MB" % (os.path.basename(path), size))
    total = sum(os.path.getsize(p) for p in glob.glob(os.path.join(root, "**", "*"), recursive=True)
                if os.path.isfile(p))
    print("  合计 %.1f MB" % (total / 1048576.0))
PY
