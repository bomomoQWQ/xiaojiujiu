#!/usr/bin/env bash
# 定时任务是不是自己跑成功了（无人值守的第一次）。
set -u
echo "=== 采集日志 ==="
ls -la /mnt/xz/xiaojiujiu-beta/logs/
echo "--- 最新那份全文 ---"
LATEST=$(ls -t /mnt/xz/xiaojiujiu-beta/logs/*.log | head -1)
echo "file: $LATEST"
cat "$LATEST"

echo
echo "=== 日报文件与生成时间 ==="
ls -la --time-style=full-iso /mnt/xz/xiaojiujiu-beta/reports/
echo
echo "=== 日报内容（定时任务写的 2026-09-16）==="
cat /mnt/xz/xiaojiujiu-beta/reports/2026-09-16.md
