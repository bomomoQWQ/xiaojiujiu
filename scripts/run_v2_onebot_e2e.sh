#!/usr/bin/env bash
# Guarded launcher for run_v2_onebot_e2e.py. It never starts/stops NapCat or sends to QQ.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ONEBOT_URL="${ONEBOT_URL:-http://127.0.0.1:6300}"
ASTRBOT_URL="${ASTRBOT_URL:-http://127.0.0.1:6185}"
RUNTIME_URL="${RUNTIME_URL:-http://127.0.0.1:8090}"
PG_DSN="${PG_DSN:-}"
V2_SCOPE="${V2_SCOPE:-}"

if [[ -z "$V2_SCOPE" ]]; then
  echo "V2_SCOPE is required (or pass --scope directly to the Python runner)." >&2
  exit 2
fi
if [[ "${CONFIRM_NAPCAT_STOPPED:-}" != "YES" ]]; then
  echo "Refusing live injection. Confirm NapCat is stopped with CONFIRM_NAPCAT_STOPPED=YES." >&2
  echo "The Python runner will separately require xxj-onebot /state connected=true." >&2
  exit 2
fi

args=(
  "$ROOT/scripts/run_v2_onebot_e2e.py"
  --onebot-url "$ONEBOT_URL"
  --astrbot-url "$ASTRBOT_URL"
  --runtime-url "$RUNTIME_URL"
  --scope "$V2_SCOPE"
  --confirm-napcat-stopped
)
if [[ -n "$PG_DSN" ]]; then
  args+=(--pg-dsn "$PG_DSN")
fi
exec "$PYTHON_BIN" "${args[@]}" "$@"
