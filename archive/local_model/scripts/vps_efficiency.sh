#!/usr/bin/env bash
# Weak-VPS efficiency profile for the fine-tuned 2B model.
#
# Sends the same appraisal request the Runtime would send, under CPU budgets a
# cheap VPS actually offers:
#   * 8 threads  - the reference laptop,
#   * 1 core     - pinned with taskset,
#   * half a core - a *verified* cgroup v2 quota (nr_throttled is printed so an
#     unenforced quota cannot masquerade as a measurement).
#
# Usage: bash scripts/vps_efficiency.sh <gguf> [label]
#        bash scripts/vps_efficiency.sh --quants   (sweep Q4_K_M/Q3_K_M/Q2_K on 1 core)

set -u

BIN=/mnt/e/llama.cpp/build/bin
OUT=/root/companion-training/outputs/vps-efficiency
CG=/sys/fs/cgroup/vpseff
PROMPT="$OUT/prompt.json"
mkdir -p "$OUT"

echo "+cpu" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
mkdir -p "$CG"
echo "50000 100000" > "$CG/cpu.max"

cat > "$PROMPT" <<'JSON'
{"model":"m","messages":[{"role":"system","content":"你是长期陪伴角色的内部评价器。只输出一个 JSON 对象，字段固定为 direction, impact, activation, uncertainty, relation_signal, responsibility, confidence。"},{"role":"user","content":"{\n \"current_event\": {\"speaker\": \"user\", \"text\": \"算了，也没什么。\"},\n \"context_turns\": [{\"speaker\": \"user\", \"text\": \"今天面试没过。\"}, {\"speaker\": \"assistant\", \"text\": \"先歇一会儿吧。\"}],\n \"background_mood\": {\"valence\": -0.2, \"arousal\": 0.3},\n \"known_facts\": [\"用户今天参加了面试\"],\n \"hints\": {\"silence_gap_h\": 3, \"recent_initiatives\": 1}\n}"}],"max_tokens":96,"temperature":0.0,"stream":false}
JSON

probe() {
  # probe <port> <label>
  python3 - "$1" "$2" "$PROMPT" "$OUT" <<'PY'
import json, sys, time, urllib.request
port, label, prompt_file, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = json.load(open(prompt_file, encoding="utf-8"))
latencies, tokens = [], 0
for _ in range(3):
    started = time.perf_counter()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3600) as response:
        payload = json.loads(response.read().decode("utf-8"))
    latencies.append(time.perf_counter() - started)
    tokens = (payload.get("usage") or {}).get("completion_tokens", tokens)
mean = sum(latencies) / len(latencies)
print(f"  {label:24} latency mean {mean:7.2f}s  min {min(latencies):7.2f}s  "
      f"tokens {tokens:3d}  -> {tokens/mean:5.2f} tok/s")
json.dump({"label": label, "latency_mean_s": round(mean, 2), "latency_min_s": round(min(latencies), 2),
           "completion_tokens": tokens, "tokens_per_s": round(tokens / mean, 2)},
          open(f"{out}/latency-{label}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

# run_one <label> <threads> <taskset|none> <cgroup|none> <gguf>
run_one() {
  local label="$1" threads="$2" pin="$3" cgmode="$4" gguf="$5"
  local port=$(( 8200 + RANDOM % 300 ))

  local cmd=()
  if [ "$cgmode" = "cgroup" ]; then cmd+=(bash -c "echo \$\$ > $CG/cgroup.procs; exec \"\$@\"" _); fi
  if [ "$pin" = "taskset" ]; then cmd+=(taskset -c 0); fi
  cmd+=("$BIN/llama-server" -m "$gguf" -c 1024 -t "$threads" --host 127.0.0.1 --port "$port" --reasoning-format none)

  "${cmd[@]}" > "$OUT/server-$label.log" 2>&1 &
  local pid=$!
  sleep 26

  if curl -sf --max-time 5 "http://127.0.0.1:$port/health" > /dev/null; then
    probe "$port" "$label"
  else
    echo "  $label: server did not come up (see $OUT/server-$label.log)"
  fi

  local rss
  rss=$(awk '/VmHWM/{print $2}' "/proc/$pid/status" 2>/dev/null || echo 0)
  local throttled
  throttled=$(grep -o 'nr_throttled [0-9]*' "$CG/cpu.stat" 2>/dev/null | awk '{print $2}' || echo 0)
  printf "  %-24s peak RSS %5.0f MiB   cgroup throttled=%s\n" "$label" "$(echo "$rss/1024" | bc)" "$throttled"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 3
}

echo "=== CPU budgets ==="
echo "  host: $(nproc) logical processors; 0.5-core quota = $(cat "$CG/cpu.max")"
echo

if [ "${1:-}" = "--quants" ]; then
  echo "=== 1 core, quantization sweep (fit a small VPS?) ==="
  for q in Q4_K_M Q3_K_M Q2_K; do
    run_one "1core-$q" 1 taskset none "/root/companion-training/outputs/qboss-2b-$q.gguf"
  done
else
  GGUF="${1:-/root/companion-training/outputs/qboss-2b-Q4_K_M.gguf}"
  echo "=== 1 core (pinned) ==="
  run_one "1core" 1 taskset none "$GGUF"
  echo "=== 0.5 core (verified cgroup quota) ==="
  run_one "halfcore" 1 none cgroup "$GGUF"
  echo "=== 8 threads (reference) ==="
  run_one "8threads" 8 none none "$GGUF"
fi

echo
echo "results in $OUT"
