#!/usr/bin/env bash
# 封测每日采集：导出 fleet 数据 -> 生成日报 -> 打印交付提醒。
#
# 为什么是这三步、为什么在这个钟点：
#   导出容器只读挂载 fleet 卷，任何时刻都能跑；但日报按 **UTC 日期** 前缀过滤
#   （events.created_at 是 UTC），而服务器是 CST=UTC+8。08:05 CST 正好是
#   UTC 00:05，此时"昨天(UTC)"刚刚走完，报表覆盖的是完整一天，不会缺末尾。
#
# 由 bomomo 的 crontab 调用，见 HANDOFF.md「每日流程」。
# 手跑：~/astrbot_test/beta_daily_collect.sh
set -u

# 日志里有中文；cron 与 ssh 的 locale 常常是 POSIX，python3 会按 ascii 写 stdout，
# 于是日志变乱码甚至 UnicodeEncodeError。显式钉死 UTF-8。
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONIOENCODING=utf-8

SRC="$HOME/astrbot_test/src/xiaojiujiu/scripts"
BETA="/mnt/xz/xiaojiujiu-beta"
VOLUME="astrbot_test_runtime-fleet-data"
FLEET="http://127.0.0.1:8800"
LOGDIR="$BETA/logs"
STAMP="$(date -u +%Y-%m-%d_%H%M)"

mkdir -p "$LOGDIR" "$BETA/reports"
LOG="$LOGDIR/${STAMP}.log"

say() { echo "[$(date '+%F %T %Z')] $*"; }

# 后面全部输出（含 docker / python 的 stdout）落进当天日志
exec >>"$LOG" 2>&1
say "=== 每日采集开始 (UTC $(date -u '+%F %T')) ==="

fail=0

# ---------- 1) 采集：容器里读卷，写机械盘 ----------
say "--- 1/3 导出 ---"
if docker run --rm \
    -v "${VOLUME}:/data:ro" \
    -v "${BETA}:/export" \
    -v "${SRC}:/scripts:ro" \
    python:3.12-slim python /scripts/export_beta_data.py --note "daily ${STAMP}"; then
  say "导出完成"
else
  say "!! 导出失败（docker run 非零退出）"
  fail=1
fi

# 最新批次目录名（形如 2026-09-16_0005）；没有就退回"今天(UTC)"
RUN="$(find "$BETA" -mindepth 1 -maxdepth 1 -type d -name '20*-*-*_*' -printf '%f\n' 2>/dev/null | sort | tail -1)"
if [ -z "$RUN" ]; then
  say "!! 找不到导出批次目录，日报跳过"
  fail=1
else
  say "最新批次: $RUN"
fi

# ---------- 2) 每日汇总：昨天(UTC) 那一整天 ----------
DAY="$(date -u -d 'yesterday' +%F)"
if [ -n "$RUN" ]; then
  say "--- 2/3 日报 ($DAY UTC) ---"
  if python3 "$SRC/beta_daily_report.py" \
      --export "$BETA" --run "$BETA/$RUN" --date "$DAY" \
      --fleet "$FLEET" --out "$BETA/reports"; then
    say "日报完成: $BETA/reports/$DAY.md"
  else
    say "!! 日报失败"
    fail=1
  fi
fi

# ---------- 3) 收尾：提醒 + 健康 ----------
say "--- 3/3 收尾 ---"
COUNT="$(curl -s --max-time 15 "$FLEET/fleet/status" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin).get("count","?"))' 2>/dev/null)"
[ -n "$COUNT" ] || COUNT="?"
say "fleet 实例数: $COUNT"
say "回放单个人: python3 $SRC/replay_session.py --export $BETA/$RUN/people/default-friendmessage-<QQ>"
say "看板: http://192.168.1.15:8800/fleet/dashboard"

# 磁盘余量：低于 20G 就在日志里喊一声（数据落在同一块机械盘上）
AVAIL_K="$(df -Pk /mnt/xz | awk 'NR==2{print $4}')"
if [ -n "${AVAIL_K:-}" ] && [ "$AVAIL_K" -lt 20971520 ]; then
  AVAIL_G=$((AVAIL_K / 1048576))
  say "!! /mnt/xz 可用空间不足 20G, 现在只剩 ${AVAIL_G}G"
fi

if [ "$fail" -ne 0 ]; then
  say "=== 每日采集结束：有失败 ==="
  exit 1
fi
say "=== 每日采集结束：OK ==="
