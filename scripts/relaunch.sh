#!/usr/bin/env bash
# 上线 runbook（可重复跑；每步都先检查再动作）。
#
#   bash relaunch.sh          # 完整上线：舰队 -> NapCat(扫码) -> AstrBot -> 验证
#   bash relaunch.sh check    # 只看当前状态
#
# ⚠️ 三条必须记住的：
#   1) **不要起 xxj-onebot（模拟前端）**：真实 QQ 与模拟前端同时连着 AstrBot 时，
#      主动发送会因为"两个 OneBot 客户端"而失败（历史上整整一天没送出一条主动消息）。
#   2) NapCat 重启会掉 QQ 登录态 -> 需要扫码：二维码在容器里 /app/napcat/cache/qrcode.png。
#   3) AstrBot 起来后要等日志出现「适配器已连接」才让人发消息 —— 那之前的消息会**静默丢失**。
set -u

STACK=/home/bomomo/astrbot_test
cd "$STACK" || exit 1

status() {
  echo "=== 当前状态 ==="
  docker ps -a --format '{{.Names}}\t{{.Status}}' \
    | grep -Ei "astrbot-test|xxj-runtime|xxj-onebot|xxj-napcat" || true
  echo "  --- 舰队 ---"
  curl -s --max-time 6 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('  控制面不可达'); raise SystemExit
p=d.get('people',[])
print('  实例 %d，health=ok %d' % (len(p), sum(1 for x in p if x.get('health')=='ok')))
for x in p: print('    %-36s port=%s %s' % (x.get('person'), x.get('port'), x.get('health')))
" 2>/dev/null || echo "  舰队未运行"
  echo "  --- AstrBot ---"
  docker logs --tail 300 astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
    | grep -aE "适配器已连接|Loading IM platform" | tail -2 || echo "  （无日志）"
}

if [ "${1:-full}" = "check" ]; then
  status
  exit 0
fi

echo "=== 1) Runtime 舰队 ==="
if docker ps --format '{{.Names}}' | grep -q '^xxj-runtime-fleet$'; then
  echo "  已在运行"
else
  docker start xxj-runtime-fleet >/dev/null && echo "  已启动"
fi
for i in $(seq 1 40); do
  sleep 5
  line=$(curl -s --max-time 6 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('0/0'); raise SystemExit
p=d.get('people',[]); print('%d/%d'%(sum(1 for x in p if x.get('health')=='ok'),len(p)))
" 2>/dev/null || echo "0/0")
  case "$line" in 7/7|8/8) echo "  舰队 $line（$((i*5))s）"; break ;; esac
done

echo
echo "=== 2) 模拟前端必须保持停止 ==="
if docker ps --format '{{.Names}}' | grep -q '^xxj-onebot$'; then
  echo "  ⚠️ 检测到 xxj-onebot 在运行 —— 停掉它（两个 OneBot 客户端会让主动发送失败）"
  docker stop xxj-onebot >/dev/null && echo "  已停"
else
  echo "  已停止 ✓"
fi

echo
echo "=== 3) NapCat（会掉登录态，需要扫码）==="
if docker ps --format '{{.Names}}' | grep -q '^xxj-napcat-test$'; then
  echo "  已在运行"
else
  docker start xxj-napcat-test >/dev/null && echo "  已启动"
fi
echo "  等二维码生成（20 秒）…"
sleep 20
if docker exec xxj-napcat-test sh -c 'test -f /app/napcat/cache/qrcode.png' 2>/dev/null; then
  docker cp xxj-napcat-test:/app/napcat/cache/qrcode.png "$STACK/qrcode.png" 2>/dev/null \
    && echo "  ✅ 二维码已复制到 $STACK/qrcode.png —— 现在扫码"
else
  echo "  没有二维码文件（可能已登录）"
fi
echo "  等登录成功（最多 150 秒；扫完码会自动继续）…"
for i in $(seq 1 30); do
  sleep 5
  if docker logs --tail 80 xxj-napcat-test 2>&1 | grep -aqE "登录成功|已登录|账号.*上线"; then
    echo "  ✅ NapCat 登录成功（$((i*5))s）"
    break
  fi
done

echo
echo "=== 4) AstrBot ==="
if docker ps --format '{{.Names}}' | grep -q '^astrbot-test$'; then
  echo "  已在运行"
else
  docker start astrbot-test >/dev/null && echo "  已启动"
fi
echo "  等适配器连接（NapCat 断线重连最长约 30 秒）…"
for i in $(seq 1 30); do
  sleep 5
  if docker logs --since 3m astrbot-test 2>&1 | grep -aq "适配器已连接"; then
    echo "  ✅ 适配器已连接（$((i*5))s）"
    break
  fi
done

echo
echo "=== 5) 上线前自检 ==="
docker exec -u 0 -i astrbot-test python3 - <<'PY'
import json
import sqlite3

con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
print("  会话数=%d（应为 0：记忆已清）  人格=%s（%d 字）" % (
    con.execute("select count(*) from conversations").fetchone()[0],
    con.execute("select persona_id from personas").fetchone()[0],
    con.execute("select length(system_prompt) from personas").fetchone()[0]))
print("  人格 skills=%r（应为 []）" % (con.execute("select skills from personas").fetchone()[0],))
con.close()
cfg = json.load(open("/AstrBot/data/cmd_config.json", encoding="utf-8-sig"))
ps = cfg.get("provider_settings", {}) or {}
print("  web_search=%s  日志级别=%s" % (ps.get("web_search"), cfg.get("log_level")))
print("  插件白名单=%s" % (cfg.get("plugin_set"),))
w = json.load(open("/AstrBot/data/config/astrbot_plugin_word_filter_config.json", encoding="utf-8"))
print("  屏蔽词=%s" % (w.get("blocked_words"),))
PY
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import json
import os
import sqlite3

empty = 0
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    events = con.execute("select count(*) from raw_events").fetchone()[0]
    values = json.loads(con.execute("select values_json from runtime_state").fetchone()[0] or "{}")
    con.close()
    if events == 0:
        empty += 1
print("  认知库为空且画像正确: %d 个（br=%s care=%s）" % (
    empty, values.get("boundary_respect"), values.get("user_care")))
PY

echo
status
echo
echo "✅ 自检完成。让她收到第一条真人消息后，用「bash relaunch.sh check」再确认一轮。"
