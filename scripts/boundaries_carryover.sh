#!/usr/bin/env bash
# 边界搬运：用户亲口立的硬约束必须活过清库。
#
#   bash boundaries_carryover.sh export [目录]   # 导出每个人的 boundaries 到 <目录>/<人>.json
#   bash boundaries_carryover.sh import [目录]   # 清库重建后把 boundaries 装回去
#   bash boundaries_carryover.sh check  [目录]   # 只读：文件里有什么 vs 库里现在有什么
#
# 为什么清库要单独搬 boundaries：
#   清库的目的是丢掉「学错的东西」（两个月里攒的一堆错分类记忆）。boundaries 不是学来的，
#   是用户亲口立的硬约束（「永远别联系我」「别再提这件事」）。忘掉一句记忆只是少了一句话；
#   忘掉约束是让她继续违反一条用户已经**没法再纠正**的规矩 —— 纠正的前提是第一次的记录
#   还在，而那份记录正是被清掉的东西。
#
# 用法（清库流程里，两步都指同一个目录）：
#   bash boundaries_carryover.sh export /mnt/xz/xiaojiujiu-beta/snapshots/<stamp>/boundaries
#   … 清库（launch_prep_cleanup*.sh）…
#   bash boundaries_carryover.sh import /mnt/xz/xiaojiujiu-beta/snapshots/<stamp>/boundaries
#   bash boundaries_carryover.sh check  /mnt/xz/xiaojiujiu-beta/snapshots/<stamp>/boundaries
#
# 实现说明：
#   * 不依赖舰队在跑：用同一个镜像单起一个容器跑 CLI，只读写那一个实例的 sqlite3。
#     清库流程里舰队是停的，docker exec 会失败，所以这里必须用 docker run。
#   * import 之后实例**不需要重启**：每次判断边界都从库里读
#     （runtime.py 里一律是 self.projections.boundaries.active(now)），不缓存在内存。
set -u

VOLUME="${VOLUME:-astrbot_test_runtime-fleet-data}"
FLEET="${FLEET:-xxj-runtime-fleet}"
ACTION="${1:-check}"
DIR="${2:-/home/bomomo/astrbot_test/boundary-carryover}"

IMAGE=$(docker inspect "$FLEET" --format '{{.Config.Image}}' 2>/dev/null || true)
if [ -z "$IMAGE" ]; then
  echo "找不到容器 $FLEET（拿不到镜像名）。用 IMAGE=... 覆盖后再试。" >&2
  exit 1
fi

mapfile -t PEOPLE < <(
  docker run --rm -v "$VOLUME":/data --entrypoint /bin/sh "$IMAGE" \
    -c 'ls -1 /data 2>/dev/null | grep "^default-friendmessage-" || true'
)
if [ "${#PEOPLE[@]}" -eq 0 ]; then
  echo "卷 $VOLUME 里没有实例目录。" >&2
  exit 1
fi

echo "镜像=$IMAGE  卷=$VOLUME  实例=${#PEOPLE[@]}  目录=$DIR"
echo

# 单个实例的 CLI：$1=人，其余参数原样交给 companion-runtime。
runtime_cli() {
  person="$1"
  shift
  docker run --rm --entrypoint companion-runtime \
    -e CR_STORAGE__DATABASE_PATH="/data/$person/companion.sqlite3" \
    -v "$VOLUME":/data "$IMAGE" "$@"
}

# 从库里现读一条汇总（走 CLI 的 export，不自己开 sqlite：口径和导入完全一致）。
db_summary() {
  runtime_cli "$1" boundaries export - 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("?/?")
    raise SystemExit
print("%d/%d" % (data.get("active", -1), data.get("count", -1)))
'
}

# 读一个导出文件里的一条汇总。
file_summary() {
  python3 - "$1" <<'PY'
import json
import sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print("坏文件: %s" % exc)
    raise SystemExit
print("count=%d active=%d" % (data.get("count", -1), data.get("active", -1)))
PY
}

case "$ACTION" in
  export)
    mkdir -p "$DIR" || exit 1
    echo "=== 导出 ==="
    for person in "${PEOPLE[@]}"; do
      out="$DIR/$person.json"
      if runtime_cli "$person" boundaries export - > "$out" 2>/tmp/boundary_export_err; then
        echo "  $person  $(file_summary "$out")  -> $out"
      else
        # 导出失败和「这个人没有边界」必须分得开：前者会导致约束丢失，不能继续。
        echo "  $person  导出失败（这是错误，不是「没有边界」）：" >&2
        sed 's/^/      /' /tmp/boundary_export_err >&2
        rm -f "$out"
        exit 1
      fi
    done
    echo
    echo "文件清单："
    ls -l "$DIR" | sed 's/^/  /'
    echo
    echo "接着清库；清完用同一条命令的 import 装回去。"
    ;;

  import)
    [ -d "$DIR" ] || { echo "目录不存在：$DIR（export 没跑过？）" >&2; exit 1; }
    echo "=== 装回去 ==="
    failed=0
    for person in "${PEOPLE[@]}"; do
      src="$DIR/$person.json"
      if [ ! -f "$src" ]; then
        echo "  $person  没有文件，跳过（视为「本来就没有边界」）"
        continue
      fi
      count=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("count", -1))' "$src")
      if [ "$count" = "0" ]; then
        echo "  $person  文件里 0 条，跳过"
        continue
      fi
      if runtime_cli "$person" boundaries import - < "$src"; then
        echo "  $person  已装回 $count 条；库里现在 活跃/总数 = $(db_summary "$person")"
      else
        echo "  $person  导入失败（继续跑完其他人，最后请用 check 复核）" >&2
        echo "           常见原因：这个人的数据目录还不存在（全新卷要先让舰队起一次）" >&2
        failed=1
      fi
    done
    echo
    if [ "$failed" != "0" ]; then
      echo "⚠️ 有实例导入失败，别急着开放使用：bash $0 check $DIR" >&2
      exit 1
    fi
    echo "核对：bash $0 check $DIR"
    ;;

  check)
    echo "=== 文件 ==="
    if [ -d "$DIR" ]; then
      for person in "${PEOPLE[@]}"; do
        src="$DIR/$person.json"
        if [ -f "$src" ]; then
          python3 - "$src" "$person" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print("  %-40s %s" % (sys.argv[2], "count=%d active=%d" % (data.get("count", -1), data.get("active", -1))))
for row in data.get("boundaries") or []:
    print("      %s type=%s scope=%s proactive=%s subject=%s revoked=%s expires=%s" % (
        row.get("boundary_id"), row.get("type"), row.get("scope"),
        row.get("allow_proactive"), row.get("subject"), row.get("revoked_at"), row.get("expires_at")))
PY
        else
          echo "  $person  没有文件"
        fi
      done
    else
      echo "  目录不存在：$DIR"
    fi
    echo
    echo "=== 库里 活跃/总数 ==="
    for person in "${PEOPLE[@]}"; do
      echo "  $person  $(db_summary "$person")"
    done
    ;;

  *)
    echo "用法: bash $0 {export|import|check} [目录]" >&2
    exit 2
    ;;
esac
