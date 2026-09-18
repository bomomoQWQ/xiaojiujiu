#!/usr/bin/env bash
# 部署三项改动并验证：
#   C 凌晨重罚（runtime 代码）  -> 容器内直接调用 silence_utility 验算
#   A 事项复述守卫（runtime）    -> 容器内直接调用 ground_suggestions 验算
#   B 防抖搬到锁前（插件）       -> 连发两条，期望只产生一轮 LLM（trace 里 1 条 astr_agent_prepare）
set -u

REPO=/home/bomomo/astrbot_test/src/xiaojiujiu
PLUGIN=/home/bomomo/astrbot_test/data/plugins/astrbot_plugin_companion_runtime
STACK=/home/bomomo/astrbot_test

echo "=== 1) 拉取两个仓库 ==="
cd "$REPO" && git pull --ff-only 2>&1 | tail -1
cd "$PLUGIN" && git pull --ff-only 2>&1 | tail -1

echo
echo "=== 2) 重建 runtime 镜像并重启舰队 ==="
cd "$REPO"
docker build -q -t xiaojiujiu-runtime:test . 2>&1 | tail -1
cd "$STACK"
docker compose -p astrbot_test -f astrbot.yml -f fleet.yml up -d --force-recreate runtime-fleet 2>&1 | tail -2
for i in $(seq 1 40); do
  sleep 5
  line=$(curl -s --max-time 6 http://127.0.0.1:8800/fleet/status | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('0/0'); raise SystemExit
people = d.get('people', [])
print('%d/%d' % (sum(1 for p in people if p.get('health') == 'ok'), len(people)))
" 2>/dev/null || echo "0/0")
  case "$line" in
    8/8) echo "  舰队 8/8 health=ok（$((i*5))s）"; break ;;
    *) [ $((i % 4)) -eq 0 ] && echo "    t=$((i*5))s $line" ;;
  esac
done

echo
echo "=== 3) 验证 C：容器内直接算凌晨的沉默效用 ==="
docker exec xxj-runtime-fleet python3 -c "
import datetime as dt, sys
sys.path.insert(0, '/app/runtime/src')
from companion_runtime.config import RuntimeConfig
from companion_runtime.motivation import silence_utility
from companion_runtime.typing import RuntimeState
cfg = RuntimeConfig()
print('  night_penalty=%s window=%s..%s' % (cfg.scheduler.night_penalty, cfg.scheduler.night_start_hour, cfg.scheduler.night_end_hour))
state = RuntimeState(); state.approach_impulse = 0.6; state.restraint = 0.5
tz = dt.datetime.now().astimezone().tzinfo
vals = {}
for hour in (15, 3):
    now = dt.datetime(2026, 3, 1, hour, 30, tzinfo=tz)
    vals[hour] = silence_utility(state=state, config=cfg, boundary_risk=0.0,
                                 cooldown_active=False, hours_since_contact=6.0, now=now)
print('  15:30 -> %.4f    03:30 -> %.4f    差 %.4f' % (vals[15], vals[3], vals[3] - vals[15]))
print('  期望差值 = %.4f  -> %s' % (cfg.scheduler.night_penalty,
      'OK' if abs((vals[3] - vals[15]) - cfg.scheduler.night_penalty) < 1e-9 else 'MISMATCH'))
"

echo
echo "=== 4) 验证 A：容器内直接跑 grounding 守卫 ==="
docker exec xxj-runtime-fleet python3 -c "
import sys
sys.path.insert(0, '/app/runtime/src')
from companion_runtime import deep_refresh as dr
payload = {'unfinished_matter_suggestions': [
    {'title': '复述旧事', 'sources': ['unf_1']},
    {'title': '有事件支撑', 'sources': ['unf_1', 'evt_9']},
]}
ops, violations = dr.ground_suggestions(payload, resolvable=lambda _i: True,
                                        is_matter=lambda i: i == 'unf_1')
print('  通过的操作: %s' % [o.payload.get('title') for o in ops])
print('  违规: %s' % violations)
print('  期望: 只剩有事件支撑 + 一条 matter_restatement -> %s' % (
    'OK' if len(ops) == 1 and violations and violations[0]['reason'] == 'matter_restatement' else 'MISMATCH'))
"

echo
echo "=== 5) 重启 astrbot-test 载入新插件代码 ==="
before=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
docker restart astrbot-test >/dev/null
for i in $(seq 1 20); do
  sleep 4
  now=$(docker logs astrbot-test 2>&1 | grep -c "适配器已连接" || true)
  [ "$now" -gt "$before" ] && { echo "  适配器已连接（$((i*4))s）"; break; }
done

echo
echo "=== 6) 验证 B：1 秒内连发两条，期望只产生一轮 LLM ==="
PREPARE_BEFORE=$(docker exec astrbot-test sh -c 'grep -c astr_agent_prepare /AstrBot/data/logs/astrbot.trace.log' || echo 0)
docker exec -i xxj-onebot python3 - <<'PY'
import json
import time
import urllib.request

def send(text):
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request("http://127.0.0.1:6300/send", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=10).status

print("  第一条 ->", send("连发测试第一条"))
time.sleep(1.0)
print("  第二条 ->", send("连发测试第二条"))
PY
sleep 25
PREPARE_AFTER=$(docker exec astrbot-test sh -c 'grep -c astr_agent_prepare /AstrBot/data/logs/astrbot.trace.log' || echo 0)
echo "  astr_agent_prepare: $PREPARE_BEFORE -> $PREPARE_AFTER（期望 +1，即只跑了一轮）"
docker logs --since 1m astrbot-test 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -aE "Prepare to send" | tail -3

echo
echo "=== 7) Runtime 侧：两条都被观察到，但只有一条回复 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import sqlite3

cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
          - dt.timedelta(minutes=4)).isoformat()
con = sqlite3.connect("file:/data/default-friendmessage-20001/companion.sqlite3?mode=ro", uri=True)
rows = con.execute(
    "select event_type, timestamp, substr(content,1,70) from raw_events"
    " where timestamp > ? order by timestamp desc limit 6", (cutoff,)).fetchall()
con.close()
for event_type, stamp, content in rows:
    print("    %s %-18s %s" % (str(stamp)[:19], event_type, content.replace(chr(10), " ⏎ ")))
PY
