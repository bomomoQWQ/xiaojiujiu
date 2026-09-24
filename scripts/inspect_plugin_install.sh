#!/usr/bin/env bash
# 测试栈里的插件是 clone 还是拷贝？怎么更新？
set -u
P=/home/bomomo/astrbot_test/data/plugins/astrbot_plugin_companion_runtime
echo "=== 类型 ==="
ls -ld "$P"
echo "=== 是不是 git 仓库 ==="
if [ -d "$P/.git" ]; then
  cd "$P"
  git remote -v
  git log --oneline -2
  git status --short | head -10
else
  echo "(不是 git 仓库，是拷贝)"
  ls "$P" | head
  echo "--- protocol.py 里有没有 TransportUnavailable ---"
  grep -c "TransportUnavailable" "$P/companion_runtime/protocol.py" 2>/dev/null || echo 0
fi
echo
echo "=== 插件加载方式（容器内看到的路径） ==="
docker exec astrbot-test sh -c 'ls -la /AstrBot/data/plugins/'
