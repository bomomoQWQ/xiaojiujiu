#!/usr/bin/env bash
# 拉运行时当前配置 + 调度计划 + 健康，确认闸门参数。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import json
import urllib.request

BASE = "http://127.0.0.1:8787"


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"_error": "%s: %s" % (type(exc).__name__, exc)}


cfg = get("/config")
def walk(prefix, node, depth=0):
    if isinstance(node, dict):
        for k in sorted(node):
            walk(prefix + "." + k if prefix else k, node[k], depth + 1)
    else:
        s = json.dumps(node, ensure_ascii=False)
        if len(s) > 90:
            s = s[:90] + "..."
        print("  %-56s %s" % (prefix, s))

print("=== /config ===")
walk("", cfg)

print()
print("=== /schedule ===")
print(json.dumps(get("/schedule"), ensure_ascii=False, indent=2)[:2500])

print()
print("=== /state (节选) ===")
st = get("/state")
if isinstance(st, dict):
    keep = {k: st.get(k) for k in (
        "version", "updated_at", "epoch_at", "last_tick_at", "last_user_message_at",
        "last_contact_at", "last_exchange_at", "cooldown_until", "foreground_pause_until",
        "contact_count_today", "contact_day", "allow_proactive", "mood_valence",
        "mood_arousal", "mood_stability", "approach_impulse", "restraint", "pressure")}
    print(json.dumps(keep, ensure_ascii=False, indent=2))
    print("  meta:", json.dumps(st.get("meta"), ensure_ascii=False))
    print("  values:", json.dumps(st.get("values"), ensure_ascii=False))

print()
print("=== /health (deep_refresh 与关键计数) ===")
h = get("/health")
if isinstance(h, dict):
    print(json.dumps({k: v for k, v in h.items() if k != "deep_refresh"},
                     ensure_ascii=False, indent=2)[:2000])
    print("  deep_refresh:", json.dumps(h.get("deep_refresh"), ensure_ascii=False)[:600])
PY
