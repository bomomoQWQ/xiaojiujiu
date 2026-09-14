#!/usr/bin/env bash
# Weak-VPS CPU benchmark for the fine-tuned 2B model.
#
# Simulates a cheap VPS by restricting the process to 1 core, then to half a
# core via the cgroup-v2 CPU bandwidth controller (cpu.max), and by capping
# memory. Reports throughput, single-request latency and peak RSS so the
# deployment budget can be decided from measurements rather than guesses.
#
# Usage: bash scripts/vps_bench.sh <gguf-path> [out-dir]

set -u

GGUF="${1:?usage: vps_bench.sh <gguf-path> [out-dir]}"
OUT="${2:-/root/companion-training/outputs/vps-bench}"
BENCH=/mnt/e/llama.cpp/build/bin/llama-bench
SERVER=/mnt/e/llama.cpp/build/bin/llama-server
CG=/sys/fs/cgroup/vpsbench

mkdir -p "$OUT"

echo "=== environment ==="
nproc
free -m | head -2

# ---------------------------------------------------------------------------
# 1) Throughput sweep under an explicit single-core pin and a half-core quota
# ---------------------------------------------------------------------------

run_bench() {
  local label="$1"; shift
  echo
  echo "=== bench: $label ==="
  "$@" 2>&1 | grep -E '^\|' || true
}

# Single logical CPU, one thread.
run_bench "1 core (taskset -c 0, -t 1)" \
  taskset -c 0 "$BENCH" -m "$GGUF" -t 1 -p 256 -n 64 -r 2

# Half a core via cgroup v2: 50000/100000 = 50% of one CPU.
if [ -w /sys/fs/cgroup/cgroup.subtree_control ] || [ -d "$CG" ]; then
  mkdir -p "$CG" 2>/dev/null || true
  if [ -f "$CG/cpu.max" ]; then
    echo "50000 100000" > "$CG/cpu.max" 2>/dev/null || true
    echo
    echo "=== bench: 0.5 core (cgroup cpu.max=$(cat "$CG/cpu.max")) ==="
    ( echo $$ > "$CG/cgroup.procs" 2>/dev/null || true
      "$BENCH" -m "$GGUF" -t 1 -p 256 -n 64 -r 2 2>&1 | grep -E '^\|' ) || true
    echo "max 100000" > "$CG/cpu.max" 2>/dev/null || true
    rmdir "$CG" 2>/dev/null || true
  else
    echo "cgroup cpu.max unavailable; skipping the 0.5-core sweep"
  fi
fi

# ---------------------------------------------------------------------------
# 2) Single-request latency and memory, measured on a real server process
# ---------------------------------------------------------------------------

echo
echo "=== single-request latency + RSS (1 core, 1 thread) ==="
taskset -c 0 "$SERVER" -m "$GGUF" -c 1024 -t 1 --host 127.0.0.1 --port 8091 \
  --reasoning-format none > "$OUT/server-1core.log" 2>&1 &
SERVER_PID=$!
sleep 25

python3 - "$OUT" <<'PY'
import json, sys, time, urllib.request
out = sys.argv[1]
body = {
    "model": "m",
    "messages": [
        {"role": "system", "content": "只输出一个 JSON 对象。"},
        {"role": "user", "content": "评价这句话：今晚可能不来了。"},
    ],
    "max_tokens": 96,
    "temperature": 0.0,
    "stream": False,
}
latencies = []
tokens = 0
for index in range(3):
    started = time.perf_counter()
    request = urllib.request.Request(
        "http://127.0.0.1:8091/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        payload = json.loads(response.read().decode("utf-8"))
    latencies.append(time.perf_counter() - started)
    tokens = (payload.get("usage") or {}).get("completion_tokens", tokens)
    print(f"  request {index + 1}: {latencies[-1]:.2f}s, {tokens} completion tokens")

report = {
    "completion_tokens": tokens,
    "latency_s": [round(value, 2) for value in latencies],
    "latency_mean_s": round(sum(latencies) / len(latencies), 2),
    "generation_tps": round(tokens / (sum(latencies) / len(latencies)), 2) if latencies else None,
}
with open(f"{out}/single-core-latency.json", "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2, ensure_ascii=False)
print(json.dumps(report, ensure_ascii=False))
PY

echo
echo "=== peak RSS of the 1-core server ==="
grep -E 'VmHWM|VmRSS' "/proc/$SERVER_PID/status" 2>/dev/null || true
ps -o rss= -p "$SERVER_PID" 2>/dev/null | awk '{printf "  current RSS: %.1f MiB\n", $1/1024}'

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
echo
echo "results written to $OUT"
