#!/usr/bin/env bash
# 看"接触之后 advantage 怎么恢复"：把自然轮次和 contact 事件对齐。
set -u
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import datetime as dt
import json
import os
import sqlite3

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
PERSON = os.environ.get("CR_PERSON", "default-friendmessage-1670681411")
path = "/data/%s/companion.sqlite3" % PERSON
con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
con.row_factory = sqlite3.Row

print("now = %s UTC" % NOW.strftime("%Y-%m-%d %H:%M:%S"))

print()
print("=== raw_events: 所有 PROACTIVE / assistant 发送类事件 ===")
types = [r[0] for r in con.execute(
    "select distinct event_type from raw_events order by event_type")]
print("  event_types:", ", ".join(types))
for r in con.execute(
        "select event_id, event_type, timestamp, actor, substr(content,1,60) c"
        " from raw_events where event_type like '%proactive%'"
        " or event_type like '%assistant%' order by timestamp desc limit 15"):
    print("  %s %-28s %-9s %s" % (r["timestamp"][:19], r["event_type"],
                                  str(r["actor"]), r["c"]))

print()
print("=== decisions（只列 09-17 当天、且时间 <= now 的）===")
for r in con.execute(
        "select decided_at, trigger, reason, advantage, hazard, action_probability,"
        " delta_t, silence_utility, acted from decisions"
        " where decided_at >= '2026-09-17T00:00:00' and decided_at <= ?"
        " order by decided_at asc", (NOW.isoformat(),)):
    print("  %s %-9s %-22s adv=%s haz=%s p=%s dt=%s sil=%s acted=%s" % (
        r["decided_at"][:19], str(r["trigger"])[:9], str(r["reason"])[:22],
        "-" if r["advantage"] is None else "%+.4f" % r["advantage"],
        "-" if r["hazard"] is None else "%.2e" % r["hazard"],
        "-" if r["action_probability"] is None else "%.4f" % r["action_probability"],
        "-" if r["delta_t"] is None else "%.0f" % r["delta_t"],
        "-" if r["silence_utility"] is None else "%.3f" % r["silence_utility"],
        r["acted"]))

print()
print("=== state_samples（09-17 当天，impulse/restraint/pressure 曲线）===")
for r in con.execute(
        "select sampled_at, reason, approach_impulse, restraint, pressure, allow_proactive"
        " from state_samples where sampled_at >= '2026-09-17T00:00:00'"
        " and sampled_at <= ? order by sampled_at asc", (NOW.isoformat(),)):
    print("  %s %-14s imp=%.4f restr=%.4f pres=%.4f allow=%s" % (
        r["sampled_at"][:19], str(r["reason"])[:14], r["approach_impulse"],
        r["restraint"], r["pressure"], r["allow_proactive"]))

print()
print("=== attempt_events（状态迁移全景，今天）===")
for r in con.execute(
        "select created_at, attempt_id, from_state, to_state, reason from attempt_events"
        " where created_at >= '2026-09-17T00:00:00' order by created_at asc"):
    print("  %s %s %s -> %-14s %s" % (
        r["created_at"][:19], r["attempt_id"][:16], str(r["from_state"]),
        str(r["to_state"]), str(r["reason"])[:40]))

print()
print("=== 未完之事 ===")
for r in con.execute(
        "select unfinished_id, title, status, priority, created_at, expire_at"
        " from unfinished_matters order by created_at desc limit 15"):
    print("  %s %-8s pri=%.2f %s %s" % (
        r["unfinished_id"][:16], str(r["status"]), r["priority"],
        str(r["created_at"])[:19], str(r["title"])[:70]))
n_open = con.execute(
    "select count(*) from unfinished_matters where status = 'open'").fetchone()[0]
print("  open 数:", n_open)

print()
print("=== candidates（当前活跃）===")
for r in con.execute(
        "select candidate_id, type, status, internal_need, unfinished_relevance,"
        " confidence, created_at, expires_at, substr(intent,1,70) i"
        " from candidate_intents where status not in ('retired','invalidated')"
        " order by created_at desc limit 12"):
    print("  %s %-8s %-10s need=%.3f unf=%.3f conf=%.3f exp=%s" % (
        r["candidate_id"][:16], str(r["type"])[:8], str(r["status"])[:10],
        r["internal_need"], r["unfinished_relevance"], r["confidence"],
        str(r["expires_at"])[:19]))
    print("      %s" % str(r["i"]))
PY
