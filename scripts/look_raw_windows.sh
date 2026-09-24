#!/usr/bin/env bash
# 看失败窗口前后 NapCat 与 AstrBot 的原始日志（不过滤，直接看上下文）。
set -u
for win in "14:1" "16:3" "17:5"; do
  echo "===== NapCat 原始日志 @$win ====="
  docker logs xxj-napcat-test --since 12h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
    | grep -E "^0?9?-17 $win|09-17 $win" | grep -viE "EGL|gpu|GLContext|viz_main|NativeCrash" | head -12
done

echo
echo "===== AstrBot 原始日志 @14:1x 与 @16:3x（全部行，看有没有断连告警）====="
docker logs astrbot-test --since 12h 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "\[1[46]:[0-9]{2}:[0-9]{2}" | grep -viE "event_bus:74|Prepare to send" | head -25

echo
echo "===== 现在这一刻：平台连接是否正常（发一条测试消息给测试前端，不碰真人）====="
echo "  （改用只读方式：看 AstrBot 最近的连接日志）"
docker logs astrbot-test --since 30m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -12
