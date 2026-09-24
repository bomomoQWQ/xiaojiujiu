#!/usr/bin/env bash
# 清库的**后半段补跑**：装回长期记忆 + 用户模型 → 验收 → 清宿主历史 → 撤假用户。
#
# 为什么单独一个脚本：launch_prep_cleanup3.sh 跑到 6b-1 就退出了 —— 它的 runtime_cli 少了 `-i`，
# `docker run` 不带 `-i` 时容器的 stdin 是空的，`memories import -` 读到空输入直接报
# "Expecting value: line 1 column 1" ✗。（boundaries_carryover.sh 里那份同样的函数也少了 `-i`，
# 09-18 那次"看起来成功"只是因为当时 0 条边界；**非空边界会静默装不进去** ✗ —— 已一并修。）
#
# 前置：前 1-5 步已经跑过（旧库已归档到 _wiped-*、新空库已建、boundaries 已（无可装））。
# 用法：bash carryover_install.sh
set -eu
STACK=/home/bomomo/astrbot_test
BETA=/mnt/xz/xiaojiujiu-beta
CARRY="${CARRY:-$BETA/carryover-20260922}"
VOLUME=astrbot_test_runtime-fleet-data
FLEET=xxj-runtime-fleet
IMAGE=$(docker inspect "$FLEET" --format '{{.Config.Image}}')

runtime_cli() {
  person="$1"
  shift
  # `-i` 是必须的：没有它容器读不到 stdin，`import -` 会拿空输入。
  docker run --rm -i --entrypoint companion-runtime \
    -e CR_STORAGE__DATABASE_PATH="/data/$person/companion.sqlite3" \
    -v "$VOLUME":/data "$IMAGE" "$@"
}

echo "=== 1) 装回长期记忆 ==="
for path in "$CARRY"/*-carried.json; do
  [ -f "$path" ] || continue
  qq=$(basename "$path" -carried.json)
  person="default-friendmessage-$qq"
  want=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8")).get("memories") or []))' "$path")
  if [ "$want" = "0" ]; then
    echo "  $qq  文件里 0 条，跳过"
    continue
  fi
  if runtime_cli "$person" memories import - < "$path" >/tmp/mem_out 2>&1; then
    got=$(runtime_cli "$person" memories import - --dry-run < "$path" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("would_install", "?"))
except Exception: print("?")' 2>/dev/null || echo "?")
    echo "  $qq  已装回 $want 条 ✓（dry-run 复核：$got）"
  else
    echo "  $qq  导入失败：" >&2
    sed 's/^/      /' /tmp/mem_out >&2
    exit 1
  fi
done

echo
echo "=== 2) 装回用户模型（写库前必须停舰队：权重缓存在实例内存里）==="
# 用 python:3.12-slim 而不是运行期镜像：这一步只用 stdlib（sqlite3/json），
# 而运行期镜像的 entrypoint 是 companion-runtime（`python3` 会被当成子命令 ✗）。
docker stop "$FLEET" >/dev/null 2>&1 || true
echo "  舰队已停（或本来就没跑）"
docker run --rm \
  -v "$VOLUME":/data \
  -v "$CARRY":/carry:ro \
  -v "$STACK/src/xiaojiujiu/scripts":/scripts:ro \
  python:3.12-slim python3 /scripts/carryover_user_model.py import /carry
docker start "$FLEET" >/dev/null && echo "  舰队已起"

echo
echo "=== 3) 期望值交给容器 ==="
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
    expected.setdefault(tag, {})["observations"] = len(
        json.load(open(path, encoding="utf-8")).get("interaction_observations") or []
    )
json.dump(expected, sys.stdout)
PY
docker cp /tmp/carry_expected.json "$FLEET":/tmp/carry_expected.json >/dev/null && echo "  已写入 ✓"

echo
echo "=== 4) 验收 ==="
docker exec -i "$FLEET" python3 - <<'PY'
import glob
import json
import os
import sqlite3

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
    observations = con.execute("select count(*) from interaction_observations").fetchone()[0]
    values = json.loads(con.execute("select values_json from runtime_state").fetchone()[0] or "{}")
    con.close()
    want = expected.get(tag, {})
    if values.get("boundary_respect") != 0.05 or values.get("user_care") != 1.0:
        problems.append("%s 画像不对" % tag)
    if want.get("memories") is not None and memories != want["memories"]:
        problems.append("%s 记忆 %d，期望 %d" % (tag, memories, want["memories"]))
    if want.get("observations") is not None and observations != want["observations"]:
        problems.append("%s 观测 %d，期望 %d" % (tag, observations, want["observations"]))
    print("  %-14s 事件=%-3d 记忆=%-3d（搬运 %-3d）观测=%-2d br=%-5s care=%-5s" % (
        tag, events, memories, carried, observations,
        values.get("boundary_respect"), values.get("user_care")))
if problems:
    print("  ✗ 验收不通过：")
    for item in problems:
        print("      %s" % item)
    raise SystemExit(1)
print("  ✓ 该在的在（记忆/观测/画像）")
PY

echo
echo "=== 5) 清空宿主会话历史（公告承诺的另一半）==="
docker exec -i astrbot-test python - <<'PY'
import json
import sqlite3

con = sqlite3.connect("/AstrBot/data/data_v4.db")


def entries() -> int:
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
print("  人格未动: %s" % con.execute("select persona_id, length(system_prompt) from personas").fetchall())
con.close()
PY

echo
echo "=== 6) 撤掉假用户 20001 ==="
# 控制面认的是 status 里的 `person`（slug，形如 default-friendmessage-20001）—— **不是端口** ✗。
SLUG=$(curl -s --max-time 8 http://127.0.0.1:8800/fleet/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(next((p['person'] for p in d.get('people',[]) if '20001' in str(p.get('person'))), ''))
")
if [ -n "$SLUG" ]; then
  echo "  撤掉（$SLUG）"
  curl -s -X POST "http://127.0.0.1:8800/fleet/deprovision/$SLUG" | head -c 200; echo
else
  echo "  已不在舰队里 ✓"
fi

echo
echo "=== 完成 ✓ 下一步：停模拟前端 → 起 NapCat → 确认适配器连上 → 发「回来了」通告 ==="
