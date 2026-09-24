#!/usr/bin/env bash
# 多人开始聊天后的现场：名册、路由、隔离、活动、错误。
set -u

echo "=== 1) fleet 名册 ==="
curl -s http://127.0.0.1:8800/fleet/status -o /tmp/fs.json
python3 - <<'PY'
import json

data = json.load(open("/tmp/fs.json", encoding="utf-8"))
people = data.get("people", [])
print(f"  实例数: {data.get('count')}   healthy: {sum(1 for p in people if p['health'] == 'ok')}")
print(f"  {'person':42s} {'port':>5s} {'health':>9s} {'events':>7s} {'unres':>6s} {'matters':>7s} "
      f"{'refresh':>9s} {'degr':>5s} {'restarts':>8s}")
for p in people:
    print(f"  {p['person']:42s} {p['port']:5d} {str(p['health']):>9s} {str(p['raw_events']):>7s} "
          f"{str(p['unresolved']):>6s} {str(p['open_unfinished']):>7s} "
          f"{str(p['deep_refresh_attempts']) + '/' + str(p['deep_refresh_settled']):>9s} "
          f"{str(p['deep_refresh_degraded']):>5s} {str(p['restarts']):>8s}")
PY

echo
echo "=== 2) 路由表 ==="
curl -s http://127.0.0.1:8800/fleet/routes | python3 -m json.tool

echo
echo "=== 3) 隔离核对：每个人的库里是不是只有他自己的会话 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, os, sqlite3

for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    convs = con.execute(
        "select conversation_id, count(*) from raw_events group by 1 order by 2 desc"
    ).fetchall()
    users = con.execute(
        "select count(*) from raw_events where event_type='user_message' and content != ''"
    ).fetchone()[0]
    replies = con.execute(
        "select count(*) from raw_events where event_type='assistant_message'"
    ).fetchone()[0]
    print(f"  {person}: 会话={convs} 真人消息={users} 回复={replies}")
PY

echo
echo "=== 4) AstrBot 最近收到谁的消息（按会话聚合）==="
docker logs astrbot-test --since 30m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -oE '博馍馍/[0-9]+|[0-9]{6,}/[0-9]+' | sort | uniq -c | sort -rn | head -20

echo
echo "=== 5) 插件侧：开通/路由相关日志 ==="
docker logs astrbot-test --since 30m 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE "companion_runtime" | tail -20

echo
echo "=== 6) 错误 ==="
echo -n "  astrbot 近 15 分钟 error/traceback: "
docker logs astrbot-test --since 15m 2>&1 | grep -ciE "error|traceback" || true
echo -n "  各实例日志近 5 分钟 error: "
docker exec xxj-runtime-fleet /bin/sh -c "for f in /data/logs/*.log; do n=\$(tail -300 \"\$f\" | grep -ciE 'error|traceback| 500 ' || true); [ \"\$n\" != \"0\" ] && echo \"\$(basename \$f): \$n\"; done" || true

echo
echo "=== 7) 容器 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-|astrbot-test'
