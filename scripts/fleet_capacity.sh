#!/usr/bin/env bash
# 容量巡检：还能再加多少人、瓶颈先出现在哪。
#
#   bash fleet_capacity.sh
#
# 判据（都是实测口径，不是猜的）：
#   · 每个 Runtime 进程 ~45MB RSS（空闲实例实测；聊得热闹的按 80MB 预算更稳）；
#   · 每人每天 ~1MB 日志 + 库增长（开了 logrotate 之后日志封顶 20MB×3 压缩）；
#   · 每人的 deep refresh 调用由 `unresolved_backlog_threshold` 与
#     `deep_refresh_min_interval_seconds` 决定，实测单次 3-4 秒、1-3k tokens；
#   · 端口槽位：8787 起按顺序分配、跳过控制面 8800，目前对外发布到 8829（43 个）。
set -u
CONTROL="${CONTROL:-http://127.0.0.1:8800}"

echo "=== 1) 舰队 ==="
python3 - "$CONTROL" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1] + "/fleet/status", timeout=8) as response:
    data = json.load(response)
people = data.get("people", [])
ports = sorted(p["port"] for p in people)
print("  实例 %d，health=ok %d，端口 %s..%s" % (
    len(people), sum(1 for p in people if p.get("health") == "ok"),
    ports[0] if ports else "-", ports[-1] if ports else "-"))
print("  端口槽位：已用 %d / 已发布 43（8787-8829）" % len(ports))
PY

echo
echo "=== 2) 资源占用 ==="
docker stats --no-stream --format '  {{.Name}}\tCPU {{.CPUPerc}}\tMEM {{.MemUsage}}' \
  xxj-runtime-fleet astrbot-test xxj-napcat-test 2>/dev/null
echo "  --- 宿主机 ---"
echo "  负载: $(cut -d' ' -f1-3 /proc/loadavg)   核数: $(nproc)"
free -h | sed -n '2,3p' | sed 's/^/  /'
df -h / /mnt/xz 2>/dev/null | tail -2 | sed 's/^/  /'

echo
echo "=== 3) 每个实例的平均内存 / 磁盘 ==="
docker exec -i xxj-runtime-fleet python3 - "$CONTROL" <<'PY'
import glob
import json
import os
import sys
import urllib.request

control = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
try:
    with urllib.request.urlopen(control + "/fleet/status", timeout=8) as response:
        people = json.load(response).get("people", [])
except Exception as exc:
    people = []
    print("  控制面读不到（%s），跳过进程内存" % exc)

# 用 /proc 而不是 ps：精简镜像里的 ps 参数支持不全，早期版本这里静默打成空。
sizes = []
for person in people:
    pid = person.get("pid")
    if not pid:
        continue
    try:
        with open("/proc/%s/statm" % pid) as handle:
            pages = int(handle.read().split()[1])
        sizes.append(pages * os.sysconf("SC_PAGE_SIZE") / 1024.0 / 1024.0)
    except OSError:
        continue
if sizes:
    average = sum(sizes) / len(sizes)
    print("  Runtime 进程 %d 个，RSS 平均 %.1f MB，合计 %.0f MB" % (len(sizes), average, sum(sizes)))
    print("  -> 再加 50 人约 +%.1f GB，再加 100 人约 +%.1f GB（按平均的两倍留余量）" % (
        50 * average * 2 / 1024, 100 * average * 2 / 1024))

databases = glob.glob("/data/*/companion.sqlite3")
total = sum(os.path.getsize(path) for path in databases) / 1024.0 / 1024.0
print("  库合计 %.0f MB（%d 个）" % (total, len(databases)))
PY

echo
echo "=== 4) LLM 用量（只有 deep refresh 花 token）==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import os
import sqlite3
from datetime import datetime, timedelta, timezone

calls = 0
recent = 0
latencies = []
cut = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    row = con.execute("select count(*), avg(json_extract(payload_json,'$.latency_ms')) "
                      "from refresh_runs where ran=1").fetchone()
    hour = con.execute("select count(*) from refresh_runs where ran=1 and ran_at >= ?",
                       (cut,)).fetchone()[0]
    con.close()
    calls += row[0] or 0
    recent += hour
    if row[1]:
        latencies.append(row[1])
print("  累计 provider 调用 %d 次（近 1 小时 %d 次）；平均延迟 %s ms" % (
    calls, recent, int(sum(latencies) / len(latencies)) if latencies else "-"))
print("  触发条件：unresolved 积压 >= %s 或空闲 >= %sh，且距上次 >= %ss" % (
    os.environ.get("CR_SEMANTIC__UNRESOLVED_BACKLOG_THRESHOLD", "4"),
    os.environ.get("CR_SEMANTIC__DEEP_REFRESH_IDLE_HOURS", "1"),
    os.environ.get("CR_SEMANTIC__DEEP_REFRESH_MIN_INTERVAL_SECONDS", "900")))
print("  不做线性外推：调用量由用户活动决定（有人说话 -> 积压到阈值 -> 才刷一次），")
print("  每人每天量级是「个位数」，50 人约几十次/天，单次 1-3k tokens。")
PY

echo
echo "=== 5) 日志体积与轮转 ==="
docker exec xxj-runtime-fleet sh -c 'du -sh /data/logs 2>/dev/null; ls -1 /data/logs | wc -l' \
  | sed 's/^/  /'
# 注意：不要再往 /etc 里挂载东西 —— 那会把容器的 /etc/resolv.conf 挂坏（read-only file system）。
docker run --rm -v /etc/logrotate.d/xxj-fleet:/tmp/xxj-fleet:ro alpine \
  sh -c 'test -f /tmp/xxj-fleet && echo "  logrotate 配置在位 ✓" || echo "  ⚠️ 没有 logrotate 配置"'

echo
echo "=== 6) 建议 ==="
cat <<'TXT'
  · 单账号风险：所有测试者共用同一个 QQ（苏清徽）。主动消息上限是**每人**每天 12 条
    （drive.max_contacts_per_day）、冷却 40 分钟（drive.cooldown_seconds）——
    50 人时理论上是 600 条/天，这是会被 QQ 盯上的量级。扩容前建议：
      CR_DRIVE__MAX_CONTACTS_PER_DAY: 4
      CR_DRIVE__COOLDOWN_SECONDS: 5400
    并把「全局发送预算」加到插件侧（按小时封顶，reply 风暴也一起挡）。
  · 内存：这台机器只有 15.5GB 且已经在用 swap（别的服务占了 5.6GB）。
    再加 50 人（+2~4GB）可行但要把余量盯住；上百人建议换机器或让 fleet 单独一台。
  · 磁盘：/ 现在 35GB 可用；构建缓存已清（回收 8.9GB）。库+日志每人约 6MB/天量级。
TXT
