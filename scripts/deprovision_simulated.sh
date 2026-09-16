#!/usr/bin/env bash
# 摘掉 13 个模拟实例，只留真人 1670681411。
#
# 先冻结一份（deprovision 本身保留数据，但 people.json 会变，留个前后对照），
# 再逐个 POST /fleet/deprovision/<slug>，最后核对名册/路由/健康/插件是否跟上。
set -u
BETA=/mnt/xz/xiaojiujiu-beta
SRC=/home/bomomo/astrbot_test/src/xiaojiujiu/scripts

echo "=== 0) 摘之前的家底 ==="
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  instances:", d["count"])
for p in d["people"]:
    print("   ", p["person"], "port", p["port"], "events", p["raw_events"])
'

echo
echo "=== 1) 冻结快照 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data -v "$BETA":/export \
  -v "$SRC":/scripts:ro python:3.12-slim \
  python /scripts/snapshot_beta.py --data-root /data --root /export/snapshots \
  --fleet http://192.168.1.15:8800 --note "before deprovisioning the 13 simulated instances" 2>&1 | tail -3

echo
echo "=== 2) 逐个摘掉（保留真人）==="
for n in 20001 20002 20003 20004 20005 20006 20007 20008 20009 20010 20011 20012 29999; do
  slug="default-friendmessage-$n"
  result=$(curl -s -XPOST "http://127.0.0.1:8800/fleet/deprovision/$slug")
  echo "  $slug -> $result"
done

echo
echo "=== 3) 摘之后的名册 ==="
curl -s http://127.0.0.1:8800/fleet/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  instances:", d["count"])
for p in d["people"]:
    print("   ", p["person"], "port", p["port"], "health", p["health"],
          "attempts/settled", p["deep_refresh_attempts"], "/", p["deep_refresh_settled"],
          "unresolved", p["unresolved"])
'

echo
echo "=== 4) 路由表 ==="
curl -s http://127.0.0.1:8800/fleet/routes

echo
echo
echo "=== 5) people.json ==="
cat /home/bomomo/astrbot_test/fleet-data/people.json

echo
echo "=== 6) 磁盘上的数据还在吗（应当都在）==="
docker exec xxj-runtime-fleet /bin/ls /data/

echo
echo "=== 7) 等插件同步注册表（≤5s）后看日志 ==="
sleep 10
docker logs astrbot-test --since 60s 2>&1 | grep -iE "companion_runtime|target|registry|route" | tail -12
