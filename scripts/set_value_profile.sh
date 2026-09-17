#!/usr/bin/env bash
# 改所有实例的价值观画像（改 PROFILE 块即可复用），并同步 fleet 配置让新人继承。
#
# 为什么不能只改 env：ValueProfile 只在实例**第一次建库**时写入 runtime_state，
# 之后以库里那份为准（runtime.py -> ensure_defaults）。所以改 env 对已存在的实例无效，
# 必须直接改 runtime_state.values_json；本脚本两个都改（现有实例 + fleet.yml/env）。
#
# 8 个轴里有两个目前是**死代码**：emotional_expression 与 autonomy 只出现在
# emotion.appraise_event，而该函数没有生产调用方（入口走 semantic.settlement_to_evaluation，
# 不带价值观）。它们仍然设置，一是记录意图，二是将来接线即生效。
#
# 生效的轴与位置（当前代码）：
#   boundary_respect          motivation:469 越界成本 / :666 压力抑制 / :673 沉默效用 +0.75x
#   user_care                 motivation:657 未了之事的驱力 +0.90x
#   relationship_maintenance  motivation:664 缺席拉力 / memory:414 关系记忆显著度
#   stability_commitment      emotion:326 情绪衰减更慢 / memory:404 长期记忆
#   conflict_directness       motivation:675 沉默效用 -0.25x
#   curiosity                 motivation:665 接近驱力 +0.20x
#
# 安全边界（与 values 无关，压低 br 不会突破）：
#   用户明确声明的边界 = authorize.py:112 硬拦（boundary_blocks_proactive / reply_not_permitted）
#   频率硬上限 = drive.cooldown_seconds 2400s、drive.max_contacts_per_day 12
set -u
BETA=/mnt/xz/xiaojiujiu-beta
FLEET=/home/bomomo/astrbot_test/fleet.yml

# ---- PROFILE（改这里）--------------------------------------------------------
PROFILE_JSON='{
  "boundary_respect": 0.05,
  "user_care": 1.0,
  "relationship_maintenance": 1.0,
  "stability_commitment": 1.0,
  "conflict_directness": 1.0,
  "curiosity": 1.0,
  "emotional_expression": 1.0,
  "autonomy": 0.05
}'
# -----------------------------------------------------------------------------

echo "=== 0) 目标画像与预测 ==="
python3 - "$PROFILE_JSON" <<'PY'
import json, sys

target = json.loads(sys.argv[1])
print("  " + json.dumps(target, ensure_ascii=False))
silence = lambda p: 0.75 * p["boundary_respect"] + 0.35 * p["stability_commitment"] - 0.25 * p["conflict_directness"]
print(f"  沉默效用 0.75*br+0.35*sc-0.25*cd = {silence(target):.4f}  （改版前是 0.8500）")
print(f"  情绪衰减 1-0.35*sc = {1 - 0.35 * target['stability_commitment']:.4f}  （越小越放不下）")
print(f"  记忆稳定 0.30+0.30*sc = {0.30 + 0.30 * target['stability_commitment']:.4f}")
PY

echo
echo "=== 1) 冻结 ==="
docker run --rm -v astrbot_test_runtime-fleet-data:/data -v "$BETA":/export \
  -v /home/bomomo/astrbot_test/src/xiaojiujiu/scripts:/scripts:ro python:3.12-slim \
  python /scripts/snapshot_beta.py --data-root /data --root /export/snapshots \
  --fleet http://192.168.1.15:8800 --note "before value profile change" 2>&1 | tail -2

echo
echo "=== 2) 应用到所有实例 ==="
PROFILE_JSON="$PROFILE_JSON" docker exec -i -e PROFILE_JSON="$PROFILE_JSON" xxj-runtime-fleet python3 - <<'PY'
import glob, json, os, sqlite3

target = json.loads(os.environ["PROFILE_JSON"])
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    person = os.path.basename(os.path.dirname(path))
    con = sqlite3.connect(path, timeout=15)
    con.execute("pragma busy_timeout = 15000")
    row = con.execute("select values_json from runtime_state").fetchone()
    before = json.loads(row[0]) if row and row[0] else {}
    merged = {**before, **target}
    with con:
        con.execute("update runtime_state set values_json = ?", (json.dumps(merged),))
    got = json.loads(con.execute("select values_json from runtime_state").fetchone()[0])
    ok = all(abs(got.get(k, -1) - v) < 1e-9 for k, v in target.items())
    print(f"  {person}: {'OK' if ok else 'MISMATCH'}  br={got.get('boundary_respect')} "
          f"uc={got.get('user_care')} cd={got.get('conflict_directness')} sc={got.get('stability_commitment')}")
    con.close()
PY

echo
echo "=== 3) 同步 fleet.yml（新人继承）==="
cp "$FLEET" "$FLEET.bak-$(date +%Y%m%d-%H%M%S)"
python3 - "$FLEET" "$PROFILE_JSON" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
target = {f"CR_VALUES__{k.upper()}": str(v) for k, v in json.loads(sys.argv[2]).items()}
lines = path.read_text(encoding="utf-8").splitlines()
out = []
for line in lines:
    stripped = line.strip()
    key = stripped.split(":", 1)[0] if ":" in stripped else ""
    out.append(f"      {key}: {target.pop(key)}" if key in target else line)
out.extend(f"      {k}: {v}" for k, v in target.items())
path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("  fleet.yml updated")
PY

echo
echo "=== 4) 生效值（Runtime 每次 state() 都是现读库，无需重启）==="
for port in $(curl -s http://127.0.0.1:8800/fleet/status | python3 -c 'import json,sys;[print(p["port"]) for p in json.load(sys.stdin)["people"]]'); do
  docker exec xxj-runtime-fleet python3 -c "
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:$port/health', timeout=5) as r:
    d = json.load(r)
print('  port $port ok')
" 2>/dev/null || echo "  port $port unreachable"
done
grep -n 'CR_VALUES' "$FLEET"
