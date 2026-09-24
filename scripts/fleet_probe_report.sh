#!/usr/bin/env bash
# 看一眼最新日报：真人那段与"没人回头读"那段。
set -u
LATEST=$(ls -t /mnt/xz/xiaojiujiu-beta/reports/*.md | head -1)
echo "file: $LATEST"
python3 - "$LATEST" <<'PY'
import sys

text = open(sys.argv[1], encoding="utf-8").read()
blocks = text.split("\n### ")
for block in blocks:
    if block.startswith("default-friendmessage-qq01"):
        print("### " + block.strip())
    if block.startswith("default-friendmessage-20001"):
        print("### " + block.strip())
PY
