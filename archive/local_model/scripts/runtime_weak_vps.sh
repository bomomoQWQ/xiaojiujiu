#!/usr/bin/env bash
# Critical-path cost of the Runtime sidecar under weak-VPS CPU budgets.
#
# The model is optional; the Runtime is not. This runs the Level 0 deployment
# (no local model) under 1 core and half a core and reports the same latency
# percentiles the architecture's 30 ms ingress budget is stated against.
#
# Usage: bash scripts/runtime_weak_vps.sh

set -u

PY=/root/companion-training/.venv/bin/python
BENCH="/mnt/f/理解痞老板/scripts/runtime_bench.py"
OUT=/root/companion-training/outputs/vps-efficiency
CG=/sys/fs/cgroup/vpsrt
mkdir -p "$OUT"

echo "+cpu" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
mkdir -p "$CG"
echo "50000 100000" > "$CG/cpu.max"

report() {
  local label="$1"; shift
  echo "=== Runtime Level 0 (no model) on $label ==="
  "$@" "$PY" "$BENCH" --rounds 200 --db "/tmp/rt-$label.sqlite3" \
        --out "$OUT/runtime-$label.json" 2>/dev/null
  local throttled
  throttled=$(grep -o 'nr_throttled [0-9]*' "$CG/cpu.stat" 2>/dev/null | awk '{print $2}')
  echo "  cgroup nr_throttled=$throttled"
  echo
}

echo "host: $(nproc) logical processors"
echo

# 1 core: pin to CPU 0.
cd /tmp
report "1core" taskset -c 0

# 0.5 core: verified cgroup quota, applied to the workload itself.
bash -c "echo \$\$ > $CG/cgroup.procs; exec \"\$@\"" _ \
  "$PY" "$BENCH" --rounds 200 --db /tmp/rt-halfcore.sqlite3 \
  --out "$OUT/runtime-halfcore.json" 2>/dev/null
echo "=== Runtime Level 0 (no model) on 0.5 core ==="
echo "  cgroup $(cat "$CG/cpu.max"), nr_throttled=$(grep -o 'nr_throttled [0-9]*' "$CG/cpu.stat" | awk '{print $2}')"
echo

echo "reports in $OUT"
