#!/usr/bin/env bash
# 查另外两个候选市场插件到底覆不覆盖私聊、以及能不能阻断该轮：
#   astrbot_plugin_word_filter      —— 屏蔽词过滤
#   astrbot_plugin_rate_limiter     —— 按人滑动窗口限次
set -u
cd /tmp

check() {
  name="$1"; url="$2"
  echo "=== $name ==="
  rm -rf "$name"
  if ! git clone --depth 1 -q "$url" "$name" 2>/dev/null; then
    echo "  克隆失败（可能仓库不可达）"
    return
  fi
  echo "  --- metadata desc ---"
  sed -n '1,14p' "$name/metadata.yaml" 2>/dev/null | sed 's/^/    /'
  echo "  --- 支持平台 / 会话类型 ---"
  grep -rnE "FRIEND_MESSAGE|GROUP_MESSAGE|get_group_id|sender_id|is_group|私聊|群聊" "$name"/*.py 2>/dev/null | head -12 | sed 's/^/    /'
  echo "  --- 有没有阻断（stop_event / 不调用模型）---"
  grep -rnE "stop_event|return True|继续|block|阻断|拦截" "$name"/*.py 2>/dev/null | head -10 | sed 's/^/    /'
  echo "  --- 钩子 ---"
  grep -rnE "@filter\.|@event\." "$name"/*.py 2>/dev/null | head -8 | sed 's/^/    /'
  echo
}

check word_filter https://github.com/yvdi-abc/astrbot_plugin_word_filter
check rate_limiter https://github.com/yuebai5203/astrbot_plugin_rate_limiter
