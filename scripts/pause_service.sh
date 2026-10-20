#!/usr/bin/env bash
# Take the test stack down for good, and say so on QQ after it is already gone.
#
# Order is the whole point, and each step exists because of a way the obvious order
# fails:
#
#   1. collect the recipient ids *first* -- they come from the fleet control plane,
#      which is the first thing we stop, and a notice with nobody to send it to is
#      worthless;
#   2. stop AstrBot and the runtime fleet -- the reply path and the proactive path;
#   3. send the notice through NapCat (`napcat_notice.py`) -- AstrBot owned the normal
#      send path, so after step 2 this is the only channel left, which is exactly why
#      the notice can honestly say the service is gone;
#   4. stop the *test* NapCat, whose name carries the `xxj-` prefix. A container called
#      plain `napcat` also exists and belongs to another stack: stopping it is a
#      different, much worse incident, so the name is asserted before it is touched;
#   5. verify, and shred the id file.
#
# Usage:
#   scripts/pause_service.sh "「系统通知」测试服务已经彻底下线。"
#   scripts/pause_service.sh "..." /path/to/ids.txt        # ids already collected
#
# Environment (all optional):
#   NAPCAT_WEBUI=http://127.0.0.1:6098   test NapCat WebUI (6099 is the production one)
#   NAPCAT_TOKEN_FILE=/etc/napcat-webui-token
#   FLEET_STATUS=http://127.0.0.1:8800/fleet/status
#   ASTRBOT=astrbot-test  FLEET=xxj-runtime-fleet  NAPCAT=xxj-napcat-test
set -u

TEXT=${1:-}
IDS_FILE=${2:-}

WEBUI=${NAPCAT_WEBUI:-http://127.0.0.1:6098}
TOKEN_FILE=${NAPCAT_TOKEN_FILE:-}
FLEET_STATUS=${FLEET_STATUS:-http://127.0.0.1:8800/fleet/status}
ASTRBOT=${ASTRBOT:-astrbot-test}
FLEET=${FLEET:-xxj-runtime-fleet}
NAPCAT=${NAPCAT:-xxj-napcat-test}

[ -n "$TEXT" ] || { echo "usage: $0 \"<notice text>\" [ids-file]"; exit 2; }
[ -n "$TOKEN_FILE" ] || { echo "set NAPCAT_TOKEN_FILE (the NapCat WebUI token)"; exit 2; }

# The production napcat is not ours to stop. Refuse instead of being clever.
if [ "$NAPCAT" != "xxj-napcat-test" ]; then
  echo "refusing: NAPCAT=$NAPCAT is not the test instance"; exit 2
fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TMP_IDS=""
cleanup() {
  if [ -n "$TMP_IDS" ] && [ -f "$TMP_IDS" ]; then
    shred -u "$TMP_IDS" 2>/dev/null || rm -f "$TMP_IDS"
  fi
}
trap cleanup EXIT

echo "=== 1) collect recipients ==="
if [ -z "$IDS_FILE" ]; then
  TMP_IDS=$(mktemp /tmp/notice-ids.XXXXXX)
  chmod 600 "$TMP_IDS"
  curl -s --max-time 10 "$FLEET_STATUS" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for person in data.get("people", []):
    tail = str(person.get("session") or "").rsplit(":", 1)[-1]
    if tail.isdigit():
        print(tail)
' > "$TMP_IDS" || true
  IDS_FILE=$TMP_IDS
fi
COUNT=$(grep -c . "$IDS_FILE" 2>/dev/null || echo 0)
echo "  recipients: $COUNT"
[ "$COUNT" -gt 0 ] || { echo "  no recipients; refusing to tear down silently"; exit 2; }

echo
echo "=== 2) stop the reply path and the proactive path ==="
for name in "$ASTRBOT" "$FLEET"; do
  printf '  %-22s ' "$name"
  docker stop "$name" >/dev/null 2>&1 && echo stopped || echo "FAILED"
done

echo
echo "=== 3) send the notice through $NAPCAT (AstrBot is already down) ==="
python3 "$SCRIPT_DIR/napcat_notice.py" \
  --webui "$WEBUI" \
  --token-file "$TOKEN_FILE" \
  --container "$NAPCAT" \
  --text "$TEXT" \
  --ids-file "$IDS_FILE"
SEND_RC=$?
echo "  send exit=$SEND_RC"

echo
echo "=== 4) stop $NAPCAT (QQ login goes with it) ==="
docker stop "$NAPCAT" >/dev/null 2>&1 && echo "  stopped" || echo "  FAILED"

echo
echo "=== 5) final state ==="
docker ps -a --format '{{.Names}}\t{{.Status}}' \
  | grep -Ei "^(astrbot-test|xxj-runtime-fleet|xxj-napcat-test|astrbot|napcat)\b" || true
echo
echo "  restart policy (unless-stopped keeps a stopped container down across daemon restarts):"
for c in "$ASTRBOT" "$FLEET" "$NAPCAT"; do
  printf '    %-20s ' "$c"
  docker inspect "$c" --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null || echo "missing"
done

exit "$SEND_RC"
