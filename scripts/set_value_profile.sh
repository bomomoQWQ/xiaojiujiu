#!/usr/bin/env bash
# 改所有实例的价值观画像（改 PROFILE 块即可复用），并同步 fleet 配置让新人继承。
#
# 为什么不能只改 env：ValueProfile 只在实例**第一次建库**时写入 runtime_state，
# 之后以库里那份为准（runtime.py -> ensure_defaults）。所以改 env 对已存在的实例无效，
# 必须直接改 runtime_state.values_json；本脚本两个都改（现有实例 + fleet.yml/env）。
#
# ============================ 画像从哪来（2026-09-18 重新推导）============================
# 依据：人格设定.md（苏清徽 / 病娇·偏执·掌控欲强·凶狠直白·短）。
#
# 中文名以**设计文档 §4.2** 为准（别在别的文件里另起译名），顺序与那儿的示例 JSON 一致：
#   autonomy=自主倾向  boundary_respect=边界观念  emotional_expression=情感表达
#   relationship_maintenance=关系维护  user_care=用户关怀
#   conflict_directness=冲突取向  stability_commitment=稳定/承诺取向  curiosity=好奇倾向
# （同一张表也写在 runtime/src/companion_runtime/typing.py::ValueProfile 的 docstring 里。）
#
# 逐轴对应关系：
#   轴                        人格里的证据                     机制位置（当前代码）
#   conflict_directness   「凶狠、威胁…富有攻击力…直来直去」   restraint -0.25v；沉默效用 -0.25v
#   relationship_maintenance「掌控欲过于强」「病娇」「偏执」     缺席拉力 (1.55+0.30v)；关系收益 0.5v；
#                                                            情绪敏感 ×(0.6+0.8v)；记忆显著 ×(0.6+0.6v)
#   stability_commitment  「偏执狂」= 放不下                   情绪衰减 ×(1-0.35v)；记忆稳定 0.30+0.30v；
#                                                            restraint +0.35v；关系收益 0.2v；敏感 ×(0.7+0.6v)
#   user_care             「我不想生气」「明天几点体检，报给我」 impulse +0.90v·unfinished；敏感 ×(0.6+0.8v)；
#                                                            关系收益 +0.3v
#   emotional_expression  「病娇」= 对方一句话就起波澜          敏感 ×(0.7+0.5v)   ★今天起才真正生效
#   autonomy              「冷静的」= 表面冷，不是不敏感        敏感 ×(1-0.25v)    ★今天起才真正生效
#                         低值=不被自我容纳削弱；她的机制含义
#                         与字面"自主"相反，别按字面调
#   boundary_respect      「掌控欲过于强」= 不顾忌（但明确声明的 越界成本 ∝v；restraint +0.75v；
#                         边界仍由 authorize 硬拦，与 values 无关）边界压力抑制 -0.35v
#   curiosity             人格未提及 —— 唯一机制是 impulse +0.20v（"没什么事也想开口"），
#                         而"没理由也想打个招呼"正是掌控型人格的开口方式，故保持满值
#
# ★ 2026-09-18 更正一处过期注释：`emotional_expression` 与 `autonomy` **不再是死代码**。
#   当天把 ingest 的情绪判定接到 `emotion.appraise_event`（runtime.py 内
#   "appraisal_source = rule/coarse_rule" 那段），而它正是唯一读这两个轴的函数。
#   于是这两个轴从"只是记录意图"变成真的在算：线上画像的情绪敏感度乘子
#   = 1.4×1.3×0.9875×1.4×1.2 ≈ 3.02×（设计文档示例画像只有 1.43×）。
#
# 安全边界（与 values 无关，压低 br 突破不了）：
#   用户明确声明的边界 = authorize.py 硬拦（boundary_blocks_proactive / reply_not_permitted）
#   频率硬上限 = drive.cooldown_seconds 2400s、drive.max_contacts_per_day 12
#   声明窗口长度本身：boundaries.detect_boundaries 只允许**延长**不允许缩短
#   （2026-09-18 修：br=0.05 曾把"今天不要主动联系我"的 24h 缩成 20.76h）
# ======================================================================================
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
import json
import sys

target = json.loads(sys.argv[1])
print("  " + json.dumps(target, ensure_ascii=False))
sensitivity = (
    (0.6 + 0.8 * target["relationship_maintenance"])
    * (0.7 + 0.6 * target["stability_commitment"])
    * (1.0 - 0.25 * target["autonomy"])
    * (0.6 + 0.8 * target["user_care"])
    * (0.7 + 0.5 * target["emotional_expression"])
)
restraint_logit = (
    -0.40
    + 0.75 * target["boundary_respect"]
    + 0.35 * target["stability_commitment"]
    - 0.25 * target["conflict_directness"]
)
print(f"  情绪敏感度乘子 = {sensitivity:.2f}x（设计文档示例画像 = 1.43x）")
print(f"  restraint logit（无边界压力/不忙）= {restraint_logit:+.4f}"
      f"  => sigmoid {1 / (1 + pow(2.718281828, -restraint_logit)):.4f}"
      f"（示例画像 0.4410 => 0.6085）")
print(f"  记忆稳定 0.30+0.30*sc = {0.30 + 0.30 * target['stability_commitment']:.4f}"
      f"   情绪衰减 1-0.35*sc = {1 - 0.35 * target['stability_commitment']:.4f}")
print("  实测参照（.scratch_blackbox/compare_value_profiles.py，同一段历史）：")
print("    示例画像 48h 内不开口（no_candidate_beats_silence，boundary_cost 0.1359）")
print("    该画像   8.5h 开口（hazard_triggered，boundary_cost 0.0077、relation 0.1975）")
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
          f"uc={got.get('user_care')} cd={got.get('conflict_directness')} sc={got.get('stability_commitment')} "
          f"ee={got.get('emotional_expression')} au={got.get('autonomy')}")
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
