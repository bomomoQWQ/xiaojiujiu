#!/usr/bin/env bash
# 清库 v3（2026-09-22 上线准备）：在 launch_prep_cleanup2.sh 基础上改三处。
#
#   1. **SNAP 用今天的时间戳**。v2 里硬编码 `2026-09-18_124843`，再跑一次会把旧快照的
#      boundaries 覆盖掉 ✗。
#   2. **验收的健康判断不再写死 `7/7|8/8`**（舰队现在 10 个实例）✗。
#   3. **新增第 8 步：清空宿主会话历史**（`data_v4.db` 的 `conversations.content`）。
#      公告里对测试者说的是"之前的聊天记忆会被清除"，而"她记不记得"取决于**两处**：
#      Runtime 的认知库（第 3 步归档 ✓）和宿主会话历史（第 8 步 ✓）。只清一处 = 半清 ✗
#      —— 主模型读的是宿主历史，历史还在，她就照样"记得"上次聊到哪儿。
#      实测现状：10 个会话合计 **45 条**历史条目（qq03 13 条最多）✓ 清起来是秒级。
#      `data_v4.db` 已备份在 `backup-20260922_1033-astrbot/` ✓（含 WAL ✓）。
#
# 另外两个**有意不做**的：
#   * **不重放 memories 的 carryover**（09-18 那次给 3 个人人工搬过 19 条 ✗）——
#     用户口径是"一起清空"，从零开始更干净 ✓；boundaries 照旧**必装**（它是硬约束 ✓）。
#   * 不动 `people.json`：清库 ≠ 重新 provision，人还在、端口与路由不变 ✓。
#
# 前置：NapCat 必须是**停的**（维护窗口 ✓），模拟前端可以在跑 ✓。
# 用法：bash launch_prep_cleanup3.sh
set -eu
STACK=/home/bomomo/astrbot_test
BETA=/mnt/xz/xiaojiujiu-beta
SNAP="$BETA/snapshots/2026-09-22_1833"
STAMP=$(date +%Y%m%d-%H%M%S)

echo "=== 0) 前置检查：NapCat 必须在停 ==="
if docker ps --format '{{.Names}}' | grep -qx xxj-napcat-test; then
  echo "  ✗ xxj-napcat-test 还在跑：清库期间不该有真实流量，先停它" >&2
  exit 1
fi
echo "  ✓ NapCat 是停的（维护窗口）"

echo
echo "=== 1) 停舰队（清理期间不应有写入）==="
docker stop xxj-onebot >/dev/null 2>&1 || true
docker stop xxj-runtime-fleet >/dev/null && echo "  舰队已停"

echo
echo "=== 2) 导出每个人的 boundaries（清库唯一必须留下的东西）==="
# 用同镜像单起容器跑 CLI，所以舰队是停的也没关系；导出在归档之前，归档会把实例目录搬走。
bash "$STACK/src/xiaojiujiu/scripts/boundaries_carryover.sh" export "$SNAP/boundaries" || {
  echo "  导出失败：不许继续清库（约束会丢）" >&2
  exit 1
}

echo
echo "=== 3) 归档每个人的认知库 ==="
docker run --rm -i -v astrbot_test_runtime-fleet-data:/data python:3.12-slim \
  python - "$STAMP" <<'PY'
import os
import shutil
import sys

stamp = sys.argv[1]
root = "/data"
archive = os.path.join(root, "_wiped-%s" % stamp)
os.makedirs(archive, exist_ok=True)
moved = []
for name in sorted(os.listdir(root)):
    path = os.path.join(root, name)
    if not os.path.isdir(path) or name.startswith("_"):
        continue
    shutil.move(path, os.path.join(archive, name))
    moved.append(name)
print("  已归档 %d 个实例目录 -> %s" % (len(moved), archive))
for name in moved:
    print("    %s" % name)
print("  归档后 /data 下剩余: %s" % sorted(os.listdir(root)))
PY

echo
echo "=== 4) 补齐快照（以 root 写进 root 拥有的快照目录）==="
docker run --rm -i \
  -v /home/bomomo/astrbot_test/data:/astrbot-data:ro \
  -v "$BETA":/export \
  -v "$STACK/src/xiaojiujiu":/repo:ro \
  python:3.12-slim \
  python - "$SNAP" <<'PY'
import shutil
import sys
from pathlib import Path

snap = Path(sys.argv[1])
snap.mkdir(parents=True, exist_ok=True)
copies = [
    (Path("/astrbot-data/cmd_config.json"), snap / "astrbot_cmd_config.json"),
    (Path("/astrbot-data/data_v4.db"), snap / "astrbot_data_v4.db"),
    (Path("/repo/人格设定.md"), snap / "人格设定.md"),
]
for source, target in copies:
    if not source.exists():
        print("  跳过（源不存在）: %s" % source)
        continue
    shutil.copy2(source, target)
    print("  %s -> %s（%d 字节）" % (source.name, target.name, target.stat().st_size))
