#!/usr/bin/env bash
# 扫码之后：NapCat 登录了吗、适配器连回来了吗、fleet 侧一切正常吗。
set -u

echo "=== 1) NapCat 登录状态 ==="
docker logs xxj-napcat-test --since 20m 2>&1 | grep -iE "登录成功|已登录|login|在线|二维码|离线|失败" | tail -8
echo "--- 还有没有二维码在刷（有输出说明仍未登录）---"
docker exec xxj-napcat-test /bin/ls -la --time-style=+%H:%M:%S /app/napcat/cache/qrcode.png 2>/dev/null || echo "  (没有二维码文件了)"

echo
echo "=== 2) AstrBot 适配器 ==="
docker logs astrbot-test --since 20m 2>&1 | grep -iE "适配器已连接|aiocqhttp" | tail -5

echo
echo "=== 3) fleet 状态 ==="
curl -s http://127.0.0.1:8800/fleet/status -o /tmp/fs.json
python3 - <<'PY'
import json

data = json.load(open("/tmp/fs.json", encoding="utf-8"))
print("  count:", data.get("count"))
for p in data.get("people", []):
    print("  ", p["person"], "| port", p["port"], "| health", p["health"],
          "| events", p["raw_events"], "| unresolved", p["unresolved"],
          "| refresh", p["deep_refresh_attempts"], "/", p["deep_refresh_settled"],
          "| degraded", p["deep_refresh_degraded"],
          "| last", p["last_refresh_reason"], "| restarts", p["restarts"])
PY

echo
echo "=== 4) 容器健康 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-|astrbot-test'

echo
echo "=== 5) 真人实例的事件与判决（最近）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, sqlite3

path = glob.glob("/data/default-friendmessage-1670681411/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
print("  events:", con.execute("select count(*) from raw_events").fetchone()[0])
print("  decisions:", con.execute("select count(*) from decisions").fetchone()[0])
print("  samples:", con.execute("select count(*) from state_samples").fetchone()[0])
print("  最近 3 条事件:")
for row in con.execute(
    "select created_at, event_type, substr(coalesce(content,''),1,40) from raw_events"
    " order by created_at desc limit 3"
):
    print("   ", str(row[0])[:19], row[1], row[2])
print("  最近 2 条判决:")
for row in con.execute(
    "select decided_at, acted, reason from decisions order by decided_at desc limit 2"
):
    print("   ", str(row[0])[:19], "acted=" + str(row[1]), row[2])
PY

echo
echo "=== 6) 错误计数 ==="
echo -n "  astrbot 近 10 分钟 error/traceback: "
docker logs astrbot-test --since 10m 2>&1 | grep -ciE "error|traceback" || true
echo -n "  napcat 近 10 分钟 error: "
docker logs xxj-napcat-test --since 10m 2>&1 | grep -ciE "error" || true
