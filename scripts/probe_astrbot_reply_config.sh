#!/usr/bin/env bash
# 摸清 astrbot-test 的回复分段/防抖现状，并在插件市场索引里搜防抖插件。
set -u

echo "=== 主配置 platform_settings ==="
docker exec astrbot-test python3 /tmp/_dump_cfg.py

echo
echo "=== 插件市场索引里含 防抖/合并/debounce 的条目 ==="
docker exec astrbot-test python3 /tmp/_search_market.py
