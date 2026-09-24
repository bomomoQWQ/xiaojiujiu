"""深挖两件事：
 1) qq01 窗口内新建的 6 件事，是从哪些 source event 派生的？（若引用的正是"她自己刚发出的
    主动消息"，那就是 source 去重抓不到的那一类）
 2) 两个"从不开口"的实例（qq06 / qq05）现在的 I/R/P 与 need —— 对照我之前那次
    过线推演，看模型哪里估错了。
"""
import datetime as dt
import sqlite3

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
CST = dt.timedelta(hours=8)
CUTOFF = "2026-09-17T12:10:00"


def cst(ts):
    try:
        return (dt.datetime.fromisoformat(str(ts).replace("Z", "").replace("+00:00", "")) + CST
                ).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return "-"


print("now = %s CST" % (NOW + CST).strftime("%m-%d %H:%M"))
print()
print("########## 1) qq01：新事 vs 旧事，各自的 source 事件 ##########")
con = sqlite3.connect("file:/data/default-friendmessage-qq01/companion.sqlite3?mode=ro",
                      uri=True)
con.row_factory = sqlite3.Row
matters = con.execute(
    "select unfinished_id, title, status, source_event_ids, created_at from unfinished_matters"
    " where status in ('open','waiting','due','muted') order by created_at").fetchall()
for m in matters:
    try:
        import json
        src = json.loads(m["source_event_ids"] or "[]")
    except ValueError:
        src = []
    kinds = []
    for event_id in src:
        row = con.execute("select event_type, actor, substr(content,1,26) c from raw_events"
                          " where event_id=?", (event_id,)).fetchone()
        kinds.append("%s/%s" % (row["event_type"], row["actor"]) if row else event_id)
    print("  %s %s %s" % (cst(m["created_at"]), m["unfinished_id"][-6:], m["title"][:44]))
    print("        sources=%s -> %s" % (src, kinds))

print()
print("  语义结算情况:")
for r in con.execute("select semantic_status, count(*) n from event_semantics group by semantic_status"):
    print("    %-12s %d" % (r["semantic_status"], r["n"]))
unresolved = con.execute(
    "select count(*) from event_semantics where semantic_status='unresolved'").fetchone()[0]
print("    unresolved = %d" % unresolved)
print("  未结算事件里，有多少是 assistant/proactive 的:",
      con.execute(
          "select count(*) from raw_events e join event_semantics s on s.event_id=e.event_id"
          " where s.semantic_status='unresolved'"
          " and e.event_type in ('assistant_message','proactive_sent')").fetchone()[0])
print("  按类型统计全部 raw_events:")
for r in con.execute("select event_type, count(*) n from raw_events group by event_type order by n desc"):
    print("    %-22s %d" % (r["event_type"], r["n"]))
con.close()

print()
print("########## 2) 两个'从不开口'的实例：状态对照我的推演 ##########")
for tag in ("qq06", "qq05"):
    con = sqlite3.connect("file:/data/default-friendmessage-%s/companion.sqlite3?mode=ro" % tag,
                          uri=True)
    con.row_factory = sqlite3.Row
    st = con.execute("select approach_impulse, restraint, pressure, last_exchange_at,"
                     " last_user_message_at, last_contact_at from runtime_state").fetchone()
    cands = con.execute(
        "select candidate_id, type, status, internal_need, unfinished_relevance, created_at,"
        " expires_at from candidate_intents where status in ('new','active')").fetchall()
    rows = con.execute(
        "select decided_at, reason, advantage from decisions where delta_t < 2000"
        " order by decided_at desc limit 3").fetchall()
    con.close()
    print("  用户 %s" % tag)
    print("    I=%.4f R=%.4f P=%.4f" % (st["approach_impulse"], st["restraint"], st["pressure"]))
    print("    距上次交流 %.1f h（last_exchange=%s）" % (
        (NOW - dt.datetime.fromisoformat(str(st["last_exchange_at"]).replace("Z", "")
                                         .replace("+00:00", ""))).total_seconds() / 3600,
        cst(st["last_exchange_at"])))
    for c in cands:
        print("    候选 %s %-9s need=%.4f unf=%.4f exp=%s" % (
            c["candidate_id"][-6:], c["type"], c["internal_need"], c["unfinished_relevance"],
            cst(c["expires_at"])))
    for r in rows:
        print("    判决 %s %-26s adv=%+.5f" % (r["decided_at"][:19], r["reason"], r["advantage"]))
