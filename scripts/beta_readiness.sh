#!/usr/bin/env bash
# 封测就绪度体检：负载量级（10 人会花多少调用）、健康度、遗留告警。
set -u
PORT=$(curl -s http://127.0.0.1:8800/fleet/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["people"][0]["port"])')
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' xxj-runtime-fleet)

echo "=== 1) 真人实例：账本按"是否真的调了 provider"分组 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob, json, os, sqlite3

path = glob.glob("/data/default-friendmessage-qq01/companion.sqlite3")[0]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
rows = con.execute(
    "select ran_at, reason, degraded, operations, settled_events, payload_json from refresh_runs"
    " order by ran_at"
).fetchall()
called = 0
by_reason: dict[str, int] = {}
for ran_at, reason, degraded, ops, settled, payload in rows:
    data = json.loads(payload or "{}")
    if data.get("provider_called"):
        called += 1
    by_reason[reason] = by_reason.get(reason, 0) + 1
print(f"  refresh_runs 总数: {len(rows)}")
print(f"  真的调用过 provider 的: {called}  (其余是本地就决定不花钱)")
print(f"  按原因: {by_reason}")
if rows:
    print(f"  时间跨度: {rows[0][0][:19]} .. {rows[-1][0][:19]}")
    span_h = None
    from datetime import datetime
    a = datetime.fromisoformat(rows[0][0])
    b = datetime.fromisoformat(rows[-1][0])
    span_h = (b - a).total_seconds() / 3600.0
    if span_h > 0:
        print(f"  跨度 {span_h:.2f}h ⇒ 折算 provider 调用 ≈ {called / span_h * 24:.1f}/天/人")
        print(f"  10 人一周 ⇒ ≈ {called / span_h * 24 * 70:.0f} 次调用")

print()
print("  == 语义/情绪 ==  ")
print("  semantics:", con.execute("select semantic_status, count(*) from event_semantics group by 1").fetchall())
for table in ("active_emotion_events", "emotion_explanations", "reappraisals", "memories",
              "interpretation_versions", "candidate_intents", "unfinished_matters"):
    print(f"  {table}:", con.execute(f"select count(*) from {table}").fetchone()[0])
print("  state:", con.execute(
    "select mood_valence, approach_impulse, restraint, version from runtime_state").fetchone())
print("  decisions:", con.execute("select count(*) from decisions").fetchone()[0])
print("  state_samples:", con.execute("select count(*) from state_samples").fetchone()[0])
PY

echo
echo "=== 2) provider 侧的累计调用统计 ==="
docker exec -i xxj-runtime-fleet python3 - "$PORT" "$IP" <<'PY'
import json, sys, urllib.request

port, ip = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=10) as resp:
    d = json.load(resp)
print("  semantic_provider:", json.dumps(d.get("semantic_provider"), ensure_ascii=False)[:400])
print("  deep_refresh:", d.get("deep_refresh"))
PY
echo "  (注意：上面 stats 是进程内的，fleet 重启后归零；账本的计数才是持久的)"

echo
echo "=== 3) 现在有没有告警 ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'xxj-|astrbot-test'
echo "--- AstrBot 近 10 分钟 error ---"
docker logs astrbot-test --since 10m 2>&1 | grep -ciE "error|traceback" || true
echo "--- 真人实例近 10 分钟 error ---"
docker exec xxj-runtime-fleet /bin/sh -c "tail -300 /data/logs/default-friendmessage-qq01.log | grep -ciE 'error|traceback|500' || true"

echo
echo "=== 4) 备份节奏：定时任务里有没有快照 ==="
crontab -l
echo "--- 现有快照 ---"
ls -1 /mnt/xz/xiaojiujiu-beta/snapshots/ 2>/dev/null | tail -5
echo "--- 导出批次 ---"
ls -1 /mnt/xz/xiaojiujiu-beta/ | tail -8
