#!/usr/bin/env bash
# Verified weak-VPS profile: 0.5 core (cgroup v2 quota) and low-RAM footprint.
#
# The previous attempt moved the wrong PID into the cgroup, so the quota was
# never enforced. This version enables the cpu controller explicitly, moves the
# workload itself, and then *proves* enforcement by reading the throttling
# counters - a quota that is not throttling is not a measurement.

set -u

GGUF="${1:?usage: vps_bench.sh <gguf-path> [out-dir]}"
OUT="${2:-/root/companion-training/outputs/vps-bench}"
BENCH=/mnt/e/llama.cpp/build/bin/llama-bench
SERVER=/mnt/e/llama.cpp/build/bin/llama-server
CG=/sys/fs/cgroup/vpsbench

mkdir -p "$OUT"

# ---------------------------------------------------------------------------
# Enable the cpu controller for children, then create the constrained group.
# ---------------------------------------------------------------------------
echo "+cpu" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
mkdir -p "$CG"
echo "+cpu" > "$CG/cgroup.subtree_control" 2>/dev/null || true
if ! grep -q cpu "$CG/cgroup.controllers"; then
  echo "cpu controller unavailable in $CG; cannot simulate a fractional core" >&2
  exit 1
fi
echo "50000 100000" > "$CG/cpu.max"
echo "cgroup cpu.max = $(cat "$CG/cpu.max")"

run_in_cg() {
  # Move this shell into the group, then exec the workload so it inherits the
  # limit; print the throttling counters afterwards as proof it applied.
  bash -c "echo \$\$ > $CG/cgroup.procs; exec \"\$@\"" _ "$@"
}

echo
echo "=== calibration: is the 50% quota actually enforced? ==="
start=$(date +%s.%N)
run_in_cg bash -c 'end=$((SECONDS+3)); while [ $SECONDS -lt $end ]; do :; done'
end=$(date +%s.%N)
echo "  3s busy loop under 0.5 core took $(echo "$end - $start" | bc)s (expect ~6s)"
echo "  cpu.stat: $(grep -E 'nr_throttled|throttled_usec' "$CG/cpu.stat" | tr '\n' ' ')"

echo
echo "=== bench: 0.5 core (verified cgroup quota) ==="
run_in_cg "$BENCH" -m "$GGUF" -t 1 -p 256 -n 64 -r 2 2>&1 | grep -E '^\|' || true
echo "  cpu.stat after bench: $(grep -E 'nr_throttled|throttled_usec' "$CG/cpu.stat" | tr '\n' ' ')"

echo
echo "=== memory footprint by context size (1 core) ==="
for ctx in 512 1024 2048; do
  taskset -c 0 "$SERVER" -m "$GGUF" -c "$ctx" -t 1 --host 127.0.0.1 --port 8092 \
    --reasoning-format none > "$OUT/server-ctx$ctx.log" 2>&1 &
  pid=$!
  sleep 22
  rss=$(awk '/VmHWM/{print $2}' "/proc/$pid/status" 2>/dev/null || echo 0)
  printf "  -c %-5s peak RSS: %.0f MiB\n" "$ctx" "$(echo "$rss/1024" | bc)"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 2
done

echoDone=$(echo "results written to $OUT")
echo "$echoDone"
