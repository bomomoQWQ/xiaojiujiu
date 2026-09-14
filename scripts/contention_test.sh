#!/usr/bin/env bash
# Does the 2B model starve a co-tenant on a 1-core VPS?
#
# AstrBot must answer the user in seconds; the appraisal model wants the same
# core for tens of seconds. This runs a latency-sensitive burst loop (the stand-in
# for AstrBot's message path) alone, then alongside the model at normal and at
# low priority, and prints the latency inflation for each case.
#
# Usage: bash scripts/contention_test.sh

set -u

BIN=/mnt/e/llama.cpp/build/bin
GGUF=/root/companion-training/outputs/qboss-2b-Q4_K_M.gguf
PROBE="/mnt/f/理解痞老板/scripts/contention_probe.py"
PY=/root/companion-training/.venv/bin/python
OUT=/root/companion-training/outputs/vps-efficiency
PORT=8391
mkdir -p "$OUT"

echo "=== A. baseline: only the latency-sensitive loop, 1 core ==="
taskset -c 0 "$PY" "$PROBE" baseline pin "$PORT"

echo
echo "=== B. co-tenant at normal priority: model + loop, both on 1 core ==="
taskset -c 0 "$BIN/llama-server" -m "$GGUF" -c 1024 -t 1 \
  --host 127.0.0.1 --port "$PORT" --reasoning-format none > "$OUT/server-contention.log" 2>&1 &
pid=$!
sleep 26
taskset -c 0 "$PY" "$PROBE" model pin "$PORT"
kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
sleep 3

echo
echo "=== C. co-tenant at low priority (nice 19): model + loop, both on 1 core ==="
taskset -c 0 nice -n 19 "$BIN/llama-server" -m "$GGUF" -c 1024 -t 1 \
  --host 127.0.0.1 --port "$PORT" --reasoning-format none > "$OUT/server-contention-nice.log" 2>&1 &
pid=$!
sleep 26
taskset -c 0 "$PY" "$PROBE" model-nice pin "$PORT"
kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true

echo
echo "done; logs in $OUT"