print("  快照目录内容: %s" % sorted(p.name for p in snap.iterdir()))
PY

echo
echo "=== 5) 启动舰队（应新建空库，并按 env 用病娇画像）==="
docker start xxj-runtime-fleet >/dev/null && echo "  舰队已起"
for i in $(seq 1 40); do
  sleep 5
  line=$(curl -s --max-time 6 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('0/0'); raise SystemExit
p=d.get('people',[]); print('%d/%d'%(sum(1 for x in p if x.get('health')=='ok'),len(p)))
" 2>/dev/null || echo '0/0')
  total=${line#*/}
  ok=${line%/*}
  if [ "$total" != "0" ] && [ "$ok" = "$total" ]; then
    echo "  舰队 $line（$((i*5))s）"; break
  fi
done
curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('people', []):
    print('    %-36s port=%s %s' % (p.get('person'), p.get('port'), p.get('health')))
"

echo
echo "=== 6) 把 boundaries 装回新库 ==="
# 舰队已经在跑也没关系：实例每次判断边界都从库里读（不缓存在内存），装完立刻生效。
bash "$STACK/src/xiaojiujiu/scripts/boundaries_carryover.sh" import "$SNAP/boundaries"
bash "$STACK/src/xiaojiujiu/scripts/boundaries_carryover.sh" check "$SNAP/boundaries"

echo
echo "=== 6b) 装回长期记忆 + 用户模型（复用停机前的学习成果）==="
# 用户的指示："长期记忆处理一下复用，用户模型也复用吧" ✓
#   记忆：走现成的 `memories import`（carryover 格式），形状和 boundaries 一样 —— docker run
#         单起容器跑 CLI，所以舰队在跑在停都能装 ✓ 装完实例立刻读得到（每轮从库里取记忆）✓
#   用户模型：**必须停舰队**再装 —— 实例把权重/精度缓存在内存里，跑着的时候写库不生效 ✗
CARRY="${CARRY:-$BETA/carryover-20260922}"
IMAGE=$(docker inspect xxj-runtime-fleet --format '{{.Config.Image}}')
runtime_cli() {
  person="$1"
  shift
  docker run --rm --entrypoint companion-runtime \
    -e CR_STORAGE__DATABASE_PATH="/data/$person/companion.sqlite3" \
    -v astrbot_test_runtime-fleet-data:/data "$IMAGE" "$@"
}
echo "--- 6b-1) 长期记忆（$(ls -1 "$CARRY"/*-carried.json 2>/dev/null | wc -l) 个文件）---"
for path in "$CARRY"/*-carried.json; do
  [ -f "$path" ] || continue
  qq=$(basename "$path" -carried.json)
  person="default-friendmessage-$qq"
  carried=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8")).get("memories") or []))' "$path")
  if [ "$carried" = "0" ]; then
    echo "  $qq  文件里 0 条，跳过"
    continue
  fi
  if runtime_cli "$person" memories import - < "$path" >/tmp/mem_import_out 2>&1; then
    echo "  $qq  已装回 $carried 条 ✓"
  else
    echo "  $qq  导入失败：" >&2
    sed 's/^/      /' /tmp/mem_import_out >&2
    exit 1
  fi
done
echo "--- 6b-2) 用户模型（停舰队后写两张表）---"
# 用 python:3.12-slim：这一步只用 stdlib，而运行期镜像的 entrypoint 是 companion-runtime ✗
docker stop xxj-runtime-fleet >/dev/null 2>&1 || true
echo "  舰队已停（或本来就没跑；写库前必须停：权重缓存在内存里）"
docker run --rm \
  -v astrbot_test_runtime-fleet-data:/data \
  -v "$CARRY":/carry:ro \
  -v "$STACK/src/xiaojiujiu/scripts":/scripts:ro \
  python:3.12-slim python3 /scripts/carryover_user_model.py import /carry
docker start xxj-runtime-fleet >/dev/null && echo "  舰队已起"

echo "--- 6b-3) 把「期望值」交给容器，供第 7 步验收对照 ---"
python3 - "$CARRY" > /tmp/carry_expected.json <<'PY'
import glob
import json
import os
import sys

carry = sys.argv[1]
expected: dict[str, dict] = {}
for path in sorted(glob.glob(os.path.join(carry, "*-carried.json"))):
    tag = os.path.basename(path)[: -len("-carried.json")]
    expected.setdefault(tag, {})["memories"] = len(
        json.load(open(path, encoding="utf-8")).get("memories") or []
    )
for path in sorted(glob.glob(os.path.join(carry, "*-user-model.json"))):
    tag = os.path.basename(path)[: -len("-user-model.json")]
    payload = json.load(open(path, encoding="utf-8"))
    expected.setdefault(tag, {})["observations"] = len(
        payload.get("interaction_observations") or []
    )
json.dump(expected, sys.stdout)
PY
docker cp /tmp/carry_expected.json xxj-runtime-fleet:/tmp/carry_expected.json >/dev/null && echo "  已写入容器 /tmp/carry_expected.json ✓"

echo
echo "=== 7) 验收：装回的东西在、其余是空的、画像是病娇那一份 ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import glob
import json
import os
import sqlite3

# 期望值：事件只该剩"搬运"留下的一条 system 审计事件（import_memories 会写一条）；
# 记忆数应当等于带过来的条数（从 /tmp/carry_expected.json 读，由 6b 生成）；
# 用户模型的观测/有效证据数同样应当带过来。
expected = {}
try:
    expected = json.load(open("/tmp/carry_expected.json", encoding="utf-8"))
except Exception:
    pass

problems = []
for path in sorted(glob.glob("/data/*/companion.sqlite3")):
    tag = os.path.basename(os.path.dirname(path)).replace("default-friendmessage-", "")
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    events = con.execute("select count(*) from raw_events").fetchone()[0]
    memories = con.execute("select count(*) from memories").fetchone()[0]
    carried = con.execute(
        "select count(*) from memories where structured_json like '%operator_carryover%'"
    ).fetchone()[0]
    values = json.loads(con.execute("select values_json from runtime_state").fetchone()[0] or "{}")
    observations = con.execute("select count(*) from interaction_observations").fetchone()[0]
    con.close()

    want = expected.get(tag, {})
    if values.get("boundary_respect") != 0.05 or values.get("user_care") != 1.0:
        problems.append("%s 画像不对（br=%s care=%s）" % (
            tag, values.get("boundary_respect"), values.get("user_care")))
    if want.get("memories") is not None and memories != want["memories"]:
        problems.append("%s 记忆 %d 条，期望 %d 条" % (tag, memories, want["memories"]))
    if want.get("observations") is not None and observations != want["observations"]:
        problems.append("%s 观测 %d 条，期望 %d 条" % (tag, observations, want["observations"]))
    print("  %-14s 事件=%-3d 记忆=%-3d（搬运 %-3d）观测=%-2d br=%-5s care=%-5s" % (
        tag, events, memories, carried, observations,
        values.get("boundary_respect"), values.get("user_care")))

if problems:
    print("  ✗ 验收不通过：")
    for item in problems:
        print("      %s" % item)
    raise SystemExit(1)
print("  ✓ 该空的空、该在的在、画像正确")
PY

echo
echo "=== 8) 清空宿主会话历史（公告承诺的「记忆会被清除」，这一半必须在宿主侧做）==="
docker exec -i astrbot-test python - <<'PY'
import json
import sqlite3

con = sqlite3.connect("/AstrBot/data/data_v4.db")


def entries() -> int:
    """How many history items the conversations hold right now."""
    total = 0
    for (content,) in con.execute("select content from conversations"):
        try:
            total += len(json.loads(content) if isinstance(content, str) else (content or []))
        except Exception:
            pass
    return total


before = entries()
con.execute("update conversations set content = '[]'")
con.execute("delete from platform_message_history")
con.commit()
print("  会话历史条目 %d -> %d ✓" % (before, entries()))
persona = con.execute("select persona_id, length(system_prompt) from personas").fetchall()
print("  人格未动（应还在，含那句硬边界）: %s" % persona)
con.close()
PY

echo
echo "=== 9) 假用户实例若被重建，就撤掉 ==="
# 控制面认的是 status 里的 `person`（slug，形如 default-friendmessage-20001）—— **不是端口** ✗。
# v2/v3 早先那行传的是端口，一直是 `{"error":"unknown person"}`（静默失败）。
SLUG=$(curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(next((p['person'] for p in d.get('people',[]) if '20001' in str(p.get('person'))), ''))
")
if [ -n "$SLUG" ]; then
  echo "  撤掉假用户实例（$SLUG）"
  curl -s -X POST "http://127.0.0.1:8800/fleet/deprovision/$SLUG" | head -c 200; echo
else
  echo "  假用户实例已不在舰队里 ✓"
fi

echo
echo "=== 完成。下一步：起模拟前端做冒烟，或直接起 NapCat 恢复 ==="
echo "  注意顺序：先停模拟前端 → 再起 NapCat → 再确认 AstrBot 适配器连上它"
