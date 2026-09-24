#!/usr/bin/env bash
# 部署前端 lifecycle 修复 + 恢复 astrbot 的日志级别 + 验证链路打通。
#
# 顺序讲究：修复要先落地（framework 是宿主机挂载进前端的目录），
# 再重启 astrbot（日志级别要重启才生效，且重启会踢掉当前连接），
# 最后重启前端 —— 让它用新代码连上并"报到"。
set -u

echo "=== 1) 拉取修复（framework 是挂载目录，不需要重建镜像）==="
cd /home/bomomo/astrbot_test/src/xiaojiujiu
git pull --ff-only 2>&1 | tail -2
grep -n "send_meta_event(\"connect\")" framework/cf/onebot.py | head -2

echo
echo "=== 2) 恢复 astrbot 的日志级别为 INFO 并重启 ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json

path = "/AstrBot/data/cmd_config.json"
with open(path, encoding="utf-8-sig") as handle:
    cfg = json.load(handle)
print("  之前 log_level =", cfg.get("log_level"))
cfg["log_level"] = "INFO"
with open(path, "w", encoding="utf-8-sig") as handle:
    json.dump(cfg, handle, ensure_ascii=False, indent=2)
with open(path, encoding="utf-8-sig") as handle:
    print("  现在 log_level =", json.load(handle).get("log_level"))
PY
docker restart astrbot-test >/dev/null && echo "  astrbot-test 已重启"

echo
echo "=== 3) 等 AstrBot 把平台加载起来（看到 Loading IM platform）==="
for i in $(seq 1 20); do
  sleep 3
  if docker logs --since 2m astrbot-test 2>&1 | grep -q "Loading IM platform adapter"; then
    echo "  platform loaded（$((i*3))s）"
    break
  fi
done

echo
echo "=== 4) 重启前端（此时它跑的是修好的代码）==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart xxj-onebot >/dev/null && echo "  已重启（连接行数基线 $before）"

echo
echo "=== 5) 等前端报到（AstrBot 应打印「适配器已连接」）==="
for i in $(seq 1 30); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  if [ "$now" -gt "$before" ]; then
    echo "  ✅ 适配器已连接（$((i*4))s）：$before -> $now"
    break
  fi
done
docker logs --since 3m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -E "适配器已连接|adapter started" | tail -3

echo
echo "=== 6) 前端还在报错吗 ==="
docker logs --tail 6 xxj-onebot 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g'

echo
echo "=== 7) 前端有没有真的在收发（最近 4 帧）==="
tail -4 /home/bomomo/astrbot_test/frontend-logs/onebot.jsonl 2>/dev/null \
  | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    payload = d.get('payload') or {}
    print('  %-10s %-10s %s' % (d.get('direction'), d.get('kind'),
                                (payload.get('action') or payload.get('post_type') or payload.get('sub_type') or '')[:30]))
"
